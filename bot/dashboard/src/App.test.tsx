import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { DashboardApp, FileButton } from "./App";
import { DashboardApi, type Connection } from "./api";
import { conversationTimeline, mergeEvents, isResponding, latestWork, type ChatEvent } from "./types";
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
    // This fixture only delivers live events; wait for its subscription before
    // sending, since it does not journal and replay events like the backend.
    await waitFor(() => expect(fixture.value.api.subscribe).toHaveBeenCalled());
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

  it("reuses the branch request identifier after a network failure", async () => {
    const fixture = connection();
    const base = fixture.request.getMockImplementation()!;
    fixture.request.mockImplementation(async (path, ...args) => {
      if (path.endsWith("/branches")) throw new Error("Network interrupted");
      if (path.endsWith("/events")) return { events: [event(1, "turn_finished", { text: "An answer", status: "completed", turn_id: "t" })] };
      return base(path, ...args);
    });
    render(<DashboardApp connection={fixture.value} />);
    fireEvent.click(await screen.findByRole("button", { name: "Branch from here" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Network interrupted");
    fireEvent.click(screen.getByRole("button", { name: "Branch from here" }));
    await waitFor(() => expect(fixture.request.mock.calls.filter(call => call[0].endsWith("/branches"))).toHaveLength(2));
    const requests = fixture.request.mock.calls.filter(call => call[0].endsWith("/branches"));
    expect(requests[0][2]).toEqual(requests[1][2]);
    expect(requests[0][2]).toEqual({ event_id: 1, request_id: expect.any(String) });
  });

  it("preserves a follow-up draft and new attachments while a send is being accepted", async () => {
    const fixture = connection();
    const base = fixture.request.getMockImplementation()!;
    let accept: (result: unknown) => void = () => {};
    fixture.request.mockImplementation(async (path, ...args) => {
      if (path.endsWith("/messages")) return await new Promise(resolve => { accept = resolve; });
      return base(path, ...args);
    });
    vi.spyOn(fixture.value.api, "upload")
      .mockResolvedValueOnce({ id: "first", filename: "first.txt", size: 1 })
      .mockResolvedValueOnce({ id: "next", filename: "next.txt", size: 1 });
    const { container } = render(<DashboardApp connection={fixture.value} />);
    const composer = await screen.findByRole("textbox", { name: "Message Kimi" });
    const input = container.querySelector('input[type="file"]')!;
    fireEvent.change(composer, { target: { value: "First message" } });
    fireEvent.change(input, { target: { files: [new File(["a"], "first.txt")] } });
    await screen.findByRole("button", { name: "Remove first.txt" });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(fixture.request).toHaveBeenCalledWith("/chats/a/messages", "POST", expect.objectContaining({ text: "First message", file_ids: ["first"] })));
    fireEvent.change(composer, { target: { value: "A follow-up thought" } });
    fireEvent.change(input, { target: { files: [new File(["b"], "next.txt")] } });
    await screen.findByRole("button", { name: "Remove next.txt" });
    await act(async () => accept({ turn_id: "t" }));
    expect(composer).toHaveValue("A follow-up thought");
    expect(screen.queryByRole("button", { name: "Remove first.txt" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Remove next.txt" })).toBeVisible();
  });
});

it("shows the connection state only while the socket is down", async () => {
  const fixture = connection();
  let status: (connected: boolean, expired: boolean) => void = () => {};
  vi.spyOn(fixture.value.api, "subscribe").mockImplementation((_chat, _after, _callback, report) => { status = report; report(true); return () => {}; });
  render(<DashboardApp connection={fixture.value} />);
  await screen.findByRole("textbox", { name: "Message Kimi" });
  expect(screen.queryByText("Reconnecting…")).not.toBeInTheDocument();
  await act(async () => status(false, false));
  expect(screen.getByText("Reconnecting…")).toBeInTheDocument();
  await act(async () => status(true, false));
  expect(screen.queryByText("Reconnecting…")).not.toBeInTheDocument();
});

it("names the configured bot when a coding task asks for input", async () => {
  const fixture = connection();
  fixture.value.session = { ...session, bot_name: "Nova" };
  render(<DashboardApp connection={fixture.value} />);
  await screen.findByRole("textbox", { name: "Message Nova" });
  fireEvent.click(screen.getByRole("button", { name: /^Work/ }));
  await act(async () => fixture.receive([event(5, "coding_task", { id: "coding", status: "waiting_for_input", text: "Which branch?" })]));
  expect(screen.getByPlaceholderText("Tell Nova how to continue…")).toBeVisible();
});

it("replay deduplicates events and clears completed work", () => {
  const start = event(1, "user_message", { turn_id: "t" });
  const end = event(2, "turn_finished", { turn_id: "t" });
  expect(isResponding([start])).toBe(true);
  expect(isResponding(mergeEvents([end], [start, end]))).toBe(false);
  expect(latestWork([event(3, "task_action", { action_id: "a" }), event(4, "task_action_result", { action_id: "a" })]).actions).toEqual([]);
});

it("keeps a live plan in the conversation when the work panel is closed", async () => {
  const fixture = connection();
  render(<DashboardApp connection={fixture.value} />);
  await waitFor(() => expect(fixture.value.api.subscribe).toHaveBeenCalled());
  await act(async () => fixture.receive([
    event(1, "user_message", { turn_id: "t", text: "Build a report" }),
    event(2, "plan", { turn_id: "t", steps: [{ content: "Read the data", status: "in_progress" }, { content: "Write the report", status: "pending" }] }),
    event(3, "activity", { turn_id: "t", label: "Reading the data…" }),
  ]));
  expect(screen.queryByRole("complementary", { name: "Work panel" })).not.toBeInTheDocument();
  const plan = within(screen.getByRole("main")).getByRole("region", { name: "Plan" });
  expect(within(plan).getByText("Read the data")).toBeVisible();
  expect(within(plan).getByRole("status")).toHaveTextContent("0 of 2 done");
  expect(screen.getByText("Reading the data…")).toBeVisible();
  await act(async () => fixture.receive([
    event(4, "plan", { turn_id: "t", steps: [{ content: "Read the data", status: "completed" }, { content: "Write the report", status: "in_progress" }] }),
  ]));
  expect(screen.getAllByRole("region", { name: "Plan" })).toHaveLength(1);
  expect(within(plan).getByRole("status")).toHaveTextContent("1 of 2 done");
  expect(within(plan).getByText("Write the report").closest("li")).toHaveAttribute("aria-current", "step");
  await act(async () => fixture.receive([
    event(5, "turn_finished", { turn_id: "t", text: "Report ready", status: "completed" }),
    event(6, "user_message", { turn_id: "next", text: "One more thing" }),
  ]));
  expect(plan).toBeVisible();
  expect(screen.queryByText("Reading the data…")).not.toBeInTheDocument();
  expect(screen.getByText("Kimi is thinking…")).toBeVisible();
});

it("replays one plan per response and keeps each plan in conversation order", () => {
  const events = [
    event(1, "user_message", { turn_id: "a" }),
    event(2, "plan", { turn_id: "a", steps: [{ content: "First task", status: "pending" }] }),
    event(3, "activity", { turn_id: "a" }),
    event(4, "plan", { turn_id: "a", steps: [{ content: "First task", status: "completed" }] }),
    event(5, "turn_finished", { turn_id: "a" }),
    event(6, "user_message", { turn_id: "b" }),
    event(7, "plan", { turn_id: "b", steps: [{ content: "Second task", status: "pending" }] }),
  ];
  const timeline = conversationTimeline(mergeEvents(events, events));
  expect(timeline.map(item => item.id)).toEqual([1, 2, 5, 6, 7]);
  expect(timeline[1].payload.steps?.[0].status).toBe("completed");
  expect(timeline[4].payload.steps?.[0].content).toBe("Second task");
  expect(conversationTimeline([...events, event(8, "plan", { turn_id: "b", steps: [] })]).at(-1)?.payload.steps).toEqual([]);
});

it("distinguishes an unavailable delivery from an expired file", () => {
  const onSelect = vi.fn();
  render(<><FileButton file={{ filename: "report.csv", unavailable: true }} onSelect={onSelect} /><FileButton file={{ id: "old", filename: "old.csv", expired: true }} onSelect={onSelect} /></>);
  expect(screen.getByRole("button", { name: /report.csv File unavailable/ })).toBeDisabled();
  expect(screen.getByRole("button", { name: /old.csv File expired/ })).toBeDisabled();
});

it("preserves Markdown and attribution in returned and re-branched results", async () => {
  const fixture = connection();
  render(<DashboardApp connection={fixture.value} />);
  await waitFor(() => expect(fixture.value.api.subscribe).toHaveBeenCalled());
  await act(async () => fixture.receive([
    event(1, "branch_result", { text: "**Returned result**", source_chat_id: "b", source_title: "An experiment" }),
    event(2, "history_message", { role: "user", text: "**Inherited result**", render_markdown: true, source_chat_id: "b", source_title: "An experiment" }),
  ]));
  expect(screen.getByText("Returned result").tagName).toBe("STRONG");
  expect(screen.getByText("Inherited result").tagName).toBe("STRONG");
  expect(screen.getAllByRole("button", { name: "An experiment" })).toHaveLength(2);
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
  // Keep this history-race test focused on pagination; computing accessible
  // names for hundreds of message actions dominates this fixture in jsdom.
  fireEvent.click(await screen.findByText("Load earlier messages", { selector: "button" }));
  fireEvent.click(within(screen.getByRole("navigation")).getByRole("button", { name: "Other conversation" }));
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
