const { expect, test } = require("@playwright/test");

async function fillContact(page) {
  await page.getByLabel("Name", { exact: true }).fill("Ada Lovelace");
  await page.getByLabel("Email", { exact: true }).fill("ada@example.com");
}

for (const viewport of [{ width: 1280, height: 900 }, { width: 390, height: 844 }]) {
  test(`anonymous model request from an empty search at ${viewport.width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize(viewport);
    await page.goto("/models");
    const heroLink = page.locator(".models-page-hero [data-model-request]");
    await expect(heroLink).toBeVisible();
    await expect(heroLink).toHaveAttribute("href", "/support?category=model#support-inquiry");

    const model = 'ExampleLab/NewModel & "Preview"';
    await page.getByRole("searchbox", { name: "Search models" }).fill(model);
    const empty = page.locator("[data-model-empty]");
    await expect(empty).toBeVisible();
    await expect(page.locator("[data-model-show-more]")).toBeHidden();
    await expect(heroLink).toHaveAttribute("href", `/support?${new URLSearchParams({ category: "model", model })}#support-inquiry`);
    await page.screenshot({ path: testInfo.outputPath("empty-search.png"), fullPage: true });
    await empty.getByRole("link", { name: "Request a model" }).click();
    await expect(page.getByRole("heading", { name: "Request a model", exact: true })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Request a model", exact: true })).toBeInViewport();
    await expect(page.getByLabel("Model name or API ID")).toHaveValue(model);
    await expect(page.getByLabel(/Request or generation ID/)).toBeHidden();
    await fillContact(page);
    await page.screenshot({ path: testInfo.outputPath("model-request.png") });
    expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(2);

    let sent;
    await page.route("**/support/inquiry", async (route) => {
      sent = route.request().postDataJSON();
      expect(route.request().method()).toBe("POST");
      await route.fulfill({ json: { ok: true } });
    });
    await page.getByRole("button", { name: "Send model request" }).click();
    await expect(page.getByRole("status").filter({ hasText: "Your model request was sent" })).toBeVisible();
    expect(sent).toEqual({
      name: "Ada Lovelace", email: "ada@example.com", category: "model",
      request_id: "", subject: model, message: `Requested model: ${model}`, website: "",
    });
    await expect(page.getByLabel("Topic")).toHaveValue("model");
    await expect(page.getByLabel("Model name or API ID")).toHaveValue("");
    await expect(page.getByRole("button", { name: "Send model request" })).toBeEnabled();
  });
}

test("model request validates fields, includes optional notes, and retains details on failure", async ({ page }) => {
  await page.goto("/support?category=model&model=ExampleLab%2FNewModel#support-inquiry");
  const sent = [];
  let status = 503;
  await page.route("**/support/inquiry", (route) => {
    sent.push(route.request().postDataJSON());
    return status ? route.fulfill({ status, json: { ok: false } }) : route.abort();
  });
  const submit = page.getByRole("button", { name: "Send model request" });
  await submit.click();
  expect(sent).toHaveLength(0);
  await fillContact(page);
  await page.getByLabel("Email", { exact: true }).fill("not-an-email");
  await submit.click();
  expect(sent).toHaveLength(0);
  await fillContact(page);
  await page.getByLabel("Provider link or notes (optional)").fill("https://example.com/models/new");
  for (const code of [503, 429, 0]) {
    status = code;
    await submit.click();
    await expect(page.locator('[data-role="status"]')).toContainText("help@trustedrouter.com");
    await expect(submit).toBeEnabled();
    await expect(page.getByLabel("Model name or API ID")).toHaveValue("ExampleLab/NewModel");
    await expect(page.getByLabel("Email", { exact: true })).toHaveValue("ada@example.com");
  }
  expect(sent).toHaveLength(3);
  expect(sent[0].message).toBe("Requested model: ExampleLab/NewModel\n\nhttps://example.com/models/new");
});

test("general and feature support keep their existing fields and delivery mode", async ({ page }) => {
  await page.goto("/support?category=feature");
  await expect(page.getByRole("button", { name: "Send feature request" })).toBeVisible();
  await fillContact(page);
  await page.getByLabel("Subject", { exact: true }).fill("A feature");
  await page.getByLabel("How can we help?").fill("Feature details");
  await page.route("**/support/inquiry", (route) => {
    expect(route.request().postDataJSON().category).toBe("feature");
    return route.fulfill({ json: { ok: true } });
  });
  await page.getByRole("button", { name: "Send feature request" }).click();
  await expect(page.locator('[data-role="status"]')).toContainText("Your request was sent");
  await expect(page.getByLabel("Topic")).toHaveValue("feature");
  await page.getByLabel("Topic").selectOption("model");
  await expect(page.getByLabel("Provider link or notes (optional)")).not.toHaveAttribute("required");
  await page.getByLabel("Topic").selectOption("api");
  await expect(page.getByLabel("How can we help?")).toHaveAttribute("required", "");
  await expect(page.getByLabel(/Request or generation ID/)).toBeVisible();
});
