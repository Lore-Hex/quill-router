import { expect, test } from "./fixtures.mjs";
import { parse } from "yaml";

const order = ["deepinfra", "novita"];

test("ordered provider array updates all configs and leaves funding unchanged", async ({page}) => {
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.goto("/");
  await expect(page.locator("#qr")).toBeVisible();
  const before = await page.evaluate(() => sessionStorage.getItem("lightningrouter-usd-session-v1"));
  const mutations = [];
  page.on("request", req => { if (req.method() !== "GET") mutations.push(req.url()); });
  await page.getByRole("tab", {name: "OpenCode", exact: true}).click();
  await page.getByLabel("Privacy", {exact: true}).selectOption("any");
  await page.getByLabel("Provider order").fill(JSON.stringify(order));
  let config = JSON.parse(await page.locator("#config-code").textContent());
  expect(config.provider.lightningrouter.models["deepseek/deepseek-flash"].options.provider).toEqual({order});
  await page.getByRole("tab", {name: "Crush", exact: true}).click();
  config = JSON.parse(await page.locator("#config-code").textContent());
  expect(config.providers.lightningrouter.extra_body).toEqual({provider: {order}});
  await page.getByRole("tab", {name: "OMP", exact: true}).click();
  config = parse(await page.locator("#config-code").textContent());
  expect(config.providers.lightningrouter.models[0].compat.extraBody).toEqual({provider: {order}});
  await page.getByRole("tab", {name: "Trusted Cowork", exact: true}).click();
  await expect(page.getByLabel("Provider order")).toBeHidden();
  await page.getByRole("tab", {name: "OMP", exact: true}).click();
  await expect(page.getByLabel("Provider order")).toHaveValue(JSON.stringify(order));
  expect(await page.evaluate(() => sessionStorage.getItem("lightningrouter-usd-session-v1"))).toEqual(before);
  expect(mutations.filter(url => !url.endsWith("/refresh"))).toEqual([]);
  expect(errors).toEqual([]);
});

test("invalid arrays block stale copying and recover; choices stay scoped to each model", async ({page}) => {
  await page.goto("/");
  await page.getByRole("tab", {name: "OpenCode", exact: true}).click();
  await page.getByLabel("Privacy", {exact: true}).selectOption("any");
  const input = page.getByLabel("Provider order");
  await input.fill(JSON.stringify(order));
  await page.locator("#model").selectOption("anthropic/claude-opus-4.8");
  await expect(input).toHaveValue("");
  await expect(page.locator("#config-code")).not.toContainText('"order"');
  await input.fill('["anthropic"]');
  await page.locator("#model").selectOption("deepseek/deepseek-flash");
  await expect(input).toHaveValue(JSON.stringify(order));
  for (const bad of ['["deepinfra",', '["deepinfra", "deepinfra"]', '{"order":[]}']) {
    await input.fill(bad);
    await expect(input).toHaveAttribute("aria-invalid", "true");
    await expect(page.locator("#provider-order-error")).not.toBeEmpty();
    await expect(page.locator("#config-code")).toBeEmpty();
    await expect(page.locator("#command-code")).toBeEmpty();
    await expect(page.getByRole("button", {name: "Copy configuration", exact: true})).toBeDisabled();
    await expect(page.getByRole("button", {name: "Copy launch command", exact: true})).toBeDisabled();
  }
  await page.getByRole("tab", {name: "Crush", exact: true}).click();
  await expect(page.locator("#config-code")).toBeEmpty();
  await input.fill("");
  await expect(input).toHaveAttribute("aria-invalid", "false");
  await expect(page.locator("#provider-order-error")).toBeEmpty();
  await expect(page.getByRole("button", {name: "Copy configuration", exact: true})).toBeEnabled();
  await expect(page.locator("#config-code")).not.toContainText("extra_body");
  await input.fill('["novita", "deepinfra"]');
  expect(JSON.parse(await page.locator("#config-code").textContent()).providers.lightningrouter.extra_body.provider.order)
    .toEqual(["novita", "deepinfra"]);
});

for (const width of [320, 375, 768, 1440]) {
  test(`provider setup fits ${width}px`, async ({page}) => {
    await page.setViewportSize({width, height: 950});
    await page.goto("/");
    await page.getByRole("tab", {name: "OMP", exact: true}).click();
    await page.getByLabel("Privacy", {exact: true}).selectOption("any");
    await page.getByLabel("Provider order").fill(JSON.stringify(order));
    await expect(page.locator("#config-code")).toContainText("extraBody");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({path: `test-results/provider-order-${width}.png`, fullPage: true});
  });
}
