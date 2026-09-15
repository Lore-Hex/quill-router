import { test as base, expect } from "@playwright/test";

export { expect };
export const test = base.extend({
  page: async ({ page }, use) => {
    try { await use(page); }
    finally { await page.unrouteAll({ behavior: "wait" }); }
  },
  isolatedRateLimits: [async ({ request }, use) => {
    const response = await request.post("/_test/reset-rate-limits", { data: {} });
    expect(response.ok()).toBe(true);
    await use();
  }, { auto: true }],
});
