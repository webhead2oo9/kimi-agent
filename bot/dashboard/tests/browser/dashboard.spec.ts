import { test, expect } from "@playwright/test";

test("saved chat, preview, task review and responsive navigation", async ({ page }, testInfo) => {
  await page.goto("/tests/fixture.html");
  await expect(page.getByRole("textbox", { name: "Message Kimi" })).toBeVisible();
  await expect(page.getByText("I've drafted a weekly digest", { exact: false })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("conversation.png"), fullPage: true });
  await page.getByRole("button", { name: /Review “Friday/ }).click();
  await expect(page.getByRole("button", { name: "Approve", exact: true })).toBeVisible();
  await page.getByText("Full task details", { exact: true }).click();
  await expect(page.getByText(/Reads messages from the workshop/)).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("work.png"), fullPage: true });
  await page.getByRole("button", { name: /community-notes.md/ }).last().click();
  await expect(page.getByRole("heading", { name: "Community notes", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "Download original" })).toHaveAttribute("href", "/api/files/digest/content");
  await page.getByRole("button", { name: "Close work panel" }).click();
  await page.getByRole("textbox", { name: "Message Kimi" }).fill("Please keep the tone informal.");
  await page.getByRole("button", { name: "Send message" }).click();
  await expect(page.getByText(/I've kept your direction/)).toBeVisible();
  await expect(page.getByRole("button", { name: "Stop response" })).toHaveCount(0);
  if (testInfo.project.name === "mobile") await page.getByRole("button", { name: "Open conversations" }).click();
  await page.getByRole("button", { name: "Options for A weekly community digest" }).click();
  await page.getByRole("button", { name: "Rename", exact: true }).click();
  await page.getByRole("dialog").getByRole("textbox").fill("Friday digest");
  await page.getByRole("button", { name: "Save name", exact: true }).click();
  await expect(page.getByRole("button", { name: "Friday digest", exact: true })).toBeVisible();
});
