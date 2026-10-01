// /index: the interactive NYTE charts (static/token-index.js) over their fixed-image fallback.
const { test, expect } = require("@playwright/test");

test.describe("/index interactive charts", () => {
  test("both charts draw from the daily closes and answer hover, keyboard, range, scale and series controls", async ({ page }) => {
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto("/index");

    const nyte = page.locator('[data-ti-chart="nyte"]');
    await expect(nyte).toHaveClass(/is-live/);
    await expect(nyte.locator("img.ti-img-dark")).toBeHidden();
    await expect(nyte.locator("path.ti-line")).toHaveCount(1);
    await expect(nyte.locator("path.ti-area")).toHaveCount(1);

    const svg = nyte.locator("svg.ti-svg");
    const box = await svg.boundingBox();
    await page.mouse.move(box.x + box.width * 0.5, box.y + box.height * 0.5);
    const tip = nyte.locator(".ti-tip");
    await expect(tip).toBeVisible();
    await expect(tip).toContainText(/20\d\d/);
    await expect(tip).toContainText(/\$\d/);

    await nyte.getByRole("button", { name: "1M", exact: true }).click();
    await expect(nyte.getByRole("button", { name: "1M", exact: true })).toHaveAttribute("aria-pressed", "true");
    await expect(nyte.locator(".ti-xlabel").first()).toHaveText(/^[A-Z][a-z]{2} \d{1,2}$/);

    const grades = page.locator('[data-ti-chart="grades"]');
    await expect(grades.locator("path.ti-line")).toHaveCount(5);
    await grades.getByRole("button", { name: "Frontier" }).click();
    await expect(grades.getByRole("button", { name: "Frontier" })).toHaveAttribute("aria-pressed", "false");
    await expect(grades.locator("path.ti-line")).toHaveCount(4);
    await grades.getByRole("button", { name: "Linear" }).click();
    await expect(grades.locator(".ti-ylabel").first()).toHaveText("$0");

    await grades.locator("svg.ti-svg").focus();
    await page.keyboard.press("End");
    await expect(grades.locator(".ti-tip")).toBeVisible();
    await expect(grades.locator("[aria-live]")).toContainText(/Advanced \$/);
    expect(errors).toEqual([]);
  });

  test("the fixed images stay when the daily closes cannot load", async ({ page }) => {
    await page.route("**/static/token-index/series.json*", (route) => route.fulfill({ status: 404, body: "" }));
    await page.goto("/index");
    await expect(page.locator('[data-ti-chart="nyte"] img.ti-img-dark')).toBeVisible();
    await expect(page.locator('[data-ti-chart="nyte"]')).not.toHaveClass(/is-live/);
    await expect(page.locator('[data-ti-chart="grades"] svg')).toHaveCount(0);
  });
});
