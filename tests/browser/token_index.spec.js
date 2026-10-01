// /index: the interactive NYTE charts (static/token-index.js) over their fixed-image fallback.
// The checks are numeric: readouts must equal the daily closes in static/token-index/series.json,
// and each drawn line must have one point per day in view, ordered like the data.
const { test, expect } = require("@playwright/test");

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const money = (v) => "$" + (v >= 100 ? Math.round(v).toLocaleString("en-US") : v.toFixed(1));
const longDate = (d) => `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}, ${d.getUTCFullYear()}`;
const dayOf = (start, i) => new Date(Date.parse(`${start}T00:00:00Z`) + i * 86_400_000);

// the (x, y) vertices of a path's first subpath
const vertices = async (path) => {
  const d = await path.getAttribute("d");
  return d.split("M")[1].split("L").map((p) => p.trim().split(/\s+/).map(Number));
};

test.describe("/index interactive charts", () => {
  test("the charts draw the daily closes and the readouts quote them", async ({ page }) => {
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto("/index");
    const data = await (await page.request.get("/static/token-index/series.json")).json();
    const all = data.series.ALL;
    const n = all.length;

    const nyte = page.locator('[data-ti-chart="nyte"]');
    await expect(nyte).toHaveClass(/is-live/);
    await expect(nyte.locator("img.ti-img-dark")).toBeHidden();

    // one vertex per day, and the highest close is drawn highest (smallest y)
    const points = await vertices(nyte.locator("path.ti-line"));
    expect(points.length).toBe(n);
    const ys = points.map(([, y]) => y);
    expect(ys.indexOf(Math.min(...ys))).toBe(all.indexOf(Math.max(...all)));
    expect(ys.indexOf(Math.max(...ys))).toBe(all.indexOf(Math.min(...all)));
    expect(points[0][0]).toBeLessThan(points[n - 1][0]);

    // keyboard readout at both ends equals the data file
    const svg = nyte.locator("svg.ti-svg");
    await svg.focus();
    await page.keyboard.press("Home");
    await expect(nyte.locator(".ti-tip")).toContainText(longDate(dayOf(data.start, 0)));
    await expect(nyte.locator(".ti-tip")).toContainText(money(all[0]));
    await page.keyboard.press("End");
    await expect(nyte.locator(".ti-tip")).toContainText(longDate(dayOf(data.start, n - 1)));
    await expect(nyte.locator(".ti-tip")).toContainText(money(all[n - 1]));

    // pointer readout
    const box = await svg.boundingBox();
    await page.mouse.move(box.x + box.width * 0.5, box.y + box.height * 0.5);
    await expect(nyte.locator(".ti-tip")).toBeVisible();

    // one month in view: 31 points, weekly date ticks, and Home is 30 days before the last day
    await nyte.getByRole("button", { name: "1M", exact: true }).click();
    await expect(nyte.getByRole("button", { name: "1M", exact: true })).toHaveAttribute("aria-pressed", "true");
    expect((await vertices(nyte.locator("path.ti-line"))).length).toBe(31);
    await expect(nyte.locator(".ti-xlabel").first()).toHaveText(/^[A-Z][a-z]{2} \d{1,2}$/);
    await svg.focus();
    await page.keyboard.press("Home");
    await expect(nyte.locator(".ti-tip")).toContainText(longDate(dayOf(data.start, n - 31)));
    await expect(svg).toHaveAttribute("aria-label", new RegExp(`from ${longDate(dayOf(data.start, n - 31))} to`));

    // grades: five lines, series toggles, linear scale, readout of every visible grade
    const grades = page.locator('[data-ti-chart="grades"]');
    await expect(grades.locator("path.ti-line")).toHaveCount(5);
    await grades.getByRole("button", { name: "Frontier" }).click();
    await expect(grades.getByRole("button", { name: "Frontier" })).toHaveAttribute("aria-pressed", "false");
    await expect(grades.locator("path.ti-line")).toHaveCount(4);
    await grades.getByRole("button", { name: "Linear" }).click();
    await expect(grades.locator(".ti-ylabel").first()).toHaveText("$0");
    await expect(grades.locator("svg.ti-svg")).toHaveAttribute("aria-label", /^Linear chart/);
    await grades.locator("svg.ti-svg").focus();
    await page.keyboard.press("End");
    const live = grades.locator("[aria-live]");
    for (const [key, name] of [["A", "Advanced"], ["B", "Professional"], ["C", "Efficient"], ["ALL", "NYTE"]]) {
      await expect(live).toContainText(`${name} ${money(data.series[key][n - 1])}`);
    }
    await expect(live).not.toContainText("Frontier");
    expect(errors).toEqual([]);
  });

  test("the fixed images stay when the daily closes cannot load or are malformed", async ({ page }) => {
    await page.route("**/static/token-index/series.json*", (route) => route.fulfill({ status: 404, body: "" }));
    await page.goto("/index");
    await expect(page.locator('[data-ti-chart="nyte"] img.ti-img-dark')).toBeVisible();
    await expect(page.locator('[data-ti-chart="nyte"]')).not.toHaveClass(/is-live/);
    await expect(page.locator('[data-ti-chart="grades"] svg')).toHaveCount(0);

    await page.unroute("**/static/token-index/series.json*");
    await page.route("**/static/token-index/series.json*", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ as_of: "2026-01-02", start: "2026-01-01", series: { ALL: [1, "oops"], AAA: [1, 2], A: [1, 2], B: [1, 2], C: [1, 2] } }) }));
    await page.goto("/index");
    await expect(page.locator('[data-ti-chart="nyte"] img.ti-img-dark')).toBeVisible();
    await expect(page.locator('[data-ti-chart="nyte"]')).not.toHaveClass(/is-live/);
  });
});
