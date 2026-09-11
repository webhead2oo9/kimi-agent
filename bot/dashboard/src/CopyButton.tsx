import { useEffect, useRef, useState } from "react";
import { Check, Copy, X } from "lucide-react";

export function CopyButton({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState(false);
  const [manual, setManual] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const generation = useRef(0);
  useEffect(() => {
    setCopied(false); setManual(false);
    return () => { generation.current++; clearTimeout(timer.current); };
  }, [text]);
  const copy = async () => {
    const current = generation.current;
    try {
      await navigator.clipboard.writeText(text);
      if (current !== generation.current) return;
      clearTimeout(timer.current); setCopied(true);
      timer.current = setTimeout(() => setCopied(false), 2000);
    } catch {
      if (current === generation.current) setManual(true);
    }
  };
  return <>
    <button type="button" className="text-button copy-button" aria-label={label} onClick={() => void copy()}>
      {copied ? <Check size={13} /> : <Copy size={13} />}<span aria-live="polite">{copied ? "Copied" : label}</span>
    </button>
    {manual && <ManualCopy text={text} onClose={() => setManual(false)} />}
  </>;
}

function ManualCopy({ text, onClose }: { text: string; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const field = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    const node = dialog.current;
    node?.showModal(); field.current?.focus(); field.current?.select();
    return () => node?.close();
  }, []);
  return <dialog ref={dialog} className="chat-dialog copy-dialog" aria-label="Copy text" onCancel={onClose}>
    <div className="copy-dialog-heading"><h2>Copy text</h2><button type="button" className="icon-button" aria-label="Close copy dialog" onClick={onClose}><X size={16} /></button></div>
    <p>Clipboard access is unavailable here. Copy the selected text using your device’s copy action.</p>
    <textarea ref={field} aria-label="Text to copy" readOnly value={text} rows={10} onFocus={event => event.currentTarget.select()} />
    <div className="dialog-actions"><button type="button" onClick={() => { field.current?.focus(); field.current?.select(); }}>Select text</button><button type="button" className="primary" onClick={onClose}>Done</button></div>
  </dialog>;
}
