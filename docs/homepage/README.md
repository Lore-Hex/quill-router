# TrustedRouter application homepage

The active implementation is in the application branch `jasmine/trustedrouter-homepage-port`. The static design repository is a reference, not the remote review target or a second implementation to maintain.

## Remote application review

https://23e0-108-6-58-177.ngrok-free.app/

This serves the actual application homepage from local port 3001 through a read-only bridge on 3002. The default view has no comparison toolbar. Catalog prices/privacy come from the application model directory; search uses the lightweight `/v1/models/picker` endpoint. Other destinations link to the existing public site. Local mutations and development sign-in are not exposed. No deployment, push, or merge occurred. The tunnel requires the local computer and processes to remain running.

The old ngrok address beginning `985c` still serves the reference design on port 3000. Do not present that as the application result.

## Source and preservation

- `templates/homepage/`: approved marketing markup split into application includes, with server-rendered catalog rows.
- `static/homepage/`: original landscape PNG, all responsive AVIF/WebP images, original colored logos, shared fonts/licenses, approved styles and adapted interactions.
- `homepage.py`: reuses the existing public catalog and model-directory price/privacy aggregation.
- `preview-asset-manifest.json`: original asset hashes from preview commit `e510502` on `jasmine/landscape-homepage`. Before subsequent mobile fixes, 29 of 30 files matched after asset URL normalization; only the interaction script differed. Non-catalog section copy and the approved content JSON matched exactly.
- The original preview, Exchange pages, chart experiments, and hosting settings were not changed during the application port.

Enable the new homepage using `TR_HOMEPAGE_LANDSCAPE_ENABLED=true`. It defaults off pending release review. `TR_HOMEPAGE_REVIEW_TOOLS_ENABLED=true` optionally restores the four-font comparison toolbar in local/test environments. Local/test pages retain noindex even with that toolbar off.

## Mobile fix, September 29

Remote inspection found all 11 supporting disclosures expanded because JavaScript responses were truncated. The application caches static file metadata; editing an asset while its process runs left an old Content-Length, causing the read-only bridge to return 502. Restarting only the new application process restored complete script delivery. Restart this process after asset edits until a development-only cache strategy is added. Never change production cache behavior to solve local editing.

Verified remotely at 390×844: all 11 disclosures closed, HTML content retained, page height reduced from 13,790px to 9,460px. Request/migration/four verification visuals/five footer groups remain optional on phones and expanded on desktop.

The hero now captures its small viewport height before body paint, and uses that stable value and stable height classes through phone browser-bar/keyboard height changes. Width/orientation changes recalculate. Reduced-motion behavior is retained. Touch hover no longer pauses/restarts the ribbon during swipes; keyboard focus still pauses it. With toolbar removed at 390×844, hero is 766px high. Marquee document Y remained 746.016px across a height-only change to 700px and page scrolling. This is browser viewport simulation, not a physical iPhone certification.

## Validation and remaining work

Four focused application tests passed and full ruff passed before the latest mobile adjustments. Full pytest was attempted but stopped at collection because the temporary environment lacks `jsonschema`; coverage is unverified. Mypy reported an installed Bigtable API mismatch in unchanged `storage_gcp_mirror.py`; an unused ignore in the new helper was removed. Later reruns were interrupted by the user's instruction to prioritize remote design review. Do not claim all release gates pass. Dependency/lock files were not changed. Do not resume dependency installation without a new reason consistent with the user's direction.

Hero and headings matched the original reference exactly at 390×844, 320×568, and 1440×900 before hiding the optional toolbar. Original layout, artwork and foreground lowering are preserved; removal of the 44px review bar gives that space to the hero. Search (including models outside the old fixture), no-results state, Escape focus restoration, mobile menu, request controls, migration keyboard tabs and catalog filters were checked in the app. Contrast token ratios: primary CTA 12.48:1; muted text 9.58:1; blue text 8.80:1; primary text 18.61:1. Motion rules inspected, OS motion settings not changed.

Remaining release work: complete repository gates in its supported environment, broader route/sign-in checks, review inherited marketing claims, connect approved homepage analytics events, and decide whether to replace illustrative request evidence with captured evidence. Status badges now consume the existing public status feed and remain links to its detail page. This is a working application design for review, not a claimed production release.


## Connected status and pricing — September 29

Navigation now has a status dot and the footer has a labeled status pill. Both fetch same-origin `/status.json`, refresh every minute while the page is visible, and handle operational, degraded, outage, unknown, stale, malformed and failed responses. Freshness checks use the actual probe timestamp and server-reported age; cached responses or returning to a suspended tab cannot retain an expired green indicator. Native links work without JavaScript. The indicators do not animate and accessible names communicate their state independently of color.

The existing read-only review bridge on port 3002 now forwards exactly `/status.json` to the public `https://trustedrouter.com/status.json` feed. Thus the ngrok review and `http://127.0.0.1:3002/` show real public health. Port 3001 remains the isolated memory-store app and truthfully reports status unavailable because it has no monitor samples. Deployment needs no bridge: the application already serves `/status.json` on its own origin. No production storage, configuration, or services were changed.

The pricing card uses the approved GLM 5.3 Flash example, computing min/max input rates from the current application catalog's Credits routes. It reuses model-directory route filtering and customer-price formatting; BYOK records are excluded. The card identifies USD per million input tokens and the standard fee basis, notes route differences, and links to that exact model. At review it showed $0.07385–$2.11, approximately 28.6×; these figures are calculated, not fixed copy. Equal prices omit the range, a free route omits the ratio, and missing model/routes remove the example rather than recycling old values. This uses the running application's catalog version, not an independent browser price feed.

Validation: 8 focused Python tests and 8 Node status tests passed; focused ruff and diff whitespace checks passed. Browser checks at 390×844 and 1440×900 verified remote loading, preserved disclosures, no horizontal overflow and keyboard status-link focus. The new indicators are static; existing reduced-motion rules are unchanged. Full repository release gates remain deferred under the earlier instruction; this is not a release-readiness claim. No dependency installation, push, merge or deployment.


## Copy-led simplification — September 29, latest direction

This entry supersedes the earlier requirement to retain all four verification diagrams and the footer status pill. The designer explicitly asked to reduce visual clutter and increase supporting text size.

- Verification now uses four plain propositions with evidence links, separated by thin rules. The four large diagram/panel visuals were removed. All four source-checklist bullets and the five repository destinations remain visible, with readable link labels.
- Supporting paragraphs are generally 18px desktop / 17px phone; section subheads 22–24px, action links 16px, footer links 16px, and necessary pricing qualifiers 15px. Table names/rates, FAQ answers, migration controls and secondary copy were enlarged. The established hero composition, landscape and viewport stability remain.
- Catalog has one outlined Search models CTA, opening the existing full-catalog search. Removed the competing Browse all models link, implementation-oriented catalog timestamp, and permanent Click to copy hint. Alias copying still reports its result through the existing accessible feedback message.
- Pricing retains all four fee/payment terms and has an outlined See pricing CTA at the upper right (right-aligned below the heading on narrow phones). Removed the budget code sample and price-ratio badge. The current catalog-backed price range remains, with units, standard fee qualifier and model link. The range helper retains ratio calculation for reuse, but the homepage no longer displays it.
- Removed the footer's ephemeral/private slogan, duplicate status pill, and Open source / Attested / Fails closed tagline. The connected status dot remains in navigation; the normal Status destination remains in footer navigation.

There are now seven mobile disclosures: request, migration and five footer groups. They close on phones and expand on desktop; verification copy and source checklist stay visible without disclosures. Desktop verification height measured 778px at 1440×900. Browser checks at 320×568, 390×844, 768×1024 and 1440×900 found no horizontal overflow. The short-phone hero still retains its CTA and marquee inside the first screen. Search dialog keyboard focus restoration passed. Existing contrast palette and reduced-motion behavior are preserved; removed visual panels introduced no new motion.

Eight focused application tests and eight status tests passed; JavaScript syntax and diff whitespace checks passed. Full release gates remain deferred; no package installation, production modification, push or deployment.


## Request path refinement — September 29, latest direction

The request section now shows a simple, static path: Your app → TrustedRouter → Model provider. The heading and short explanation focus on model/privacy requirements and the fail-closed outcome. The four repeated positioning controls, named served/standby/skipped providers, made-up attestation age, token counts and sample charge were removed. The hero's Secure / Smart / Scalable / Savings line is preserved.

An accessible native disclosure, closed at every viewport width, contains illustrative JSON, Copy request and receipt-verification documentation. The three steps remain visible on mobile. There are now six responsive disclosures elsewhere (migration plus five footer groups); this request disclosure is independent so resizing does not force technical details open. All example content stays in HTML.

Desktop and 390px/320px phone checks verified layout, opening with Enter, copying and no horizontal overflow, including expanded JSON at 320px. Eight focused application tests passed; JavaScript syntax and whitespace checks passed. Full release gates remain deferred. No new animation was introduced.

Real public gateway evidence was captured and strictly verified without credentials or billable inference; see `evidence/2026-09-30-gateway/README.md`. This is a dated gateway verification, not a captured model response or signed inference receipt. The homepage remains explanatory and does not present that record as live proof.


## Captured request in the homepage — September 29, latest direction

The designer clarified that the authorized DeepSeek capture was intended to power the diagram. The visible three-step path now shows the actual France question, TrustedRouter routing, and DeepSeek Flash's Paris answer. The response status, token counts and reported cost come from the saved response, with the UTC capture date displayed. An initially collapsed native disclosure summarizes the verified signature, content hashes and selected provider, and clearly distinguishes this TLS route from confidential provider compute. This supersedes the earlier illustrative Hello request. No new inference was made. The API key, internal identifiers, reasoning content and full downloadable evidence bundle are not published. Full capture files remain in the local evidence directory.

The change is frontend presentation of a dated capture, not a live feed or new backend. Full release gates remain deferred under the existing direction; this is not a production-release claim.

Validation: 8 focused homepage tests passed. The captured answer was verified in the rendered localhost and ngrok pages. Desktop 1440×900 and phone widths 390px/320px had no horizontal overflow, including expanded receipt details; Enter toggles the native disclosure with a visible focus outline. The request section has no animation, preserves reduced-motion styling and uses the existing high-contrast palette. Full repository gates remain deferred. The raw evidence directory remains local and uncommitted.


## Request panel restored to the marketing mock — September 29, latest direction

Compared the supplied screenshot and original `Downloads/trustedrouter-homepage-mock/index.html` / `styles.css` (route panel rules) with the generic cards from the prior pass. The prior pass had lost the enclosing panel, compact typographic hierarchy, connected node structure, provider emphasis and boxed response. Restored those visual elements with square borders, static dashed connectors, a monospace model header/metadata, a selected provider outline and a right-aligned cost inside the response node. The answer now belongs to the response, separate from the provider. Shared homepage fonts and readable supporting type remain.

The original mock shows three provider states. The real capture only supports one served DeepSeek route, so this panel retains one selected provider and does not invent standby/rejected routes, confidential compute, cloud failover, SDK use or attestation age. Actual question, answer, response code, usage, cost and verified receipt summary are retained. A date labels the captured evidence; receipt details remain collapsed at all widths.

Validation: localhost and ngrok both show the new panel. Checked 1440px desktop and 390px/320px phones, keyboard Enter/focus and expanded details without horizontal overflow. Muted metadata contrast is at least 8.96:1; proof badge text is 11.55:1. The connector is static and introduces no motion. Eight focused homepage tests and whitespace checks passed; full release gates remain deferred, not claimed passing. Screenshot: `qa/captured-request-panel-desktop.png`. Raw evidence is preserved locally and remains uncommitted. No dependency changes, production changes, push or deployment.


## Connector motion and cleaner panel — September 29, latest direction

Matched the original mock’s 2px dotted connectors, 4px dash/gap spacing and one-second linear flow. The final response connector reverses direction like the mock. Motion is enabled only with `prefers-reduced-motion: no-preference`; reduced motion keeps the lines static. Removed the visible capture timestamp/date and “Not a live feed” footer at the designer’s request. Receipt details identify this as a captured request; the original evidence files retain the exact timestamp.

Verified progressing computed animation positions, one-second duration and reversed final connector on localhost; verified updated assets on ngrok. Desktop and 390px phone checks found no horizontal overflow. Reduced-motion CSS was inspected; OS preferences were not changed. Whitespace checks passed. This CSS/copy change adds no backend behavior; full release gates remain deferred.
