const { test, expect } = require("@playwright/test");

const pages = [
  ["sign-in-as-ycombinator", "Y Combinator"],
  ["sign-in-as-startx", "StartX"],
  ["sign-in-as-vc", "VC-backed", "Sequoia Capital"],
];

for (const [slug, organization, matchedOrganization = organization] of pages) {
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
    }, { code, organization: matchedOrganization });
    expect(results[0].companyContext.funding_organization).toBe(matchedOrganization);
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
      for (const summary of await page.locator(".company-button-asset summary").all()) await summary.click();
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

  test(`${organization}: buttons render, embed copies, and downloads`, async ({ page }, testInfo) => {
    await page.addInitScript(() => {
      Object.defineProperty(navigator, "clipboard", { value: { writeText: async (text) => { window.copiedCompanyText = text; } } });
    });
    await page.goto(`/${slug}`);
    const tabs = page.getByRole("navigation", { name: "Company sign-in guides" });
    await expect(tabs.getByRole("link", { name: "VCs", exact: true })).toBeVisible();
    for (const theme of ["light", "dark"]) {
      const asset = page.locator(".company-button-asset").filter({ has: page.getByRole("link", { name: `${theme[0].toUpperCase() + theme.slice(1)} SVG` }) });
      const preview = asset.locator(".company-signin-button-image");
      expect(await preview.evaluate((img) => img.complete && img.naturalWidth === 360 && img.naturalHeight === 88)).toBeTruthy();
      await preview.screenshot({ path: testInfo.outputPath(`${slug}-${theme}-button.png`) });
      await asset.locator("summary").click();
      await asset.getByRole("button", { name: "Copy button html" }).click();
      expect(await page.evaluate(() => window.copiedCompanyText)).toBe((await asset.locator("pre").textContent()).trim());
      const downloaded = page.waitForEvent("download");
      await asset.getByRole("link").click();
      const download = await downloaded;
      expect(download.suggestedFilename()).toBe(`${slug.replace("sign-in-as-", "")}-${theme}.svg`);
      expect(await download.failure()).toBeNull();
    }
  });
}

test("VC sample accepts every supported firm and excludes accelerators", async ({ page }) => {
  await page.goto("/sign-in-as-vc");
  const code = await page.locator("#company-match-code").textContent();
  const names = await page.locator(".company-firms a").allTextContents();
  expect(names).toHaveLength(10);
  const results = await page.evaluate(async ({ code, names }) => {
    const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
    const run = new AsyncFunction("fetch", "accessToken", code + "\nreturn companyContext;");
    return Promise.all([...names, "Y Combinator", "StartX", "sequoia capital", "Unknown"].map((name) => run(async () => ({ ok: true, json: async () => ({ data: { sub: "user", email_verified: true, company_affiliations: [{ funding_organization: name, match_method: "verified_email_domain" }] } }) }), "test")));
  }, { code, names });
  expect(results.slice(0, 10).map((claim) => claim.funding_organization)).toEqual(names);
  expect(results.slice(10)).toEqual([null, null, null, null]);
});

test("standalone SVG text is inside its image bounds", async ({ page }) => {
  for (const name of ["ycombinator", "startx", "vc"]) {
    for (const theme of ["light", "dark"]) {
      await page.goto(`/static/sign-in/${name}-${theme}.svg`);
      const boxes = await page.locator("text").evaluateAll((nodes) => nodes.map((node) => {
        const { x, y, width, height } = node.getBBox();
        return { x, y, width, height };
      }));
      expect(boxes).toHaveLength(2);
      for (const box of boxes) {
        expect(box.x).toBeGreaterThanOrEqual(76);
        expect(box.x + box.width).toBeLessThan(352);
        expect(box.y).toBeGreaterThan(0);
        expect(box.y + box.height).toBeLessThan(80);
      }
      expect(boxes[0].y + boxes[0].height).toBeLessThan(boxes[1].y);
    }
  }
});

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
