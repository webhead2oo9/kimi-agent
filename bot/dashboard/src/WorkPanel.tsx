import { useEffect, useRef, useState } from "react";
import { ArrowLeft, Check, ChevronRight, Code2, Download, FileText, Folder, FolderOpen, LoaderCircle, Play, RefreshCw, Send, Square, X } from "lucide-react";
import type { Connection } from "./api";
import { Markdown } from "./Markdown";
import { FileButton, readableSize } from "./App";
import { activeCodingStates, latestWork, mergeEvents, type Chat, type ChatEvent, type FileRecord, type Preview, type TaskPreview, type WorkspaceFile } from "./types";

function statusLabel(status: string): string {
  const text = status.replaceAll("_", " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

interface Props {
  chat: Chat | undefined;
  events: ChatEvent[];
  files: FileRecord[];
  selectedFile: FileRecord | null;
  onSelectFile: (file: FileRecord | null) => void;
  onClose: () => void;
  connection: Connection;
  onAction: (taskId: string, action: string, revision?: number, message?: string) => Promise<void>;
  onError: (error: unknown) => void;
}

export function WorkPanel(props: Props) {
  const { chat, events, files, selectedFile, onSelectFile, onClose, connection, onAction, onError } = props;
  const [directory, setDirectory] = useState("");
  const [workspaceFiles, setWorkspaceFiles] = useState<WorkspaceFile[] | null>(null);
  const [workspaceOpen, setWorkspaceOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [states, setStates] = useState<Record<string, string>>({});
  const [savedWork, setSavedWork] = useState<ChatEvent[]>([]);
  const work = latestWork(mergeEvents(savedWork, events));
  const recentPreviewId = [...events].reverse().find(event => event.payload.task_preview)?.id;
  const recentActionId = [...events].reverse().find(event => event.kind === "task_action_result")?.id;
  useEffect(() => { setDirectory(""); setWorkspaceFiles(null); setWorkspaceOpen(false); setStates({}); }, [chat?.id]);
  useEffect(() => {
    if (!chat) return;
    let live = true;
    void connection.api.request<{ tasks: { id: string; revision: number; status: string }[]; events?: ChatEvent[] }>(`/chats/${chat.id}/tasks`).then(result => {
      if (live) { setStates(Object.fromEntries(result.tasks.map(task => [`${task.id}:${task.revision}`, task.status]))); setSavedWork(result.events || []); }
    }).catch(onError);
    return () => { live = false; };
  }, [chat?.id, connection.api, recentActionId, recentPreviewId, onError]);
  const browseGeneration = useRef(0);
  useEffect(() => () => { browseGeneration.current++; }, []);
  const browse = async (path: string) => {
    if (!chat) return;
    const generation = ++browseGeneration.current;
    setLoading(true);
    try {
      const result = await connection.api.request<{ files: WorkspaceFile[] }>(`/chats/${chat.id}/workspace?directory=${encodeURIComponent(path)}`);
      if (generation !== browseGeneration.current) return;
      setWorkspaceFiles(result.files); setDirectory(path); setWorkspaceOpen(true);
    } catch (error) { if (generation === browseGeneration.current) onError(error); }
    finally { if (generation === browseGeneration.current) setLoading(false); }
  };
  return <aside className="work-panel" aria-label="Work panel">
    <header className="work-header"><h2>{selectedFile ? "File preview" : "Work"}</h2><button className="icon-button" aria-label="Close work panel" onClick={onClose}><X size={18} /></button></header>
    {selectedFile ? <FilePreview file={selectedFile} connection={connection} onBack={() => onSelectFile(null)} /> : <div className="work-scroll">
      {!chat && <div className="work-empty"><FolderOpen size={28} /><p>Files and task progress appear here.</p></div>}
      {work.actions.map(event => <div className="action-running" role="status" key={event.id}><LoaderCircle size={15} className="spin" />{event.payload.action === "test" ? "Testing task preview…" : "Updating task…"}</div>)}
      {work.previews.map(preview => <ScheduledCard key={`${preview.id}:${preview.revision}`} preview={preview} pending={work.actions.some(event => event.payload.task_id === preview.id)} status={states[`${preview.id}:${preview.revision}`] || preview.status} openLink={connection.openLink} onAction={onAction} />)}
      {work.coding.map(event => <CodingCard key={event.payload.id} event={event} botName={connection.session.bot_name} openLink={connection.openLink} onAction={onAction} />)}
      {chat && <section className="work-section"><h3>Files in this chat<span>{files.length}</span></h3>{files.length ? <div className="panel-files">{files.map(file => <FileButton key={file.id} file={file} onSelect={onSelectFile} />)}</div> : <p className="muted small-text">No files yet.</p>}</section>}
      {chat && <section className="work-section workspace-section"><button className="section-toggle" aria-expanded={workspaceOpen} onClick={() => workspaceOpen ? setWorkspaceOpen(false) : void browse(directory)}><FolderOpen size={16} /><span>Server workspace</span><ChevronRight size={15} className={workspaceOpen ? "down" : ""} /></button>{workspaceOpen && <div className="workspace-browser"><div className="workspace-path"><button className="icon-button" disabled={!directory || loading} aria-label="Parent folder" onClick={() => void browse(directory.split("/").slice(0, -1).join("/"))}><ArrowLeft size={15} /></button><span>{directory || "Your files"}</span><button className="icon-button" aria-label="Refresh workspace" disabled={loading} onClick={() => void browse(directory)}><RefreshCw size={14} /></button></div>{loading && <p role="status">Opening files…</p>}{!loading && workspaceFiles?.length === 0 && <p className="muted small-text">This folder is empty.</p>}{!loading && workspaceFiles?.map(file => <button key={file.path} className="workspace-file" onClick={() => {
        if (file.directory) void browse(file.path);
        else { const generation = browseGeneration.current; void connection.api.request<FileRecord>(`/chats/${chat.id}/workspace/snapshot`, "POST", { path: file.path }).then(result => { if (generation === browseGeneration.current) onSelectFile(result); }).catch(error => { if (generation === browseGeneration.current) onError(error); }); }
      }}>{file.directory ? <Folder size={16} /> : <FileText size={16} />}<span>{file.filename}</span><small>{file.directory ? "" : readableSize(file.size)}</small></button>)}</div>}</section>}
    </div>}
  </aside>;
}

function FilePreview({ file, connection, onBack }: { file: FileRecord; connection: Connection; onBack: () => void }) {
  const [preview, setPreview] = useState<Preview | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let live = true; setPreview(null); setError("");
    void connection.api.request<Preview>(`/files/${file.id}/preview`).then(result => { if (live) setPreview(result); }).catch((error: Error) => { if (live) setError(error.message); });
    return () => { live = false; };
  }, [file.id, connection.api]);
  return <div className="preview-shell"><button className="text-button preview-back" onClick={onBack}><ArrowLeft size={14} /> Back</button><div className="preview-filename"><FileText size={20} /><div><strong>{file.filename}</strong><small>{readableSize(file.size)}</small></div></div><a className="download-button" href={`/api/files/${file.id}/content`} download={file.filename}><Download size={15} /> Download original</a><div className="preview-content">
    {error && <p role="alert">{error}</p>}{!preview && !error && <p role="status">Preparing preview…</p>}
    {preview?.kind === "image" && <img className="image-preview" src={`/api/files/${file.id}/content?image=1`} alt={file.filename} />}
    {preview?.kind === "text" && <pre className="text-preview">{preview.text}</pre>}
    {preview?.kind === "markdown" && <Markdown text={preview.text || ""} openLink={connection.openLink} />}
    {preview?.kind === "download" && <p className="muted">{preview.notice || "Download this file to open it in its original application."}</p>}
    {preview?.truncated && <p className="notice">Showing the beginning of this file. Download it for the full content.</p>}
  </div></div>;
}

function ScheduledCard({ preview, status, pending, openLink, onAction }: { pending: boolean; preview: TaskPreview; status: string; openLink: Connection["openLink"]; onAction: Props["onAction"] }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const action = async (action: string) => {
    setBusy(true); setError("");
    try { await onAction(preview.id, action, preview.revision); }
    catch (error) { setError(error instanceof Error ? error.message : "This action is unavailable."); }
    finally { setBusy(false); }
  };
  return <section className="task-card"><div className="card-label"><span>Scheduled task</span><span className={`status-pill ${status === "approved" ? "success" : status === "pending" ? "warning" : ""}`}><i />{status === "pending" ? "Ready for review" : statusLabel(status)}</span></div><div className="task-title"><h3>{preview.name}</h3><span>Revision {preview.revision}</span></div><Markdown text={preview.text} openLink={openLink} /><details><summary>Full task details</summary><Markdown text={preview.details} openLink={openLink} /></details><details><summary>Instructions · SKILL.md</summary><pre>{preview.skill}</pre></details>{preview.python !== null && <details><summary>Python source · task.py</summary><pre>{preview.python}</pre></details>}{error && <p role="alert" className="danger-text">{error}</p>}{status === "pending" && <div className="approval-actions"><button className="approve" disabled={busy || pending} onClick={() => void action("approve")}><Check size={14} />Approve</button><button disabled={busy || pending} onClick={() => void action("test")}><Play size={14} />Test preview</button><button className="reject" disabled={busy || pending} onClick={() => void action("reject")}>Reject</button></div>}</section>;
}

function CodingCard({ event, botName, openLink, onAction }: { event: ChatEvent; botName: string; openLink: Connection["openLink"]; onAction: Props["onAction"] }) {
  const [answer, setAnswer] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const payload = event.payload;
  const active = activeCodingStates.has(payload.status || "");
  const act = async (action: string) => {
    setBusy(true); setError("");
    try { await onAction(payload.id!, action, 0, answer); if (action === "steer") setAnswer(""); }
    catch (error) { setError(error instanceof Error ? error.message : "This action is unavailable."); }
    finally { setBusy(false); }
  };
  return <section className="task-card coding-card"><div className="card-label"><Code2 size={15} /><span>Coding task</span><span className="status-pill accent"><i />{statusLabel(payload.status || "")}</span></div><Markdown text={payload.text || "Working…"} openLink={openLink} />{active && <form className="steering-form" onSubmit={event => { event.preventDefault(); void act("steer"); }}><label>{payload.status === "waiting_for_input" ? "Your answer" : "Add a direction"}<textarea value={answer} onChange={event => setAnswer(event.target.value)} placeholder={payload.status === "waiting_for_input" ? `Tell ${botName} how to continue…` : "Something to keep in mind…"} maxLength={8000} rows={2} /></label><div><button className="text-button" type="button" disabled={busy} onClick={() => void act("cancel")}><Square size={12} />Stop task</button><button type="submit" className="primary" disabled={busy || !answer.trim()}><Send size={13} />Send</button></div></form>}{error && <p role="alert" className="danger-text">{error}</p>}</section>;
}
