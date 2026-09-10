import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

afterEach(() => { cleanup(); localStorage.clear(); });
Object.defineProperty(window, "matchMedia", { writable: true, value: vi.fn().mockImplementation(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })) });
HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
