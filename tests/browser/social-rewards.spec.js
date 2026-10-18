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
    if (request.method() === "GET" && options.status) return options.status(route, state);
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
      ${options.keys ? fs.readFileSync(path.join(root, "src/trusted_router/templates/console/_social_rewards_invitation.html"), "utf8") : ""}
      ${options.welcome ? fs.readFileSync(path.join(root, "src/trusted_router/templates/console/_social_rewards_invitation.html"), "utf8") : ""}
      ${options.reveal ? '<div class="console-key-reveal">One-time API key reveal</div>' : ""}</main>
      ${fs.readFileSync(path.join(root, "src/trusted_router/templates/console/_social_rewards.html"), "utf8").replace(/\{\{.*?\}\}/g, "test")}</body></html>`,
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

test("one click opens the destination and grants without attestation", async ({ page }) => {
  const { requests } = await rewardsPage(page);
  const popupPromise = page.waitForEvent("popup");
  await page.getByRole("button", { name: "Star on GitHub: Star the repository" }).click();
  const popup = await popupPromise;
  await expect(popup).toHaveURL("https://example.org/github");
  await popup.close();
  await expect(page.getByText("1 of 5 rewards claimed")).toBeVisible();
  await expect(page.getByRole("button", { name: "✓ Claimed" })).toBeDisabled();
  await expect(page.getByRole("checkbox")).toHaveCount(0);
  await expect(page.locator("#social-rewards progress")).toHaveJSProperty("value", 1);
  expect(requests.filter((r) => r.url.endsWith("/claim")).map((r) => r.body)).toEqual([{ action: "github" }]);
  await page.reload();
  await page.getByRole("button", { name: "Social rewards", exact: true }).click();
  await expect(page.getByRole("button", { name: "✓ Claimed" })).toBeDisabled();
});

test("failed manual dismissal survives reload, retries, and still permits reopening", async ({ page }) => {
  const { state, requests } = await rewardsPage(page, { state: { first_call_complete: false } });
  const opener = page.getByRole("button", { name: "Social rewards", exact: true });
  await opener.click();
  let failDismissal = true;
  await page.route("**/console/social-rewards/dismiss", async (route) => {
    if (failDismissal) return route.abort();
    state.dismissed = true;
    return route.fulfill({ json: state });
  });
  await page.getByRole("button", { name: "Skip for now" }).click();
  await expect(page.getByRole("dialog")).not.toBeVisible();
  expect(state.dismissed).toBe(false);
  state.first_call_complete = true;
  await page.reload();
  await expect(opener).toBeVisible();
  await expect(page.getByRole("dialog")).not.toBeVisible();
  expect(requests.some((r) => r.url.endsWith("/shown"))).toBe(false);
  failDismissal = false;
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await expect.poll(() => state.dismissed).toBe(true);
  await expect(page.getByRole("dialog")).not.toBeVisible();
  await opener.click();
  await expect(page.getByRole("dialog")).toBeVisible();
});

test("manual dismissal suppresses popups when browser storage is blocked", async ({ page }) => {
  await page.addInitScript(() => {
    Storage.prototype.getItem = Storage.prototype.setItem = () => { throw new Error("blocked"); };
  });
  const { state, requests } = await rewardsPage(page, { state: { first_call_complete: false } });
  await page.getByRole("button", { name: "Social rewards", exact: true }).click();
  await page.route("**/console/social-rewards/dismiss", (route) => route.abort());
  await page.keyboard.press("Escape");
  state.first_call_complete = true;
  const response = page.waitForResponse((r) => r.url().endsWith("/console/social-rewards"));
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await response;
  await expect(page.getByRole("dialog")).not.toBeVisible();
  expect(requests.some((r) => r.url.endsWith("/shown"))).toBe(false);
});

test("another accounts local dismissal does not suppress this accounts offer", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("social-rewards-v1:dismissed:another-user", "1"));
  await rewardsPage(page);
  await expect(page.getByRole("dialog")).toBeVisible();
});

test("platform actions have a single appropriate button", async ({ page }) => {
  await rewardsPage(page);
  await expect(page.locator("[data-rewards-list] button")).toHaveCount(5);
  await expect(page.getByRole("checkbox")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Star on GitHub: Star the repository" })).toHaveText("Star");
  await expect(page.getByRole("button", { name: "Subscribe on YouTube: Subscribe to TrustedRouter" })).toHaveText("Subscribe");
  await expect(page.locator("[data-rewards-list]")).not.toContainText("I followed");
});

test("pending claim cannot repeat and a lost committed response reconciles", async ({ page }) => {
  let release;
  await rewardsPage(page, { claim: async (route, state) => {
    await new Promise((resolve) => { release = resolve; });
    state.actions[0].claimed = true;
    await route.abort("failed");
  } });
  const popupPromise = page.waitForEvent("popup");
  await page.getByRole("button", { name: "Star on GitHub: Star the repository" }).first().click();
  await (await popupPromise).close();
  await expect(page.getByRole("button", { name: "Adding reward…" })).toBeDisabled();
  release();
  await expect(page.getByText("1 of 5 rewards claimed")).toBeVisible();
  await expect(page.getByRole("button", { name: "Retry" })).toHaveCount(0);
});

test("transient failure retries automatically with the same action", async ({ page }) => {
  let attempts = 0;
  await rewardsPage(page, { claim: async (route, state) => {
    if (++attempts === 1) return route.fulfill({ status: 503, json: { detail: "Unavailable" } });
    state.actions[0].claimed = true;
    return route.fulfill({ json: state });
  } });
  const popupPromise = page.waitForEvent("popup");
  await page.getByRole("button", { name: "Star on GitHub: Star the repository" }).first().click();
  await (await popupPromise).close();
  await expect(page.getByText("1 of 5 rewards claimed")).toBeVisible();
  expect(attempts).toBe(2);
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
  await expect(page.locator("[data-rewards-list] button")).toHaveCount(0);
});

test("disabled flag hides all invitations and closes an open panel", async ({ page }) => {
  const { state } = await rewardsPage(page, { keys: true, state: { first_call_complete: false } });
  await page.getByRole("button", { name: "Earn extra tokens", exact: true }).click();
  state.enabled = false;
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await expect(page.getByRole("dialog")).not.toBeVisible();
  await expect(page.locator("[data-rewards-key-card]")).not.toBeVisible();
  for (const opener of await page.locator("[data-rewards-open]").all()) await expect(opener).not.toBeVisible();
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
      // The compact heading may sit beside Close, but their boxes must not overlap.
      expect(heading.x + heading.width <= close.x || heading.y >= close.y + close.height).toBe(true);
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

for (const width of [1440, 390, 320]) {
  test(`welcome invitation opens before any request at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    const { requests } = await rewardsPage(page, {
      welcome: true, reveal: true, state: { first_call_complete: false },
    });
    const invitation = page.getByRole("button", { name: "Earn extra tokens", exact: true });
    await expect(invitation).toBeVisible();
    await expect(page.getByRole("dialog")).not.toBeVisible();
    expect(requests.some((r) => r.url.endsWith("/shown"))).toBe(false);
    await invitation.click();
    await expect(page.getByRole("dialog")).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(invitation).toBeFocused();
    await expect(page.getByRole("dialog")).not.toBeVisible();
  });
}

 test("early invitation allows claims before activation without interrupting key setup", async ({ page }) => {
  const { requests } = await rewardsPage(page, { welcome: true, reveal: true, state: { first_call_complete: false } });
  await expect(page.getByRole("button", { name: "Earn extra tokens", exact: true })).toBeVisible();
  await expect(page.getByRole("dialog")).not.toBeVisible();
  await page.getByRole("button", { name: "Earn extra tokens", exact: true }).click();
  const popup = page.waitForEvent("popup");
  await page.getByRole("button", { name: "Star on GitHub: Star the repository" }).click();
  await (await popup).close();
  await expect(page.getByText("1 of 5 rewards claimed")).toBeVisible();
  expect(requests.filter(r => r.url.endsWith("/claim"))).toHaveLength(1);
});

test("analytics distinguish visible invitations, flow opens, and platform clicks", async ({ page }) => {
  const { requests } = await rewardsPage(page, { welcome: true,
    state: { first_call_complete: false, dismissed: true } });
  const observations = () => requests.filter((r) => r.url.endsWith("/observe")).map((r) => r.body);
  await expect.poll(() => observations().some((r) => r.event === "social_rewards_invitation_viewed"
    && r.placement === "api_key_setup")).toBe(true);
  await page.getByRole("button", { name: "Earn extra tokens", exact: true }).click();
  await expect.poll(() => observations().some((r) => r.event === "social_rewards_opened"
    && r.placement === "api_key_setup")).toBe(true);
  await page.locator('[data-reward-id="github"] button').click();
  await expect.poll(() => observations().some((r) => r.event === "social_reward_clicked"
    && r.action === "github" && r.placement === "api_key_setup")).toBe(true);
  expect(observations().filter((r) => r.event === "social_rewards_invitation_viewed"
    && r.placement === "api_key_setup")).toHaveLength(1);
  expect(new Set(observations().map((r) => r.observation_id)).size).toBe(observations().length);
});


test("lost claim response and unavailable reconciliation remain safely retryable", async ({ page }) => {
  let claimed = false;
  let failStatus = false;
  let grants = 0;
  const { requests } = await rewardsPage(page, {
    status: (route, state) => failStatus
      ? route.fulfill({ status: 503, json: {} }) : route.fulfill({ json: state }),
    claim: (route, state) => {
      if (!claimed) {
        claimed = true; grants += 1; state.actions[0].claimed = true; failStatus = true;
        return route.abort("failed");
      }
      return route.abort("failed");
    },
  });
  const popupPromise = page.waitForEvent("popup");
  await page.getByRole("button", { name: "Star on GitHub: Star the repository" }).click();
  await (await popupPromise).close();
  await expect(page.getByRole("button", { name: "Retry" })).toBeEnabled();
  failStatus = false;
  await page.getByRole("button", { name: "Retry" }).click();
  await expect(page.getByText("1 of 5 rewards claimed")).toBeVisible();
  expect(grants).toBe(1);
  expect(requests.filter((r) => r.url.endsWith("/claim")).map((r) => r.body))
    .toEqual([{ action: "github" }, { action: "github" }]);
});

for (const width of [1440, 390, 320]) {
  test(`API Keys invitation opens rewards without overflow at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    const { requests } = await rewardsPage(page, { keys: true, state: { first_call_complete: false } });
    const card = page.locator('[data-rewards-key-card]');
    await expect(card).toBeVisible();
    await expect(card).toContainText('Your next million tokens.');
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await card.getByRole('button').click();
    await expect(page.getByRole('dialog')).toBeVisible();
    await expect.poll(() => requests.some((r) => r.body?.event === 'social_rewards_opened'
      && r.body.placement === 'api_key_setup')).toBe(true);
  });
}

test('API Keys invitation reflects progress, pause, completion, and eligibility', async ({ page }) => {
  const { state } = await rewardsPage(page, { keys: true, state: { first_call_complete: false } });
  const card = page.locator('[data-rewards-key-card]');
  async function refresh() {
    const response = page.waitForResponse((r) => r.url().endsWith('/console/social-rewards'));
    await page.evaluate(() => window.dispatchEvent(new Event('focus')));
    await response;
  }
  state.actions[0].claimed = true;
  await refresh();
  await expect(card).toContainText('4 rewards left to claim');
  state.enabled = false;
  await refresh();
  await expect(card).not.toBeVisible();
  state.enabled = true;
  state.actions.forEach((action) => { action.claimed = true; });
  await refresh();
  await expect(card).toContainText('All five rewards claimed.');
  await expect(card.getByRole('button')).toHaveText('View rewards');
  state.eligible = false;
  await refresh();
  await expect(card).not.toBeVisible();
});


test("hung requests stop within 15 seconds and Retry never opens another tab", async ({ page }) => {
  test.setTimeout(40000);
  let recover = false;
  let started = false;
  let popups = 0;
  page.on("popup", () => { popups += 1; });
  await rewardsPage(page, {
    status: (route, state) => started && !recover ? new Promise(() => {}) : route.fulfill({ json: state }),
    claim: (route, state) => {
      started = true;
      if (!recover) return new Promise(() => {});
      state.actions[0].claimed = true;
      return route.fulfill({ json: state });
    },
  });
  const popup = page.waitForEvent("popup");
  await page.getByRole("button", { name: "Star on GitHub: Star the repository" }).click();
  await (await popup).close();
  const began = Date.now();
  await expect(page.getByRole("button", { name: "Adding reward…" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Retry", exact: true })).toBeEnabled({ timeout: 17000 });
  expect(Date.now() - began).toBeLessThan(17000);
  await expect(page.getByText("We couldn’t confirm your reward yet.")).toBeVisible();
  recover = true;
  await page.getByRole("button", { name: "Retry", exact: true }).click();
  await expect(page.getByRole("button", { name: "✓ Claimed" })).toBeDisabled();
  expect(popups).toBe(1);
});

test("authorization rejection is reconciled but not automatically retried", async ({ page }) => {
  const { requests } = await rewardsPage(page, { claim: (route) => route.fulfill({ status: 403, json: {} }) });
  const popup = page.waitForEvent("popup");
  await page.getByRole("button", { name: "Star on GitHub: Star the repository" }).click();
  await (await popup).close();
  await expect(page.getByRole("button", { name: "Retry", exact: true })).toBeEnabled();
  expect(requests.filter((request) => request.url.endsWith("/claim"))).toHaveLength(1);
});
