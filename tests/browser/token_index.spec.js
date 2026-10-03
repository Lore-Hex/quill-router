// /index: the interactive NYTE charts (static/token-index.js) over their fixed-image fallback.
// The checks are numeric: readouts must equal the daily closes in static/token-index/series.json,
// and each drawn line must have one point per day in view, ordered like the data.
const { test, expect } = require("@playwright/test");

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const money = (v) => "$" + (v >= 100 ? Math.round(v).toLocaleString("en-US") : v.toFixed(1));
const longDate = (d) => `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}, ${d.getUTCFullYear()}`;
const dayOf = (start, i) => new Date(Date.parse(`${start}T00:00:00Z`) + i * 86_400_000);

// every (x, y) vertex of a path, across its subpaths
const vertices = async (path) => {
  const d = await path.getAttribute("d");
  return d.split("M").filter(Boolean).flatMap((run) => run.split("L").map((p) => p.trim().split(/\s+/).map(Number)));
};

// Every drawn point must sit where the data puts it, against axes read independently of the paths:
// the gridlines' ends bound the dates (the first day in view at the left end, the last at the right,
// evenly spaced) and their labels fix the value scale (y affine in the value, or in its log).
const expectGeometry = async (figure, data, keys, scale, firstDay) => {
  const f = scale === "log" ? Math.log10 : (v) => v;
  const ticks = await figure.locator(".ti-ylabel").evaluateAll((nodes) =>
    nodes.map((n) => [Number(n.textContent.replace(/[$,]/g, "")), Number(n.getAttribute("y"))]));
  expect(ticks.length).toBeGreaterThan(2);
  const [[v0, y0], [v1, y1]] = [ticks[0], ticks[ticks.length - 1]];
  const slope = (y1 - y0) / (f(v1) - f(v0));
  const [xl, xr] = await figure.locator(".ti-grid line").first().evaluate((line) => [Number(line.getAttribute("x1")), Number(line.getAttribute("x2"))]);
  expect(xr - xl).toBeGreaterThan(100);
  for (const key of keys) {
    const pts = await vertices(figure.locator(`path.ti-line.ti-${key.toLowerCase()}`));
    const values = data.series[key].slice(firstDay);
    expect(pts.length).toBe(values.length);
    pts.forEach(([x, y], i) => {
      expect(Math.abs(x - (xl + (i * (xr - xl)) / (values.length - 1)))).toBeLessThan(0.6);
      expect(Math.abs(y - (y0 + slope * (f(values[i]) - f(v0))))).toBeLessThan(1);
    });
  }
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

    // every point where the data puts it (linear scale), the highest close drawn highest
    await expectGeometry(nyte, data, ["ALL"], "linear", 0);
    const ys = (await vertices(nyte.locator("path.ti-line"))).map(([, y]) => y);
    expect(ys.indexOf(Math.min(...ys))).toBe(all.indexOf(Math.max(...all)));

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
    await expectGeometry(nyte, data, ["ALL"], "linear", n - 31);
    await expect(nyte.locator(".ti-xlabel").first()).toHaveText(/^[A-Z][a-z]{2} \d{1,2}$/);
    await svg.focus();
    await page.keyboard.press("Home");
    await expect(nyte.locator(".ti-tip")).toContainText(longDate(dayOf(data.start, n - 31)));
    await expect(svg).toHaveAttribute("aria-label", new RegExp(`from ${longDate(dayOf(data.start, n - 31))} to`));

    // grades: five lines on one log scale, series toggles, linear scale, readout of every visible grade
    const grades = page.locator('[data-ti-chart="grades"]');
    await expect(grades.locator("path.ti-line")).toHaveCount(5);
    await expectGeometry(grades, data, ["AAA", "A", "B", "C", "ALL"], "log", 0);
    await grades.getByRole("button", { name: "Frontier" }).click();
    await expect(grades.getByRole("button", { name: "Frontier" })).toHaveAttribute("aria-pressed", "false");
    await expect(grades.locator("path.ti-line")).toHaveCount(4);
    await grades.getByRole("button", { name: "Linear" }).click();
    await expect(grades.locator(".ti-ylabel").first()).toHaveText("$0");
    await expectGeometry(grades, data, ["A", "B", "C", "ALL"], "linear", 0);
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
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ as_of: "2026-01-03", start: "2026-01-01", series: { ALL: [1, "oops", 2], AAA: [1, 2, 3], A: [1, 2, 3], B: [1, 2, 3], C: [1, 2, 3] } }) }));
    await page.goto("/index");
    await expect(page.locator('[data-ti-chart="nyte"] img.ti-img-dark')).toBeVisible();
    await expect(page.locator('[data-ti-chart="nyte"]')).not.toHaveClass(/is-live/);
  });
});
