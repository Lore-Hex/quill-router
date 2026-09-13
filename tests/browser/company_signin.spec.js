const { test, expect } = require("@playwright/test");

const pages = [
  ["sign-in-as-ycombinator", "Y Combinator"],
  ["sign-in-as-startx", "StartX"],
];

for (const [slug, organization] of pages) {
  test(`${organization}: copy prompt and examples with the real CSP`, async ({ page }) => {
    await page.addInitScript(() => {
      Object.defineProperty(navigator, "clipboard", {
        value: { writeText: async (text) => { window.copiedCompanyText = text; } },
      });
    });
    await page.goto(`/${slug}`);
    await expect(page.getByRole("heading", { level: 1 })).toContainText(organization);
    await page.getByText("View the response shape", { exact: true }).click();
    for (const id of ["company-agent-prompt", "company-match-code", "company-response"]) {
      const text = (await page.locator(`#${id}`).textContent()).trim();
      const button = page.locator(`.company-code [data-copy-prompt-target="${id}"]`);
      await button.click();
      expect(await page.evaluate(() => window.copiedCompanyText)).toBe(text);
      await expect(page.locator(`#${id}-status`)).toHaveText("Copied. Ready for your agent chat.");
      await expect(button.locator("img")).toBeVisible();
    }
  });

  test(`${organization}: example code accepts only verified matching claims`, async ({ page }) => {
    await page.goto(`/${slug}`);
    const code = await page.locator("#company-match-code").textContent();
    const results = await page.evaluate(async ({ code, organization }) => {
      const claim = { funding_organization: organization, match_method: "verified_email_domain", domain: "example.com" };
      const cases = [
        { sub: "usr_test", email_verified: true, company_affiliations: [claim] },
        { sub: "usr_test", email_verified: false, company_affiliations: [claim] },
        { sub: "usr_test", email_verified: true },
        { sub: "usr_test", email_verified: true, company_affiliations: null },
        { sub: "usr_test", email_verified: true, company_affiliations: [{ ...claim, funding_organization: "Other" }] },
        { sub: "usr_test", email_verified: true, company_affiliations: [{ ...claim, match_method: "self_reported" }] },
      ];
      // Execute the displayed snippet, injecting only a fake fetch and token.
      const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
      const run = new AsyncFunction("fetch", "accessToken", code + "\nreturn { userId, companyContext };");
      return Promise.all(cases.map((data) => run(async (url, options) => {
        if (url !== "https://trustedrouter.com/v1/auth/userinfo" || options.headers.Authorization !== "Bearer test-token" || options.cache !== "no-store") throw new Error("Wrong profile request");
        return { ok: true, json: async () => ({ data }) };
      }, "test-token")));
    }, { code, organization });
    expect(results[0].companyContext.funding_organization).toBe(organization);
    for (const result of results.slice(1)) {
      expect(result.userId).toBe("usr_test");
      expect(result.companyContext).toBeNull();
    }
  });

  for (const width of [375, 768, 1440]) {
    test(`${organization}: readable layout at ${width}px`, async ({ page }, testInfo) => {
      await page.setViewportSize({ width, height: 900 });
      await page.goto(`/${slug}`);
      await page.getByText("View the response shape", { exact: true }).click();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
      for (const button of await page.locator(".company-copy").all()) {
        const box = await button.boundingBox();
        expect(box.height).toBeGreaterThanOrEqual(44);
        expect(box.x).toBeGreaterThanOrEqual(0);
        expect(box.x + box.width).toBeLessThanOrEqual(width);
      }
      const boxes = await page.locator(".company-intro, .company-flow, .company-section").evaluateAll((elements) => elements.map((element) => {
        const box = element.getBoundingClientRect();
        return { top: box.top, bottom: box.bottom };
      }));
      for (let index = 1; index < boxes.length; index++) expect(boxes[index].top).toBeGreaterThanOrEqual(boxes[index - 1].bottom - 1);
      await page.getByText("View the response shape", { exact: true }).click();
      await page.evaluate(() => scrollTo(0, 0));
      await page.screenshot({ path: testInfo.outputPath(`${slug}-${width}-viewport.png`) });
      await page.screenshot({ path: testInfo.outputPath(`${slug}-${width}.png`), fullPage: true });
    });
  }
}

for (const mode of ["denied", "unavailable"]) {
  test(`company prompt clipboard fallback: ${mode}`, async ({ page }) => {
    await page.addInitScript((mode) => {
      Object.defineProperty(navigator, "clipboard", { value: mode === "unavailable" ? undefined : {
        writeText: async () => { throw new DOMException("Denied", "NotAllowedError"); },
      } });
    }, mode);
    await page.goto("/sign-in-as-ycombinator");
    await page.getByRole("button", { name: "Copy integration prompt" }).first().click();
    expect(await page.evaluate(() => getSelection().toString())).toBe((await page.locator("#company-agent-prompt").textContent()).trim());
    await expect(page.locator("#company-intro-copy-status")).toContainText("Prompt selected");
  });
}

test("company prompt is readable without JavaScript", async ({ browser, baseURL }) => {
  const context = await browser.newContext({ baseURL, javaScriptEnabled: false });
  const page = await context.newPage();
  await page.goto("/sign-in-as-startx");
  await expect(page.locator("#company-agent-prompt")).toBeVisible();
  await expect(page.locator(".company-copy").first()).toBeHidden();
  await context.close();
});

test("company guide remains readable in light mode on narrow screens", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 320, height: 812 });
  await page.addInitScript(() => localStorage.setItem("tr-theme", "light"));
  await page.goto("/sign-in-as-ycombinator");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await expect(page.locator(".company-copy img").first()).toHaveCSS("filter", "invert(1)");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
  expect((await page.locator(".company-flow").boundingBox()).y).toBeLessThan(780);
  await page.screenshot({ path: testInfo.outputPath("company-signin-light-320.png") });
});

test("cached trust pages can still load the old copy asset", async ({ page }) => {
  await page.goto("/trust");
  await page.evaluate(async () => {
    const button = document.querySelector("#copy-trust-prompt");
    const replacement = button.cloneNode(true);
    replacement.removeAttribute("data-copy-prompt-target");
    replacement.removeAttribute("data-copy-prompt-status");
    replacement.hidden = true;
    button.replaceWith(replacement);
    // Simulate an older cached document loading the compatibility entry point.
    const original = await fetch("/static/trust-prompt.js").then((r) => r.text());
    await new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = "/static/trust-prompt.js";
      script.onload = resolve;
      script.onerror = reject;
      document.body.append(script);
    });
    if (!original.includes('import("./copy-prompt.js")')) throw new Error("Not the compatibility entry point");
  });
  await expect(page.getByRole("button", { name: "Copy prompt" })).toBeVisible();
});
