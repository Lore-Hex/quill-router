const { test, expect } = require("@playwright/test");
const { execFileSync } = require("node:child_process");
const path = require("node:path");

// The page HTML and every JSON answer come from the real routes over the
// in-memory store (see the fixture). The browser only replays them.
const fixture = JSON.parse(execFileSync("uv", ["run", "--no-sync", "python", "tests/browser/activity_tags_fixture.py"], {
  cwd: path.resolve(__dirname, "../.."), encoding: "utf8", maxBuffer: 32 * 1024 * 1024,
  env: { ...process.env, PYTHONPATH: "src", TR_ENVIRONMENT: "test", TR_STORAGE_BACKEND: "memory", TR_SENTRY_DSN: "" },
}));

// The same canonical form the fixture keys its answers by.
function key(body) {
  return JSON.stringify(Object.keys(body).sort().reduce((sorted, name) => {
    sorted[name] = body[name];
    return sorted;
  }, {}));
}

// `usage` answers the chart's request: a function of the range, or undefined
// for the fixture's real answers. `refuse` picks tag requests, by their body,
// that the server fails. `rewrite(name, body, answer)` returns the answer to
// send in place of the fixture's, and may wait first.
async function open(page, workspace, seen, usage, refuse, rewrite) {
  await page.route("**/console/activity", (route) => route.fulfill({ contentType: "text/html", body: workspace.page }));
  await page.route("**/console/activity/usage.json*", (route) => {
    const range = new URL(route.request().url()).searchParams.get("range");
    if (usage) return usage(route, range);
    const empty = { granularity: "day", buckets: [], range, latest_activity_at: null };
    return route.fulfill({ json: (workspace.usage || {})[range] || empty });
  });
  await page.route("**/console/activity/client-reliability.json*", (route) => route.fulfill({
    json: { data: null, meta: { reason: "unavailable" } },
  }));
  for (const name of ["tags", "requests"]) {
    await page.route(`**/console/activity/${name}.json*`, async (route) => {
      const request = route.request();
      const body = request.postDataJSON();
      seen.push({ name, method: request.method(), url: request.url(), body });
      if (refuse && refuse(body)) return route.fulfill({ status: 503, json: { error: { message: "unavailable" } } });
      const answer = (workspace[name] || {})[key(body || {})];
      if (!answer) return route.fulfill({ status: 500, json: { error: "the fixture has no answer for this request" } });
      const sent = rewrite ? await rewrite(name, body, answer.body) : answer.body;
      return route.fulfill({ status: answer.status, json: sent });
    });
  }
  await page.goto("/console/activity");
  await page.evaluate(() => document.fonts.ready);
}

function ranges(seen, name = "tags") {
  return seen.filter((request) => request.name === name).map((request) => request.body.range);
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
  // The median is the server's generation time, and the column says so.
  const heading = panel.locator("thead th").nth(4);
  await expect(heading).toHaveText("Median generation time");
  await expect(heading).toHaveAttribute("title", /not the client-observed latency/);
  // With no filter there is nothing to clear and no value to type. This also
  // proves the page got this checkout's stylesheet: unstyled, a hidden .btn
  // and a round chip cannot be told from broken ones.
  await expect(panel.locator("[data-tags-clear]")).toBeHidden();
  await expect(panel.locator("[data-tags-value-note]")).toBeHidden();
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

  // The filter and the breakdown key are independent. Two requests: the
  // median is the floor of the mean of 760 and 820.
  await panel.locator("[data-tags-group]").selectOption({ label: "environment" });
  await expect(rows).toHaveCount(2);
  await expect(rows.nth(0).locator("td")).toHaveText(["environment: production", "2", "$3.25", "3,010", "790 ms"]);
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
  expect(ranges(seen)).toEqual(["30d", "7d"]);
});

test("a filter survives a range that has no request for it", async ({ page }, testInfo) => {
  const seen = [];
  await page.setViewportSize({ width: 1280, height: 1100 });
  await open(page, fixture.tagged, seen);
  const panel = page.locator("[data-tags-panel]");
  const list = page.locator("[data-activity-rows]");
  const note = page.locator("[data-activity-filter-note]");
  const message = panel.locator("[data-tags-message]");

  await list.locator("[data-tag-chip]", { hasText: "team: search" }).first().click();
  await panel.locator("[data-tags-group]").selectOption({ label: "environment" });
  await expect(list.locator("tr")).toHaveCount(3);

  // The last hour has no tagged request at all. The filter stays in view,
  // with its Clear control, and the page says the range has nothing for it.
  await page.locator("[data-usage-panel] label", { hasText: "1h" }).click();
  await expect(message).toHaveText("No request in the last hour is tagged team: search. The filter is kept for other ranges.");
  await expect(panel.locator("[data-tags-empty]")).toBeHidden();
  await expect(panel.locator("[data-tags-content]")).toBeVisible();
  await expect(panel.locator("[data-tags-filter-key]")).toHaveValue("team");
  await expect(panel.locator("[data-tags-filter-value]")).toHaveValue("search");
  await expect(panel.locator("[data-tags-clear]")).toBeVisible();
  await expect(panel.locator("[data-tags-group]")).toBeDisabled();
  await expect(panel.locator("[data-tags-rows] td")).toHaveText("No request in the last hour carries a tag.");
  await expect(note).toContainText("No requests tagged team: search in the last hour.");
  await expect(list.locator("td")).toHaveText("No requests with this tag in the range.");
  await page.screenshot({ path: testInfo.outputPath("activity-tags-kept-filter.png"), fullPage: true });

  // Back in a range that has it, the filter and the chosen key apply again.
  await page.locator("[data-usage-panel] label", { hasText: "30d" }).click();
  await expect(note).toContainText("Showing requests tagged team: search in the last 30 days.");
  await expect(list.locator("tr")).toHaveCount(3);
  await expect(panel.locator("[data-tags-filter-key]")).toHaveValue("team");
  await expect(panel.locator("[data-tags-filter-value]")).toHaveValue("search");
  await expect(panel.locator("[data-tags-group]")).toHaveValue("environment");
  await expect(panel.locator("[data-tags-rows] tr").nth(0).locator("td").nth(0)).toHaveText("environment: production");

  // Every request after the click carried the filter, in each range.
  const filtered = seen.filter((request) => request.body.tag_key === "team");
  expect(filtered.map((request) => request.body.range)).toEqual(expect.arrayContaining(["30d", "1h"]));
  expect(seen.filter((request) => request.body.range === "1h").every((request) => request.body.tag_value === "search")).toBe(true);
  // Only the viewer clears it.
  await panel.locator("[data-tags-clear]").click();
  await expect(note).toBeHidden();
  await expect(list.locator("tr")).toHaveCount(6);
});

test("the panel follows the range when the chart cannot load", async ({ page }) => {
  const seen = [];
  await page.setViewportSize({ width: 1280, height: 1100 });
  await open(page, fixture.tagged, seen, (route) => route.fulfill({ status: 500, json: { error: "unavailable" } }));
  const panel = page.locator("[data-tags-panel]");
  const message = panel.locator("[data-tags-message]");

  await expect(message).toHaveText("Every request in the last 30 days is counted.");
  await expect(page.locator("[data-usage-error]")).toBeVisible();
  await expect(page.locator("[data-usage-layout]")).toBeHidden();
  // The chart never draws, so its range label never changes. The usage panel
  // still announces the range it was asked for.
  await page.locator("[data-usage-panel] label", { hasText: "7d" }).click();
  await expect(message).toHaveText("Every request in the last 7 days is counted.");
  await page.locator("[data-usage-panel] label", { hasText: "1h" }).click();
  await expect(panel.locator("[data-tags-empty]")).toContainText("No tagged requests in the last hour.");
  expect(ranges(seen)).toEqual(["30d", "7d", "1h"]);
});

test("a range whose tags cannot be read says so and other ranges still load", async ({ page }) => {
  const seen = [];
  await page.setViewportSize({ width: 1280, height: 1100 });
  await open(page, fixture.tagged, seen, undefined, (body) => body.range === "90d");
  const panel = page.locator("[data-tags-panel]");
  const message = panel.locator("[data-tags-message]");

  await expect(message).toHaveText("Every request in the last 30 days is counted.");
  await page.locator("[data-usage-panel] label", { hasText: "90d" }).click();
  // No table and no empty state: nothing partial, and nothing that reads as
  // "this workspace has no tags".
  await expect(message).toHaveText("Couldn't load tags for this range right now. A shorter range may load.");
  await expect(message).toHaveClass(/error/);
  await expect(panel.locator("[data-tags-content]")).toBeHidden();
  await expect(panel.locator("[data-tags-empty]")).toBeHidden();
  await page.locator("[data-usage-panel] label", { hasText: "7d" }).click();
  await expect(message).toHaveText("Every request in the last 7 days is counted.");
  await expect(message).not.toHaveClass(/error/);
  await expect(panel.locator("[data-tags-rows] tr")).toHaveCount(4);
});

test("the panel follows the chart's Show 90 days control", async ({ page }) => {
  const seen = [];
  const sixtyDaysAgo = new Date(Date.now() - 60 * 24 * 60 * 60 * 1000).toISOString();
  await page.setViewportSize({ width: 1280, height: 1100 });
  // Nothing in the last 30 days, activity 60 days ago: the chart offers the
  // wider range with a button that checks the radio from script.
  await open(page, fixture.tagged, seen, (route, range) => route.fulfill({
    json: { granularity: "day", buckets: [], range, latest_activity_at: sixtyDaysAgo },
  }));
  const message = page.locator("[data-tags-panel] [data-tags-message]");

  await expect(message).toHaveText("Every request in the last 30 days is counted.");
  const widen = page.locator("[data-usage-expand-range]");
  await expect(widen).toHaveText("Show 90 days");
  await widen.click();
  await expect(page.locator('input[name="usage-range"][value="90d"]')).toBeChecked();
  await expect(message).toHaveText("Every request in the last 90 days is counted.");
  expect(ranges(seen)).toEqual(["30d", "90d"]);
});

test("the panel starts on the range the usage panel restored", async ({ page }) => {
  const seen = [];
  await page.setViewportSize({ width: 1280, height: 1100 });
  // The usage panel remembers the viewer's range and opens on it. It has
  // published that range by the time this panel starts, so the first tag
  // request is already for it: one request, not the default and then the choice.
  await page.addInitScript(() => window.localStorage.setItem("tr.console.activity.usage-range", "7d"));
  await open(page, fixture.tagged, seen);
  const message = page.locator("[data-tags-panel] [data-tags-message]");

  await expect(message).toHaveText("Every request in the last 7 days is counted.");
  await expect(page.locator('input[name="usage-range"][value="7d"]')).toBeChecked();
  await expect(page.locator("[data-usage-panel]")).toHaveAttribute("data-usage-range", "7d");
  expect(ranges(seen)).toEqual(["7d"]);
});

test("the panel follows the range event and nothing else", async ({ page }) => {
  const seen = [];
  await page.setViewportSize({ width: 1280, height: 1100 });
  await open(page, fixture.tagged, seen);
  const message = page.locator("[data-tags-panel] [data-tags-message]");
  await expect(message).toHaveText("Every request in the last 30 days is counted.");

  // The contract is the event's detail. A radio checked without the usage
  // panel announcing it changes nothing here.
  await page.evaluate(() => {
    document.querySelector('input[name="usage-range"][value="7d"]').checked = true;
  });
  const announce = (range) => page.evaluate((value) => {
    document.dispatchEvent(new CustomEvent("activity:range-change", { detail: { range: value } }));
  }, range);
  await announce("24h");
  await expect(message).toHaveText("Every request in the last 24 hours is counted.");
  // The same range again, a range that does not exist and an event without a
  // range ask for nothing.
  await announce("24h");
  await announce("2h");
  await page.evaluate(() => document.dispatchEvent(new CustomEvent("activity:range-change")));
  await announce("90d");
  await expect(message).toHaveText("Every request in the last 90 days is counted.");
  expect(ranges(seen)).toEqual(["30d", "24h", "90d"]);
});

test("a key with many values says so and takes a typed value", async ({ page }, testInfo) => {
  const seen = [];
  await page.setViewportSize({ width: 1280, height: 1100 });
  await open(page, fixture.many, seen);
  const panel = page.locator("[data-tags-panel]");
  const rows = panel.locator("[data-tags-rows] tr");
  const valueNote = panel.locator("[data-tags-value-note]");
  const list = page.locator("[data-activity-rows]");

  // Every request carries its own id, so "request" is the most used key and
  // cannot be broken down. The first view is the next key, not a refusal.
  await expect(panel.locator("[data-tags-group]")).toHaveValue("team");
  await expect(rows).toHaveCount(1);
  await expect(rows.nth(0).locator("td").nth(0)).toHaveText("team: search");
  await expect(rows.nth(0).locator("td").nth(1)).toHaveText("55");

  // Asked for, the key gets an answer: why there is no table.
  await panel.locator("[data-tags-group]").selectOption({ label: "request" });
  await expect(rows).toHaveCount(1);
  // The count behind a refusal is an estimate in ClickHouse, and the page
  // words it as one.
  await expect(rows.locator("td")).toHaveText(
    `This key has about ${fixture.many.distinct_limit} or more distinct values in the last 30 days, too many to break down. Choose another key or a shorter range.`
  );

  // Its value list holds the 50 most used of 55 values and says it is cut.
  await panel.locator("[data-tags-filter-key]").selectOption({ label: "request" });
  await expect(valueNote).toBeVisible();
  await expect(valueNote).toContainText("The list holds this key's most used values. To filter by another, type it (at most 256 characters):");
  await expect(panel.locator("[data-tags-filter-value] option")).toHaveCount(51);
  await expect(panel.locator("[data-tags-filter-value] option", { hasText: "req-54" })).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath("activity-tags-many-values.png"), fullPage: true });

  // A value the list leaves out can be typed.
  await panel.locator("[data-tags-value-input]").fill("req-54");
  await valueNote.getByRole("button", { name: "Filter", exact: true }).click();
  await expect(page.locator("[data-activity-filter-note]")).toContainText("Showing requests tagged request: req-54 in the last 30 days.");
  await expect(list.locator("tr")).toHaveCount(1);
  await expect(list.locator("[data-tag-chip]", { hasText: "request: req-54" })).toHaveCount(1);
  await expect(panel.locator("[data-tags-filter-value]")).toHaveValue("req-54");
  await expect(panel.locator("[data-tags-value-input]")).toHaveValue("");
  const typed = seen.filter((request) => request.body.tag_value === "req-54");
  expect(typed.map((request) => request.name).sort()).toEqual(["requests", "tags"]);
  for (const request of typed) expect(request.url).not.toContain("req-54");
});

test("a typed value is sent as typed: empty, long, and outside the basic plane", async ({ page }) => {
  const seen = [];
  await page.setViewportSize({ width: 1280, height: 1100 });
  await open(page, fixture.many, seen);
  const panel = page.locator("[data-tags-panel]");
  const input = panel.locator("[data-tags-value-input]");
  const error = panel.locator("[data-tags-value-error]");
  const note = page.locator("[data-activity-filter-note]");
  const filtered = () => seen.filter((request) => request.body.tag_value !== null && request.body.tag_key === "request");

  await panel.locator("[data-tags-filter-key]").selectOption({ label: "request" });
  await expect(panel.locator("[data-tags-value-note]")).toBeVisible();
  // The field has no length limit of its own to cut what is typed.
  await expect(input).not.toHaveAttribute("maxlength", /.*/);

  // 129 letters from outside the basic plane: 258 UTF-16 units, 129
  // characters by the server's count, under its limit of 256. All of it is
  // sent, and the server accepts it.
  expect(fixture.astral.length).toBe(258);
  await input.fill(fixture.astral);
  await panel.getByRole("button", { name: "Filter", exact: true }).click();
  await expect(note).toContainText("No requests tagged request: ");
  await expect(error).toBeHidden();
  expect(filtered().map((request) => request.body.tag_value)).toEqual([fixture.astral, fixture.astral]);
  expect(Array.from(filtered()[0].body.tag_value).length).toBe(129);

  // One character past the server's limit is refused here, in words, and
  // nothing is sent. The text stays as typed.
  const tooLong = "x".repeat(257);
  await input.fill(tooLong);
  await panel.getByRole("button", { name: "Filter", exact: true }).click();
  await expect(error).toHaveText("A tag value has at most 256 characters. This one has 257.");
  await expect(input).toHaveValue(tooLong);
  expect(filtered().length).toBe(2);

  // An empty field is not the empty value. The empty value has its own
  // control, because a request can carry it.
  await input.fill("");
  await panel.getByRole("button", { name: "Filter", exact: true }).click();
  await expect(error).toHaveText("Type a value first. For requests whose value is empty, use the button for the empty value.");
  expect(filtered().length).toBe(2);
  await panel.getByRole("button", { name: "Filter by the empty value" }).click();
  await expect(note).toContainText("No requests tagged request: (empty) in the last 30 days.");
  await expect(error).toBeHidden();
  await expect(panel.locator("[data-tags-filter-value]")).toHaveValue("(empty)");
  expect(filtered().slice(2).map((request) => [request.name, request.body.tag_value]).sort()).toEqual([
    ["requests", ""], ["tags", ""],
  ]);
});

test("an approximate ranking and an approximate value list say so", async ({ page }) => {
  const seen = [];
  await page.setViewportSize({ width: 1280, height: 1100 });
  // What a store answers for a key with more than 50 values whose Other row
  // outweighs the smallest named one, and for a value list it could not
  // count exactly. The fixture's workspaces have neither, so an Other row
  // and the two flags are put on its answers here.
  await open(page, fixture.tagged, seen, undefined, undefined, (name, body, answer) => {
    if (name !== "tags") return answer;
    const filter = answer.filter ? { ...answer.filter, values_approximate: true } : null;
    if (body.group_by !== "team") return { ...answer, filter };
    const other = {
      kind: "other", value: null, requests: 40, cost_microdollars: 9000000, byok_microdollars: 0,
      prompt_tokens: 700, completion_tokens: 300, median_elapsed_ms: 650,
    };
    const rows = answer.breakdown.filter((row) => row.kind === "value")
      .concat([other], answer.breakdown.filter((row) => row.kind === "untagged"));
    return { ...answer, breakdown: rows, breakdown_approximate: true, filter };
  });
  const panel = page.locator("[data-tags-panel]");
  const message = panel.locator("[data-tags-message]");
  const rows = panel.locator("[data-tags-rows] tr");

  await expect(message).toHaveText("Every request in the last 30 days is counted.");
  await panel.locator("[data-tags-group]").selectOption({ label: "team" });
  await expect(message).toHaveText(
    "Every request in the last 30 days is counted. The ranking is approximate: a value counted in Other may belong among the rows shown. This is also reported when Other only holds many small values."
  );
  // The Other row says what it adds up to and nothing about how many values
  // that is.
  await expect(rows).toHaveCount(5);
  await expect(rows.nth(3).locator("td")).toHaveText(["Other", "40", "$9.00", "1,000", "650 ms"]);
  await panel.locator("[data-tags-filter-key]").selectOption({ label: "team" });
  await expect(panel.locator("[data-tags-value-note]")).toContainText(
    "This key has more values than are counted exactly, so the list is approximately its most used ones. To filter by another, type it"
  );
});

test("a late answer for a superseded choice is ignored", async ({ page }) => {
  const seen = [];
  let release = null;
  const held = new Promise((resolve) => { release = resolve; });
  await page.setViewportSize({ width: 1280, height: 1100 });
  // The filtered answers for 30 days are held back until the test lets them
  // go. They differ from the 24-hour ones: three requests, not two.
  await open(page, fixture.tagged, seen, undefined, undefined, async (name, body, answer) => {
    if (body.range === "30d" && body.tag_key === "team") await held;
    return answer;
  });
  const panel = page.locator("[data-tags-panel]");
  const list = page.locator("[data-activity-rows]");
  const message = panel.locator("[data-tags-message]");
  const note = page.locator("[data-activity-filter-note]");
  const requests = panel.locator("[data-tags-rows] tr").nth(0).locator("td").nth(1);

  await expect(message).toHaveText("Every request in the last 30 days is counted.");
  // Filter by a chip: both answers for 30 days are now on their way and held.
  await list.locator("[data-tag-chip]", { hasText: "team: search" }).first().click();
  await expect(message).toHaveText("Loading tags…");
  await expect(note).toContainText("Loading tagged requests…");
  // Choose 24 hours before they arrive. Its answers come at once.
  await page.locator("[data-usage-panel] label", { hasText: "24h" }).click();
  await expect(message).toHaveText("Every request in the last 24 hours is counted. Filtered to team: search.");
  await expect(note).toContainText("Showing requests tagged team: search in the last 24 hours.");
  await expect(list.locator("tr")).toHaveCount(2);
  await expect(requests).toHaveText("2");

  // The 30-day answers arrive late, for the breakdown and for the list. The
  // page keeps what it shows for the range the viewer chose last.
  const late = Promise.all(["tags", "requests"].map((name) => page.waitForResponse((response) => {
    const body = response.request().postDataJSON();
    return response.url().endsWith(`/${name}.json`) && body.range === "30d" && body.tag_key === "team";
  })));
  release();
  await late;
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  await expect(list.locator("tr")).toHaveCount(2);
  await expect(requests).toHaveText("2");
  await expect(message).toHaveText("Every request in the last 24 hours is counted. Filtered to team: search.");
  await expect(note).toContainText("Showing requests tagged team: search in the last 24 hours.");
  expect(ranges(seen)).toEqual(["30d", "30d", "24h"]);
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

test("the typed-value note fits a phone", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 900 });
  await open(page, fixture.many, []);
  const panel = page.locator("[data-tags-panel]");

  await panel.locator("[data-tags-filter-key]").selectOption({ label: "request" });
  await expect(panel.locator("[data-tags-value-note]")).toBeVisible();
  expect(await page.evaluate(spill)).toEqual([]);
});
