const { test, expect } = require("@playwright/test");
const { execFileSync } = require("node:child_process");
const path = require("node:path");

// The page HTML and every JSON answer come from the real routes over the
// in-memory store (see the fixture). The browser only replays them.
const fixture = JSON.parse(execFileSync("uv", ["run", "--no-sync", "python", "tests/browser/activity_tags_fixture.py"], {
  cwd: path.resolve(__dirname, "../.."), encoding: "utf8", maxBuffer: 16 * 1024 * 1024,
  env: { ...process.env, PYTHONPATH: "src", TR_ENVIRONMENT: "test", TR_STORAGE_BACKEND: "memory", TR_SENTRY_DSN: "" },
}));

// The same canonical form the fixture keys its answers by.
function key(body) {
  return JSON.stringify(Object.keys(body).sort().reduce((sorted, name) => {
    sorted[name] = body[name];
    return sorted;
  }, {}));
}

async function open(page, workspace, seen) {
  await page.route("**/console/activity", (route) => route.fulfill({ contentType: "text/html", body: workspace.page }));
  await page.route("**/console/activity/usage.json*", (route) => {
    const range = new URL(route.request().url()).searchParams.get("range");
    const empty = { granularity: "day", buckets: [], range, latest_activity_at: null };
    return route.fulfill({ json: (workspace.usage || {})[range] || empty });
  });
  await page.route("**/console/activity/client-reliability.json*", (route) => route.fulfill({
    json: { data: null, meta: { reason: "unavailable" } },
  }));
  for (const name of ["tags", "requests"]) {
    await page.route(`**/console/activity/${name}.json*`, (route) => {
      const request = route.request();
      const body = request.postDataJSON();
      seen.push({ name, method: request.method(), url: request.url(), body });
      const answer = (workspace[name] || {})[key(body || {})];
      if (!answer) return route.fulfill({ status: 500, json: { error: "the fixture has no answer for this request" } });
      return route.fulfill({ status: answer.status, json: answer.body });
    });
  }
  await page.goto("/console/activity");
  await page.evaluate(() => document.fonts.ready);
}

test("tags show in the request list, break down by value, and filter the list", async ({ page }, testInfo) => {
  const seen = [];
  await page.setViewportSize({ width: 1280, height: 1100 });
  await open(page, fixture.tagged, seen);
  const panel = page.locator("[data-tags-panel]");
  const list = page.locator("[data-activity-rows]");
  const rows = panel.locator("[data-tags-rows] tr");
  const note = page.locator("[data-activity-filter-note]");

  // Every request's tags are chips in the list; a request without tags has none.
  await expect(list.locator("tr")).toHaveCount(6);
  await expect(list.locator("[data-tag-chip]", { hasText: "team: search" })).toHaveCount(3);
  await expect(list.locator("tr").nth(0).locator("[data-tag-chip]")).toHaveText(["environment: production", "team: search"]);
  await expect(list.locator("tr").nth(3).locator("[data-tag-chip]")).toHaveCount(0);

  // The breakdown for the most used key over the whole range, ordered by cost.
  await expect(panel.locator("[data-tags-group]")).toHaveValue("team");
  await expect(rows).toHaveCount(4);
  await expect(rows.nth(0).locator("td")).toHaveText(["team: search", "3", "$3.65", "4,512", "760 ms"]);
  await expect(rows.nth(1).locator("td")).toHaveText(["team: ads", "1", "$0.12", "1,504", "910 ms"]);
  await expect(rows.nth(2).locator("td").nth(4)).toHaveText("n/a");
  await expect(rows.nth(3).locator("td")).toHaveText(["Untagged", "1", "$0.05", "1,506", "700 ms"]);
  await expect(panel.locator("[data-tags-message]")).toHaveText("Every request in the last 30 days is counted.");
  await expect(panel.locator("[data-tags-empty]")).toBeHidden();
  await expect(note).toBeHidden();
  // With no filter there is nothing to clear. This also proves the page got
  // this checkout's stylesheet: unstyled, a hidden .btn and a round chip
  // cannot be told from broken ones.
  await expect(panel.locator("[data-tags-clear]")).toBeHidden();
  expect(await list.locator("[data-tag-chip]").first().evaluate((chip) => getComputedStyle(chip).borderTopLeftRadius)).toBe("999px");

  // Customer text stays text, in the server-rendered list and in the table
  // the script builds.
  await expect(rows.nth(2).locator("[data-tag-chip-value]")).toHaveText(fixture.markup);
  await expect(list.locator("tr").nth(4).locator("[data-tag-chip-value]")).toHaveText(fixture.markup);
  await expect(page.locator("[data-tags-panel] img, [data-activity-rows] img")).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath("activity-tags.png"), fullPage: true });

  // A chip filters the list and the breakdown by its tag.
  await list.locator("[data-tag-chip]", { hasText: "team: search" }).first().click();
  await expect(note).toContainText("Showing requests tagged team: search in the last 30 days.");
  await expect(list.locator("tr")).toHaveCount(3);
  await expect(list.locator("tr").nth(0).locator("td").nth(5)).toHaveText("$1.25");
  await expect(panel.locator("[data-tags-filter-key]")).toHaveValue("team");
  await expect(panel.locator("[data-tags-filter-value]")).toHaveValue("search");
  await expect(panel.locator("[data-tags-message]")).toContainText("Filtered to team: search.");
  await expect(panel.locator("[data-tags-clear]")).toBeVisible();
  await expect(rows).toHaveCount(1);

  // The filter and the breakdown key are independent.
  await panel.locator("[data-tags-group]").selectOption({ label: "environment" });
  await expect(rows).toHaveCount(2);
  await expect(rows.nth(0).locator("td")).toHaveText(["environment: production", "2", "$3.25", "3,010", "760 ms"]);
  await expect(rows.nth(1).locator("td").nth(0)).toHaveText("Untagged");
  await page.screenshot({ path: testInfo.outputPath("activity-tags-filtered.png"), fullPage: true });

  // The panel follows the usage panel's range.
  await page.locator("[data-usage-panel] label", { hasText: "7d" }).click();
  await expect(note).toContainText("in the last 7 days.");
  await expect(list.locator("tr")).toHaveCount(2);
  await expect(rows.nth(0).locator("td")).toHaveText(["environment: production", "1", "$1.25", "1,500", "820 ms"]);

  // The value picker lists the key's values; "Any value" keeps the key only.
  await panel.locator("[data-tags-filter-value]").selectOption({ label: "Any value" });
  await expect(note).toContainText("Showing requests tagged team in the last 7 days.");
  await expect(list.locator("tr")).toHaveCount(4);

  // Clearing the filter puts the server-rendered list back.
  await note.getByRole("button", { name: "Clear filter" }).click();
  await expect(note).toBeHidden();
  await expect(list.locator("tr")).toHaveCount(6);
  await expect(rows).toHaveCount(3);
  await expect(panel.locator("[data-tags-filter-key]")).toHaveValue("No filter");
  await expect(panel.locator("[data-tags-filter-value]")).toBeDisabled();
  await expect(panel.locator("[data-tags-clear]")).toBeHidden();

  // A value outside what a tag may contain is refused by the server; the
  // page says so and goes back to the unfiltered view. Nothing ran.
  await list.locator("[data-tag-chip]").filter({ hasText: "<img" }).click();
  await expect(panel.locator("[data-tags-message]")).toHaveText("That tag cannot be used as a filter here.");
  await expect(note).toBeHidden();
  await expect(list.locator("tr")).toHaveCount(6);
  expect(await page.evaluate(() => window.__tagMarkupRan)).toBeUndefined();
  await expect(page.locator("[data-tags-panel] img, [data-activity-rows] img")).toHaveCount(0);

  // Tag text is sent in a JSON body. No request URL carries a key or a value.
  expect(seen.length).toBeGreaterThan(8);
  for (const request of seen) {
    expect(request.method).toBe("POST");
    expect(request.url).toMatch(/\/console\/activity\/(tags|requests)\.json$/);
  }
  expect(seen.some((request) => request.name === "requests" && request.body.tag_value === "search")).toBe(true);
  expect(seen.some((request) => request.body.tag_value === fixture.markup)).toBe(true);
  expect(page.url()).toMatch(/\/console\/activity$/);
});

test("a workspace without tags is told what tags are for", async ({ page }, testInfo) => {
  const seen = [];
  await page.setViewportSize({ width: 1280, height: 1100 });
  await open(page, fixture.plain, seen);
  const panel = page.locator("[data-tags-panel]");
  const empty = panel.locator("[data-tags-empty]");

  await expect(empty).toBeVisible();
  await expect(empty).toContainText("No tagged requests in the last 30 days.");
  await expect(empty).toContainText("Tags label each request with your own keys and values");
  await expect(empty.locator("code")).toHaveText('"tags": {"team": "search"}');
  await expect(empty.getByRole("link", { name: "Read the tagging docs." })).toHaveAttribute("href", "/docs/tagging");
  // No empty table and no controls with nothing to pick.
  await expect(panel.locator("[data-tags-content]")).toBeHidden();
  await expect(panel.locator("[data-tags-message]")).toBeHidden();
  await expect(page.locator("[data-activity-rows] tr")).toHaveCount(1);
  await expect(page.locator("[data-activity-rows] [data-tag-chip]")).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath("activity-tags-empty.png"), fullPage: true });

  await page.locator("[data-usage-panel] label", { hasText: "7d" }).click();
  await expect(empty).toContainText("No tagged requests in the last 7 days.");
  expect(seen.map((request) => request.body.range)).toEqual(["30d", "7d"]);
});

// Elements of the Tags panel or the request list whose right edge is past the
// viewport, not counting content that scrolls inside its own container.
function spill() {
  const sections = [document.querySelector("[data-tags-panel]"), document.querySelector("[data-activity-rows]").closest("section")];
  const found = [];
  for (const section of sections) {
    for (const node of [section, ...section.querySelectorAll("*")]) {
      const box = node.getBoundingClientRect();
      if (box.width === 0 || box.right <= innerWidth + 0.5) continue;
      let parent = node.parentElement;
      let scrolls = false;
      while (parent && parent !== document.body && !scrolls) {
        scrolls = ["auto", "scroll", "hidden"].includes(getComputedStyle(parent).overflowX);
        parent = parent.parentElement;
      }
      if (!scrolls) found.push(`${node.tagName.toLowerCase()} right=${Math.round(box.right)}`);
    }
  }
  return found;
}

for (const width of [320, 390]) {
  test(`tags do not widen the page at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    await open(page, fixture.tagged, []);
    const panel = page.locator("[data-tags-panel]");
    const list = page.locator("[data-activity-rows]");

    await expect(panel.locator("[data-tags-rows] tr")).toHaveCount(4);
    expect(await list.locator("[data-tag-chip]").first().evaluate((chip) => getComputedStyle(chip).whiteSpace)).toBe("nowrap");
    // Both tables are wider than a phone. Each scrolls inside its panel and
    // nothing of the panel or the list reaches past the viewport.
    for (const table of [panel.locator("table"), page.locator("table", { has: list })]) {
      const scroller = table.locator("xpath=..");
      await expect(scroller).toHaveClass(/table-scroll/);
      expect(await scroller.evaluate((node) => node.scrollWidth > node.clientWidth)).toBe(true);
    }
    expect(await page.evaluate(spill)).toEqual([]);
    // At 390px the whole page fits. At 320px a badge in the reliability
    // panel's heading is already wider than the viewport on main; that panel
    // is not part of this change, so the page-wide check stops at 390.
    if (width >= 390) expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    // The filter still works when its chip has to be scrolled to.
    await list.locator("[data-tag-chip]", { hasText: "team: search" }).first().click();
    await expect(page.locator("[data-activity-filter-note]")).toContainText("Showing requests tagged team: search");
    await expect(panel.locator("[data-tags-clear]")).toBeVisible();
    expect(await page.evaluate(spill)).toEqual([]);
    await page.screenshot({ path: testInfo.outputPath(`activity-tags-${width}.png`), fullPage: true });
  });
}
