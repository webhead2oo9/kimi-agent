import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ApiError, DashboardApi, retryRead } from "./api";

class Socket {
  static all: Socket[] = [];
  onmessage?: (message: { data: string }) => void;
  onclose?: (event: { code: number }) => void;
  onerror?: () => void;
  close = vi.fn(() => this.onclose?.({ code: 1000 }));
  constructor(readonly url: URL) { Socket.all.push(this); }
  receive(events: unknown[]) { this.onmessage?.({ data: JSON.stringify({ events, ready: true }) }); }
}

beforeEach(() => { vi.useFakeTimers(); Socket.all = []; vi.stubGlobal("WebSocket", Socket); });
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

it.each([new ApiError("Unavailable", 503), new TypeError("Network unavailable")])("recovers read initialization after %s without replaying late results", async error => {
  const read = vi.fn().mockRejectedValue(error);
  const receive = vi.fn(), failed = vi.fn(), retrying = vi.fn();
  const stop = retryRead(read, receive, failed, retrying);
  await vi.advanceTimersByTimeAsync(1000);
  expect(read).toHaveBeenCalledTimes(2);
  expect(receive).not.toHaveBeenCalled();
  expect(failed).not.toHaveBeenCalled();
  let resolve: (value: unknown) => void = () => {};
  read.mockImplementation(() => new Promise(done => { resolve = done; }));
  await vi.advanceTimersByTimeAsync(2000);
  stop(); resolve({ events: [] });
  await vi.advanceTimersByTimeAsync(30000);
  expect(read).toHaveBeenCalledTimes(3);
  expect(receive).not.toHaveBeenCalled();
});

it("does not retry denied read access", async () => {
  const error = new ApiError("Access revoked", 403);
  const read = vi.fn().mockRejectedValue(error), failed = vi.fn();
  const stop = retryRead(read, vi.fn(), failed, vi.fn());
  await vi.advanceTimersByTimeAsync(30000);
  expect(read).toHaveBeenCalledOnce();
  expect(failed).toHaveBeenCalledWith(error);
  stop();
});

it("waits for verified data and resumes after the last delivered event", async () => {
  const api = new DashboardApi();
  vi.spyOn(api, "request").mockResolvedValue({});
  const receive = vi.fn(), status = vi.fn();
  const stop = api.subscribe("a", 7, receive, status);
  expect(status).not.toHaveBeenCalledWith(true);
  Socket.all[0].receive([{ id: 8, kind: "activity", payload: {}, created_at: 1 }]);
  expect(status).toHaveBeenLastCalledWith(true);
  Socket.all[0].onclose?.({ code: 1013 });
  await vi.advanceTimersByTimeAsync(1000);
  expect(Socket.all).toHaveLength(2);
  expect(Socket.all[1].url.searchParams.get("after")).toBe("8");
  expect(status).not.toHaveBeenCalledWith(false, true);
  Socket.all[1].receive([]);
  expect(status).toHaveBeenLastCalledWith(true);
  stop();
});

it("backs off during verification outages without reopening an unverified socket", async () => {
  const api = new DashboardApi();
  const request = vi.spyOn(api, "request").mockRejectedValue(new ApiError("Unavailable", 503));
  const status = vi.fn();
  const stop = api.subscribe("a", 0, vi.fn(), status);
  Socket.all[0].onclose?.({ code: 1013 });
  await vi.advanceTimersByTimeAsync(1000);
  expect(Socket.all).toHaveLength(1);
  request.mockResolvedValue({});
  await vi.advanceTimersByTimeAsync(1999);
  expect(Socket.all).toHaveLength(1);
  await vi.advanceTimersByTimeAsync(1);
  expect(Socket.all).toHaveLength(2);
  expect(status).not.toHaveBeenCalledWith(false, true);
  stop();
});

it("stops on revoked saved-channel access even when the launch session may still be valid", async () => {
  const api = new DashboardApi();
  vi.spyOn(api, "request").mockRejectedValue(new ApiError("Revoked", 403));
  const status = vi.fn();
  const stop = api.subscribe("a", 0, vi.fn(), status);
  Socket.all[0].onclose?.({ code: 1006 });
  await vi.advanceTimersByTimeAsync(30000);
  expect(status).toHaveBeenLastCalledWith(false, true);
  expect(Socket.all).toHaveLength(1);
  stop();
});

it("ignores late data and pending retries after changing conversations", async () => {
  const api = new DashboardApi();
  let resolve: (value: unknown) => void = () => {};
  vi.spyOn(api, "request").mockImplementation(() => new Promise(done => { resolve = done; }));
  const receive = vi.fn(), status = vi.fn();
  const stop = api.subscribe("a", 0, receive, status);
  Socket.all[0].onclose?.({ code: 1006 });
  stop(); status.mockClear();
  Socket.all[0].receive([{ id: 1 }]);
  resolve({});
  await vi.advanceTimersByTimeAsync(30000);
  expect(Socket.all).toHaveLength(1);
  expect(receive).not.toHaveBeenCalled();
  expect(status).not.toHaveBeenCalled();
});

it("reports the fourth-tab limit without expiring the session or retrying forever", async () => {
  const api = new DashboardApi();
  vi.spyOn(api, "request").mockResolvedValue({});
  const status = vi.fn();
  const stop = api.subscribe("a", 0, vi.fn(), status);
  Socket.all[0].onmessage?.({ data: JSON.stringify({ error: "Close another dashboard tab before reconnecting", code: "socket_limit" }) });
  Socket.all[0].onclose?.({ code: 4008 });
  await vi.advanceTimersByTimeAsync(30000);
  expect(status).toHaveBeenLastCalledWith(false, false, "Close another dashboard tab before reconnecting");
  expect(Socket.all).toHaveLength(1);
  stop();
});
