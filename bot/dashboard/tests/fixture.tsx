// Browser-only fixture. Vite's production entry is index.html, so this is not shipped.
import { createRoot } from "react-dom/client";
import { DashboardApp } from "../src/App";
import { DashboardApi } from "../src/api";
import type { ChatEvent, Chat, FileRecord } from "../src/types";
import "../src/tokens.css";
import "../src/styles.css";

const now = Date.now() / 1000;
const chats: Chat[] = [
  { id: "brief", guild_id: "2", channel_id: "3", parent_channel_id: "3", channel_name: "the-workshop", title: "A weekly community digest", created_at: now, updated_at: now },
  { id: "project", guild_id: "2", channel_id: "3", parent_channel_id: "3", channel_name: "the-workshop", title: "Ideas for the next game night", created_at: now, updated_at: now - 100 },
];
const file: FileRecord = { id: "digest", filename: "community-notes.md", size: 4200, media_type: "text/markdown", kind: "output" };
const events: ChatEvent[] = [
  { id: 1, kind: "user_message", created_at: now - 150, payload: { turn_id: "first", text: "Can you help me put together a weekly digest for our community? I'd like something useful and easy to skim." } },
  { id: 2, kind: "turn_finished", created_at: now - 140, payload: { turn_id: "first", status: "completed", text: "I've drafted a weekly digest that brings the useful bits together: **what we made, what we learned, and what's coming next.**\n\nThe attached notes are a starting point. You can edit the tone and sections before we schedule anything.\n\n| Section | What goes in it |\n| --- | --- |\n| This week | Project updates and good conversations |\n| Worth a look | Links shared by the community |\n| Coming up | Events and ways to get involved |", files: [file], task_preview: { id: "task", revision: 1, name: "Friday community digest", status: "pending", text: "Every Friday at **4:00 PM America/Los_Angeles**, summarize this week's highlights for #community-updates.\n\nYou can test a sample before approving.", details: "Reads messages from the workshop and posts one digest in community-updates. Owner: Charlie. Schedule: weekly.", skill: "# Weekly digest\n\nCollect useful updates from this week. Write three short sections, cite original messages, and invite people to the next event.", python: null } } },
];
let receive: (events: ChatEvent[]) => void = () => {};
const api = new DashboardApi();
api.request = async <T,>(path: string, method = "GET", body?: unknown): Promise<T> => {
  const data = body as Record<string, string> | undefined;
  let result: unknown = {};
  if (path === "/chats" && method === "POST") { const chat = { ...chats[0], id: crypto.randomUUID(), title: "New chat" }; chats.unshift(chat); result = chat; }
  else if (path === "/chats") result = { chats: [...chats] };
  else if (path.endsWith("/events")) result = { events: path.includes("/brief/") ? events : [] };
  else if (path.endsWith("/files")) result = { files: [file] };
  else if (path.endsWith("/tasks")) result = { tasks: [] };
  else if (path.endsWith("/preview")) result = { kind: "markdown", text: "# Community notes\n\nA few highlights from this week.\n\n- A new tool to try\n- A project worth following\n- Friday game night" };
  else if (path.includes("/workspace?")) result = { files: [{ filename: "projects", directory: true, path: "projects", size: 0 }, { filename: "notes.md", directory: false, path: "notes.md", size: 1200 }] };
  else if (path.endsWith("/messages")) { const turn = crypto.randomUUID(); const id = events.at(-1)!.id + 1; const update = [{ id, kind: "user_message", created_at: now, payload: { turn_id: turn, text: data?.text } }, { id: id + 1, kind: "turn_finished", created_at: now, payload: { turn_id: turn, status: "completed", text: "That sounds good. I've kept your direction with this conversation." } }]; events.push(...update); receive(update); result = { turn_id: turn }; }
  else if (method === "PATCH") { const chat = chats.find(chat => path.endsWith(chat.id)); if (chat && data) chat.title = data.title; }
  else if (method === "DELETE") { const index = chats.findIndex(chat => path.endsWith(chat.id)); if (index >= 0) chats.splice(index, 1); }
  return result as T;
};
api.subscribe = (_chat, _after, callback, status) => { receive = callback; status(true); return () => {}; };
createRoot(document.getElementById("root")!).render(<DashboardApp connection={{ api, displayName: "Charlie", openLink: async () => {}, session: { user_id: "1", guild_id: "2", channel_id: "3", csrf: "fixture", bot_name: "Kimi", retention_days: 30, consent_required: false, consent_title: "Privacy", consent_text: "", max_upload_bytes: 25000000, max_message_chars: 32000 } }} />);
