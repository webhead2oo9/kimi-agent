import { createRoot } from "react-dom/client";
import { connectActivity, type Launch } from "./api";
import { DashboardApp } from "./App";
import { LaunchScreen, type LaunchState } from "./LaunchScreen";
import "./styles.css";

const root = createRoot(document.getElementById("root")!);
let launch: Launch | undefined;
const show = (state: LaunchState, message?: string) => root.render(<LaunchScreen state={state} name={launch?.bot_name} avatar={launch?.bot_avatar} message={message} onRetry={() => location.reload()} />);
show("connecting");
void connectActivity(current => { launch = current; show("connecting"); })
  .then(connection => root.render(<DashboardApp connection={connection} />))
  .catch((error: Error) => show("failed", error.message));
