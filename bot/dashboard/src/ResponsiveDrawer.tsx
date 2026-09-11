import { useEffect, useRef, useState, type KeyboardEvent, type ReactNode, type RefObject } from "react";

function containTab(event: KeyboardEvent<HTMLDialogElement>) {
  if (event.key !== "Tab") return;
  const node = event.currentTarget;
  const controls = [...node.querySelectorAll<HTMLElement>("a[href],button,input,select,textarea,summary,[tabindex]")].filter(control => control.tabIndex >= 0 && !control.matches(":disabled") && control.getClientRects().length > 0);
  const first = controls[0], last = controls.at(-1);
  if (!first) { event.preventDefault(); node.focus(); return; }
  if (event.shiftKey && (document.activeElement === first || document.activeElement === node)) {
    event.preventDefault(); last?.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault(); first.focus();
  }
}

export function ResponsiveDrawer({ query, open, onClose, label, children, returnFocusRef }: { query: string; open: boolean; onClose: () => void; label: string; children: ReactNode; returnFocusRef: RefObject<HTMLButtonElement | null> }) {
  const [modal, setModal] = useState(() => window.matchMedia(query).matches);
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const media = window.matchMedia(query);
    const changed = () => setModal(media.matches);
    changed(); media.addEventListener("change", changed);
    return () => media.removeEventListener("change", changed);
  }, [query]);
  useEffect(() => {
    const node = dialog.current;
    if (!modal || !open || !node) return;
    const active = document.activeElement;
    const opener = active instanceof HTMLElement && active !== document.body ? active : returnFocusRef.current;
    node.showModal();
    return () => { node.close(); if (opener?.isConnected) opener.focus(); };
  }, [modal, open, returnFocusRef]);
  if (!modal) return children;
  if (!open) return null;
  return <dialog ref={dialog} tabIndex={-1} className="drawer-dialog" aria-label={label} onKeyDown={containTab} onCancel={event => { event.preventDefault(); onClose(); }} onClick={event => { if (event.target === event.currentTarget) onClose(); }}>{children}</dialog>;
}
