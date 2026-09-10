import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowUp, Check, ChevronLeft, Files, Hash, LockKeyhole, Menu, MessageSquare, MoreHorizontal, Paperclip, Plus, Search, Square, Trash2, X } from "lucide-react";
import type { Connection } from "./api";
import { ApiError } from "./api";
import { Markdown } from "./Markdown";
import { WorkPanel } from "./WorkPanel";
import { activeCodingStates, isResponding, latestWork, mergeEvents, type Chat, type ChatEvent, type FileRecord, type Session } from "./types";

type Draft = { text: string; files: FileRecord[] };
const emptyDraft: Draft = { text: "", files: [] };
type Dialog = { kind: "rename" | "delete"; chat: Chat } | null;

export function readableSize(size = 0): string {
  return size >= 1024 * 1024 ? `${(size / 1024 / 1024).toFixed(1)} MB` : size >= 1024 ? `${Math.ceil(size / 1024)} KB` : `${size} B`;
}

export function FileButton({ file, onSelect }: { file: FileRecord; onSelect: (file: FileRecord) => void }) {
  return <button className="file-chip" disabled={!file.id || file.expired} onClick={() => onSelect(file)}>
    <Files size={18} /><span><strong>{file.filename}</strong><small>{file.expired ? "File expired" : readableSize(file.size)}</small></span>
  </button>;
}

export function DashboardApp({ connection }: { connection: Connection }) {
  const { api, openLink, displayName } = connection;
  const [session, setSession] = useState<Session>(connection.session);
  const [chats, setChats] = useState<Chat[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [events, setEvents] = useState<ChatEvent[]>([]);
  const [files, setFiles] = useState<FileRecord[]>([]);
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const [search, setSearch] = useState("");
  const [error, setError] = useState("");
  const [expired, setExpired] = useState(false);
  const [loading, setLoading] = useState(true);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [moreHistory, setMoreHistory] = useState(false);
  const [moreChats, setMoreChats] = useState(false);
  const [chatCursor, setChatCursor] = useState<Chat | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [workOpen, setWorkOpen] = useState(() => window.matchMedia("(min-width: 1100px)").matches);
  const [selectedFile, setSelectedFile] = useState<FileRecord | null>(null);
  const [connected, setConnected] = useState(false);
  const [sending, setSending] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [optimisticBusy, setOptimisticBusy] = useState<string | null>(null);
  const [dialog, setDialog] = useState<Dialog>(null);
  const [menu, setMenu] = useState<string | null>(null);
  const pending = useRef<{ chat: string; body: string; requestId: string } | null>(null);
  const composer = useRef<HTMLTextAreaElement>(null);
  const history = useRef<HTMLDivElement>(null);
  const followEnd = useRef(true);
  const uploadInput = useRef<HTMLInputElement>(null);
  const currentChatId = useRef(activeId);
  currentChatId.current = activeId;
  const active = chats.find(chat => chat.id === activeId);
  const draft = activeId ? drafts[activeId] || emptyDraft : emptyDraft;
  const busy = isResponding(events) || optimisticBusy === activeId && activeId !== null;
  const rememberKey = `kimi-dashboard:last:${session.user_id}:${session.guild_id}`;

  const report = useCallback((error: unknown) => {
    if (error instanceof ApiError && error.status === 401) {
      setExpired(true); setEvents([]); setFiles([]); setSelectedFile(null);
    }
    setError(error instanceof Error ? error.message : "Something went wrong. Please try again.");
  }, []);

  const refreshChats = useCallback(async () => {
    const result = await api.request<{ chats: Chat[] }>("/chats");
    setChats(previous => [...result.chats, ...previous.filter(chat => !result.chats.some(item => item.id === chat.id))]);
    setMoreChats(result.chats.length === 100);
    setChatCursor(result.chats.at(-1) || null);
    return result.chats;
  }, [api]);

  useEffect(() => {
    let live = true;
    void refreshChats().then(async result => {
      if (!live) return;
      let remembered: string | null = null;
      try { remembered = localStorage.getItem(rememberKey); } catch { /* storage may be disabled */ }
      if (remembered && !result.some(chat => chat.id === remembered)) {
        try {
          const saved = await api.request<Chat>(`/chats/${encodeURIComponent(remembered)}`);
          if (!live) return;
          setChats(previous => [...previous.filter(chat => chat.id !== saved.id), saved]);
          setActiveId(saved.id); return;
        } catch (error) {
          if (!(error instanceof ApiError) || ![403, 404].includes(error.status)) throw error;
        }
      }
      if (live) setActiveId(result.find(chat => chat.id === remembered)?.id || result[0]?.id || null);
    }).catch(report).finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [api, refreshChats, rememberKey, report]);

  useEffect(() => {
    let live = true;
    let unsubscribe: (() => void) | undefined;
    setEvents([]); setFiles([]); setSelectedFile(null); setConnected(false);
    followEnd.current = true;
    if (!activeId || expired) return;
    try { localStorage.setItem(rememberKey, activeId); } catch { /* optional preference */ }
    setHistoryLoading(true);
    const refreshFiles = () => void api.request<{ files: FileRecord[] }>(`/chats/${activeId}/files`).then(result => { if (live) setFiles(result.files); }).catch(report);
    void api.request<{ events: ChatEvent[] }>(`/chats/${activeId}/events`).then(result => {
      if (!live) return;
      setEvents(result.events); setMoreHistory(result.events.length === 200);
      setOptimisticBusy(previous => previous === activeId ? null : previous);
      const after = result.events.at(-1)?.id || 0;
      unsubscribe = api.subscribe(activeId, after, incoming => {
        if (!live) return;
        setEvents(previous => mergeEvents(previous, incoming));
        if (incoming.some(event => event.kind === "turn_finished")) {
          setOptimisticBusy(null); void refreshChats().catch(report); refreshFiles();
        }
        if (incoming.some(event => ["coding_task", "task_action_result"].includes(event.kind))) refreshFiles();
      }, (connected, accessExpired) => {
        if (!live) return;
        setConnected(connected);
        if (accessExpired) {
          setExpired(true); setEvents([]); setFiles([]); setSelectedFile(null);
          setError("Your session or channel access expired. Reopen the dashboard to continue.");
        }
      });
      refreshFiles();
    }).catch(report).finally(() => { if (live) setHistoryLoading(false); });
    return () => { live = false; unsubscribe?.(); };
  }, [activeId, api, expired, rememberKey, refreshChats, report]);

  useEffect(() => {
    if (followEnd.current && history.current) history.current.scrollTop = history.current.scrollHeight;
  }, [events, busy]);

  const choose = (id: string) => { setActiveId(id); setSidebarOpen(false); setError(""); setMenu(null); };
  const updateDraft = (chatId: string, update: Partial<Draft>) => setDrafts(current => ({ ...current, [chatId]: { ...(current[chatId] || emptyDraft), ...update } }));
  const newChat = async (text = "") => {
    try {
      const chat = await api.request<Chat>("/chats", "POST", {});
      setChats(previous => [chat, ...previous]); choose(chat.id);
      if (text) updateDraft(chat.id, { text });
      requestAnimationFrame(() => composer.current?.focus());
      return chat;
    } catch (error) { report(error); return null; }
  };
  const selectFile = (file: FileRecord) => { setSelectedFile(file); setWorkOpen(true); };
  const send = async () => {
    if (!activeId || sending || uploading || busy || !draft.text.trim() && !draft.files.length) return;
    const chatId = activeId;
    const payload = { text: draft.text, file_ids: draft.files.map(file => file.id!) };
    const body = JSON.stringify(payload);
    const requestId = pending.current?.chat === chatId && pending.current.body === body ? pending.current.requestId : crypto.randomUUID();
    pending.current = { chat: chatId, body, requestId };
    setSending(true); setOptimisticBusy(chatId); setError(""); followEnd.current = true;
    try {
      await api.request(`/chats/${chatId}/messages`, "POST", { ...payload, request_id: requestId });
      updateDraft(chatId, emptyDraft); pending.current = null;
      void refreshChats().catch(report);
    } catch (error) { setOptimisticBusy(null); report(error); }
    finally { setSending(false); composer.current?.focus(); }
  };
  const attach = async (uploads: FileList | null) => {
    if (!activeId || !uploads?.length) return;
    const chatId = activeId;
    if (uploads.length + draft.files.length > 10 || [...uploads].reduce((sum, file) => sum + file.size, draft.files.reduce((sum, file) => sum + (file.size || 0), 0)) > session.max_upload_bytes) {
      setError(`Attach up to 10 files, totaling ${readableSize(session.max_upload_bytes)}.`); return;
    }
    setUploading(true);
    try {
      for (const file of uploads) {
        const saved = await api.upload<FileRecord>(chatId, file);
        setDrafts(previous => { const old = previous[chatId] || emptyDraft; return { ...previous, [chatId]: { ...old, files: [...old.files, saved] } }; });
      }
    } catch (error) { report(error); }
    finally { setUploading(false); if (uploadInput.current) uploadInput.current.value = ""; }
  };
  const taskAction = async (taskId: string, action: string, revision?: number, message?: string) => {
    if (!activeId) return;
    await api.request(`/chats/${activeId}/task-actions`, "POST", { task_id: taskId, action, revision: revision || 0, message: message || "", request_id: crypto.randomUUID() });
  };
  const older = async () => {
    if (!activeId || !events.length) return;
    try {
      const older = await api.request<{ events: ChatEvent[] }>(`/chats/${activeId}/events?before=${events[0].id}`);
      if (currentChatId.current !== activeId) return;
      followEnd.current = false;
      setEvents(previous => mergeEvents(older.events, previous)); setMoreHistory(older.events.length === 200);
    } catch (error) { report(error); }
  };

  const messages = events.filter(event => event.kind === "user_message" || event.kind === "turn_finished" || event.kind === "task_action_result" || event.kind === "coding_task" && !activeCodingStates.has(event.payload.status || ""));
  const activity = [...events].reverse().find(event => event.kind === "activity")?.payload.label;
  const work = latestWork(events);
  const visibleChats = chats.filter(chat => chat.title.toLowerCase().includes(search.toLowerCase()));

  return <div className={`app-shell ${workOpen ? "with-work" : ""}`}>
    {sidebarOpen && <button className="drawer-scrim" aria-label="Close conversations" onClick={() => setSidebarOpen(false)} />}
    <aside className={`sidebar ${sidebarOpen ? "is-open" : ""}`} aria-label="Saved conversations">
      <div className="brand"><div className="kimi-mark small">k</div><span>{session.bot_name}<small>Your assistant</small></span><button className="icon-button mobile-only" aria-label="Close conversations" onClick={() => setSidebarOpen(false)}><X size={19} /></button></div>
      <button className="new-chat primary" onClick={() => void newChat()} disabled={expired}><Plus size={18} /> New chat</button>
      <label className="chat-search"><Search size={15} /><input aria-label="Search conversations" placeholder="Find a conversation" value={search} onChange={event => setSearch(event.target.value)} /></label>
      <div className="sidebar-label">YOUR CONVERSATIONS</div>
      <nav className="chat-list" aria-label="Conversations">
        {loading && <p className="muted small-text">Loading conversations…</p>}
        {!loading && !visibleChats.length && <p className="muted small-text">{search ? "No matching conversations." : "A fresh start is one message away."}</p>}
        {visibleChats.map(chat => <div className={`chat-row ${chat.id === activeId ? "selected" : ""}`} key={chat.id}>
          <button className="chat-choice" onClick={() => choose(chat.id)} aria-current={chat.id === activeId ? "page" : undefined}><MessageSquare size={16} /><span>{chat.title}</span></button>
          <button className="icon-button chat-menu" aria-label={`Options for ${chat.title}`} aria-expanded={menu === chat.id} onClick={() => setMenu(menu === chat.id ? null : chat.id)}><MoreHorizontal size={16} /></button>
          {menu === chat.id && <div className="context-menu"><button onClick={() => { setDialog({ kind: "rename", chat }); setMenu(null); }}>Rename</button><button className="danger-text" onClick={() => { setDialog({ kind: "delete", chat }); setMenu(null); }}><Trash2 size={14} /> Delete</button></div>}
        </div>)}
        {moreChats && <button className="text-button" onClick={() => void api.request<{ chats: Chat[] }>(`/chats?before=${chatCursor?.updated_at}&before_id=${chatCursor?.id}`).then(result => { setChats(previous => [...previous, ...result.chats.filter(chat => !previous.some(item => item.id === chat.id))]); setMoreChats(result.chats.length === 100); setChatCursor(result.chats.at(-1) || null); }).catch(report)}>Older conversations</button>}
      </nav>
      <footer className="sidebar-footer"><div className="profile-avatar">{displayName[0]?.toUpperCase() || "Y"}</div><div><strong>{displayName}</strong><small><LockKeyhole size={11} /> Private to you in this server</small></div></footer>
    </aside>

    <main className="chat-main">
      <header className="chat-header">
        <button className="icon-button mobile-only" aria-label="Open conversations" onClick={() => setSidebarOpen(true)}><Menu size={20} /></button>
        <div className="chat-heading"><strong>{active?.title || "A little space to think"}</strong><span><Hash size={12} />{active?.channel_name || "Your server"}<i />{activeId ? connected ? "Connected" : "Reconnecting…" : "Private conversations"}</span></div>
        <button className={`work-toggle ${workOpen ? "active" : ""}`} onClick={() => setWorkOpen(!workOpen)} aria-expanded={workOpen}><Files size={17} /><span>Work</span>{(files.length > 0 || work.coding.length > 0) && <b>{files.length + work.coding.length}</b>}</button>
      </header>
      {error && <div className="notice error" role="alert"><span>{error}</span>{expired ? <button onClick={() => location.reload()}>Reconnect</button> : <button className="icon-button" aria-label="Dismiss error" onClick={() => setError("")}><X size={16} /></button>}</div>}
      {session.consent_required && !expired && <div className="consent"><LockKeyhole size={24} /><h2>{session.consent_title}</h2><p>{session.consent_text}</p><button className="primary" onClick={() => void api.request("/consent", "POST", { accept: true }).then(() => setSession({ ...session, consent_required: false })).catch(report)}>Accept and continue</button></div>}
      <div className="message-scroll" ref={history} onScroll={() => { const node = history.current; if (node) followEnd.current = node.scrollHeight - node.scrollTop - node.clientHeight < 100; }}>
        {!activeId && !loading && !expired && <div className="empty-state"><div className="welcome-mark">✳</div><p className="eyebrow">A CONVERSATION, JUST FOR YOU</p><h1>What are we making<br />today?</h1><p>Start with a question, a file, or a half-formed idea.<br />We’ll take it from there.</p><button className="primary" onClick={() => void newChat()}><Plus size={17} /> Start a conversation</button><div className="starter-grid">{["Help me explore an idea", "Make something with code", "Work through a document"].map(text => <button key={text} onClick={() => void newChat(text)}>{text}<ArrowUp size={15} /></button>)}</div></div>}
        {activeId && !messages.length && !historyLoading && !session.consent_required && !expired && <div className="conversation-start"><div className="kimi-mark">k</div><h2>Hey, {displayName.split(" ")[0]}.</h2><p>What’s on your mind?</p></div>}
        {historyLoading && <p className="loading-history" role="status">Opening conversation…</p>}
        {moreHistory && <button className="history-more text-button" onClick={() => void older()}>Load earlier messages</button>}
        <div className="messages">
          {!expired && messages.map(event => {
            const user = event.kind === "user_message";
            const p = event.payload;
            return <article key={event.id} className={`message ${user ? "user-message" : "assistant-message"}`}>
              {!user && <div className="message-avatar kimi-mark">k</div>}
              <div className="message-body"><div className="message-meta"><strong>{user ? "You" : session.bot_name}</strong><time dateTime={new Date(event.created_at * 1000).toISOString()}>{new Date(event.created_at * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}</time>{event.kind === "task_action_result" && <span className="pill">Task review</span>}</div>
                {event.kind === "coding_task" && <span className="result-label">Coding task · {(p.status || "finished").replaceAll("_", " ")}</span>}
                {event.kind === "turn_finished" && p.status !== "completed" && <span className="result-label">{p.status === "cancelled" ? "Response stopped" : "Response interrupted"}</span>}
                {p.text && (user ? <p className="user-text">{p.text}</p> : <Markdown text={p.text} openLink={openLink} />)}
                {p.posts?.map((post, index) => <details key={index} className="sample-post"><summary>Sample for channel {post.channel_id}</summary><Markdown text={post.content} openLink={openLink} /></details>)}
                {!!p.files?.length && <div className="message-files">{p.files.map((file, index) => <FileButton key={file.id || index} file={file} onSelect={selectFile} />)}</div>}
                {p.task_preview && <button className="review-link" onClick={() => { setSelectedFile(null); setWorkOpen(true); }}><Check size={16} /> Review “{p.task_preview.name}”<ChevronLeft size={16} className="reverse" /></button>}
                {p.coding_task_id && <button className="review-link" onClick={() => setWorkOpen(true)}>Follow coding progress <ChevronLeft size={16} className="reverse" /></button>}
              </div>
            </article>;
          })}
          {busy && !expired && <div className="live-activity" role="status"><span className="thinking-dot" /><span>{activity || `${session.bot_name} is thinking…`}</span></div>}
        </div>
      </div>
      {activeId && <div className="composer-region">
        <form className={`composer ${busy ? "responding" : ""}`} onSubmit={event => { event.preventDefault(); void send(); }}>
          {!!draft.files.length && <div className="draft-files">{draft.files.map(file => <span key={file.id}><Paperclip size={13} />{file.filename}<button type="button" aria-label={`Remove ${file.filename}`} onClick={() => updateDraft(activeId, { files: draft.files.filter(item => item.id !== file.id) })}><X size={13} /></button></span>)}</div>}
          <textarea ref={composer} aria-label={`Message ${session.bot_name}`} placeholder={`Message ${session.bot_name}…`} value={draft.text} maxLength={session.max_message_chars} disabled={session.consent_required || expired} rows={2} onChange={event => updateDraft(activeId, { text: event.target.value })} onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing && !window.matchMedia("(pointer: coarse)").matches) { event.preventDefault(); void send(); } }} />
          <div className="composer-tools"><input ref={uploadInput} type="file" multiple className="visually-hidden" tabIndex={-1} onChange={event => void attach(event.target.files)} /><button className="icon-button" type="button" aria-label="Attach files" disabled={uploading || expired || session.consent_required} onClick={() => uploadInput.current?.click()}><Paperclip size={19} /></button><span className="composer-hint">{uploading ? "Uploading…" : `${readableSize(session.max_upload_bytes)} per message`}</span>
            {busy ? <button type="button" className="send-button stop-button" aria-label="Stop response" onClick={() => void api.request(`/chats/${activeId}/stop`, "POST", {}).then(() => setOptimisticBusy(null)).catch(report)}><Square size={16} fill="currentColor" /></button> : <button className="send-button" type="submit" aria-label="Send message" disabled={sending || uploading || expired || session.consent_required || !draft.text.trim() && !draft.files.length}><ArrowUp size={20} /></button>}
          </div>
        </form>
        <p className="retention-note"><LockKeyhole size={10} />{session.retention_days ? `Chats expire after ${session.retention_days} days without activity.` : "Chats are kept until you delete them."} Your files follow the server’s file retention.</p>
      </div>}
    </main>

    {workOpen && !expired && <WorkPanel key={activeId} chat={active} events={events} files={files} selectedFile={selectedFile} onSelectFile={setSelectedFile} onClose={() => setWorkOpen(false)} connection={connection} onAction={taskAction} onError={report} />}
    {dialog && <ChatDialog dialog={dialog} onClose={() => setDialog(null)} onSubmit={async title => {
      if (dialog.kind === "rename") await api.request(`/chats/${dialog.chat.id}`, "PATCH", { title });
      else { await api.request(`/chats/${dialog.chat.id}`, "DELETE"); setChats(previous => previous.filter(chat => chat.id !== dialog.chat.id)); setDrafts(previous => { const copy = { ...previous }; delete copy[dialog.chat.id]; return copy; }); }
      const result = await refreshChats();
      if (dialog.kind === "delete" && activeId === dialog.chat.id) setActiveId(result[0]?.id || null);
      setDialog(null);
    }} />}
  </div>;
}

function ChatDialog({ dialog, onClose, onSubmit }: { dialog: NonNullable<Dialog>; onClose: () => void; onSubmit: (title: string) => Promise<void> }) {
  const node = useRef<HTMLDialogElement>(null);
  const [title, setTitle] = useState(dialog.chat.title);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => { node.current?.showModal(); return () => node.current?.close(); }, []);
  return <dialog ref={node} className="chat-dialog" onCancel={onClose} aria-labelledby="dialog-title"><form onSubmit={event => { event.preventDefault(); setBusy(true); void onSubmit(title).catch((error: Error) => setError(error.message)).finally(() => setBusy(false)); }}><h2 id="dialog-title">{dialog.kind === "rename" ? "Rename conversation" : "Delete this conversation?"}</h2>{dialog.kind === "rename" ? <input aria-label="Conversation name" value={title} onChange={event => setTitle(event.target.value)} maxLength={120} autoFocus required /> : <p>“{dialog.chat.title}” and its chat history will be deleted. Any work running in this chat will stop. Your shared workspace and approved schedules stay available.</p>}{error && <p role="alert">{error}</p>}<div className="dialog-actions"><button type="button" onClick={onClose} disabled={busy}>Cancel</button><button type="submit" className={dialog.kind === "delete" ? "danger" : "primary"} disabled={busy}>{busy ? "Working…" : dialog.kind === "rename" ? "Save name" : "Delete conversation"}</button></div></form></dialog>;
}
