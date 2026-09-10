import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": { target: "http://127.0.0.1:8088", ws: true } } },
  test: { environment: "jsdom", setupFiles: ["./tests/setup.ts"], include: ["src/**/*.test.ts", "src/**/*.test.tsx"] },
});
