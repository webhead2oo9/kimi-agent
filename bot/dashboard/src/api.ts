import type { ChatEvent, Session } from "./types";

export class ApiError extends Error {
  constructor(message: string, readonly status: number) { super(message); }
}

export class DashboardApi {
  constructor(public csrf = "") {}

  async request<T>(path: string, method = "GET", body?: unknown): Promise<T> {
    const headers: Record<string, string> = {};
    if (method !== "GET") headers["X-CSRF-Token"] = this.csrf;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const response = await fetch(`/api${path}`, {
      method, credentials: "same-origin", headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    return this.decode<T>(response);
  }

  async upload<T>(chatId: string, file: File): Promise<T> {
    const response = await fetch(`/api/chats/${chatId}/upload?filename=${encodeURIComponent(file.name)}`, {
      method: "POST", credentials: "same-origin", headers: { "X-CSRF-Token": this.csrf }, body: file,
    });
    return this.decode<T>(response);
  }

  private async decode<T>(response: Response): Promise<T> {
    const payload = await response.json().catch(() => null);
    if (!response.ok) throw new ApiError(payload?.error || "Kimi is unavailable. Try again shortly.", response.status);
    if (!payload) throw new ApiError("The server returned an unreadable response.", 502);
    return payload as T;
  }

  subscribe(chatId: string, after: number, receive: (events: ChatEvent[]) => void, status: (connected: boolean, expired?: boolean) => void): () => void {
    let socket: WebSocket | undefined;
    let retry: ReturnType<typeof setTimeout> | undefined;
    let stopped = false;
    let last = after;
    let delay = 1000;
    const connect = () => {
      if (stopped) return;
      const url = new URL("/api/ws", window.location.href);
      url.protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      url.searchParams.set("chat", chatId);
      url.searchParams.set("after", String(last));
      socket = new WebSocket(url);
      socket.onopen = () => { delay = 1000; status(true); };
      socket.onmessage = (message) => {
        try {
          const data: { events: ChatEvent[] } = JSON.parse(message.data);
          if (!Array.isArray(data.events)) return;
          for (const event of data.events) last = Math.max(last, event.id);
          receive(data.events);
        } catch { status(false); }
      };
      socket.onclose = (event) => {
        status(false, event.code === 1008);
        if (!stopped && event.code !== 1008) {
          retry = setTimeout(() => {
            void this.request("/session").then(connect).catch(error => {
              if (stopped) return;
              if (error instanceof ApiError && [401, 403].includes(error.status)) status(false, true);
              else connect();
            });
          }, delay);
          delay = Math.min(delay * 2, 15000);
        }
      };
      socket.onerror = () => socket?.close();
    };
    connect();
    return () => { stopped = true; clearTimeout(retry); socket?.close(); };
  }
}

export interface Connection {
  api: DashboardApi;
  session: Session;
  displayName: string;
  openLink: (url: string) => Promise<void>;
}

export async function connectActivity(): Promise<Connection> {
  if (!new URLSearchParams(location.search).has("frame_id")) {
    throw new Error("Open the Kimi dashboard from the App Launcher or /dashboard inside a Discord server.");
  }
  const api = new DashboardApi();
  const bootstrap = await api.request<{ client_id: string; state: string }>("/bootstrap");
  const { DiscordSDK } = await import("@discord/embedded-app-sdk");
  const sdk = new DiscordSDK(bootstrap.client_id, { disableConsoleLogOverride: true });
  await Promise.race([sdk.ready(), new Promise<never>((_, reject) => setTimeout(() => reject(new Error("Discord did not connect. Close this Activity and open it again.")), 20000))]);
  const { code } = await sdk.commands.authorize({
    client_id: bootstrap.client_id, response_type: "code", state: bootstrap.state,
    prompt: "none", scope: ["identify"],
  });
  const result = await api.request<Session & { access_token: string }>("/auth", "POST", {
    code, state: bootstrap.state, instance_id: sdk.instanceId,
  });
  const identity = await sdk.commands.authenticate({ access_token: result.access_token });
  // The OAuth token is used only for the SDK handshake; requests use the
  // HttpOnly session cookie. Only the last selected chat goes in localStorage.
  const { access_token: _accessToken, ...session } = result;
  api.csrf = session.csrf;
  return {
    api, session, displayName: identity.user.global_name || identity.user.username,
    openLink: async (url: string) => { await sdk.commands.openExternalLink({ url }); },
  };
}
