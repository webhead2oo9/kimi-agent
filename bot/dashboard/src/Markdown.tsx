import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";

export function safeUrl(value: string): string | undefined {
  const safe = defaultUrlTransform(value);
  return /^https?:\/\//i.test(safe) ? safe : undefined;
}

export function Markdown({ text, openLink }: { text: string; openLink: (url: string) => Promise<void> }) {
  // HTML stays inert. Remote images are links so rendering a reply cannot
  // contact tracking pixels or expose the member's reading activity.
  return <div className="markdown"><ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml urlTransform={safeUrl} components={{
    a: ({ href, children }) => href ? <a href={href} rel="noreferrer" onClick={event => { event.preventDefault(); void openLink(href).catch(() => {}); }}>{children}</a> : <span>{children}</span>,
    img: ({ src, alt }) => src ? <a href={src} rel="noreferrer" onClick={event => { event.preventDefault(); void openLink(src).catch(() => {}); }}>{alt || "Open image"}</a> : <span>{alt}</span>,
    table: ({ children }) => <div className="table-scroll"><table>{children}</table></div>,
  }}>{text}</ReactMarkdown></div>;
}
