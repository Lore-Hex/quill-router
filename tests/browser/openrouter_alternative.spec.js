const { test, expect } = require("@playwright/test");

for (const [width, height] of [[320, 568], [360, 800], [390, 844], [430, 932], [768, 1024], [1024, 768], [1280, 800], [1440, 900]]) {
  test(`OpenRouter alternative fits ${width}px with readable tables`, async ({ page }) => {
    await page.setViewportSize({ width, height });
    await page.goto("/openrouter-alternative");
    await expect(page.locator("main h1")).toHaveCount(1);
    await expect(page.locator('[aria-labelledby="quick-answer-heading"] .panel-body > p:first-child strong')).toHaveCSS("color", "rgb(183, 228, 202)");
    await expect(page.locator(".faq-panel details")).toHaveCount(11);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    await expect(page.locator(".v2-stack")).toHaveCount(4);
    await expect(page.locator("main > section").nth(1)).toHaveAttribute("aria-labelledby", "quick-answer-heading");
    await expect(page.locator('.quote-feature[aria-labelledby="privacy-tiers-heading"] h3')).toHaveCount(3);
    const layout = await page.evaluate(() => {
      const style = (selector) => getComputedStyle(document.querySelector(selector));
      const heading = style('.quote-feature h3');
      const body = style('.quote-feature p');
      return {
        ctaInset: parseFloat(style('main > .cta-band').paddingLeft),
        resourceBorder: style('.link-list li').borderTopWidth,
        codeInset: parseFloat(style('.copy-terminal pre').paddingLeft),
        headingSize: parseFloat(heading.fontSize),
        bodySize: parseFloat(body.fontSize),
        headingWeight: parseFloat(heading.fontWeight),
      };
    });
    expect(layout.ctaInset).toBeGreaterThanOrEqual(24);
    expect(layout.resourceBorder).toBe("0px");
    expect(layout.codeInset).toBeGreaterThanOrEqual(20);
    expect(layout.headingSize).toBeGreaterThan(layout.bodySize);
    expect(layout.headingWeight).toBeGreaterThanOrEqual(600);
    const ctaStyles = await page.getByRole('button', { name: 'Create my API key', exact: true }).evaluateAll(buttons => buttons.map(button => {
      const css = getComputedStyle(button);
      return Object.fromEntries(['fontFamily', 'fontSize', 'fontWeight', 'lineHeight', 'height', 'padding', 'borderRadius', 'backgroundColor', 'color', 'border', 'boxShadow', 'transition'].map(key => [key, css[key]]));
    }));
    expect(ctaStyles).toHaveLength(3);
    for (const style of ctaStyles) expect(style).toEqual(ctaStyles[2]);
    for (const button of await page.getByRole('button', { name: 'Create my API key', exact: true }).all()) {
      expect(await button.evaluate(el => {
        const container = el.closest('.cta-band, .marketing-copy');
        return Math.abs(el.getBoundingClientRect().left - container.getBoundingClientRect().left
          - parseFloat(getComputedStyle(container).paddingLeft) - parseFloat(getComputedStyle(container).borderLeftWidth));
      })).toBeLessThan(1);
    }
    await expect(page.locator('.v2-table-cta')).toHaveCSS('align-items', 'flex-start');
    await expect(page.locator('.v2-table-cta')).toHaveCSS('flex-direction', 'column');
    if (width <= 720) {
      await expect(page.locator(".v2-stack thead").first()).toBeVisible();
      expect(await page.locator(".v2-saving-table").evaluate(el => el.getBoundingClientRect().width)).toBeLessThanOrEqual(width - 40);
      await expect(page.locator(".v2-stack tbody tr").first()).toHaveCSS("display", "table-row");
    }
  });
}

test('Tables and code use native scrolling with boundary-aware edge cues', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/openrouter-alternative');
  await expect(page.locator('.table-scroll-tools')).toHaveCount(0);
  for (const viewport of [page.locator('.model-table-wrap').first(), page.locator('.copy-terminal pre')]) {
    await expect(viewport).toHaveClass(/can-scroll-right/);
    await expect(viewport).not.toHaveClass(/can-scroll-left/);
    await viewport.focus();
    await page.keyboard.press('ArrowRight');
    const ring = await viewport.evaluate(el => {
      const frame = el.parentElement;
      const style = getComputedStyle(frame, '::after');
      return {
        focusVisible: el.matches(':focus-visible'),
        mask: getComputedStyle(frame).maskImage,
        border: style.borderTopWidth,
        color: style.borderTopColor,
        hitTesting: style.pointerEvents,
        width: frame.getBoundingClientRect().width,
        viewportWidth: el.getBoundingClientRect().width,
      };
    });
    expect(ring.focusVisible).toBe(true);
    expect(ring.mask).toBe('none');
    expect(ring.border).toBe('2px');
    expect(ring.color).toBe('rgb(183, 228, 202)');
    expect(ring.hitTesting).toBe('none');
    expect(ring.width).toBe(ring.viewportWidth);
    await expect.poll(() => viewport.evaluate(el => el.scrollLeft)).toBeGreaterThan(0);
    await expect(viewport).toHaveClass(/can-scroll-left/);
    await viewport.evaluate(el => { el.scrollLeft = el.scrollWidth; });
    await expect(viewport).not.toHaveClass(/can-scroll-right/);
  }
  await page.setViewportSize({ width: 1440, height: 900 });
  await expect(page.locator('.model-table-wrap').first()).not.toHaveClass(/can-scroll/);
});

test('FAQ pricing has no stale as-of date in visible text or structured data', async ({ page }) => {
  await page.goto('/openrouter-alternative');
  const answer = 'TrustedRouter charges provider prices plus 5.5%';
  await expect(page.locator('.faq-panel')).toContainText(answer);
  await expect(page.locator('.faq-panel')).not.toContainText('As of October 2026');
  const structured = await page.locator('script[type="application/ld+json"]').allTextContents();
  expect(structured.join(' ')).toContain(answer);
  expect(structured.join(' ')).not.toContain('As of October 2026');
});

 test('API key CTAs open signup, including below the alternatives table', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/openrouter-alternative');
  const ctas = page.getByRole('button', { name: 'Create my API key', exact: true });
  await expect(ctas).toHaveCount(3);
  await expect(page.locator('.v2-table-cta')).toHaveCount(1);
  await expect(page.locator('main')).not.toContainText('Attested gateways publish hardware proof');
  for (const cta of await ctas.all()) {
    await cta.click();
    await expect(page.locator('#signinModal')).toBeVisible();
    await expect(page.locator('#signinTitle')).toHaveText('Get your API key');
    await page.keyboard.press('Escape');
  }
 });

test('API key CTAs share hover and keyboard-focus styling', async ({ page }) => {
  await page.goto('/openrouter-alternative');
  const buttons = page.getByRole('button', { name: 'Create my API key', exact: true });
  const readStyle = button => button.evaluate(el => {
    const css = getComputedStyle(el);
    return Object.fromEntries(['backgroundColor', 'color', 'border', 'boxShadow', 'outline', 'outlineOffset', 'transform'].map(key => [key, css[key]]));
  });
  for (const state of ['hover', 'focus']) {
    const styles = [];
    for (const button of await buttons.all()) {
      await page.mouse.move(0, 0);
      await button.evaluate(el => el.blur());
      if (state === 'hover') await button.hover();
      else {
        await button.focus();
        await page.keyboard.press('ArrowRight');
        expect(await button.evaluate(el => el.matches(':focus-visible'))).toBe(true);
      }
      await button.evaluate(async el => {
        await Promise.all(el.getAnimations().map(animation => animation.finished));
      });
      styles.push(await readStyle(button));
      await button.evaluate(el => el.blur());
    }
    for (const style of styles) expect(style).toEqual(styles[2]);
  }
});
