import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { Markdown } from "./Markdown";
import { CopyButton } from "./CopyButton";

const original = Object.getOwnPropertyDescriptor(navigator, "clipboard");
afterEach(() => {
  if (original) Object.defineProperty(navigator, "clipboard", original);
  else Reflect.deleteProperty(navigator, "clipboard");
});

it("copies only fenced code and preserves its whitespace", async () => {
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
  render(<Markdown text={'Before\n\n```python\ndef hello():\n    print("<script>")\n```\n\nAfter'} openLink={vi.fn()} />);
  fireEvent.click(screen.getByRole("button", { name: "Copy code" }));
  await waitFor(() => expect(writeText).toHaveBeenCalledWith('def hello():\n    print("<script>")\n'));
  expect(await screen.findByText("Copied")).toBeVisible();
  expect(document.querySelector("script")).toBeNull();
});

it("offers selected text instead of claiming success when clipboard permission is denied", async () => {
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: vi.fn().mockRejectedValue(new Error("Denied")) } });
  render(<CopyButton text={"**A full response**\n\nWith formatting."} label="Copy response" />);
  fireEvent.click(screen.getByRole("button", { name: "Copy response" }));
  expect(await screen.findByRole("dialog", { name: "Copy text" })).toBeVisible();
  const field = screen.getByRole("textbox", { name: "Text to copy" }) as HTMLTextAreaElement;
  expect(field.value).toBe("**A full response**\n\nWith formatting.");
  expect(field.selectionStart).toBe(0);
  expect(field.selectionEnd).toBe(field.value.length);
  expect(screen.queryByText("Copied")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Done" }));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});
