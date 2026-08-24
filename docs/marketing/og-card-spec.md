# OG social-card spec (for image generation)

These are the per-page link-preview cards. The site already falls back to the
default brand card (`static/og.png`) for any page whose card doesn't exist yet,
so you can generate and drop these in **one at a time** — each auto-activates
the moment its PNG lands in `src/trusted_router/static/og/`. No code change or
redeploy needed beyond shipping the image file.

## Hard requirements (every card)

- **Dimensions:** exactly **1200 × 630 px** (the OG/Twitter `summary_large_image`
  standard). Cards that aren't this ratio get cropped ugly.
- **Format:** PNG, sRGB, under ~300 KB (the default `og.png` is ~130 KB — match
  that ballpark so unfurls load fast).
- **Safe margins:** keep all text ≥ 80 px from every edge. Some clients crop.
- **Legibility at thumbnail size:** the headline must be readable when the card
  is shown at ~250 px wide in a timeline. Big type, high contrast.
- **Output path:** `src/trusted_router/static/og/<filename>.png` (filenames below).

## Brand

Match the existing `static/og.png` and the site (`static/dashboard.css`):
- Background: dark, near-black with a subtle deep blue/teal — the site's
  `#101820` / `#17384a` terminal gradient is the reference.
- Accent: the site green `#19a06d` / `#7be0b1` (used for the "verifiable" chips).
- Wordmark: `TR` mark + `TrustedRouter` in the top-left, consistent across all
  cards so they read as a set.
- Footer line, small, bottom-left: `trustedrouter.com`
- Optional motif: a subtle lock / checkmark / signed-seal glyph (the
  "verifiable" idea). Keep it minimal — text legibility wins.

## The cards

Each card = the page's hook as a big headline + a one-line subhead. Keep the
headline to ≤ 7 words.

| filename | headline | subhead |
|---|---|---|
| `openrouter-alternative.png` | The OpenRouter alternative you can verify | Open source · hardware-attested · no prompt logs |
| `private-llm-api.png` | Private LLM API. Provably. | Verify the code path. It logs nothing. |
| `hipaa-llm-api.png` | LLM routing you can actually audit | Attested gateway · open source · no PHI logging |
| `llm-zero-data-retention.png` | Zero retention, verifiable in source | Not a contract clause. A property of the code. |
| `claude-api-privacy.png` | Claude, through a path you can verify | Anthropic's posture + an open, attested router |
| `litellm-alternative.png` | Self-host. And prove what it runs. | LiteLLM's freedom + hardware attestation |
| `portkey-alternative.png` | Routing without logging every prompt | Usage metering, zero content logs |
| `confidential-computing-llm.png` | Confidential computing for LLMs | GCP Confidential Space · every provider · one API |
| `tinfoil-alternative.png` | Verifiable privacy, every provider | Same bet as Tinfoil — applied as a router |

## Optional, higher-value cards (do these first if generating a subset)

The pages that get shared the most are the homepage, the playground, and the
flagship comparison. The homepage already has `og.png`. Two worth tailoring:

| filename | page | headline | subhead |
|---|---|---|---|
| `chat.png` | `/chat` | Try any model. Zero tokens until you sign in. | Compare 4 LLMs side by side, free |
| `compare-openrouter.png` | `/compare/openrouter` | OpenRouter, but you can verify it | Change one line. Keep your models. Prove the path. |

(These two pages don't use `PublicPage.og_card` yet — `/chat` renders via
`public_chat_html` and `/compare/openrouter` via the `compare/openrouter`
PublicPage entry. Wiring is a one-line change once the images exist; ping me
and I'll add it, or set `og_card="compare-openrouter.png"` on that entry.)

## How activation works (so you can verify)

`dashboard.py:_og_image_url()` returns the per-page card **only if the PNG
exists on disk**, else the default `og.png`. After dropping a file:
1. `ls src/trusted_router/static/og/` — confirm the PNG is there.
2. Hit the page locally and grep the head for `og:image` — it should point at
   `/static/og/<filename>.png`.
3. After deploy, validate the live unfurl at
   https://cards-dev.twitter.com/validator or by pasting the URL into Slack.
