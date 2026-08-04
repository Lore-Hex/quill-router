const { expect, test } = require("@playwright/test");

test("Bedrock group buy keeps pledge details private and shares the canonical campaign", async ({ page }) => {
  const authHeaders = { "x-trustedrouter-user": "bedrock-browser@example.com" };
  await page.setExtraHTTPHeaders(authHeaders);
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "share", {
      configurable: true,
      value: async (payload) => { window.__bedrockShare = payload; },
    });
  });
  await page.goto("/bedrock-group-buy");

  await expect(page.getByRole("heading", { name: "Buy Bedrock together. Keep 10%." })).toBeVisible();
  await page.getByLabel("Full name", { exact: true }).fill("Private Browser Buyer");
  await page.getByLabel("Title", { exact: true }).fill("CTO");
  await page.getByLabel("Company", { exact: true }).fill("Private Browser Company");
  await page.getByLabel("Company URL", { exact: true }).fill("https://private-browser.example");
  await page.locator('input[name="monthly_minimum"]').fill("25000.01");
  await page.locator('input[name="expected_bedrock_monthly"]').fill("40000.02");
  await page.locator('input[name="expected_all_llm_monthly"]').fill("75000.03");
  await page.locator('input[name="last_month_llm_spend"]').fill("18392.47");
  await page.getByRole("checkbox", { name: "Amazon Bedrock", exact: true }).check();
  await page.getByRole("checkbox", { name: "Anthropic direct", exact: true }).check();
  await page.getByRole("checkbox", {
    name: "I am authorized to submit this purchasing plan for my company.",
    exact: true,
  }).check();
  await page.getByRole("checkbox", {
    name: "I intend to commit the stated monthly minimum for 12 months if I accept the final group agreement.",
    exact: true,
  }).check();
  await page.getByRole("button", { name: "Join the group buy", exact: true }).click();

  await expect(page).toHaveURL(/\/bedrock-group-buy\?saved=1#share$/);
  await expect(page.getByRole("heading", { name: "You are in. Bring one more buyer." })).toBeVisible();
  const shareButtons = page.locator("[data-bgb-share]");
  await expect(shareButtons).toHaveCount(2);
  await page.getByRole("button", { name: "Share now", exact: true }).click();
  await expect.poll(() => page.evaluate(() => window.__bedrockShare)).toEqual({
    title: "The $1M Bedrock Group Buy",
    text: "Founders are combining Bedrock commitments to negotiate as one buyer and keep 10%.",
    url: "http://127.0.0.1:18081/bedrock-group-buy",
  });

  const own = await page.request.get("/v1/bedrock-group-buy/me", { headers: authHeaders });
  expect(own.ok()).toBeTruthy();
  expect((await own.json()).pledge).toMatchObject({
    last_month_llm_spend_microdollars: 18_392_470_000,
    last_month_spend_sources: ["bedrock", "anthropic_direct"],
  });
  const campaign = await page.request.get("/v1/bedrock-group-buy");
  const publicPayload = await campaign.json();
  expect(publicPayload.last_month_llm_spend_microdollars).toBeUndefined();
  expect(publicPayload.last_month_spend_sources).toBeUndefined();
  expect(JSON.stringify(publicPayload)).not.toContain("Private Browser");

  await page.setViewportSize({ width: 390, height: 844 });
  await page.reload();
  await expect(page.getByRole("group", { name: "Where was it spent?" })).toBeVisible();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(2);

  const removed = await page.request.delete("/v1/bedrock-group-buy/pledge", {
    headers: authHeaders,
  });
  expect(removed.ok()).toBeTruthy();
});
