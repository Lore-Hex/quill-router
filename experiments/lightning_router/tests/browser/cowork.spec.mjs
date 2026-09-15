import { expect, test } from "./fixtures.mjs";

const coworkURL = "https://trustedrouter.com/confidential-cowork";

test("Trusted Cowork is the first default tab with the manual key flow", async ({page}) => {
  await page.goto("/");
  const tabs = page.getByRole("tab");
  await expect(tabs).toHaveText(["Trusted Cowork", "OpenCode", "Crush", "OMP"]);
  await expect(tabs.first()).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("tabpanel")).toHaveAttribute("aria-labelledby", "tab-cowork");
  await expect(page.locator("#cowork-setup")).toBeVisible();
  await expect(page.locator("#cowork-setup")).toContainText("Use an API key");
  await expect(page.locator("#cowork-setup")).toContainText("trustedrouter/confidential");
  await expect(page.getByRole("link", {name: "Download Trusted Cowork"})).toHaveAttribute("href", coworkURL);
  await expect(page.getByRole("link", {name: "Download Trusted Cowork"})).toHaveAttribute("rel", "noopener noreferrer");
  await expect(page.locator("#copy-cowork-key")).toBeDisabled();
  await expect(page.locator("#agent-model-settings")).toBeHidden();
  await expect(page.locator("#cli-setup")).toBeHidden();
  await expect(page.locator("#qr")).toBeVisible();
});

test("all four tabs support keyboard navigation and preserve CLI model selection", async ({page}) => {
  await page.goto("/");
  await expect(page.locator("#qr")).toBeVisible();
  const initial = await page.evaluate(() => JSON.parse(sessionStorage.getItem("lightningrouter-usd-session-v1")));
  const tabs = page.getByRole("tab");
  const initialTabY = (await tabs.first().boundingBox()).y;
  await tabs.first().focus();
  await page.keyboard.press("ArrowLeft");
  await expect(page.locator("#tab-omp")).toBeFocused();
  await page.keyboard.press("ArrowRight");
  await expect(page.locator("#tab-cowork")).toBeFocused();
  await page.keyboard.press("End");
  await expect(page.locator("#tab-omp")).toBeFocused();
  await page.keyboard.press("Home");
  await expect(page.locator("#tab-cowork")).toBeFocused();
  for (const id of ["opencode", "crush", "omp"]) {
    await page.keyboard.press("ArrowRight");
    await expect(page.locator("#tab-" + id)).toBeFocused();
    await expect(page.locator("#tab-" + id)).toHaveAttribute("aria-selected", "true");
    await expect(page.locator("#cowork-setup")).toBeHidden();
    await expect(page.locator("#config-code")).toContainText("https://api.trustedrouter.com/v1");
    // Account for scrolling caused by keyboard focus; compare document positions.
    expect((await tabs.first().boundingBox()).y + await page.evaluate(() => scrollY)).toBe(initialTabY);
  }
  await page.selectOption("#model", "kimi/kimi-k2.7");
  await tabs.first().click();
  await page.locator("#tab-opencode").click();
  await expect(page.locator("#model")).toHaveValue("kimi/kimi-k2.7");
  await expect(page.locator("#config-code")).toContainText("kimi/kimi-k2.7");
  const after = await page.evaluate(() => JSON.parse(sessionStorage.getItem("lightningrouter-usd-session-v1")));
  expect([after.key, after.requestId, after.invoice.id]).toEqual([initial.key, initial.requestId, initial.invoice.id]);
});

test("Cowork copies only a connected key and never sends it in the download link", async ({page, request}) => {
  const existing = await (await request.post("/_test/existing", {data: {}})).json();
  await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.goto("/");
  await expect(page.locator("#qr")).toBeVisible();
  await expect(page.locator("#copy-cowork-key")).toBeDisabled();
  await page.getByLabel("Existing API key").fill(existing.key);
  await page.getByRole("button", {name: "Use API key", exact: true}).click();
  await expect(page.locator("#balance-usd")).toContainText("$20.00");
  await expect(page.locator("#copy-cowork-key")).toBeEnabled();
  await page.locator("#copy-cowork-key").click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(existing.key);
  await expect(page.getByRole("link", {name: "Download Trusted Cowork"})).toHaveAttribute("href", coworkURL);
  const links = await page.locator("a").evaluateAll(nodes => nodes.map(node => node.href));
  expect(links.every(link => !link.includes(existing.key))).toBe(true);
  await page.locator("#tab-opencode").click();
  await expect(page.locator("#env-code")).toContainText(existing.key);
  await page.locator("#tab-cowork").click();
  await page.getByRole("button", {name: "Sign out", exact: true}).click();
  await expect(page.locator("#copy-cowork-key")).toBeDisabled();
  await expect(page.locator("#env-code")).not.toContainText(existing.key);
});

test("newly funded keys are copyable in Cowork without losing checkout recovery", async ({page, request}) => {
  await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.goto("/");
  await expect(page.locator("#qr")).toBeVisible();
  await expect(page.locator("#copy-cowork-key")).toBeDisabled();
  await request.post("/_test/pay", {data: {}});
  await expect(page.locator("#copy-cowork-key")).toBeEnabled({timeout: 12000});
  const key = await page.locator("#existing-key").inputValue();
  await page.locator("#copy-cowork-key").click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(key);
  const saved = await page.evaluate(() => JSON.parse(sessionStorage.getItem("lightningrouter-usd-session-v1")));
  expect(saved.saved).toBe(true);
  expect(saved.key).toBe(key);
  await page.reload();
  await expect(page.locator("#copy-cowork-key")).toBeEnabled();
  await expect(page.locator("#balance-usd")).toContainText("$10.00");
});

test("Cowork setup remains available when the model catalog cannot load", async ({page}) => {
  await page.route("**/api/models", route => route.fulfill({status: 503, json: {error: "unavailable"}}));
  await page.goto("/");
  await expect(page.locator("#model-count")).toContainText("Try again shortly");
  await expect(page.locator("#cowork-setup")).toBeVisible();
  await expect(page.getByRole("link", {name: "Download Trusted Cowork"})).toHaveAttribute("href", coworkURL);
});
