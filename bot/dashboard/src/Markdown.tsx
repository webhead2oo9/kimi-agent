import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";
import { Children, isValidElement, type ReactNode } from "react";
import { CopyButton } from "./CopyButton";

function plainText(children: ReactNode): string {
  return Children.toArray(children).map(child => isValidElement<{ children?: ReactNode }>(child)
    ? plainText(child.props.children) : typeof child === "string" || typeof child === "number" ? String(child) : "").join("");
}

function CodeBlock({ children }: { children?: ReactNode }) {
  return <div className="code-block"><div className="code-toolbar"><CopyButton label="Copy code" text={plainText(children)} /></div><pre>{children}</pre></div>;
}

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
    pre: CodeBlock,
  }}>{text}</ReactMarkdown></div>;
}
