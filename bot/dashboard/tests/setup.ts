import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

const matchMedia = (): MediaQueryList => ({
  matches: false,
  addEventListener: vi.fn(),
  removeEventListener: vi.fn(),
} as unknown as MediaQueryList);
afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.mocked(window.matchMedia).mockImplementation(matchMedia);
});
Object.defineProperty(window, "matchMedia", { writable: true, value: vi.fn().mockImplementation(matchMedia) });
HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
