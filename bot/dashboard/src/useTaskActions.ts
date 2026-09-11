import { useCallback, useRef, useState } from "react";
import { ApiError, type DashboardApi } from "./api";
import type { ChatEvent } from "./types";

type ActionBody = { task_id: string; action: string; revision: number; message: string; request_id: string };
export interface PendingTaskAction {
  chatId: string;
  body: ActionBody;
  actionId?: string;
  uncertain: boolean;
  sending: boolean;
  finished: Set<string>;
}

export function useTaskActions(api: DashboardApi) {
  const actions = useRef(new Map<string, PendingTaskAction>());
  const [pending, setPending] = useState<PendingTaskAction[]>([]);
  const publish = useCallback(() => setPending([...actions.current.values()].map(action => ({ ...action }))), []);
  const reconcile = useCallback((chatId: string, events: ChatEvent[]) => {
    for (const [key, action] of actions.current) {
      if (action.chatId !== chatId) continue;
      for (const event of events) {
        if (event.kind === "task_action" && event.payload.request_id === action.body.request_id) action.actionId = event.payload.action_id;
        if (event.kind === "task_action_result") {
          if (event.payload.action_id) action.finished.add(event.payload.action_id);
          if (event.payload.request_id === action.body.request_id) actions.current.delete(key);
        }
      }
      if (action.actionId && action.finished.has(action.actionId)) actions.current.delete(key);
    }
    publish();
  }, [publish]);
  const perform = useCallback(async (chatId: string, taskId: string, action: string, revision = 0, message = "") => {
    const key = `${chatId}:${taskId}`;
    let pending = actions.current.get(key);
    if (pending) {
      if (pending.sending || !pending.uncertain) return;
      if (pending.body.action !== action || pending.body.revision !== revision || pending.body.message !== message) throw new Error("Wait for the pending task action to finish.");
    } else {
      pending = { chatId, body: { task_id: taskId, action, revision, message, request_id: crypto.randomUUID() }, uncertain: false, sending: false, finished: new Set() };
      actions.current.set(key, pending);
    }
    pending.sending = true;
    publish();
    try {
      const result = await api.request<{ action_id: string }>(`/chats/${chatId}/task-actions`, "POST", pending.body);
      if (actions.current.get(key) !== pending) return;
      pending.actionId = result.action_id;
      pending.uncertain = false;
      if (pending.finished.has(result.action_id)) actions.current.delete(key);
    } catch (error) {
      if (actions.current.get(key) === pending) {
        // A network/5xx failure can hide an accepted action. Keep its exact body
        // and identifier for retry; conflicting actions remain disabled.
        if (!pending.uncertain && error instanceof ApiError && error.status >= 400 && error.status < 500) actions.current.delete(key);
        else pending.uncertain = true;
      }
      throw error;
    } finally {
      pending.sending = false;
      publish();
    }
  }, [api, publish]);
  return { pending, perform, reconcile };
}
