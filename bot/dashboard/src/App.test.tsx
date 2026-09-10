import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { DashboardApp } from "./App";
import { DashboardApi, type Connection } from "./api";
import { mergeEvents, isResponding, latestWork, type ChatEvent } from "./types";
import { Markdown } from "./Markdown";

const session = { user_id: "1", guild_id: "2", channel_id: "3", csrf: "c", bot_name: "Kimi", retention_days: 30, consent_required: false, consent_title: "Privacy", consent_text: "Please accept", max_upload_bytes: 10000, max_message_chars: 32000 };
const chat = { id: "a", guild_id: "2", channel_id: "3", parent_channel_id: "3", channel_name: "general", title: "A test conversation", created_at: 1, updated_at: 1 };
const event = (id: number, kind: string, payload: ChatEvent["payload"]): ChatEvent => ({ id, kind, payload, created_at: 1 });

function connection() {
  let receive: (events: ChatEvent[]) => void = () => {};
  const api = new DashboardApi();
  const request = vi.spyOn(api, "request").mockImplementation(async path => {
    if (path === "/chats") return { chats: [chat] };
    if (path.endsWith("/events")) return { events: [] };
    if (path.endsWith("/tasks")) return { tasks: [] };
    return { files: [] };
  });
  vi.spyOn(api, "subscribe").mockImplementation((_chat, _after, callback, status) => { receive = callback; status(true); return () => {}; });
  return { value: { api, session, displayName: "Charlie", openLink: vi.fn() } as Connection, request, receive: (events: ChatEvent[]) => receive(events) };
}

describe("saved chat", () => {
  it("finishing before HTTP acceptance does not leave the composer busy", async () => {
    const fixture = connection();
    fixture.request.mockImplementation(async path => {
      if (path === "/chats") return { chats: [chat] };
      if (path.endsWith("/events")) return { events: [] };
      if (path.endsWith("/tasks")) return { tasks: [] };
      if (path.endsWith("/messages")) {
        fixture.receive([event(1, "user_message", { text: "hello", turn_id: "t" }), event(2, "turn_finished", { text: "Done", status: "completed", turn_id: "t" })]);
        return { turn_id: "t" };
      }
      return { files: [] };
    });
    render(<DashboardApp connection={fixture.value} />);
    fireEvent.change(await screen.findByRole("textbox", { name: "Message Kimi" }), { target: { value: "hello" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    expect(await screen.findByText("Done")).toBeVisible();
    await waitFor(() => expect(screen.queryByRole("button", { name: "Stop response" })).not.toBeInTheDocument());
  });

  it("keeps an unsent draft and reuses its request identifier after network failure", async () => {
    const fixture = connection();
    const base = fixture.request.getMockImplementation()!;
    fixture.request.mockImplementation(async (path, ...args) => {
      if (path.endsWith("/messages")) throw new Error("Network interrupted");
      return base(path, ...args);
    });
    render(<DashboardApp connection={fixture.value} />);
    const composer = await screen.findByRole("textbox", { name: "Message Kimi" });
    fireEvent.change(composer, { target: { value: "Keep this draft" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Network interrupted");
    expect(composer).toHaveValue("Keep this draft");
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(fixture.request.mock.calls.filter(call => call[0].endsWith("/messages"))).toHaveLength(2));
    const requests = fixture.request.mock.calls.filter(call => call[0].endsWith("/messages"));
    expect(requests[0][2]).toEqual(requests[1][2]);
  });

  it("updates durable work results from live events", async () => {
    const fixture = connection();
    render(<DashboardApp connection={fixture.value} />);
    await screen.findByRole("textbox", { name: "Message Kimi" });
    await act(async () => fixture.receive([event(4, "coding_task", { id: "coding", status: "completed", text: "Built the report", files: [{ id: "file", filename: "report.csv" }] })]));
    expect(screen.getByText("Built the report")).toBeVisible();
    expect(screen.getByRole("button", { name: /report.csv/ })).toBeEnabled();
  });
});

it("replay deduplicates events and clears completed work", () => {
  const start = event(1, "user_message", { turn_id: "t" });
  const end = event(2, "turn_finished", { turn_id: "t" });
  expect(isResponding([start])).toBe(true);
  expect(isResponding(mergeEvents([end], [start, end]))).toBe(false);
  expect(latestWork([event(3, "task_action", { action_id: "a" }), event(4, "task_action_result", { action_id: "a" })]).actions).toEqual([]);
});

it("renders generated HTML as inert text and blocks executable URLs", () => {
  const { container } = render(<Markdown text={'<script>alert(1)</script>\n\n[unsafe](javascript:alert)\n\n![remote](https://example.com/track.png)'} openLink={vi.fn()} />);
  expect(container.querySelector("script")).toBeNull();
  expect(container.querySelector("img")).toBeNull();
  expect(container.querySelector('[href^="javascript:"]')).toBeNull();
});

it("ignores older-history responses after selecting another chat", async () => {
  const fixture = connection();
  const other = { ...chat, id: "b", title: "Other conversation" };
  let release: (result: unknown) => void = () => {};
  fixture.request.mockImplementation(async path => {
    if (path === "/chats") return { chats: [chat, other] };
    if (path.includes("before=")) return await new Promise(resolve => { release = resolve; });
    if (path === "/chats/a/events") return { events: Array.from({ length: 200 }, (_, index) => event(index + 2, "turn_finished", { text: `A message ${index}`, status: "completed" })) };
    if (path.endsWith("/events")) return { events: [] };
    if (path.endsWith("/tasks")) return { tasks: [] };
    return { files: [] };
  });
  render(<DashboardApp connection={fixture.value} />);
  fireEvent.click(await screen.findByRole("button", { name: "Load earlier messages" }));
  fireEvent.click(screen.getByRole("button", { name: "Other conversation" }));
  await waitFor(() => expect(fixture.request).toHaveBeenCalledWith("/chats/b/events"));
  await act(async () => release({ events: [event(1, "turn_finished", { text: "Only belongs in A", status: "completed" })] }));
  expect(screen.queryByText("Only belongs in A")).not.toBeInTheDocument();
});

it("reconciles a response that finishes while another chat is open", async () => {
  const fixture = connection();
  const other = { ...chat, id: "b", title: "Other conversation" };
  let completed = false;
  fixture.request.mockImplementation(async path => {
    if (path === "/chats") return { chats: [chat, other] };
    if (path === "/chats/a/events") return { events: completed ? [event(2, "turn_finished", { turn_id: "t", text: "Finished while away", status: "completed" })] : [] };
    if (path.endsWith("/events")) return { events: [] };
    if (path.endsWith("/tasks")) return { tasks: [] };
    if (path.endsWith("/messages")) return { turn_id: "t" };
    return { files: [] };
  });
  render(<DashboardApp connection={fixture.value} />);
  fireEvent.change(await screen.findByRole("textbox", { name: "Message Kimi" }), { target: { value: "Work on this" } });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  await screen.findByRole("button", { name: "Stop response" });
  fireEvent.click(screen.getByRole("button", { name: "Other conversation" }));
  await waitFor(() => expect(fixture.request).toHaveBeenCalledWith("/chats/b/events"));
  completed = true;
  fireEvent.click(screen.getByRole("button", { name: "A test conversation" }));
  expect(await screen.findByText("Finished while away")).toBeVisible();
  expect(screen.queryByRole("button", { name: "Stop response" })).not.toBeInTheDocument();
});
