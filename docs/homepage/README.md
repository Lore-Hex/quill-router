# TrustedRouter application homepage

The active implementation is in the application branch `jasmine/trustedrouter-homepage-port`. The static design repository is a reference, not the remote review target or a second implementation to maintain.

## Remote application review

https://fd8d-108-6-58-177.ngrok-free.app/

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


## Verification balance and punctuation — September 29, latest direction

Removed em dashes from homepage copy, attribution, accessible catalog label, page/social titles, optional font-comparison label and backing content JSON. Source comments are not marketing copy. The request explanation now uses two sentences.

Open source now leads at the upper left and spans the three shorter verification panels on desktop. Its four checklist items and all five repository links remain visible. The other propositions stack to the right; panels use the original mock's restrained enclosure treatment, consistent padding and readable type without restoring the removed graphics. Mobile stacks all four in source order. Screenshot: `qa/verification-balanced-desktop.png`.

Audit found an obsolete unguarded Copy request event handler after the earlier removal of its button. Removed that handler; it had prevented the remainder of homepage.js from initializing. Verified model search (Qwen: 119 matches), Escape focus restoration, catalog filtering and TypeScript migration-tab rendering after the fix.

Validation: desktop 1440px and phones 390px/320px have no horizontal overflow; all five repository destinations remain. Rendered localhost/ngrok body copy contains no em dashes. Eight focused homepage tests and whitespace checks passed. Existing contrast palette and reduced-motion handling remain. Full release gates remain deferred, not claimed passing.

Current remaining work: (1) a final visual consistency pass across section spacing, supporting type and actions; (2) connect homepage-specific analytics hooks to the existing event pipeline, whose allowlist/schema currently accepts acquisition/onboarding events only; (3) broader destination/sign-in checks, marketing-claim confirmation and full repository release gates in the supported environment. All nine sections, real catalog search/prices, status feed and captured response presentation are implemented. No new catalog/status/receipt backend is required for this design.


## Whole-page consistency and analytics connection — September 30, latest direction

Completed a visual consistency pass in the actual application. Shared section spacing is now 80px desktop / 56px phone. Catalog rows are more compact without reducing the approved readable text scale. Migration, customer proof and routing-alias enclosures now use restrained 2px corners and the same border tone as the request and verification panels. Secondary actions use blue underlined text; catalog and pricing retain their single outlined CTA. Footer navigation and legal links now align with the closing panel's outer edges. Hero artwork/composition, captured request evidence, all verification propositions/checklists/repositories, and responsive disclosures remain intact.

Pricing terms now form a four-column ruled strip, two columns on tablets and one divided list on phones. Spend controls and the current catalog-backed price comparison use equal bordered panels with aligned desktop action baselines. The price range is separated from its unit and qualifier for easier scanning. Screenshots: `qa/consistency-pricing-desktop.png` and `qa/consistency-pricing-mobile.png`.

The existing ten `home.*` hooks now post event names only to `/analytics/events`, extending its strict allowlist and reusing the current attribution/logger pipeline (`acquisition.home.*`). No new service, storage write or inference path was introduced. Properties remain local custom-event data: search terms, model IDs, clipboard contents and arbitrary metadata are not transmitted. GPC/DNT suppress browser transport and the existing server privacy controls suppress attribution; requests without attribution do not create identifiers. Failed transport does not interrupt interaction. The agent-prompt button no longer also counts as an API-key CTA; the initially open FAQ no longer counts as a visitor opening it. This intentionally supports event counts, not per-model/per-tab breakdowns.

The ngrok bridge remains read-only and still rejects analytics POSTs. Analytics was verified against port 3001, including successful HTTP 204 responses and attributed filter/tab/CTA log events. Remote review serves the revised styles and scripts; its status indicator continues to reflect the public feed (reported delayed during this check). Only the local application process was restarted.

Validation:
- 24 focused Python checks passed (8 homepage + 16 analytics); 13 Node checks passed (5 analytics + 8 status).
- Full repository ruff passed. All three gates were attempted through `uv run --no-sync` with the existing temporary environment, without dependency changes. Mypy still reports the installed Bigtable API mismatch at `storage_gcp_mirror.py:60` (`_RetryableMutateRowsWorker`). Full pytest still stops during collection because `jsonschema` is missing in `test_provider_catalog_receipts.py`. Coverage remains unverified. These are the same environment blockers documented before this pass, not a claim of release readiness.
- Rendered application checked at 1440×900, 768×1024, 390×844 and 320×568 with no horizontal overflow. Desktop price panels are equal height and their action baselines align. All five source repositories remain; no em dashes in rendered copy. Mobile disclosures remain closed initially, desktop migration expanded, receipt independently closed at both sizes.
- Search returned 119 Qwen matches; Escape restored focus to Search models. Catalog filtering, migration arrow-key navigation, receipt Enter/focus and expanded receipt at 320px passed. Local sign-in entry redirects to the existing modal; only MetaMask is configured in this isolated local environment. No authentication was submitted. This does not certify production OAuth.
- Existing contrast colors and reduced-motion rules remain. New links use the established blue palette and visible keyboard outlines. No new motion was introduced; OS reduced-motion preference was not changed.

Remaining release work: complete repository checks and coverage in the supported environment, broader destination/production sign-in checks, and confirmation of inherited marketing claims. Raw evidence remains local and untracked. No main-checkout edits, dependency installation, push, merge or deployment.


## Final UX priorities — September 30, latest direction

Responded to the designer's final hierarchy review. This entry supersedes prior receipt-disclosure, pricing-card, customer-layout and footer-size directions.

- Hero lab icons increased from 24px to 30px desktop / 28px phone, retaining the approved landscape and stable marquee placement.
- Kept the request path as a product explanation. The marketing handoff's DECISIONS explicitly says the hero shows the four promises working on a request and replaces a demo video; SPEC section 5 describes rule-based provider selection, not decoration. The current captured request only demonstrates the observed request/response path, not standby/failover or confidential provider compute. Removed the technical expansion in favor of a brief captured-request/verified-receipt caption and `/docs/receipts` link. Raw evidence remains local and untracked.
- Catalog note now reads: “Customer prices per 1M tokens. ‘From’ is the lowest-priced route. Privacy varies by route.” The previous extra detail was integration wording to distinguish route prices from privacy availability; the shorter wording keeps that distinction without Credits jargon.
- Removed “Everything else stays as it is.” from markup, content JSON and runtime rendering. The useful agent-prompt paste hint still appears only on the agent tab. Code copying remains.
- Four pricing terms now lead in a pale blue panel. Lower budget and route-price details are quieter, smaller and unboxed. Their prices and qualifiers remain application-backed.
- Customer proof uses one mint panel: 170,974 legal documents is the dominant headline; token volume and delivery time are supporting results; the original quote is smaller. One primary case-study link remains. The competing Axios quotation and duplicate customer link were removed; Axios is a small press line below.
- FAQ questions are 17px desktop / 16px phone, answers 16px. Footer uses a darker foundation, 14px desktop links, tighter spacing, and a 240px desktop landscape CTA instead of 420px. All destinations remain; mobile links retain 44px targets. Desktop footer measured approximately 700px total at 1440×900.
- Editorial photo cards would fit a case-study/article destination, not routing controls. No stock images or new resources strip were added; the existing shorter landscape CTA already provides an image-backed action. The handoff deliberately cut the resources row.

Validation: desktop 1440×900, tablet 768×1024 and phones 390×844 / 320×568 checked without horizontal overflow; 320px pricing cells also have no internal overflow. The short-phone hero still fits its CTA and larger logo ribbon inside 568px. Mobile migration/footer disclosures remain closed initially. Search opens and loads 646 models; Escape restores visible focus. Agent/Python tab switching correctly shows/hides the remaining hint. Rendered copy contains no em dashes. New blue-panel supporting text has 7.35:1 contrast and customer supporting text 7.53:1; main pricing text 11.65:1. No new motion; existing reduced-motion rules retained. QA screenshots use the `priority-` prefix.

24 focused Python tests, 13 Node tests, full ruff and diff whitespace checks pass. All repository gates were attempted with the existing environment and no dependency installation: mypy still fails on the installed Bigtable `_RetryableMutateRowsWorker` mismatch; full pytest still stops at missing `jsonschema`. Coverage and release readiness remain unverified.

The old `23e0` ngrok tunnel stopped during this review. Restarted ngrok against the unchanged read-only bridge on 3002; the new verified phone URL is https://fd8d-108-6-58-177.ngrok-free.app/. A first visit can show ngrok's Visit Site notice. Port 3001 still serves the isolated memory-store application; the bridge still blocks POST requests and forwards the public status feed. No production changes, push or deployment.


## Reduced choices and reusable actions — September 30, latest direction

This entry supersedes the prior five-repository presentation, receipt caption, two-column customer story, lower pricing panels and landscape-inside-footer directions.

- Header model search remains a catalog shortcut, with a magnifier and existing keyboard shortcut. Marketing's DECISIONS nav row and SPEC section 4 explicitly explain its purpose. No extra instructional copy. Search hides when desktop navigation needs the room; mobile menu and catalog search remain available.
- Request panel now has one “Captured request” caption. Removed the mixed-style receipt summary/link outside the graphic. The observed gateway/provider metadata stays accurate. Intro explains matching model/privacy requirements without weakening privacy; no invented failover evidence.
- Migration drops the three noninteractive fact chips and duplicate agent/MCP/guide actions. Get your API key is primary; Migration guide is secondary. Agent prompt remains a functional code tab.
- Clipboard icons replace Copy text for URL and code, with 44px targets, accessible labels, visible keyboard focus, checkmark confirmation and live feedback. A regression test checks clipboard payload and restoration of icon/label/enabled state.
- Shared action primitives live in static/homepage/components.css. Import after page styles; use class="button button-primary" for the main action, "button button-secondary" for outlined actions and "icon-button" for familiar icon controls with aria-label/title. All API-key buttons use the same label, 48px minimum height, padding, color and focus treatment. Other pages are not changed.
- Verification retains four propositions and the source checklist. Five repository buttons are consolidated into one View source code destination; source is the lead column and the other claims use quieter ruled rows.
- Pricing retains four prominent pale-blue terms and one short strict-budget sentence/link. Removed the repeated route-price comparison. Marketing SPEC section 9 and DECISIONS explain budgets/cost visibility as buyer concerns; current route prices remain available through the catalog.
- Customer story has one vertical reading path: headline result, supporting metrics/body, one case-study link, then a smaller quote. Removed the extra press line and duplicate attribution.
- A separate closing.html section inside main now owns the full-width landscape CTA (440px desktop, 400px phone). The darker utility footer follows it.

Validation: 24 focused Python checks, 14 Node checks and full ruff passed. All full gates were attempted: mypy still fails on the installed Bigtable _RetryableMutateRowsWorker API mismatch; full pytest collection is blocked by missing jsonschema. Coverage and release readiness remain unverified; no dependency installation. Final browser review at 320px verified equal 48px primary buttons, 44px copy controls, expanded migration without horizontal overflow and the separate closing section. Desktop verification/customer/closing screenshots are saved with the hierarchy- prefix. A 1024px header check found no horizontal overflow. Existing contrast palette and reduced-motion behavior remain.

Remaining work is release validation in the supported environment, final inherited-claim confirmation and broader destination/production sign-in checks. The existing read-only ngrok preview remains https://fd8d-108-6-58-177.ngrok-free.app/. Raw capture evidence stays local/untracked. No push, merge, deployment or main-checkout changes.


## Mobile action alignment, quote and footer polish — September 30

Latest designer direction supersedes the first-open FAQ and underlined action styles. Request copy now leads with benefit and control: “Use the models you need through one API. Control which providers handle your requests and the privacy standards they must meet.”

Catalog search and pricing use the shared secondary button primitive; on phones they follow the heading and align left. Primary API-key buttons remain mint. Standalone reading actions use the shared action-link primitive (16px, 44px target, arrow, underline on hover, visible keyboard outline); navigation, inline prose and functional tabs/filters retain their respective roles. The customer quote has a vertical rule instead of the long horizontal divider; the case-study action no longer has its inherited border underline.

All seven FAQs start collapsed. SPEC section 11 explicitly specified the first open, but neither that section nor DECISIONS provides a research justification for that default. The designer preferred collapsed. Keep the homepage dark for now: SPEC line 49 says dark only, while line 67 lists a theme toggle, an unresolved inconsistency in the original handoff. No unfinished theme selector is added. OpenRouter's official brand-refresh article describes both light and dark experiences; this is context, not a requirement to add an untested second homepage theme.

Mobile footer gaps and padding reduced: at 390px the collapsed utility footer measured 420px, down from 620px. Groups still have 48px summaries and expanded links retain 44px targets. Verified desktop 1440px quote layout and matching secondary button styles; phone 390px catalog/pricing alignment, footer/FAQ Enter toggles and focus; 320px hero and pricing without horizontal overflow. QA screenshots have the polish- prefix. No new motion or palette introduced.

24 focused Python tests and full ruff pass. Required full gates attempted again: mypy remains blocked by the installed Bigtable _RetryableMutateRowsWorker mismatch; pytest collection remains blocked by missing jsonschema. Coverage is unverified. No dependency changes, push, deployment, production edits or raw evidence publication.


## Secondary action presence and mobile customer story — September 30

Designer requested stronger secondary CTAs after the text-action pass. Verify trust now shares the primary hero button's dimensions (180px desktop; equal 135px columns at 320px), retaining a distinct outlined treatment. Routing controls, migration guide, verification evidence links, case study and Talk to sales now use the shared secondary button. Spend controls remains a tertiary text action. Catalog/pricing actions follow their headings on desktop as well as mobile, keeping them in the reading path.

Hero “shows its work” and closing “verifiable” have restrained italic emphasis; the customer quote is italic. Main heading weights remain clear. The mobile customer card retains its result, metrics and case-study button; explanation/quote live in a native Customer perspective disclosure, expanded on desktop and collapsed on phones. It reuses the existing responsive disclosure controller; there are now seven data-mobile-disclosure elements. Content remains accessible without JavaScript. Mobile card measured 345px high at 390px. Added a labeled arrow Back to top anchor in the utility footer. Header navigation restructuring remains deferred at the designer's request. Social icons with accessible names/visible labels are a future option, not a reason to obscure navigation now.

QA: desktop 1440px hero, verification, pricing and customer story visually reviewed; mobile 390px customer collapse/Enter expansion and 320px balanced 48px-height hero buttons/no horizontal overflow verified. Back to top returned scroll position to zero. Updated preview verified remotely. Screenshots use cta- prefix. Full ruff, 24 focused Python checks and 14 Node analytics/status checks pass. Full gates attempted: mypy still fails on installed Bigtable _RetryableMutateRowsWorker mismatch; full pytest collection still lacks jsonschema. Coverage remains unverified. No dependency changes, push or deployment.

Production readiness: event-name analytics already uses the existing first-party endpoint with GPC/DNT safeguards; public status is already wired with stale/failure behavior. Remaining release work is supported-environment full checks/coverage, authentication and linked-destination validation, inherited claim confirmation, deployed event/status smoke checks and release workflow approval. Preview bridge intentionally rejects analytics POSTs; it is not an analytics production test. Navigation/mega-menu work, light mode and broader design-system adoption are later iterations.


## Adjacent desktop CTAs and quieter supporting actions — September 30

Removed directional arrows from boxed CTAs. Verification now has one outlined View source code action and three tertiary evidence links; the lead source column remains the entry point. Catalog/pricing buttons sit 24px beside their headings on wide screens, wrapping naturally when needed and remaining below/left on phones. Back to top is now a 44px accessible arrow-only control in the footer utility row, with a tooltip and visible keyboard focus; it no longer occupies a dedicated row. Desktop 1440px catalog/pricing/verification and 320px phone pricing/footer reviewed. No boxed CTA retains a text arrow. Back-to-top navigation returned scroll to zero. Eight focused homepage tests and diff checks pass. Per the user's request for minimum local progress, did not repair dependencies or rerun the known-blocked full gates this presentation-only pass. Full CI remains required before release.

Release planning only, no agents/chats dispatched: repository CI runs on PRs targeting main, pushes to main and manual dispatch. It creates frozen dependency environments, runs ruff/mypy and six test shards, combines coverage and enforces 70 percent. Deployment checks successful CI for its exact commit. Recommended parallel work later: CI validation, links/sign-in/claim review, and analytics/status release-readiness review. Actual deployment waits for these outcomes and post-release smoke checks follow rollout. Local dependency repair need not precede a CI attempt; a CI failure still requires investigation. No push, PR, workflow dispatch, merge or deployment performed.


## Production activation configuration — September 30

The designer authorized activation in PR #1422. Both scripts/deploy/public_surface.sh (the public website) and scripts/deploy/rollout.sh (the combined service) now explicitly set TR_HOMEPAGE_LANDSCAPE_ENABLED=true. This supersedes earlier notes that a separate activation decision is outstanding. The setting default remains false outside these deployment configurations; alternate brands retain their existing homepage. No live config was changed and no merge/deployment was performed.

## CI repairs and remaining release work — September 30, afternoon

The failed CI run 36666560181 had two root causes: exact public deployment environment expectations did not include the newly enabled homepage flag, and generated public OpenAPI assets did not include the new analytics-event enum values. Both normal and post-cutover suites repeated these failures. Coverage correctly refused to certify an incomplete test run; this was not evidence of coverage below 70 percent. Updated the branch onto current main without conflicts, corrected the expected production flag, and regenerated JSON/gzip with scripts/generate_public_openapi.py. The semantic schema diff is limited to MarketingEventRequest.properties.event.enum. No user design/account changes are required to resolve these failures.

Remaining work and why it matters (not implemented in this CI repair):

- **Before release: qualify the 5.5% pricing claim.** That rate applies to standard prepaid text/embeddings and has a $0.01/M token floor; signed-receipt requests and video have different pricing. The unqualified headline can misstate what a customer will pay, especially beside a signed-receipt example. Keep the four-term layout and add a compact qualifier rather than another section.
- **Before release: replace cryptocurrency with stablecoin.** This matches the implemented/published checkout rail; the broader term suggests support for assets the product may not accept.
- **Recommended before release: wire existing homepage JS tests into CI and add an enabled-homepage browser smoke.** Existing CI browser startup leaves the redesign flag off, so a green browser job covers the old homepage. The two new Node suites are not invoked by CI. Running them in the existing Node-enabled job covers privacy transport and stale-status regressions cheaply; a separate flag-on browser smoke should verify search, responsive disclosures and sign-in entry without removing legacy coverage.
- **Non-blocking polish: restore signed-in header labels.** New anchors omit the selector used by existing auth-aware chrome, so authenticated users can still see Sign in/Get your API key instead of Console. Links still work; this is a presentation regression.
- **After rollout: confirm live analytics/status and authentication.** Source review and local preview cannot certify production OAuth, event ingestion or current operational health. Keep the reviewed release gates intact and perform these checks after a successful rollout.

The old temporary application runtime and ngrok tunnel had disappeared. Restored an isolated runtime and read-only bridge; new verified preview: https://ee74-108-6-58-177.ngrok-free.app/. The full locked dev environment cannot install on this Intel Mac because the optional document-analysis stack requires unavailable Torch wheels. The temporary preview/test runtime excludes docling/sec-parser and uses compatible cbor2/cryptography wheels; repository dependency files are unchanged. CI remains the authoritative full locked-environment validation. Preview GET returned 200 and POST returned 405; no raw private evidence is served.

Repair validation: 26 focused deployment/OpenAPI tests passed, the generator's --check passed, full Ruff passed and git diff --check passed. Full locked-environment tests and coverage are delegated to the new CI run; no claim of full local validation. The rebased branch is pushed with an explicit lease on the previously reviewed remote head to protect concurrent work.

## Returning-user compatibility — September 30

Restored data-action=open-signin on the new header and three API-key links, reusing existing dashboard auth-aware chrome and modal handling. Existing tr_signed_in hint changes labels to Console/Open console while real authorization remains enforced by the session-backed destination. Shared dashboard code is unchanged. Homepage CTA analytics now uses delegated clicks because shared auth-aware chrome replaces anchors; direct listeners would disappear for returning users. Added a regression suite exercising actual markup and shipped shared auth functions, including signed-out labels, signed-in replacement, preserved styles/destinations and CTA analytics. All 17 homepage Node tests now run in the existing CI browser job with no new dependencies. Pricing/payment copy remains unchanged pending the product wording decision. An enabled-homepage browser smoke remains a separate recommended check; the new Node step does not claim to provide browser coverage. Rollback via the feature flag still requires a reviewed configuration rollout.
