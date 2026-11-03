# Social cards (link previews)

What a page shows when its link is pasted into Slack, LinkedIn, X or iMessage.
Every public page sets `og:image`; this is where each kind of page gets it.

| Pages | Image | Made by |
|---|---|---|
| Every page without a card of its own: model pages, model comparisons, docs, legal, most core pages (3,639 of 3,823 sitemap URLs on 2026-11-02) | `static/og.png`, the default card | `scripts/generate_og_card.py` |
| Homepage | `static/homepage/social-card-v2.jpg` | browser capture of `docs/homepage/social-card.html` |
| Provider pages | `static/og/providers/<slug>.png` | `scripts/generate_provider_og.py`, hourly from the catalog |
| Blog posts with a leading chart | `static/og/blog/<slug>.png` | `scripts/generate_blog_og.py` |
| 24 pages with a `PublicPage.og_card` | `static/og/<name>.png` | hand-made in June 2026, older style |

## The default card

The default card is the homepage card with the homepage card's words, rendered
from the shared template `docs/marketing/og-card.html`: the river photo, the
dark left shade, the mark and wordmark, an Archivo headline with a pale-green
italic second line, a one-line subhead and `trustedrouter.com` bottom-left.
Only the words are variables.

```bash
uv run python scripts/generate_og_card.py          # rewrites static/og.png
```

The words live in `DEFAULT_CARD` in the script. Keep them in step with
`docs/homepage/social-card.html`, so a shared link anywhere on the site says
what the homepage says. The script needs the repo's Playwright Chromium
(`npm ci`; Node 18 or newer) and Pillow.

`/og.png` is served by `routes/public.py` with a one-hour cache. Social
networks cache the image behind a link for days: links shared before a deploy
keep the old card until the network re-fetches the page (Slack and LinkedIn
have re-scrape tools; X re-fetches on its own).

## Hard requirements (every card)

- **Dimensions:** exactly **1200 × 630 px**. Other ratios get cropped.
- **Format:** PNG under **300 KB**. The template's photo is about 600 KB at
  full colour; the script reduces it to a median-cut palette, 128 to 160
  colours, which looks the same at full size. No dithering: it showed as grain
  in the sky.
- **Headline:** at most 7 words over two lines. The script shrinks the type
  from 76 px until both lines fit in 840 px, so the river stays visible.
- **Legible at thumbnail size:** the headline must read at about 250 px wide.

## Per-page cards

A `PublicPage` entry in `dashboard.py` can name its own card with
`og_card="<name>.png"`. `_og_image_url()` serves it only if
`static/og/<name>.png` exists; otherwise the page gets the default card, so a
declared card never 404s. Only 24 pages have one, all from June 2026 in an
older style. Site-wide consistency comes from the default card, not from
adding more of these.

If a page genuinely needs its own words, render it in the same style and drop
it in place:

```bash
uv run python scripts/generate_og_card.py --out src/trusted_router/static/og/<name>.png \
  --headline "Six words at most" --accent "italic second line" --subhead "One sentence."
```

Then check the page's head locally: `og:image` should point at
`/static/og/<name>.png`.
