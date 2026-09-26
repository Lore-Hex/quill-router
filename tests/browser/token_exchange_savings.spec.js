const { test, expect } = require("@playwright/test");

test("enterprise estimate responds to routing, prices and invalid spend", async ({ page }) => {
  await page.goto("/token-exchange/savings");
  await expect(page.locator("#tx-saved")).toHaveText("$1,198,630");
  await expect(page.locator("#tx-price")).toHaveValue("10");
  await page.getByRole("switch").uncheck();
  await expect(page.locator("#tx-total")).toHaveText("$3,350,000");
  await expect(page.locator("#tx-fee")).toHaveText("$0");
  await expect(page.locator('#tx-request-log [data-route="exchange"]')).toHaveCount(0);
  await page.getByRole("switch").check();
  await page.locator("#tx-spend").fill("100");
  await page.locator("#tx-share").fill("100");
  await page.locator("#tx-price").fill("100");
  await expect(page.locator("#tx-total")).toHaveText("$105.50");
  await expect(page.locator("#tx-savings-label")).toHaveText("Estimated monthly increase");
  await page.locator("#tx-spend").fill("-1");
  await expect(page.locator("#tx-spend")).toHaveAttribute("aria-invalid", "true");
  await expect(page.locator("#tx-copy")).toBeDisabled();
  await page.getByRole("button", { name: "Reset example" }).click();
  await expect(page.locator("#tx-saved")).toHaveText("$1,198,630");
  await expect(page.locator("#tx-copy")).toBeEnabled();
});

test("estimate links restore assumptions and can be copied", async ({ page, context }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.goto("/token-exchange/savings#spend=123.45&share=100&discount=80&enabled=0");
  await expect(page.locator("#tx-total")).toHaveText("$123.45");
  await expect(page.locator("#tx-price")).toHaveValue("20");
  await expect(page.getByRole("switch")).not.toBeChecked();
  await page.locator("#tx-copy").click();
  await expect(page.locator("#tx-copy-status")).toContainText("Copied.");
  const url = new URL(await page.evaluate(() => navigator.clipboard.readText()));
  expect(url.pathname).toBe("/token-exchange/savings");
  expect(url.hash).toBe("#spend=123.45&share=100&discount=80&enabled=0");
});

test("request animation moves, pauses, and reroutes without leaking old examples", async ({ page }) => {
  await page.goto("/token-exchange/savings");
  await page.locator("#tx-flow").scrollIntoViewIfNeeded();
  await expect(page.locator("#tx-calculator")).toHaveAttribute("data-animation", "running");
  const pixels = () => page.locator("canvas#tx-flow-canvas").evaluate(canvas => canvas.toDataURL());
  const first = await pixels();
  await expect.poll(pixels).not.toBe(first);
  const nonblank = await page.locator("#tx-flow-canvas").evaluate(canvas => canvas.getContext("2d").getImageData(0, 0, canvas.width, canvas.height).data.some((v, i) => i % 4 === 3 && v > 0));
  expect(nonblank).toBe(true);
  await page.getByRole("button", { name: "Pause animation" }).click();
  await expect(page.locator("#tx-calculator")).toHaveAttribute("data-animation", "paused");
  const frozen = await pixels();
  await page.waitForTimeout(300);
  expect(await pixels()).toBe(frozen);
  await page.getByRole("button", { name: "Resume animation" }).click();
  await expect.poll(pixels).not.toBe(frozen);
  await page.getByRole("switch").uncheck();
  await expect(page.locator('#tx-request-log [data-route="exchange"]')).toHaveCount(0);
  await page.locator("#tx-share").fill("100");
  await page.getByRole("switch").check();
  await expect(page.locator('#tx-request-log [data-route="exchange"]')).toHaveCount(5);
  await page.locator("#tx-flow").scrollIntoViewIfNeeded();
  await page.waitForTimeout(3200);
  await expect(page.locator("#tx-request-log li")).toHaveCount(5);
  await expect(page.locator('#tx-request-log [data-route="current"]')).toHaveCount(0);
  await page.locator("#tx-share").fill("0");
  await expect(page.locator('#tx-request-log [data-route="exchange"]')).toHaveCount(0);
});

test("reduced motion, offscreen and zero spend stop the animation loop", async ({ page }) => {
  await page.goto("/token-exchange/savings");
  await page.locator("#tx-flow").scrollIntoViewIfNeeded();
  await expect(page.locator("#tx-calculator")).toHaveAttribute("data-animation", "running");
  await page.emulateMedia({ reducedMotion: "reduce" });
  await expect(page.getByRole("button", { name: "Reduced motion" })).toBeDisabled();
  await expect(page.locator("#tx-calculator")).toHaveAttribute("data-animation", "paused");
  await page.getByRole("switch").uncheck();
  await expect(page.locator("#tx-saved")).toHaveText("$0");
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await page.locator("#tx-flow").scrollIntoViewIfNeeded();
  await expect(page.locator("#tx-calculator")).toHaveAttribute("data-animation", "running");
  await page.locator("footer").scrollIntoViewIfNeeded();
  await expect(page.locator("#tx-calculator")).toHaveAttribute("data-animation", "paused");
  await page.locator("#tx-spend").fill("0");
  await page.locator("#tx-flow").scrollIntoViewIfNeeded();
  await expect(page.locator("#tx-calculator")).toHaveAttribute("data-animation", "paused");
});

test("mobile routing canvas is nonblank and the example feed updates when the diagram is offscreen", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/token-exchange/savings");
  await page.locator("#tx-flow").scrollIntoViewIfNeeded();
  const pixels = () => page.locator("#tx-flow-canvas").evaluate(canvas => canvas.toDataURL());
  const first = await pixels();
  await expect.poll(pixels).not.toBe(first);
  const canvasBox = await page.locator("#tx-flow-canvas").boundingBox();
  const stageBox = await page.locator("#tx-flow").boundingBox();
  expect(canvasBox).toEqual(stageBox);
  await page.locator("#tx-request-log").scrollIntoViewIfNeeded();
  await expect(page.locator("#tx-calculator")).toHaveAttribute("data-animation", "running");
  await expect.poll(() => page.locator("#tx-request-log .tx-request-arrived").count()).toBeGreaterThan(0);
  await expect(page.locator("#tx-request-log li")).toHaveCount(5);
});

test("mobile calculator has no horizontal overflow and supports keyboard ranges", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 740 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/token-exchange/savings");
  await expect(page.locator("#tx-calculator")).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.locator("#tx-share").focus();
  await page.keyboard.press("ArrowRight");
  await expect(page.locator("#tx-share-label")).toHaveText("41%");
  await page.locator("#tx-spend").fill("0");
  await expect(page.locator("#tx-total")).toHaveText("$0");
  await expect(page.locator("#tx-percent")).toHaveText("0.0% less on tokens");
});
