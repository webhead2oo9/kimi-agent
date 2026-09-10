export interface Session {
  user_id: string;
  guild_id: string;
  channel_id: string;
  csrf: string;
  bot_name: string;
  retention_days: number;
  consent_required: boolean;
  consent_title: string;
  consent_text: string;
  max_upload_bytes: number;
  max_message_chars: number;
}

export interface Chat {
  id: string;
  guild_id: string;
  channel_id: string;
  parent_channel_id: string;
  channel_name: string;
  title: string;
  created_at: number;
  updated_at: number;
}

export interface FileRecord {
  id?: string;
  filename: string;
  size?: number;
  media_type?: string;
  kind?: string;
  expired?: boolean;
}

export interface TaskPreview {
  id: string;
  revision: number;
  name: string;
  status: string;
  text: string;
  details: string;
  skill: string;
  python: string | null;
}

export interface ChatEvent {
  id: number;
  kind: string;
  created_at: number;
  payload: {
    id?: string;
    turn_id?: string;
    text?: string;
    label?: string;
    status?: string;
    files?: FileRecord[];
    steps?: { content: string; status: string }[];
    task_preview?: TaskPreview;
    coding_task_id?: string;
    action_id?: string;
    action?: string;
    task_id?: string;
    revision?: number;
    outcome?: string;
    posts?: { channel_id: string; content: string }[];
  };
}

export interface Preview {
  kind: "text" | "markdown" | "image" | "download";
  text?: string;
  truncated?: boolean;
  notice?: string;
}

export interface WorkspaceFile {
  path: string;
  filename: string;
  directory: boolean;
  size: number;
}

export const activeCodingStates = new Set(["queued", "recovering", "running", "waiting_for_job", "waiting_for_input", "cancelling"]);

export function mergeEvents(previous: ChatEvent[], incoming: ChatEvent[]): ChatEvent[] {
  const merged = new Map(previous.map(event => [event.id, event]));
  for (const event of incoming) merged.set(event.id, event);
  return [...merged.values()].sort((left, right) => left.id - right.id);
}

export function isResponding(events: ChatEvent[]): boolean {
  const active = new Set<string>();
  for (const event of events) {
    const turn = event.payload.turn_id;
    if (!turn) continue;
    if (event.kind === "turn_finished") active.delete(turn);
    else if (["user_message", "activity", "plan"].includes(event.kind)) active.add(turn);
  }
  return active.size > 0;
}

export function latestWork(events: ChatEvent[]) {
  const coding = new Map<string, ChatEvent>();
  const previews = new Map<string, TaskPreview>();
  const actions = new Map<string, ChatEvent>();
  for (const event of events) {
    if (event.kind === "coding_task" && event.payload.id) coding.set(event.payload.id, event);
    if (event.payload.task_preview) previews.set(event.payload.task_preview.id, event.payload.task_preview);
    if (event.payload.action_id) {
      if (event.kind === "task_action_result") actions.delete(event.payload.action_id);
      else actions.set(event.payload.action_id, event);
    }
  }
  return { coding: [...coding.values()], previews: [...previews.values()], actions: [...actions.values()] };
}
