import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { DashboardApp, FileButton } from "./App";
import { ApiError, DashboardApi, type Connection } from "./api";
import { conversationTimeline, mergeEvents, isResponding, latestWork, type ChatEvent } from "./types";
import { Markdown } from "./Markdown";

const session = { user_id: "1", guild_id: "2", channel_id: "3", csrf: "c", bot_name: "Kimi", retention_days: 30, consent_required: false, consent_title: "Privacy", consent_text: "Please accept", max_upload_bytes: 10000, max_message_chars: 32000, user_avatar: null };
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
  return { value: { api, session, displayName: "Charlie", botAvatar: null, openLink: vi.fn() } as Connection, request, receive: (events: ChatEvent[]) => receive(events) };
}

const proposal = { id: "schedule", revision: 1, name: "Weekly digest", status: "pending", text: "Review this", details: "Details", skill: "Instructions", python: null };

it.each(["scheduled", "coding"])("keeps %s actions pending until the matching durable result", async kind => {
  const fixture = connection();
  const base = fixture.request.getMockImplementation()!;
  fixture.request.mockImplementation(async (path, ...args) => path.endsWith("/task-actions") ? { action_id: "mine" } : base(path, ...args));
  render(<DashboardApp connection={fixture.value} />);
  await waitFor(() => expect(fixture.value.api.subscribe).toHaveBeenCalled());
  fireEvent.click(screen.getByRole("button", { name: /^Work/ }));
  await act(async () => fixture.receive([kind === "coding" ? event(1, "coding_task", { id: "coding", status: "running", text: "Building" }) : event(1, "turn_finished", { task_preview: proposal })]));
  const button = await screen.findByRole("button", { name: kind === "coding" ? "Stop task" : "Test preview" });
  fireEvent.click(button);
  await waitFor(() => expect(fixture.request.mock.calls.filter(call => call[0].endsWith("/task-actions"))).toHaveLength(1));
  expect(button).toBeDisabled();
  await act(async () => fixture.receive([event(2, "task_action_result", { action_id: "other", task_id: kind === "coding" ? "coding" : "schedule" })]));
  expect(button).toBeDisabled();
  await act(async () => fixture.receive([event(3, "task_action_result", { action_id: "mine", status: "completed" })]));
  expect(button).toBeEnabled();
});

it("retries an uncertain task action with the same identifier and blocks conflicting actions", async () => {
  const fixture = connection();
  const base = fixture.request.getMockImplementation()!;
  fixture.request.mockImplementation(async (path, ...args) => {
    if (path.endsWith("/task-actions")) throw new TypeError("Network interrupted");
    return base(path, ...args);
  });
  render(<DashboardApp connection={fixture.value} />);
  await waitFor(() => expect(fixture.value.api.subscribe).toHaveBeenCalled());
  fireEvent.click(screen.getByRole("button", { name: /^Work/ }));
  await act(async () => fixture.receive([event(1, "turn_finished", { task_preview: proposal })]));
  fireEvent.click(await screen.findByRole("button", { name: "Test preview" }));
  fireEvent.click(await screen.findByRole("button", { name: "Retry action" }));
  await waitFor(() => expect(fixture.request.mock.calls.filter(call => call[0].endsWith("/task-actions"))).toHaveLength(2));
  const calls = fixture.request.mock.calls.filter(call => call[0].endsWith("/task-actions"));
  expect(calls[0][2]).toEqual(calls[1][2]);
  expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
});

it("renders consent Markdown and provides the promised Decline control", async () => {
  const fixture = connection();
  fixture.value.session = { ...session, consent_required: true, consent_text: "**Your privacy**. Choose Accept or Decline." };
  render(<DashboardApp connection={fixture.value} />);
  expect(await screen.findByText("Your privacy", { selector: "strong" })).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Decline" }));
  await waitFor(() => expect(fixture.request).toHaveBeenCalledWith("/consent", "POST", { accept: false }));
  expect(screen.getByRole("textbox", { name: "Message Kimi" })).toBeDisabled();
});

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
  await waitFor(() => expect(fixture.value.api.subscribe).toHaveBeenCalled());
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
  await waitFor(() => expect(fixture.value.api.subscribe).toHaveBeenCalled());
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
    if (path === "/chats/a/events") return { events: [
      ...Array.from({ length: 199 }, (_, index) => event(index + 2, "activity", {})),
      event(201, "turn_finished", { text: "A recent message", status: "completed" }),
    ] };
    if (path.endsWith("/events")) return { events: [] };
    if (path.endsWith("/tasks")) return { tasks: [] };
    return { files: [] };
  });
  render(<DashboardApp connection={fixture.value} />);
  // A full event page enables pagination; only its final response needs to be
  // visible for this test of switching chats during an in-flight history read.
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

it("recovers chat list and history initialization after transient verification failures", async () => {
  vi.useFakeTimers();
  try {
    const fixture = connection();
    const base = fixture.request.getMockImplementation()!;
    const unavailable = new Set(["/chats", "/chats/a/events"]);
    fixture.request.mockImplementation(async (path, ...args) => {
      if (unavailable.delete(path)) throw new ApiError("Verification unavailable", 503);
      return base(path, ...args);
    });
    render(<DashboardApp connection={fixture.value} />);
    await act(async () => {});
    expect(screen.getByText("Connection unavailable. Retrying…")).toBeVisible();
    await act(async () => vi.advanceTimersByTimeAsync(1000));
    expect(screen.getByText("Reconnecting…")).toBeVisible();
    expect(fixture.value.api.subscribe).not.toHaveBeenCalled();
    await act(async () => vi.advanceTimersByTimeAsync(1000));
    expect(fixture.value.api.subscribe).toHaveBeenCalledOnce();
    expect(screen.queryByText("Reconnecting…")).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  } finally { vi.useRealTimers(); }
});

it("cancels history retries when another conversation is selected", async () => {
  vi.useFakeTimers();
  try {
    const fixture = connection();
    const base = fixture.request.getMockImplementation()!;
    fixture.request.mockImplementation(async (path, ...args) => {
      if (path === "/chats") return { chats: [chat, { ...chat, id: "b", title: "Other conversation" }] };
      if (path === "/chats/a/events") throw new ApiError("Verification unavailable", 503);
      return base(path, ...args);
    });
    render(<DashboardApp connection={fixture.value} />);
    await act(async () => {});
    expect(screen.getByText("Reconnecting…")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Other conversation" }));
    await act(async () => vi.advanceTimersByTimeAsync(30000));
    expect(fixture.request.mock.calls.filter(call => call[0] === "/chats/a/events")).toHaveLength(1);
    expect(fixture.value.api.subscribe).toHaveBeenCalledOnce();
    expect(fixture.value.api.subscribe).toHaveBeenCalledWith("b", 0, expect.any(Function), expect.any(Function));
  } finally { vi.useRealTimers(); }
});

it("loads earlier parent history and focuses the exact branch starting message", async () => {
  const fixture = connection();
  const parent = { ...chat, id: "parent", title: "Parent conversation" };
  const branch = { ...chat, parent_id: parent.id, parent_event_id: 1, parent_title: parent.title };
  fixture.request.mockImplementation(async path => {
    if (path === "/chats") return { chats: [branch, parent] };
    if (path === "/chats/parent") return parent;
    if (path === "/chats/a/events") return { events: [event(301, "history_message", { role: "assistant", text: "Copied answer" })] };
    if (path === "/chats/parent/events") return { events: [event(201, "turn_finished", { text: "Later answer", status: "completed" })] };
    if (path === "/chats/parent/events?before=201") return { events: [event(1, "turn_finished", { text: "Starting answer", status: "completed" })] };
    if (path.endsWith("/tasks")) return { tasks: [] };
    return { files: [] };
  });
  render(<DashboardApp connection={fixture.value} />);
  expect(await screen.findByText("Copied conversation history")).toBeVisible();
  expect(screen.getByText("New work in this branch")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Parent: Parent conversation" }));
  const starting = (await screen.findByText("Starting answer")).closest("article");
  expect(starting).toHaveClass("highlighted-message");
  expect(starting).toHaveFocus();
  expect(screen.getByText("Later answer")).toBeVisible();
});

it("shows persisted return feedback and explains deletion of copied conversations", async () => {
  const fixture = connection();
  fixture.request.mockImplementation(async path => {
    if (path === "/chats") return { chats: [{ ...chat, parent_id: "parent", parent_title: "Parent" }] };
    if (path.endsWith("/events")) return { events: [event(1, "turn_finished", { text: "Already returned", status: "completed", returned_to_parent: true })] };
    if (path.endsWith("/tasks")) return { tasks: [] };
    return { files: [] };
  });
  render(<DashboardApp connection={fixture.value} />);
  expect(await screen.findByRole("button", { name: "Brought to parent" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Options for A test conversation" }));
  fireEvent.click(screen.getByRole("button", { name: "Delete" }));
  expect(screen.getByRole("dialog")).toHaveTextContent("Branches and responses already brought into other conversations remain.");
});

describe("identity and loading", () => {
  const bot = "data:image/png;base64,Qk9U";
  const me = "data:image/png;base64,VVNS";
  const exchange = [event(1, "user_message", { text: "hi", turn_id: "t" }), event(2, "turn_finished", { text: "hello", status: "completed", turn_id: "t" })];

  it("holds the launch screen until conversations load, then shows the shell once", async () => {
    const fixture = connection();
    const base = fixture.request.getMockImplementation()!;
    let release: (value: unknown) => void = () => {};
    fixture.request.mockImplementation(async (path, ...args) => path === "/chats" ? new Promise(resolve => { release = resolve; }) : base(path, ...args));
    const { container } = render(<DashboardApp connection={{ ...fixture.value, botAvatar: bot }} />);
    expect(container.querySelector(".launch-screen img")).toHaveAttribute("src", bot);
    expect(screen.getByText("Kimi")).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
    await act(async () => release({ chats: [chat] }));
    expect(await screen.findByRole("textbox", { name: "Message Kimi" })).toBeVisible();
    expect(container.querySelector(".launch-screen")).not.toBeInTheDocument();
  });

  it("shows placeholder rows while a conversation opens", async () => {
    const fixture = connection();
    const base = fixture.request.getMockImplementation()!;
    let release: (value: unknown) => void = () => {};
    fixture.request.mockImplementation(async (path, ...args) => path.endsWith("/events") ? new Promise(resolve => { release = resolve; }) : base(path, ...args));
    render(<DashboardApp connection={fixture.value} />);
    await screen.findByRole("textbox", { name: "Message Kimi" });
    expect(await screen.findByRole("status", { busy: true })).toHaveTextContent("Opening conversation");
    await act(async () => release({ events: exchange }));
    expect(await screen.findByText("hello")).toBeVisible();
    expect(screen.queryByText("Opening conversation")).not.toBeInTheDocument();
  });

  it("renders Discord avatars inline and falls back to initials", async () => {
    const fixture = connection();
    const base = fixture.request.getMockImplementation()!;
    fixture.request.mockImplementation(async (path, ...args) => path.endsWith("/events") ? { events: exchange } : base(path, ...args));
    const live = render(<DashboardApp connection={{ ...fixture.value, botAvatar: bot, session: { ...session, user_avatar: me } }} />);
    await screen.findByText("hello");
    expect([...live.container.querySelectorAll(".message-avatar img")].map(img => img.getAttribute("src"))).toEqual([me, bot]);
    expect(live.container.querySelector(".brand img")).toHaveAttribute("src", bot);
    live.unmount();
    const fallback = render(<DashboardApp connection={fixture.value} />);
    await screen.findByText("hello");
    expect([...fallback.container.querySelectorAll(".message-avatar")].map(node => node.textContent)).toEqual(["C", "K"]);
    expect(fallback.container.querySelector(".message-avatar img")).toBeNull();
  });

  it("labels failed, stopped, and interrupted responses distinctly", async () => {
    const fixture = connection();
    const base = fixture.request.getMockImplementation()!;
    fixture.request.mockImplementation(async (path, ...args) => path.endsWith("/events") ? { events: [
      event(1, "turn_finished", { text: "a", status: "failed", turn_id: "x" }),
      event(2, "turn_finished", { text: "b", status: "cancelled", turn_id: "y" }),
      event(3, "turn_finished", { text: "c", status: "interrupted", turn_id: "z" }),
    ] } : base(path, ...args));
    render(<DashboardApp connection={fixture.value} />);
    expect(await screen.findByText("Response failed")).toHaveClass("danger");
    expect(screen.getByText("Response stopped")).not.toHaveClass("danger");
    expect(screen.getByText("Response interrupted")).toBeVisible();
  });
});


it.each([undefined, "Too many dashboard tabs. Close another tab, then close and reopen this Activity to continue."])("disables mutations on a terminal connection with reopen instructions: %s", async notice => {
  const fixture = connection();
  let status: Parameters<DashboardApi["subscribe"]>[3] = () => {};
  vi.mocked(fixture.value.api.subscribe).mockImplementation((_chat, _after, _receive, callback) => { status = callback; callback(true); return () => {}; });
  render(<DashboardApp connection={fixture.value} />);
  const composer = await screen.findByRole("textbox", { name: "Message Kimi" });
  fireEvent.change(composer, { target: { value: "Keep my draft" } });
  fireEvent.click(screen.getByRole("button", { name: "Options for A test conversation" }));
  fireEvent.click(screen.getByRole("button", { name: "Rename" }));
  await act(async () => status(false, true, notice));
  expect(screen.getByRole("alert")).toHaveTextContent(/close and reopen this Activity/i);
  expect(composer).toBeDisabled();
  expect(composer).toHaveValue("Keep my draft");
  expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Attach files" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "New chat" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Options for A test conversation" })).toBeDisabled();
  expect(screen.queryByRole("button", { name: "Save name" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Reconnect" })).not.toBeInTheDocument();
  expect(screen.queryByText("Reconnecting…")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Dismiss error" })).not.toBeInTheDocument();
});

it("renders history placeholders without CSP-blocked inline styles", async () => {
  const fixture = connection();
  const base = fixture.request.getMockImplementation()!;
  fixture.request.mockImplementation((path, ...args) => path.endsWith("/events") ? new Promise(() => {}) : base(path, ...args));
  const { container } = render(<DashboardApp connection={fixture.value} />);
  await screen.findByText("Opening conversation");
  expect(container.querySelectorAll(".skeleton [style]")).toHaveLength(0);
});


it("keeps terminal recovery instructions when an older request fails late", async () => {
  const fixture = connection();
  let status: Parameters<DashboardApi["subscribe"]>[3] = () => {};
  let fail: (error: Error) => void = () => {};
  vi.mocked(fixture.value.api.subscribe).mockImplementation((_chat, _after, _receive, callback) => { status = callback; callback(true); return () => {}; });
  const base = fixture.request.getMockImplementation()!;
  fixture.request.mockImplementation((path, ...args) => path.endsWith("/messages") ? new Promise((_resolve, reject) => { fail = reject; }) : base(path, ...args));
  render(<DashboardApp connection={fixture.value} />);
  fireEvent.change(await screen.findByRole("textbox", { name: "Message Kimi" }), { target: { value: "hello" } });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  await act(async () => status(false, true, "Too many dashboard tabs. Close another tab, then close and reopen this Activity to continue."));
  expect(screen.getByRole("button", { name: "Stop response" })).toBeDisabled();
  await act(async () => fail(new Error("Network interrupted")));
  expect(screen.getByRole("alert")).toHaveTextContent("Close another tab");
  expect(screen.getByRole("alert")).toHaveTextContent("close and reopen this Activity");
  fireEvent.click(screen.getByRole("button", { name: "A test conversation" }));
  expect(screen.getByRole("alert")).toHaveTextContent("Close another tab");
});
