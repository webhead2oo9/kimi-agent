import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => ({
  plugins: [react()],
  server: { proxy: { "/api": { target: "http://127.0.0.1:8088", ws: true } } },
  // Browser tests use bundled assets and the same CSP as the production server.
  build: mode === "browser-tests" ? { outDir: ".browser-test-site", rollupOptions: { input: "tests/fixture.html" } } : undefined,
  preview: { headers: { "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors https://discord.com https://*.discord.com https://discordapp.com https://*.discordapp.com" } },
  test: { environment: "jsdom", setupFiles: ["./tests/setup.ts"], include: ["src/**/*.test.ts", "src/**/*.test.tsx"] },
}));
