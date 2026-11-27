const { test, expect } = require('@playwright/test');

async function capture(page) {
  const sent = [];
  await page.route('**/analytics/events', async route => {
    const body = route.request().postDataJSON();
    if (body.event.startsWith('models.')) sent.push({body, headers: route.request().headers()});
    await route.fulfill({status: 204});
  });
  return sent;
}

test('catalog events debounce search and preserve privacy without duplicate renders', async ({ page }) => {
  const sent = await capture(page);
  await page.goto('/models?q=initial-query');
  await page.waitForTimeout(600);
  expect(sent).toHaveLength(0); // restored URL state is not a new interaction
  const search = page.locator('[data-model-search]');
  await search.fill('private-query-canary');
  await search.fill('private-query-canary-final');
  await expect.poll(() => sent.length).toBe(2);
  expect(sent.map(x => x.body)).toEqual([{event:'models.search_used'}, {event:'models.search_zero_results'}]);
  await search.blur();
  await page.locator('[data-model-sort]').selectOption('cached');
  await expect.poll(() => sent.length).toBe(3);
  expect(sent[2].body).toEqual({event:'models.sort_changed',catalog:{sort:'cached'}});
  await page.locator('[data-model-query="deepseek"]').click();
  await expect.poll(() => sent.length).toBe(4);
  expect(sent[3].body).toEqual({event:'models.popular_pick_clicked',catalog:{pick:'deepseek'}});
  await page.waitForTimeout(600);
  expect(sent).toHaveLength(4);
  for (const entry of sent) {
    expect(entry.headers.referer).toBeUndefined();
    expect(JSON.stringify(entry.body)).not.toContain('private-query');
  }
  await page.locator('[data-catalog-filter="zdr"]').click();
  await expect.poll(() => sent.length).toBe(5);
  expect(sent[4].body).toEqual({event:'models.filter_changed',catalog:{filter:'zdr'}});
});

test('comparison reopening and static/dynamic model/provider links each emit once', async ({ page }) => {
  const sent = await capture(page);
  await page.route('**/endpoints', route => route.fulfill({json:{data:[{
    provider:'openai',provider_name:'OpenAI',usage_type:'Credits',pricing:{prompt:'0.000001',completion:'0.000002'},
  }]}}));
  await page.goto('/models?q=glm-5.3-flash');
  const card = page.locator('[data-model-id="z-ai/glm-5.3-flash"]');
  await card.locator('summary').click();
  await expect(card.locator('.model-route-row')).toHaveCount(1);
  await card.locator('summary').click();
  await card.locator('summary').click();
  await expect.poll(() => sent.length).toBe(2);
  // Prevent navigation only after the real delegated listener runs.
  await page.evaluate(() => document.addEventListener('click', e => {
    if (e.target.closest('[data-model-card] a')) e.preventDefault();
  }));
  await card.locator('.model-route-provider a').click();
  await card.locator('.model-card-title a').click();
  await card.locator('.model-publisher-logo').click();
  await expect.poll(() => sent.length).toBe(5);
  expect(sent.map(x => x.body.event)).toEqual([
    'models.comparison_expanded','models.comparison_expanded','models.provider_clicked',
    'models.model_clicked','models.provider_clicked',
  ]);
  expect(sent[2].body.catalog).toEqual({model_id:'z-ai/glm-5.3-flash',provider_id:'openai'});
});

for (const signal of ['globalPrivacyControl', 'doNotTrack']) {
  test(`catalog respects ${signal}`, async ({ page }) => {
    await page.addInitScript(signal => Object.defineProperty(navigator, signal, {
      get: () => signal === 'globalPrivacyControl' ? true : '1',
    }), signal);
    const sent = await capture(page);
    await page.goto('/models');
    await page.locator('[data-model-search]').fill('private-query-canary');
    await page.locator('[data-model-sort]').selectOption('name');
    await page.locator('[data-model-query="deepseek"]').click();
    await page.waitForTimeout(600);
    expect(sent).toHaveLength(0);
  });
}

test('failed telemetry leaves search and sorting usable', async ({ page }) => {
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/analytics/events', route => route.abort());
  await page.goto('/models');
  await page.locator('[data-model-search]').fill('private-query-canary');
  await page.waitForTimeout(600);
  await expect(page.locator('[data-model-empty]')).toBeVisible();
  await page.locator('[data-model-query="deepseek"]').click();
  await page.locator('[data-model-sort]').selectOption('name');
  await expect(page.locator('[data-model-empty]')).toBeHidden();
  expect(errors).toEqual([]);
});
