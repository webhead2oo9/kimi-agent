import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import { ArrowUp, Check, ChevronLeft, CornerUpLeft, Files, GitBranch, Hash, LockKeyhole, Menu, MoreHorizontal, Paperclip, Plus, Search, Square, Trash2, X } from "lucide-react";
import type { Connection } from "./api";
import { ApiError, retryRead } from "./api";
import { Markdown } from "./Markdown";
import { CopyButton } from "./CopyButton";
import { Avatar, initialOf } from "./Avatar";
import { LaunchScreen } from "./LaunchScreen";
import { WorkPanel } from "./WorkPanel";
import { conversationTimeline, isResponding, latestWork, mergeEvents, type Chat, type ChatEvent, type FileRecord, type Session } from "./types";

type Draft = { text: string; files: FileRecord[] };
const emptyDraft: Draft = { text: "", files: [] };
type Dialog = { kind: "rename" | "delete"; chat: Chat } | null;

export function readableSize(size = 0): string {
  return size >= 1024 * 1024 ? `${(size / 1024 / 1024).toFixed(1)} MB` : size >= 1024 ? `${Math.ceil(size / 1024)} KB` : `${size} B`;
}

export function FileButton({ file, onSelect }: { file: FileRecord; onSelect: (file: FileRecord) => void }) {
  const detail = file.expired ? "File expired" : file.unavailable || !file.id ? "File unavailable" : readableSize(file.size);
  return <button className="file-chip" aria-label={`${file.filename} ${detail}`} disabled={!file.id || file.expired || file.unavailable} onClick={() => onSelect(file)}>
    <Files size={18} /><span><strong>{file.filename}</strong><small>{detail}</small></span>
  </button>;
}

export function DashboardApp({ connection }: { connection: Connection }) {
  const { api, openLink, displayName, botAvatar } = connection;
  const [session, setSession] = useState<Session>(connection.session);
  const [chats, setChats] = useState<Chat[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [events, setEvents] = useState<ChatEvent[]>([]);
  const [files, setFiles] = useState<FileRecord[]>([]);
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const [search, setSearch] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [expired, setExpired] = useState(false);
  const [loading, setLoading] = useState(true);
  const [openingRetry, setOpeningRetry] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [loadedChatId, setLoadedChatId] = useState<string | null>(null);
  const [jumpTarget, setJumpTarget] = useState<{ chatId: string; eventId: number } | null>(null);
  const [focusedMessage, setFocusedMessage] = useState<{ chatId: string; eventId: number } | null>(null);
  const [jumpLoading, setJumpLoading] = useState(false);
  const [moreHistory, setMoreHistory] = useState(false);
  const [moreChats, setMoreChats] = useState(false);
  const [chatCursor, setChatCursor] = useState<Chat | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [workOpen, setWorkOpen] = useState(() => window.matchMedia("(min-width: 1100px)").matches);
  const [selectedFile, setSelectedFile] = useState<FileRecord | null>(null);
  // null until the socket reports, so the header only flags a real drop.
  const [connected, setConnected] = useState<boolean | null>(null);
  const [sending, setSending] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [branching, setBranching] = useState(false);
  const branchRequests = useRef(new Map<string, string>());
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
  const initial = initialOf(displayName) || "Y";
  const botInitial = initialOf(session.bot_name);

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
    return retryRead(async () => {
      const { chats } = await api.request<{ chats: Chat[] }>("/chats");
      let remembered: string | null = null;
      try { remembered = localStorage.getItem(rememberKey); } catch { /* storage may be disabled */ }
      let saved: Chat | undefined;
      if (remembered && !chats.some(chat => chat.id === remembered)) {
        try {
          saved = await api.request<Chat>(`/chats/${encodeURIComponent(remembered)}`);
        } catch (error) {
          if (!(error instanceof ApiError) || ![403, 404].includes(error.status)) throw error;
        }
      }
      return { chats, remembered, saved };
    }, ({ chats, remembered, saved }) => {
      setChats(saved ? [...chats, saved] : chats);
      setMoreChats(chats.length === 100); setChatCursor(chats.at(-1) || null);
      setActiveId(saved?.id || chats.find(chat => chat.id === remembered)?.id || chats[0]?.id || null);
      setLoading(false); setOpeningRetry(false);
    }, error => { report(error); setLoading(false); setOpeningRetry(false); }, () => setOpeningRetry(true));
  }, [api, rememberKey, report]);

  useEffect(() => {
    let live = true;
    let unsubscribe: (() => void) | undefined;
    setEvents([]); setFiles([]); setSelectedFile(null); setConnected(null); setMoreHistory(false); setHistoryLoading(false); setLoadedChatId(null);
    followEnd.current = true;
    if (!activeId || expired) return;
    try { localStorage.setItem(rememberKey, activeId); } catch { /* optional preference */ }
    setHistoryLoading(true);
    const refreshFiles = () => void api.request<{ files: FileRecord[] }>(`/chats/${activeId}/files`).then(result => { if (live) setFiles(result.files); }).catch(report);
    const stopLoading = retryRead(() => api.request<{ events: ChatEvent[] }>(`/chats/${activeId}/events`), result => {
      setEvents(result.events); setMoreHistory(result.events.length === 200);
      setLoadedChatId(activeId); setHistoryLoading(false);
      setOptimisticBusy(previous => previous === activeId ? null : previous);
      const after = result.events.at(-1)?.id || 0;
      unsubscribe = api.subscribe(activeId, after, incoming => {
        if (!live) return;
        setEvents(previous => mergeEvents(previous, incoming));
        if (incoming.some(event => event.kind === "turn_finished")) {
          setOptimisticBusy(null); void refreshChats().catch(report); refreshFiles();
        }
        if (incoming.some(event => ["coding_task", "task_action_result", "branch_result"].includes(event.kind))) refreshFiles();
        if (incoming.some(event => ["branch_created", "branch_result"].includes(event.kind))) void refreshChats().catch(report);
      }, (connected, accessExpired) => {
        if (!live) return;
        setConnected(connected);
        if (accessExpired) {
          setExpired(true); setEvents([]); setFiles([]); setSelectedFile(null);
          setError("Your session or channel access expired. Reopen the dashboard to continue.");
        }
      });
      refreshFiles();
    }, error => { report(error); setHistoryLoading(false); }, () => setConnected(false));
    return () => { live = false; stopLoading(); unsubscribe?.(); };
  }, [activeId, api, expired, rememberKey, refreshChats, report]);

  useEffect(() => {
    if (followEnd.current && history.current) history.current.scrollTop = history.current.scrollHeight;
  }, [events, busy]);

  useEffect(() => {
    if (!jumpTarget || loadedChatId !== jumpTarget.chatId || activeId !== jumpTarget.chatId || expired) return;
    let live = true;
    setJumpLoading(true); followEnd.current = false;
    const findMessage = async () => {
      let page = events;
      while (live && !page.some(event => event.id === jumpTarget.eventId)) {
        const before = page[0]?.id;
        if (!before || before <= jumpTarget.eventId) throw new Error("The branch starting message is no longer available.");
        const result = await api.request<{ events: ChatEvent[] }>(`/chats/${jumpTarget.chatId}/events?before=${before}`);
        if (!live) return;
        if (!result.events.length || result.events[0].id >= before) throw new Error("The branch starting message is no longer available.");
        const earlier = result.events;
        page = earlier;
        setEvents(previous => mergeEvents(earlier, previous)); setMoreHistory(earlier.length === 200);
      }
      if (live) setFocusedMessage(jumpTarget);
    };
    void findMessage().catch(error => { if (live) report(error); }).finally(() => { if (live) setJumpLoading(false); });
    return () => { live = false; };
    // The event snapshot belongs to loadedChatId. Live updates and pagination
    // must not restart a navigation already loading its earlier pages.
  }, [jumpTarget, loadedChatId, activeId, expired, api, report]);

  useEffect(() => {
    if (focusedMessage?.chatId !== activeId) return;
    const node = document.getElementById(`message-${focusedMessage.eventId}`);
    node?.scrollIntoView?.({ block: "center" }); node?.focus({ preventScroll: true });
  }, [focusedMessage, activeId]);

  const choose = (id: string) => { setActiveId(id); setSidebarOpen(false); setError(""); setNotice(""); setMenu(null); setJumpTarget(null); setFocusedMessage(null); setJumpLoading(false); };
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
  const openChat = async (id: string, eventId?: number | null) => {
    const from = activeId;
    try {
      const chat = await api.request<Chat>(`/chats/${encodeURIComponent(id)}`);
      setChats(previous => [chat, ...previous.filter(item => item.id !== chat.id)]);
      if (currentChatId.current === from) { choose(chat.id); if (eventId) setJumpTarget({ chatId: chat.id, eventId }); }
    } catch (error) { report(error); }
  };
  const branchFrom = async (eventId: number) => {
    if (!activeId || branching || expired) return;
    const chatId = activeId;
    const key = `${chatId}:${eventId}`;
    const requestId = branchRequests.current.get(key) || crypto.randomUUID();
    branchRequests.current.set(key, requestId);
    setBranching(true); setError("");
    try {
      const branch = await api.request<Chat>(`/chats/${chatId}/branches`, "POST", { event_id: eventId, request_id: requestId });
      branchRequests.current.delete(key);
      setChats(previous => [branch, ...previous.filter(chat => chat.id !== branch.id)]);
      if (currentChatId.current === chatId) { choose(branch.id); requestAnimationFrame(() => composer.current?.focus()); }
    } catch (error) { report(error); }
    finally { setBranching(false); }
  };
  const bringToParent = async (eventId: number) => {
    if (!activeId || !active?.parent_id || branching || expired) return;
    const chatId = activeId;
    setBranching(true); setError("");
    try {
      const parent = await api.request<Chat>(`/chats/${chatId}/return-result`, "POST", { event_id: eventId });
      setChats(previous => [parent, ...previous.filter(chat => chat.id !== parent.id)]);
      if (currentChatId.current === chatId) { choose(parent.id); setNotice("Response brought to parent."); }
    } catch (error) { report(error); }
    finally { setBranching(false); }
  };
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
      setDrafts(previous => {
        const current = previous[chatId];
        if (!current) return previous;
        return { ...previous, [chatId]: {
          text: current.text === payload.text ? "" : current.text,
          files: current.files.filter(file => !payload.file_ids.includes(file.id!)),
        } };
      });
      pending.current = null;
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

  const messages = conversationTimeline(events);
  const returnedMessages = new Set(events.filter(event => event.kind === "branch_returned").map(event => event.payload.event_id));
  const completedTurns = new Set(events.filter(event => event.kind === "turn_finished").map(event => event.payload.turn_id));
  const activity = [...events].reverse().find(event => event.kind === "activity" && !completedTurns.has(event.payload.turn_id))?.payload.label;
  const work = latestWork(events);
  const visibleChats = chats.filter(chat => chat.title.toLowerCase().includes(search.toLowerCase()));

  if (loading) return <LaunchScreen state={openingRetry ? "retrying" : "connecting"} name={session.bot_name} avatar={botAvatar} />;
  return <div className={`app-shell ${workOpen ? "with-work" : ""}`}>
    {sidebarOpen && <button className="drawer-scrim" aria-label="Close conversations" onClick={() => setSidebarOpen(false)} />}
    <aside className={`sidebar ${sidebarOpen ? "is-open" : ""}`} aria-label="Saved conversations">
      <div className="brand"><Avatar src={botAvatar} fallback={botInitial} /><span>{session.bot_name}</span><button className="icon-button mobile-only" aria-label="Close conversations" onClick={() => setSidebarOpen(false)}><X size={18} /></button></div>
      <button className="new-chat" onClick={() => void newChat()} disabled={expired}><Plus size={16} /> New chat</button>
      <label className="chat-search"><Search size={15} /><input aria-label="Search conversations" placeholder="Find a conversation" value={search} onChange={event => setSearch(event.target.value)} /></label>
      <div className="sidebar-label">Recent</div>
      <nav className="chat-list" aria-label="Conversations">
        {!visibleChats.length && <p className="muted small-text">{search ? "No matching conversations." : "A fresh start is one message away."}</p>}
        {visibleChats.map(chat => <div className={`chat-row ${chat.id === activeId ? "selected" : ""}`} key={chat.id}>
          <button className="chat-choice" aria-label={chat.title} onClick={() => choose(chat.id)} aria-current={chat.id === activeId ? "page" : undefined}>{chat.parent_title && <GitBranch size={14} aria-hidden="true" />}<span>{chat.title}</span></button>
          <button className="icon-button chat-menu" aria-label={`Options for ${chat.title}`} aria-expanded={menu === chat.id} onClick={() => setMenu(menu === chat.id ? null : chat.id)}><MoreHorizontal size={16} /></button>
          {menu === chat.id && <div className="context-menu"><button onClick={() => { setDialog({ kind: "rename", chat }); setMenu(null); }}>Rename</button><button className="danger-text" onClick={() => { setDialog({ kind: "delete", chat }); setMenu(null); }}><Trash2 size={14} /> Delete</button></div>}
        </div>)}
        {moreChats && <button className="text-button" onClick={() => void api.request<{ chats: Chat[] }>(`/chats?before=${chatCursor?.updated_at}&before_id=${chatCursor?.id}`).then(result => { setChats(previous => [...previous, ...result.chats.filter(chat => !previous.some(item => item.id === chat.id))]); setMoreChats(result.chats.length === 100); setChatCursor(result.chats.at(-1) || null); }).catch(report)}>Older conversations</button>}
      </nav>
    </aside>

    <main className="chat-main">
      <header className="chat-header">
        <button className="icon-button mobile-only" aria-label="Open conversations" onClick={() => setSidebarOpen(true)}><Menu size={20} /></button>
        <div className="chat-heading"><strong>{active?.title || session.bot_name}</strong>{active && <span><Hash size={12} />{active.channel_name}{connected === false && <><i /><span className="status-pill reconnecting" role="status">Reconnecting…</span></>}</span>}</div>
        <button className={`work-toggle ${workOpen ? "active" : ""}`} aria-label="Work" onClick={() => setWorkOpen(!workOpen)} aria-expanded={workOpen}><Files size={17} /><span>Work</span>{(files.length > 0 || work.coding.length > 0) && <b>{files.length + work.coding.length}</b>}</button>
      </header>
      {active?.parent_title && <div className="branch-banner"><GitBranch size={16} /><div>{active.parent_id ? <button className="text-button" title="Go to the starting message in the parent conversation" onClick={() => void openChat(active.parent_id!, active.parent_event_id)}><CornerUpLeft size={14} />Parent: {active.parent_title}</button> : <span>Parent conversation deleted</span>}<p>Context copied through the selected message. Workspace files are shared.</p></div></div>}
      {notice && !expired && <div className="notice success-notice" role="status"><Check size={15} /><span>{notice}</span><button type="button" className="icon-button" aria-label="Dismiss confirmation" onClick={() => setNotice("")}><X size={16} /></button></div>}
      {error && <div className="notice error" role="alert"><span>{error}</span>{expired ? <button onClick={() => location.reload()}>Reconnect</button> : <button className="icon-button" aria-label="Dismiss error" onClick={() => setError("")}><X size={16} /></button>}</div>}
      {session.consent_required && !expired && <div className="consent"><LockKeyhole size={24} /><h2>{session.consent_title}</h2><p>{session.consent_text}</p><button className="primary" onClick={() => void api.request("/consent", "POST", { accept: true }).then(() => setSession({ ...session, consent_required: false })).catch(report)}>Accept and continue</button></div>}
      <div className="message-scroll" ref={history} onScroll={() => { const node = history.current; if (node) followEnd.current = node.scrollHeight - node.scrollTop - node.clientHeight < 100; }}>
        {!activeId && !expired && <div className="empty-state"><Avatar className="large" src={botAvatar} fallback={botInitial} /><h1>What are we working on?</h1><button className="primary" onClick={() => void newChat()}><Plus size={16} /> New chat</button><div className="starter-grid">{["Help me explore an idea", "Make something with code", "Work through a document"].map(text => <button key={text} onClick={() => void newChat(text)}>{text}<ArrowUp size={15} /></button>)}</div></div>}
        {activeId && !messages.length && !historyLoading && !session.consent_required && !expired && <div className="conversation-start"><Avatar className="large" src={botAvatar} fallback={botInitial} /><h2>What are we working on?</h2></div>}
        {historyLoading && <HistorySkeleton />}
        {jumpLoading && <p className="loading-history" role="status">Finding the branch starting message…</p>}
        {moreHistory && <button className="history-more text-button" disabled={jumpLoading} onClick={() => void older()}>Load earlier messages</button>}
        <div className="messages">
          {!expired && messages.map((event, index) => {
            if (event.kind === "plan") return <PlanCard key={event.id} event={event} />;
            if (event.kind === "branch_created") return <div className="branch-link" key={event.id}><GitBranch size={15} /><span>Conversation branched</span><button className="text-button" onClick={() => void openChat(event.payload.chat_id!)}>Open branch</button></div>;
            const user = event.kind === "user_message" || event.kind === "branch_result" || event.kind === "history_message" && event.payload.role === "user";
            const p = event.payload;
            const branchable = ["user_message", "history_message", "branch_result"].includes(event.kind) || ["turn_finished", "coding_task"].includes(event.kind) && p.status === "completed";
            const canReturn = active?.parent_id && ["turn_finished", "coding_task"].includes(event.kind) && p.status === "completed";
            const returned = p.returned_to_parent || returnedMessages.has(event.id);
            return <Fragment key={event.id}>
              {event.kind === "history_message" && messages[index - 1]?.kind !== "history_message" && <div className="branch-divider">Copied conversation history</div>}
              <article id={`message-${event.id}`} tabIndex={-1} className={`message ${user ? "user-message" : "assistant-message"}${focusedMessage?.chatId === activeId && focusedMessage.eventId === event.id ? " highlighted-message" : ""}`}>
              {user ? <Avatar className="message-avatar" src={session.user_avatar} fallback={initial} /> : <Avatar className="message-avatar" src={botAvatar} fallback={botInitial} />}
              <div className="message-body"><div className="message-meta"><strong>{user ? "You" : session.bot_name}</strong><time dateTime={new Date(event.created_at * 1000).toISOString()}>{new Date(event.created_at * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}</time>{event.kind === "task_action_result" && <span className="pill">Task review</span>}</div>
                {event.kind === "coding_task" && <span className="result-label">Coding task · {(p.status || "finished").replaceAll("_", " ")}</span>}
                {event.kind === "turn_finished" && p.status !== "completed" && <span className={p.status === "failed" ? "result-label danger" : "result-label"}>{p.status === "cancelled" ? "Response stopped" : p.status === "failed" ? "Response failed" : "Response interrupted"}</span>}
                {p.source_chat_id && <div className="returned-result"><CornerUpLeft size={14} /><span>Brought back from </span><button className="text-button" onClick={() => void openChat(p.source_chat_id!, p.source_event_id)}>{p.source_title}</button></div>}
                {p.text && (user && event.kind !== "branch_result" && !p.render_markdown ? <p className="user-text">{p.text}</p> : <Markdown text={p.text} openLink={openLink} />)}
                {p.posts?.map((post, index) => <details key={index} className="sample-post"><summary>Sample for channel {post.channel_id}</summary><Markdown text={post.content} openLink={openLink} /></details>)}
                {!!p.files?.length && <div className="message-files">{p.files.map((file, index) => <FileButton key={file.id || index} file={file} onSelect={selectFile} />)}</div>}
                {p.task_preview && <button className="review-link" onClick={() => { setSelectedFile(null); setWorkOpen(true); }}><Check size={16} /> Review “{p.task_preview.name}”<ChevronLeft size={16} className="reverse" /></button>}
                {p.coding_task_id && <button className="review-link" onClick={() => setWorkOpen(true)}>Follow coding progress <ChevronLeft size={16} className="reverse" /></button>}
                {(branchable || !user && p.text) && <div className="message-actions">{!user && p.text && <CopyButton text={p.text} label="Copy response" />}{branchable && <button className="text-button" disabled={branching || session.consent_required} onClick={() => void branchFrom(event.id)} title="Start a new conversation with context through this message"><GitBranch size={13} />Branch from here</button>}{canReturn && <button className="text-button" disabled={returned || branching || session.consent_required} onClick={() => void bringToParent(event.id)} title={returned ? "This response is already in the parent conversation" : "Add this response to the parent conversation without starting a new reply"}>{returned ? <Check size={13} /> : <CornerUpLeft size={13} />}{returned ? "Brought to parent" : "Bring to parent"}</button>}</div>}
              </div>
            </article>
            {event.kind === "history_message" && messages[index + 1]?.kind !== "history_message" && <div className="branch-divider">New work in this branch</div>}
            </Fragment>;
          })}
          {busy && !expired && <div className="live-activity" role="status"><span className="thinking-dot" /><span>{activity || `${session.bot_name} is thinking…`}</span></div>}
        </div>
      </div>
      {activeId && <div className="composer-region">
        <form className={`composer ${busy ? "responding" : ""}`} onSubmit={event => { event.preventDefault(); void send(); }}>
          {!!draft.files.length && <div className="draft-files">{draft.files.map(file => <span key={file.id}><Paperclip size={13} />{file.filename}<button type="button" aria-label={`Remove ${file.filename}`} onClick={() => updateDraft(activeId, { files: draft.files.filter(item => item.id !== file.id) })}><X size={13} /></button></span>)}</div>}
          <textarea ref={composer} aria-label={`Message ${session.bot_name}`} placeholder={`Message ${session.bot_name}…`} value={draft.text} maxLength={session.max_message_chars} disabled={session.consent_required || expired} rows={2} onChange={event => updateDraft(activeId, { text: event.target.value })} onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing && !window.matchMedia("(pointer: coarse)").matches) { event.preventDefault(); void send(); } }} />
          <div className="composer-tools"><input ref={uploadInput} type="file" multiple className="visually-hidden" tabIndex={-1} onChange={event => void attach(event.target.files)} /><button className="icon-button" type="button" aria-label="Attach files" disabled={uploading || expired || session.consent_required} onClick={() => uploadInput.current?.click()}><Paperclip size={18} /></button>{uploading && <span className="small-text muted" role="status">Uploading…</span>}
            {busy ? <button type="button" className="send-button stop-button" aria-label="Stop response" onClick={() => void api.request(`/chats/${activeId}/stop`, "POST", {}).then(() => setOptimisticBusy(null)).catch(report)}><Square size={16} fill="currentColor" /></button> : <button className="send-button" type="submit" aria-label="Send message" disabled={sending || uploading || expired || session.consent_required || !draft.text.trim() && !draft.files.length}><ArrowUp size={18} /></button>}
          </div>
        </form>
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

// Placeholder rows hold the conversation's shape while its history arrives.
function HistorySkeleton() {
  return <div className="skeleton" role="status" aria-busy="true">
    <span className="visually-hidden">Opening conversation</span>
    {[["58%"], ["92%", "84%", "40%"], ["34%"], ["76%", "88%"]].map((widths, row) => <div className="skeleton-row" key={row}>
      <span className="avatar skeleton-avatar" /><div><i /> {widths.map((width, line) => <i key={line} style={{ width }} />)}</div>
    </div>)}
  </div>;
}

function PlanCard({ event }: { event: ChatEvent }) {
  const steps = event.payload.steps || [];
  if (!steps.length) return null;
  const completed = steps.filter(step => step.status === "completed").length;
  return <section className="conversation-plan" aria-labelledby={`plan-${event.id}`}>
    <div className="plan-heading"><h3 id={`plan-${event.id}`}>Plan</h3><span role="status">{completed} of {steps.length} done</span></div>
    <ol className="plan">{steps.map((step, index) => <li key={index} className={step.status} aria-current={step.status === "in_progress" ? "step" : undefined}>
      <span className="step-marker" aria-hidden="true">{step.status === "completed" ? <Check size={12} /> : index + 1}</span>
      <span><span className="visually-hidden">{step.status.replaceAll("_", " ")}: </span>{step.content}</span>
    </li>)}</ol>
  </section>;
}

function ChatDialog({ dialog, onClose, onSubmit }: { dialog: NonNullable<Dialog>; onClose: () => void; onSubmit: (title: string) => Promise<void> }) {
  const node = useRef<HTMLDialogElement>(null);
  const [title, setTitle] = useState(dialog.chat.title);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => { node.current?.showModal(); return () => node.current?.close(); }, []);
  return <dialog ref={node} className="chat-dialog" onCancel={onClose} aria-labelledby="dialog-title"><form onSubmit={event => { event.preventDefault(); setBusy(true); void onSubmit(title).catch((error: Error) => setError(error.message)).finally(() => setBusy(false)); }}><h2 id="dialog-title">{dialog.kind === "rename" ? "Rename conversation" : "Delete this conversation?"}</h2>{dialog.kind === "rename" ? <input aria-label="Conversation name" value={title} onChange={event => setTitle(event.target.value)} maxLength={120} autoFocus required /> : <p>“{dialog.chat.title}” and its chat history will be deleted. Any work running in this chat will stop. Branches and responses already brought into other conversations remain. Your shared workspace and approved schedules stay available.</p>}{error && <p role="alert">{error}</p>}<div className="dialog-actions"><button type="button" onClick={onClose} disabled={busy}>Cancel</button><button type="submit" className={dialog.kind === "delete" ? "danger" : "primary"} disabled={busy}>{busy ? "Working…" : dialog.kind === "rename" ? "Save name" : "Delete conversation"}</button></div></form></dialog>;
}
