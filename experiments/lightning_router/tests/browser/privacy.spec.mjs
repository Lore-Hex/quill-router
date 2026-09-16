import {expect, test} from "./fixtures.mjs";
import {parse} from "yaml";

test("E2EE is first and default, including when a URL requests a non-E2EE model", async ({page}) => {
  await page.goto("/?model=anthropic/claude-opus-4.8&privacy=any");
  await page.getByRole("tab", {name: "OpenCode", exact: true}).click();
  const privacy = page.getByLabel("Privacy", {exact: true});
  await expect(privacy).toHaveValue("confidential");
  await expect(page.locator("#model option")).toHaveText(["DeepSeek Flash", "Kimi K2.7"]);
  await expect(page.locator("#model-count")).toHaveText("2 of 3 models");
  expect((await privacy.boundingBox()).y).toBeLessThan((await page.locator("#model").boundingBox()).y);
  await expect(page.locator("#config-code")).toContainText('"min_privacy": "confidential"');
  await privacy.selectOption("any");
  await expect(page.locator("#model option")).toHaveCount(3);
  await page.reload();
  await page.getByRole("tab", {name: "OpenCode", exact: true}).click();
  await expect(privacy).toHaveValue("confidential");
  await expect(page.locator("#model option")).toHaveCount(2);
});

test("switching privacy filters both models and eligible provider orders without touching funding", async ({page}) => {
  await page.goto("/");
  await expect(page.locator("#qr")).toBeVisible();
  const before = await page.evaluate(() => sessionStorage.getItem("lightningrouter-usd-session-v1"));
  const mutations = [];
  page.on("request", req => { if (req.method() !== "GET" && !req.url().endsWith("/refresh")) mutations.push(req.url()); });
  await page.getByRole("tab", {name: "OpenCode", exact: true}).click();
  await page.getByLabel("Provider order").fill('["deepinfra"]');
  await expect(page.locator("#provider-order-error")).toContainText("eligible providers");
  await expect(page.locator("#copy-config")).toBeDisabled();
  await expect(page.locator("#config-code")).toBeEmpty();
  await page.getByLabel("Provider order").fill('["tinfoil", "chutes"]');
  for (const agent of ["OpenCode", "Crush", "OMP"]) {
    await page.getByRole("tab", {name: agent, exact: true}).click();
    const content = await page.locator("#config-code").textContent();
    const config = agent === "OMP" ? parse(content) : JSON.parse(content);
    const provider = agent === "OpenCode" ? config.provider.lightningrouter.models["deepseek/deepseek-flash"].options.provider
      : agent === "Crush" ? config.providers.lightningrouter.extra_body.provider
      : config.providers.lightningrouter.models[0].compat.extraBody.provider;
    expect(provider).toEqual({min_privacy: "confidential", order: ["tinfoil", "chutes"]});
  }
  await page.getByLabel("Privacy", {exact: true}).selectOption("zdr");
  await expect(page.locator("#model option")).toHaveText(["DeepSeek Flash", "Claude Opus 4.8"]);
  await expect(page.getByLabel("Provider order")).toHaveValue("");
  await page.locator("#model").selectOption("anthropic/claude-opus-4.8");
  await expect(page.locator("#eligible-providers")).toHaveText("Eligible providers: anthropic");
  await expect(page.locator("#config-code")).toContainText('"min_privacy":"zdr"');
  await page.getByLabel("Privacy", {exact: true}).selectOption("confidential");
  await expect(page.locator("#model")).toHaveValue("deepseek/deepseek-flash");
  await expect(page.getByLabel("Provider order")).toHaveValue('["tinfoil", "chutes"]');
  expect(await page.evaluate(() => sessionStorage.getItem("lightningrouter-usd-session-v1"))).toEqual(before);
  expect(mutations).toEqual([]);
});

test("missing privacy metadata stays hidden until All models is explicitly selected", async ({page}) => {
  await page.route("**/api/models", route => route.fulfill({json: {data: [{id: "a/unknown", name: "Unknown"}]}}));
  await page.goto("/");
  await page.getByRole("tab", {name: "OpenCode", exact: true}).click();
  await expect(page.locator("#model-empty")).toBeVisible();
  await expect(page.locator("#model")).toBeDisabled();
  for (const agent of ["OpenCode", "Crush", "OMP"]) {
    await page.getByRole("tab", {name: agent, exact: true}).click();
    await expect(page.locator("#config-code")).toBeEmpty();
    await expect(page.locator("#copy-config")).toBeDisabled();
    await expect(page.locator("#copy-command")).toBeDisabled();
  }
  await page.getByLabel("Privacy", {exact: true}).selectOption("any");
  await expect(page.locator("#config-code")).toContainText("a/unknown");
  await expect(page.locator("#copy-config")).toBeEnabled();
  await page.getByLabel("Privacy", {exact: true}).selectOption("confidential");
  await expect(page.locator("#config-code")).toBeEmpty();
  await expect(page.locator("#command-code")).toBeEmpty();
  await expect(page.locator("#copy-config")).toBeDisabled();
});

for (const width of [320, 375, 768, 1440]) {
  test(`privacy-first setup fits ${width}px`, async ({page}) => {
    await page.setViewportSize({width, height: 950});
    await page.goto("/");
    await page.getByRole("tab", {name: "OpenCode", exact: true}).click();
    await expect(page.locator("#config-code")).toContainText("confidential");
    await page.getByLabel("Privacy", {exact: true}).scrollIntoViewIfNeeded();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({path: `test-results/privacy-${width}.png`});
  });
}
