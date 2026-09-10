import { createRoot } from "react-dom/client";
import { connectActivity } from "./api";
import { DashboardApp } from "./App";
import "./styles.css";

const root = createRoot(document.getElementById("root")!);
root.render(<main className="launch-screen"><div className="kimi-mark large">k</div><p role="status">Connecting to Discord…</p></main>);
void connectActivity().then(connection => root.render(<DashboardApp connection={connection} />)).catch((error: Error) => {
  root.render(<main className="launch-screen"><div className="kimi-mark large">k</div><h1>Couldn’t open the dashboard</h1><p>{error.message}</p><button onClick={() => location.reload()}>Try again</button></main>);
});
