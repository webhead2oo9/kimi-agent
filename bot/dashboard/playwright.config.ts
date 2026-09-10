import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "./tests/browser",
  use: { baseURL: "http://127.0.0.1:5178", browserName: "chromium" },
  webServer: { command: "npm run dev -- --port 5178", url: "http://127.0.0.1:5178/tests/fixture.html", reuseExistingServer: !process.env.CI },
  projects: [
    { name: "desktop", use: { viewport: { width: 1440, height: 1000 } } },
    { name: "mobile", use: { viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true } },
  ],
});
