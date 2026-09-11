import { Avatar, initialOf } from "./Avatar";

export type LaunchState = "connecting" | "retrying" | "failed";

// Shown from the first paint until the conversation list is ready, so the shell
// appears once rather than filling in piece by piece.
export function LaunchScreen({ name, avatar, state, message, onRetry }: { name?: string | null; avatar?: string | null; state: LaunchState; message?: string; onRetry?: () => void }) {
  return <main className="launch-screen">
    <Avatar className={state === "failed" ? "launch" : "launch breathing"} src={avatar} fallback={initialOf(name)} />
    {name && <span className="launch-name">{name}</span>}
    {state === "connecting" && <p className="visually-hidden" role="status">Opening the dashboard</p>}
    {state === "retrying" && <p className="small-text" role="status">Connection unavailable. Retrying…</p>}
    {state === "failed" && <><h1>Couldn’t open the dashboard</h1><p>{message}</p><button onClick={onRetry}>Try again</button></>}
  </main>;
}
