import { test, expect } from "@playwright/test";

test("secondary text meets normal-text contrast on each dashboard surface", async ({ page }) => {
  await page.goto("/tests/fixture.html");
  const ratios = await page.evaluate(() => {
    const style = getComputedStyle(document.documentElement);
    const light = (token: string) => {
      const color = style.getPropertyValue(token).trim().slice(1);
      const rgb = [0, 2, 4].map(index => parseInt(color.slice(index, index + 2), 16) / 255).map(value => value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4);
      return rgb[0] * .2126 + rgb[1] * .7152 + rgb[2] * .0722;
    };
    return ["--faint", "--muted"].flatMap(text => ["--canvas", "--sidebar", "--panel", "--surface", "--hover", "--composer"].map(background => ({ text, background, ratio: (light(text) + .05) / (light(background) + .05) })));
  });
  for (const pair of ratios) expect(pair.ratio, `${pair.text} on ${pair.background}`).toBeGreaterThanOrEqual(4.5);
});

test("mobile drawers contain keyboard focus and restore it on Escape", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "Mobile drawer behavior");
  await page.goto("/tests/fixture.html");
  for (const [button, name] of [["Open conversations", "Saved conversations"], ["Work", "Work panel"]]) {
    const trigger = page.getByRole("button", { name: button, exact: true });
    await trigger.click();
    const dialog = page.getByRole("dialog", { name, exact: true });
    await expect(dialog).toBeVisible();
    for (const direction of ["Tab", "Shift+Tab"]) {
      for (let index = 0; index < 18; index++) {
        await page.keyboard.press(direction);
        expect(await dialog.evaluate(node => node.contains(document.activeElement))).toBe(true);
      }
    }
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    await expect(trigger).toBeFocused();
  }
});

test("saved chat, preview, task review and responsive navigation", async ({ page }, testInfo) => {
  await page.goto("/tests/fixture.html");
  await expect(page.getByRole("textbox", { name: "Message Bram" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Work", exact: true })).toBeVisible();
  await expect(page.getByText("I've drafted a weekly digest", { exact: false })).toBeVisible();
  await expect(page.locator(".message-avatar img").first()).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("conversation.png"), fullPage: true });
  await page.getByRole("button", { name: /Review “Friday/ }).click();
  await expect(page.getByRole("button", { name: "Approve", exact: true })).toBeVisible();
  await page.getByText("Full task details", { exact: true }).click();
  await expect(page.getByText(/Reads messages from the workshop/)).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("work.png"), fullPage: true });
  await page.getByRole("button", { name: /community-notes.md/ }).last().click();
  await expect(page.getByRole("heading", { name: "Community notes", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "View original" })).toHaveAttribute("href", "/api/files/digest/content");
  await page.getByRole("button", { name: "Close work panel" }).click();
  const plan = page.getByRole("main").getByRole("region", { name: "Plan" });
  await expect(plan).toHaveCount(1);
  await plan.scrollIntoViewIfNeeded();
  await expect(plan.getByText("Gather community highlights")).toBeVisible();
  await expect(plan.getByRole("status")).toHaveText("2 of 2 done");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.getByRole("textbox", { name: "Message Bram" }).fill("Please keep the tone informal.");
  await page.getByRole("button", { name: "Send message" }).click();
  await expect(page.getByText(/I've kept your direction/)).toBeVisible();
  await expect(page.getByRole("button", { name: "Stop response" })).toHaveCount(0);
  if (testInfo.project.name === "mobile") await page.getByRole("button", { name: "Open conversations" }).click();
  await page.getByRole("button", { name: "Options for A weekly community digest" }).click();
  await page.getByRole("button", { name: "Rename", exact: true }).click();
  await page.getByRole("dialog", { name: "Rename conversation", exact: true }).getByRole("textbox").fill("Friday digest");
  await page.getByRole("button", { name: "Save name", exact: true }).click();
  await expect(page.getByRole("button", { name: "Friday digest", exact: true })).toBeVisible();
});

test("branch a response, navigate its parent, and bring a result back", async ({ page }, testInfo) => {
  await page.goto("/tests/fixture.html");
  await page.getByRole("main").getByRole("button", { name: "Branch from here", exact: true }).last().click();
  const parentLink = page.getByRole("button", { name: "Parent: A weekly community digest", exact: true });
  await expect(parentLink).toBeVisible();
  await expect(page.getByText("Copied conversation history", { exact: true })).toBeVisible();
  await expect(page.getByText("New work in this branch", { exact: true })).toBeVisible();
  await expect(page.getByText("I've drafted a weekly digest", { exact: false })).toBeVisible();
  await expect(page.getByRole("button", { name: "Stop response" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Approve", exact: true })).toHaveCount(0);
  await page.getByRole("textbox", { name: "Message Bram" }).fill("Explore another format.");
  await page.getByRole("button", { name: "Send message" }).click();
  await page.getByRole("button", { name: "Bring to parent", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "Response brought to parent." })).toBeVisible();
  await expect(page.getByText("Brought back from", { exact: false })).toBeVisible();
  await expect(parentLink).toHaveCount(0);
  await page.getByRole("button", { name: "Branch · A weekly community digest", exact: true }).last().click();
  await expect(parentLink).toBeVisible();
  await expect(page.getByText("Explore another format.", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Brought to parent", exact: true })).toBeDisabled();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("branch.png"), fullPage: true });
  await parentLink.click();
  await expect(page.locator("#message-4")).toHaveClass(/highlighted-message/);
  await expect(page.locator("#message-4")).toBeFocused();
  await expect(page.getByText("Brought back from", { exact: false })).toBeVisible();
});

test("copy responses and code, with manual fallback when clipboard is blocked", async ({ page, context }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.goto("/tests/fixture.html");
  await page.getByRole("button", { name: "Copy code", exact: true }).click();
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe("This week: community highlights\n");
  await page.getByRole("button", { name: "Copy response", exact: true }).click();
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toContain("```text\nThis week: community highlights\n```");
  await page.evaluate(() => Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: () => Promise.reject(new Error("Blocked by client")) } }));
  await page.getByRole("button", { name: "Copy code", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Copy text", exact: true });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("textbox", { name: "Text to copy" })).toHaveValue("This week: community highlights\n");
  await dialog.getByRole("button", { name: "Done", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});


test("history skeleton renders varied widths under the production CSP", async ({ page }) => {
  const violations: string[] = [];
  await page.exposeFunction("recordViolation", (directive: string) => violations.push(directive));
  await page.addInitScript(() => document.addEventListener("securitypolicyviolation", event => {
    void (window as unknown as { recordViolation: (directive: string) => Promise<void> }).recordViolation(event.violatedDirective);
  }));
  const response = await page.goto("/tests/fixture.html?loading");
  expect(response?.headers()["content-security-policy"]).toContain("style-src 'self'");
  await expect(page.getByText("Opening conversation", { exact: true })).toBeVisible();
  await expect(page.locator(".skeleton [style]")).toHaveCount(0);
  const widths = await page.locator(".skeleton-row i:not(:first-child)").evaluateAll(nodes => nodes.map(node => node.getBoundingClientRect().width / node.parentElement!.getBoundingClientRect().width));
  expect(widths).toHaveLength(7);
  for (const [index, expected] of [.58, .92, .84, .40, .34, .76, .88].entries()) expect(widths[index]).toBeCloseTo(expected, 2);
  expect(violations).toEqual([]);
});
