const { test, expect } = require("@playwright/test");
const fs = require("node:fs");
const path = require("node:path");
const root = path.resolve(__dirname, "../..");

async function rewardsPage(page, options = {}) {
  const state = {
    eligible: true, enabled: true, first_call_complete: true, dismissed: false,
    workspace_name: "Personal workspace",
    actions: [
      ["github", "Star TrustedRouter on GitHub"],
      ["x_trustedrouter", "Follow TrustedRouter on X"],
      ["x_founder", "Follow founder @jperla on X"],
      ["linkedin", "Follow TrustedRouter on LinkedIn"],
      ["youtube", "Subscribe to TrustedRouter on YouTube"],
    ].map(([id, label]) => ({ id, label, url: `https://example.org/${id}`, claimed: false })),
    ...options.state,
  };
  const requests = [];
  await page.route("**/static/**", (route) => {
    const filename = new URL(route.request().url()).pathname.replace("/static/", "");
    return route.fulfill({ path: path.join(root, "src/trusted_router/static", filename) });
  });
  await page.route("**/console/social-rewards**", async (route) => {
    const request = route.request();
    requests.push({ method: request.method(), url: request.url(), body: request.postDataJSON() });
    if (request.url().endsWith("/shown")) {
      const showNow = !state.dismissed;
      state.dismissed = true;
      if (options.shown) await options.shown();
      return route.fulfill({ json: { ...state, show_now: showNow } });
    }
    if (request.url().endsWith("/dismiss")) state.dismissed = true;
    if (request.url().endsWith("/claim")) {
      if (options.claim) return options.claim(route, state);
      state.actions.find((a) => a.id === request.postDataJSON().action).claimed = true;
    }
    return route.fulfill({ json: state });
  });
  await page.route("**/social-rewards-preview", (route) => route.fulfill({
    contentType: "text/html; charset=utf-8",
    body: `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
      <link rel="stylesheet" href="/static/tokens.css"><link rel="stylesheet" href="/static/dashboard.css"><link rel="stylesheet" href="/static/console.css">
      </head><body class="console"><main style="padding:32px"><h1>Credits</h1><p>Available $0.150000</p><button class="btn" data-rewards-open hidden>Social rewards</button>
      ${options.reveal ? '<div class="console-key-reveal">One-time API key reveal</div>' : ""}</main>
      ${fs.readFileSync(path.join(root, "src/trusted_router/templates/console/_social_rewards.html"), "utf8").replace(/\?v=\{\{.*?\}\}/g, "")}</body></html>`,
  }));
  await page.goto("http://127.0.0.1:18083/social-rewards-preview");
  return { state, requests };
}

test("server evidence gates automatic offer; key reveal is never interrupted", async ({ page }) => {
  const { requests } = await rewardsPage(page, { reveal: true });
  await expect(page.getByRole("button", { name: "Social rewards", exact: true })).toBeVisible();
  await expect(page.getByRole("dialog")).not.toBeVisible();
  expect(requests.some((r) => r.url.endsWith("/shown"))).toBe(false);
});

for (const blocker of ["key reveal", "another dialog"]) {
  test(`automatic offer rechecks ${blocker} after reserving display`, async ({ page }) => {
    let release;
    await rewardsPage(page, { shown: () => new Promise((resolve) => { release = resolve; }) });
    await expect.poll(() => Boolean(release)).toBe(true);
    await page.evaluate((kind) => {
      const node = document.createElement(kind === "key reveal" ? "div" : "dialog");
      node.id = "late-blocker";
      if (kind === "key reveal") node.className = "console-key-reveal";
      document.body.append(node);
      if (kind === "another dialog") node.showModal();
    }, blocker);
    const response = page.waitForResponse((item) => item.url().endsWith("/shown"));
    release();
    await response;
    // Let the fetch continuation and rendering finish before testing absence.
    await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    await expect(page.locator("#social-rewards")).not.toBeVisible();
    await page.locator("#late-blocker").evaluate((node) => node.remove());
    await page.getByRole("button", { name: "Social rewards", exact: true }).click();
    await expect(page.locator("#social-rewards")).toBeVisible();
  });
}

test("dismissal persists, Credits reopens, Escape restores focus", async ({ page }) => {
  const { state } = await rewardsPage(page);
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.getByRole("button", { name: "Skip for now" }).click();
  await expect(page.getByRole("dialog")).not.toBeVisible();
  expect(state.dismissed).toBe(true);
  await page.reload();
  const opener = page.getByRole("button", { name: "Social rewards", exact: true });
  await expect(opener).toBeVisible();
  await expect(page.getByRole("dialog")).not.toBeVisible();
  await opener.click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(opener).toBeFocused();
});

test("outbound click does not claim; explicit attestation commits one action", async ({ page }) => {
  const { requests } = await rewardsPage(page);
  await expect(page.getByRole("dialog")).toBeVisible();
  const popupPromise = page.waitForEvent("popup");
  await page.getByRole("link", { name: /Star TrustedRouter/ }).click();
  const popup = await popupPromise;
  await popup.close();
  expect(requests.filter((r) => r.url.endsWith("/claim"))).toHaveLength(0);
  await page.getByRole("button", { name: "I’ve done this — claim reward" }).first().click();
  await expect(page.getByText("1 of 5 rewards claimed")).toBeVisible();
  await expect(page.getByRole("button", { name: "✓ Claimed" })).toBeDisabled();
  expect(requests.find((r) => r.url.endsWith("/claim")).body).toEqual({ action: "github", attested: true });
});

test("pending claim cannot repeat and a lost committed response reconciles", async ({ page }) => {
  let release;
  await rewardsPage(page, { claim: async (route, state) => {
    await new Promise((resolve) => { release = resolve; });
    state.actions[0].claimed = true;
    await route.abort("failed");
  } });
  await page.getByRole("button", { name: "I’ve done this — claim reward" }).first().click();
  await expect(page.getByRole("button", { name: "Adding reward…" })).toBeDisabled();
  release();
  await expect(page.getByText("1 of 5 rewards claimed")).toBeVisible();
  await expect(page.getByRole("button", { name: "Retry claim" })).toHaveCount(0);
});

test("failed claim is retryable with the same action", async ({ page }) => {
  let attempts = 0;
  await rewardsPage(page, { claim: async (route, state) => {
    if (++attempts === 1) return route.fulfill({ status: 503, json: { detail: "Unavailable" } });
    state.actions[0].claimed = true;
    return route.fulfill({ json: state });
  } });
  await page.getByRole("button", { name: "I’ve done this — claim reward" }).first().click();
  await page.getByRole("button", { name: "Retry claim" }).click();
  await expect(page.getByText("1 of 5 rewards claimed")).toBeVisible();
});

for (const state of [{ eligible: false }, { enabled: false }, { first_call_complete: false }]) {
  test(`ineligible or paused status prevents automatic offer: ${JSON.stringify(state)}`, async ({ page }) => {
    const { requests } = await rewardsPage(page, { state });
    await expect.poll(() => requests.length).toBeGreaterThan(0);
    await expect(page.getByRole("dialog")).not.toBeVisible();
    expect(requests.some((r) => r.url.endsWith("/shown"))).toBe(false);
  });
}

test("ownership loss clears existing claims and disables the offer", async ({ page }) => {
  const { state } = await rewardsPage(page);
  await expect(page.getByRole("dialog")).toBeVisible();
  state.eligible = false;
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await expect(page.getByText("This account is not eligible for this offer.")).toBeVisible();
  await expect(page.getByRole("button", { name: "I’ve done this — claim reward" })).toHaveCount(0);
});

test("Credits shows paused and locked states without enabling claims", async ({ page }) => {
  await rewardsPage(page, { state: { enabled: false } });
  await page.getByRole("button", { name: "Social rewards", exact: true }).click();
  await expect(page.getByText("Rewards are currently paused. Your claimed rewards are safe.")).toBeVisible();
  for (const button of await page.getByRole("button", { name: "I’ve done this — claim reward" }).all()) {
    await expect(button).toBeDisabled();
  }
});

for (const width of [1440, 390, 320]) {
  test(`offer layout and keyboard at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: width === 1440 ? 1050 : 844 });
    await rewardsPage(page);
    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible();
    expect(await dialog.innerText()).not.toMatch(/\$|2M|verif/i);
    expect(await dialog.evaluate((node) => node.scrollWidth <= node.clientWidth)).toBe(true);
    if (width < 600) {
      const heading = await page.locator("#social-rewards-title").boundingBox();
      const close = await page.getByRole("button", { name: "Close social rewards", exact: true }).boundingBox();
      expect(heading.y).toBeGreaterThanOrEqual(close.y + close.height);
    }
    await expect(page.locator("#social-rewards-title")).toBeFocused();
    await page.keyboard.press("Shift+Tab");
    await expect(page.getByRole("button", { name: "Close social rewards", exact: true })).toBeFocused();
    await page.keyboard.press("Shift+Tab");
    await expect(page.getByRole("button", { name: "Skip for now" })).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(page.getByRole("button", { name: "Close social rewards", exact: true })).toBeFocused();
    await dialog.evaluate((node) => { node.scrollTop = 0; });
    await page.screenshot({ path: `/tmp/social-rewards-${width}.png` });
  });
}
