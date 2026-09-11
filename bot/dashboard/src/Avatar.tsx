// Discord avatars arrive inline from the server, so the page keeps its
// same-origin image policy. The initial stands in when Discord served nothing.
export function Avatar({ src, fallback, className = "" }: { src?: string | null; fallback: string; className?: string }) {
  return <span className={`avatar ${className}`} aria-hidden="true">{src ? <img src={src} alt="" /> : fallback}</span>;
}

export function initialOf(name?: string | null): string {
  return name?.trim()[0]?.toUpperCase() || "";
}
