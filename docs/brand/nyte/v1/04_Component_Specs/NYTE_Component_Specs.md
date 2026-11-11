# NYTE · Component specs

V1 (Nov 2026). 23 components. Each spec says what the component is, what the consumer provides, and the do and don't.
Classes live in `nyte-components.css` (prefix `n-`) and use only the variables in `nyte-tokens.css`. Live previews: the NYTE design system artifact.

**Shared rules for every component**

- Square corners (`radius-none`), 1.5 px rules (`stroke-rule`), no shadows, no gradients.
- Text on any colour field is `ink`. Orange is the primary button fill only, one per view.
- Focus: 2 px solid outline in `text`, 2 px offset. Never removed.
- Real data only; sample values labelled "Sample values"; unverified figures bracketed.

## Actions

### Button

Square, 48 px buttons with a 1.5 px rule, set in Pelikan Medium 16.

- **Primary** (`orange` fill, `ink` rule and label, 6.5:1): the single most important action in a view. One per view. Hover fills `ink` with `cream` text.
- **Secondary** (outline): every other action. On Ink sections the rule and label turn `cream`.
- **Text link** (`.n-link`): inline and tertiary actions; underline thickens 1 → 2 px on hover.
- Focus: 2 px solid outline in `ink` (or `cream` on dark), 2 px offset. Never remove it.
- Labels are a verb plus an object, sentence case, five words or fewer ("Start building", "Get the buyer deck"). Never "Learn more" alone. A trailing ↗ means it leaves the page.
- Never put an orange button on a Yellow or Lime field without its `ink` rule; never two primaries side by side.
- Consumer provides: the label, the `href`, and whether it is primary.

## Navigation

### Nav

The Ink bar at the top of every page: horizontal lockup (paper) left, four families (Buy, Sell, Exchange, Trust), Sign in, and one orange primary action.

- Identical on every page; the page's lead colour never enters the nav.
- 64 px tall, `ink` ground, `cream` Medium 15 links. The open family gets a 2 px underline in that family's colour (see NavMenu).
- On Home and Exchange pages the Ticker sits directly under it.
- Below 900 px the links collapse to a 40 px square menu button (the `menu` icon: three stepped lines).
- Consumer provides: the current family, the CTA label and href.

### NavMenu

The open state of a nav family: all four families at once, each titled in its own colour, every item with a one-line description from the live site.

- Ink ground; family title in `lime` (Buy), `sky` (Sell), `purple` (Exchange), `yellow` (Trust) under a 40 × 4 px bar of the same colour.
- Items: `heading-3` name plus `small` description in `ink-2-dark`. No icons.
- Right column: one live figure (the Index in `yellow`) and the primary action.
- Opens on click, not hover; Esc and outside click close it; focus moves into the menu.
- Consumer provides: the families and items (names and descriptions must match the live site).

### Footer

The Ink footer on every page: the stacked, type-only **NY Token Exchange** logo, the four families with their links, and the large globe mark in `#2A2825` (barely lighter than Ink) bleeding to the right.

- Logo: `ny-token-exchange-stacked-paper.svg` (Logos), 140 px wide on desktop, 112 px on mobile.
- Family labels in their colours (Buy lime, Sell sky, Exchange purple, Trust yellow); links `cream` 14 px.
- Legal row: © Lore Hex Corp · Powered by TrustedRouter · Email us · Media kit · Privacy · Terms · Status, then the not-a-securities-or-crypto-exchange line. Both are required.
- On the Ink Token Index board add a 1 px `dark-rule` top border so the footer separates from the page.
- Mobile: one column, links in two columns, globe scales to full width under the links.

### Tabs

Text tabs over a 1.5 px `rule`; the selected tab carries a 3 px underline in the page's lead colour (`purple` here).

- Use for filters on a table (route privacy) and for code languages (TypeScript, Python, cURL).
- Real `role="tab"` buttons with `aria-selected`; arrow keys move between tabs.
- Never boxed tabs, never filled pills. Four tabs or fewer; more becomes a select.

## Graphics

### Band

The stepped stripes: six groups of stripes, 2 · 5 · 8 · 12 · 17 · 22 px thick on a 24 px pitch, thin to thick in the direction of travel. **The groups touch: never leave a gap between one group and the next.** The spacing inside a group never changes.

- Means speed. Once per page, full-bleed, 96–144 px tall: closing the hero or opening the footer, never both.
- Colour: the page's lead colour on Cream; `ink` across the bottom of a colour field (Trust, Models, Docs heroes); a bright colour on Ink.
- Assets: `Patterns/band-<colour>.svg` (1440 × 120, five rows). Scale the whole pattern; never single stripes. Stretch horizontally with `preserveAspectRatio="none"`; keep the 24 px pitch vertically.
- Never curve, rotate, recolour per stripe, add gradients, or run it right to left (except mirrored around the NYTE square in a supply-and-demand illustration).
- Motion: may reveal once, thin to thick, over `duration-reveal`; static under reduced motion.

### Ladder

Equal-weight stripes stacked on a 12 px step. The number of filled rungs encodes a real quantity (a grade, a protection layer, a share of funds) and always sits beside the number it encodes.

- Filled rungs in a colour (or `ink` on a colour field); empty rungs in `stone` on Cream, `dark-rule` on Ink, `lilac` on Purple.
- Index grades use fixed colours everywhere: C `yellow`, B `lime`, A `sky`, AAA `purple`.
- Width: 64–160 px fixed, or full column width.
- Never decorative. If there is no number, there is no ladder.

## Type

### Eyebrow

A 40 × 4 px bar in the page's lead colour over a `label` (Condensed Medium caps, +10% tracking), then a Pelikan Light display headline. It opens every section.

- Bar colour: the page lead on Cream; `ink` on a colour field; any bright colour on Ink.
- Headline emphasis: one phrase in `deep-purple` on Cream ("will all agree on."), or in `lime`, `sky` or `yellow` on Ink. One phrase per headline, never a whole headline.
- Labels are facts ("As of Oct 26, 2026", "In production in three weeks"), not slogans. No dots, no icons.

## Data

### Ticker

A 40 px Ink band of live listings in `ticker` (Mono Medium 13), led by a lime **Live** dot. The only element that moves on its own.

- Sits directly under the Nav on Home, Token Index and Models & Providers.
- Content is real feed data: index level, grade prices, model · operator · price · protections. No invented trades.
- Scrolls left at 60 px/s after a 2 s pause; the list repeats once for a seamless loop. Under reduced motion it is a static, clipped line.
- Never coloured chips, never a second ticker on the page.

### StateChip

One mono chip whose **form** carries the state, so it reads without colour:

- **Verified** — filled `lime`, ink check and text. NYTE checked the evidence.
- **Declared** — outline only. The provider says so; NYTE has not verified it.
- **Ineligible** — filled `stone` (or `dark-rule` on Ink), `ink-2` text and a cross. The route fails a rule.
- **Policy** chips (ZDR, E2EE, CONFIDENTIAL, US-EAST, ≤ $5 / 1M) are outline chips naming a rule or attribute.
- **Estimated vs billed**: an estimate is `~` and `text-2`; the billed amount is `text`, weight 500.
- Caps, Mono Medium 12, +4% tracking, 5 × 9 px padding, square. Never coloured fills for policy chips; never lime for anything that is not verified.

### DataTable

The default way to show offers, providers, attestation values and status. Rules, not boxes.

- Head: `label` style over a 2.5 px `text` rule. Rows: 1.5 px `rule`, 16 px vertical padding; hover fills `sunken`.
- Model cells: name in Medium, id in `data` `text-2` below. Figures in `data`, right-aligned, tabular.
- State in StateChips; never colour a whole row except the one selected route (`yellow-tint`).
- Every table carries a source and an as-of line in `small` below it. Sample data is labelled as sample.
- Under 900 px the head hides and each row becomes a two-column block.
- Consumer provides: columns, rows, the as-of date, the selected row if any.

### LineChart

A line of real prices. One idea per chart.

- Series: `ink` on Cream; the page lead colour on Ink (`yellow` on the Token Index board). A second series uses the lead colour, a third `text-2`. Never more than three.
- 2.5 px line, round joins. One 1.5 px baseline in `text`; gridlines 1 px `rule`. No chart borders, no fills under the line, no 3D, no pie or donut charts.
- Label the line at its end (value and date) in `data`; no legends when labels fit.
- Every chart carries a source and an as-of date. Real data only: where the shape between published points is interpolated, say so under the chart. Never invent market activity.
- Motion: may draw left to right over `duration-data`; complete under reduced motion.

### Stat

A headline figure in `figure` (Pelikan Light, tabular), a `label` under it, and a 10 px colour bar above.

- Use in rows of three or four. Each bar takes a different bright colour (purple, sky, yellow, lime) — this is one of the sanctioned accent hits.
- Figures come from the live site with their qualifier in the label ("Fee · wholesale as low as 0.1% at scale"). Unverified figures are bracketed placeholders: `[N]`.
- On Ink, the figure itself may take the colour instead of the bar (`sky`, `lime`, `yellow`, `purple`), never both.
- No icons on stats.

### LiveGlobe

The rotating dot globe: an orthographic globe of land dots in `ink` on a `cream` disc, a graticule in `ink-2-dark`, the mark's speed lines passing behind it, purple trade pings with deep-purple rings, and an Ink "just now" tag naming city, model, tokens and price.

- Lives on a full `sky` field on Home, with counters (tokens bought today, trades today), three split bars and a latest-trades list (newest row highlighted `yellow`).
- Rotates 6°/s; a ping every 1.6 s. Under reduced motion: one still frame.
- Data is the exchange feed. Until wired, label it "Sample values".
- Source code: canvas board `R4-Web-1-Home` (logic class) — reuse it, do not redraw it.
- Never a 3D render, a glowing globe, arcs between cities, or map pins.

## Trust

### Badge

**NYTE Verified Secure** — the only badge wording.

- Black version (white outline, lime check) on light grounds: Cream, Gray, Stone. White version (lime shield, black check) on Ink.
- Never on a colour field (Purple, Yellow, Lime, Sky) and never inside a coloured box.
- Minimum 64 px tall on screen, 20 mm in print. Size by height only.
- Only where the claim is real and verifiable: next to the routes or layers it describes, with the evidence one click away. One badge per view.
- Never recolour, re-letter, rebuild or animate it. The media kit's "Privacy Secured" seal naming is pending client confirmation; use "Verified Secure" until told otherwise.

### Receipt

The signed inference receipt: proof of one request, verifiable offline.

- `surface` card in a 1.5 px `ink` rule; horizontal lockup and "Inference receipt" label; rows of `label` key and `data` value over 1 px `stone` rules; an Ink footer strip with "✓ signature valid" in `lime`.
- Fields in this order: request, model, provider, region, privacy floor, tokens, cost, gateway build, signature. Unknown values are bracketed placeholders, never invented hashes.
- Shown on a colour field (Purple on Trust) so the white card reads as a document.

## Content

### PhotoFrame

Colour photography of the people behind the models, square-cropped inside a 1.5 px `ink` rule, with a Cream caption tab bottom-left in `label` style.

- Caption: "[Company] · [City]" or the person's name and role — only with their written consent. No invented names on real people.
- Under a launch card, the photo sits on a colour caption block (`purple`, `lime`, `sky` or `yellow`) holding the eyebrow, headline and byline.
- Full-bleed photos (Home hero) drop the side rules and keep top and bottom rules.
- The preview shows a Stone placeholder: this system ships no photographs. See Photography in the brand book for art direction and selection rules.

### LaunchCard

A launch story: photo above, a full-strength colour block below with eyebrow, headline and byline in `ink`.

- Use in rows of three under "The people behind the models". Each card takes a different bright colour; never two the same in a row.
- Headline `heading-2` Regular, byline `small`. The whole card is one link.
- Content comes from real launches with the company's approval; until then, bracketed placeholders.

### StoryCard

The featured founder or customer story: a large photo (7 columns) joined to a `yellow` panel (5 columns) that shares its rule.

- Panel: eyebrow with `ink` bar, `display-3` headline, two-sentence summary, names and the one primary action.
- One per page, near the people section. On mobile the panel stacks under the photo and takes a left rule.
- Facts only, approved by the company; never testimonials written for them.

### Card

Optional. An outline card: 1.5 px `ink` rule, square, 28 px padding, on the section ground. Guide cards add a 6 px colour bar along the top.

- At most one grid of cards per page; never cards inside cards; never shadows.
- Prefer a DataTable or rows when items share attributes. Cards are for short, separate things (guides, filter modes).
- A card may fill with `yellow` or `ink` to contrast a pair (ZDR vs Confidential in Docs).

### CodeBlock

Ink code panels in `data` (Mono 14 / 1.7). Strings `lime`, the key privacy value `yellow`, comments `ink-2-dark`.

- Docs pages only. Never on Home, Trust or in decks as decoration ("dense terminal styling everywhere" is out).
- Language tabs above (Tabs); a copy button top-right using the `copy` icon.
- Examples must run against the real hosts and parameters (`base_url`, `nyte/auto`, `min_privacy`).

## Forms

### Input

Forms are plain and square: a `label` above, a 52 px field in a 1.5 px `text` rule on `surface`, help text in `small` `text-2` below.

- Focus: 2 px `ink` outline, 2 px offset. Error: a 4 px `orange` underline inside the field plus a written message in `text` — never colour alone.
- Placeholders are examples, not labels. Every field has a real `<label>`.
- Search fields show a key hint chip (`⌘K`, `/`) at the right.
- Checkboxes and radios use the native control tinted `ink`.
- Consumer provides: label, name, type, help and error text.

## Page

### PageThemes

How a page gets its identity: one **lead colour** from its family, plus **accent hits** from the other bright colours.

- Families: Market `purple` (Home, Models & Providers, Exchange), Price `yellow` (Token Index, Reserved tokens, Buy), Trust `lime` (Trust, Security, Privacy, Status), Knowledge `sky` (Docs, Suppliers, Providers, launch stories).
- The lead colour takes the hero (as a Field, or as the Band under a Cream hero), the eyebrow bars, selected tabs and one hero-scale element.
- Accent hits from the other three colours: colour bars over stats and guides, colour caption blocks on launch cards, a colour section ground (CTA, live globe, receipt), coloured type on Ink. At least two full-strength colour sections per page besides the hero.
- Tints never ground a section. Cream is one ground among several.
