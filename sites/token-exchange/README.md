# Regional Token Exchanges

**Adding a city or region? Start with [ADDING-MARKETS.md](ADDING-MARKETS.md).**
Developers and coding agents should use that guide to preserve the approved
shared design, regional copy, evidence scope and social-image process.

Static enterprise acquisition sites for 13 markets and 30 owned domains.
The global site is **thetokenexchange.com**; New York is **nytokenexchange.com**.
`markets.json` is the source of truth for canonical hosts, aliases and local copy.
Its `scope` field records a city, region or global audience. The header market
picker and footer share one ordered directory and highlight the current market.

## Architecture

- Public GCS bucket `quill-token-exchange-public`, containing only generated site assets.
- Cloud CDN backend bucket `token-exchange-static`.
- Existing global HTTPS load balancer, address `35.241.14.18`.
- URL map matchers prefixed `exchange-`. Other host rules, services, gateway
  routes and certificates are preserved. Normal four-service deploys preserve
  unrelated matchers through `scripts/deploy/service_surface_url_map.py`.
- One canonical root per market; alternate names and `www` redirect permanently,
  retaining query parameters. Canonical URLs, sitemaps and OG images are generated.
- Buyers request the brochure on the page. The form posts to TrustedRouter's existing
  email-gated `/token-exchange/brief` action, which accepts the canonical exchange
  origins; submitted email addresses are never stored in browser analytics.
  Suppliers use the existing provider marketplace application. Credentials are
  handled separately by that onboarding flow.
- Static pages fetch the public `/token-exchange/evidence/{profile}.json` feed
  from TrustedRouter on load and every minute. There is no direct Spanner
  access, inference, cookie, or third-party pixel on these sites. The brochure form
  submits a resource request; NYTE `/buy` can also post a bounded card reservation request. The call wizard keeps request
  fields in memory and hands them directly to Calendly. Allowlisted UTM fields pass to intake. The first-party
  `/token-exchange/events` collector records visible visits and ten-second
  engagement. A random 30-minute per-tab identifier joins brochure downloads
  within the same exchange domain, with GPC/DNT opt-outs. The growth worker
  exports these metadata-only events to `trustedrouter-marketing` in Axiom.
  This is not cross-domain user or payment tracking. Deploy the collector,
  worker, and exact-event logging sink before the static tracking asset.

The HTTPS proxy also retains independent flagship certificates
`token-exchange-global-20260920` and `token-exchange-new-york-20260920` for each
site's apex and `www`. These isolate their availability from the larger regional
certificate batches. Preserve them on future publishes. A hostname marked
`ACTIVE` inside a batch is not sufficient: the certificate itself must be
`ACTIVE`, and a normal public HTTPS request must pass without disabling validation.

## Build and Test

### Spot exchange API

New York's `/confidential#start` includes the quickstart from `nyte-v2/api.html`, using
`api.confidential.nytokenexchange.com/v1` with explicit confidential routing.
`api.nytokenexchange.com/v1` is the general catalog endpoint. Both use the
existing TrustedRouter key, balance and request semantics; `nyte/*` aliases
canonicalize to `trustedrouter/*`. Account management stays on trustedrouter.com.
The documentation is at https://trustedrouter.com/docs#nyte-api.

These API names terminate inside quill-cloud-proxy's attested gateway. Its DNS
reconciler publishes each name only after TLS and attestation qualification,
including the confidential policy probe where required. Website deployment
does not create API records or terminate prompt traffic. Preserve the website's
apex/www routing and Certificate Manager authorization records. Publish the
quickstart as live only after both API origins pass production tests. Verify
strict TLS-bound attestation, receipt-key evidence, missing/weaker privacy
rejections and a completed streamed `nyte/auto` request. Certificate issuance
also requires the enclave's existing TXT-writer role to cover the two delegated
NYTE challenge records; see quill-cloud-proxy's `docs/nyte-api.md`. Never publish
DNS records to bypass qualification.

The redesign is built from `nyte-v2/` through `nyte_v2.py`. Its pages are
New York's homepage, `/confidential`, `/trusted-cowork`, `/suppliers`, `/providers`,
`/index`, `/buy`, `/reserved-tokens-terms`, and `/neocloud-accelerator`; its shared
assets are under `assets/nyte-v2/`. Back up live objects before an authorized
release and preserve concurrent content and unrelated market pages.
The NYTE Confidential API page loads `assets/nyte-v2/live-evidence.js`, a
byte-identical build copy of the approved evidence renderer. A New York-only
release must not overwrite `assets/live-evidence.js`, which other markets use.
The Token Exchange Sites workflow builds and tests but does not publish. A
redesign release must publish its complete HTML and asset dependency set,
including both extensionless and `.html` page objects. Do not preserve obsolete
live HTML by applying navigation-only replacements when deploying the approved
redesign. `/index.html` still maps to `new-york/index.html`, not the Index page.

### NYTE Token Index and open-source supplier stack

`https://nytokenexchange.com/index` uses the same published data and chart assets
as `https://trustedrouter.com/index`. The build reads the public literal-data
module `src/trusted_router/content/token_index.py` and copies its public chart
assets unchanged. It makes no database or network calls. Keep its snapshot date
visible; the index is provisional and is not an executable supplier quote.
`/index.html` remains the homepage; do not publish the index page at that path.

New York's navigation links `/index`. Its `/suppliers#open-stack` section invites
operators to list spare inference capacity under their provider identity and
links the Apache-2.0 neocloud stack and existing supplier onboarding.
The stack is pre-alpha; confidential serving requires independent hardware
and software qualification, not merely installing the software.

Run `node sites/token-exchange/verify-index.cjs http://127.0.0.1:8089`
alongside the existing browser checks. The Index now uses the NYTE shared shell;
its release also needs the matching `assets/nyte-v2/` and `assets/analytics.js`,
in addition to `assets/index-page.css`, `assets/token-index.css`,
`assets/token-index.js` and `assets/token-index/`. Publish extensionless page
objects (`index`, `confidential`, `suppliers`) as `text/html`. Back up live objects,
preserve unrelated markets and concurrent content, and use generation
preconditions on every upload.

Release the public catalogue JSON/CORS support before the redesigned homepage:
`deploy.py` checks that `/v1/models/count` and `/v1/providers` are readable from
the NYTE origin before publishing. The `nyte.*` analytics events also require
the collector and the separately dispatched marketing-worker/source-filter
update in `.github/workflows/deploy-growth-sync.yml`. Local preview success
does not verify an authenticated confidential inference request or its attestation.

### Trusted Cowork and shared navigation

`/trusted-cowork` and `/trusted-cowork.html` use the shared NYTE shell and
`assets/nyte-v2/cowork.css`. Keep Trusted Cowork, Reserved tokens and Neocloud
accelerator in the navigation on every NYTE page. The shared navigation collapses
at 1280 px and below, including its no-JavaScript fallback.

The Apple silicon and Intel Mac download links emit `nyte.cta_clicked` with
`download_apple_silicon` or `download_intel_mac`, plus `nyte.outbound_clicked`
for GitHub. The closing download section uses `position=close`; both landing
and download events retain `/trusted-cowork` through the collector and growth
export. These events share the existing privacy controls and bounded metadata.

### Reserved tokens flow (local implementation; publish separately)

`/buy` and `/buy.html` use the NYTE shell and four steps: reservation unit,
underlying and timing, optional contact prefill, then a 15-minute Calendly call.
Step 1 is a plain radio list: “1 billion tokens (a block to draw on over a term)”,
“300 tokens per second (a reserved rate)”, and “Not sure yet”. A short seller
line links to `/suppliers`, `/providers` and `/neocloud-accelerator`; keep these repository links intact
when reconciling live-link rewrites. Defaults are one block, 300 tok/s, a
one-month term, next month in local time, and NYTE Index grade A. Allowlisted
`grade` and `unit` query parameters override their defaults. Allowlisted
`facility=solar|underwater` opens step 2 with that planned facility selected. Three Continue
clicks reach scheduling without editing a field; contact details can be entered
in Calendly, and a supplied email is validated before handoff. Index-grade token blocks show blocks × the
published dated grade price; throughput, named models and undecided requests
show no price. These call-request estimates are not orders or executable quotes.
The separate card panel below uses the authoritative discounted quote and fixed
2027 window described below. Capacity derivatives remain roadmap offerings.

The call wizard never submits contact fields to TrustedRouter or writes request data to browser storage. Card checkout submits only grade, lots and explicit terms acceptance.
Only step, unit, grade and the allowlisted facility ID are written to page history. Contact details and the
request summary go directly to Calendly's iframe/link prefill, without its widget
script. Without JavaScript, the offer, editable model/provider text fields and
direct booking link remain available; the start month is optional. With JavaScript,
it defaults to next month in local time and remains editable. At 1000 px and below, CSS
places the wizard before the brand panel; the DOM retains the introduction and
its h1 before the wizard headings for screen readers. Inactive underlying
fieldsets are both hidden and disabled.

`nyte-v2/app.js` shares the anonymous, bounded catalogue fetch helper with the
homepage: `/v1/providers` and `/v1/models/count` remain on trustedrouter.com;
model IDs use `https://api.nytokenexchange.com/v1/models`, which is CORS-readable
from the NYTE origin. Both model/provider fields accept free text if fetching
fails; a provider preference is subject to model availability on the call.

Run `node sites/token-exchange/verify-buy.cjs http://127.0.0.1:8089` against the
local build (set `PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH` to use system Chrome). It intercepts external services, covers each buyer path and no-JS
fallback, and writes screenshots to `/private/tmp/nyte-buy-shots/`. Calendly in
those captures is an explicitly labelled fixture, not a real booking.

The reservation analytics are `nyte.buy_step_1_viewed` through
`nyte.buy_step_4_viewed` and `nyte.buy_completed`, with bounded unit,
underlying-kind (`index`, `model`, `facility`) and grade values. Facility events
use `unit=blocks` and `grade=none`, without facility names. Completion means reaching scheduling, not a
confirmed booking. Release the collector/log-field changes and dispatch the
existing `deploy-growth-sync.yml` workflow (which derives the exact source
filter from `scripts/axiom_growth/model.py`) before publishing the static assets.

A narrow content release needs `buy`, `buy.html`, `assets/nyte-v2/buy.css`,
`assets/nyte-v2/buy.js`, `assets/nyte-v2/app.js`, `assets/analytics.js`,
`assets/index-page.css`,
`new-york/index.html`, `index`, `confidential`, `confidential.html`, `suppliers`,
`suppliers.html`, `providers`, `providers.html`, `trusted-cowork`, `trusted-cowork.html`,
`neocloud-accelerator`, `neocloud-accelerator.html`, and `new-york/sitemap.xml`.
The existing NYTE shell pages need the new nav link and updated shared-asset
versions; the homepage also needs its primary CTA, and `/index` its four grade
CTAs. `/index.html` is still the homepage route, never the index-page object.
No other market HTML or existing unchanged styles, photos, charts or fonts need
republishing. `deploy.py publish` is a full-build rsync plus infrastructure
checks, not a narrow publisher: do not use it for this set.

Back up every existing target's bytes, metadata and generation before an
authorized upload. Compare freshly downloaded live HTML/assets with the reviewed
base; if live content differs, apply only the reviewed navigation, CTA and asset
changes to those fresh bytes. Preserve unrelated edits and versions. Upload each
object with its observed `if-generation-match` (0 for a new object), assets
before HTML, sitemap last, and `Cache-Control: public,max-age=300`. Use
`text/html` for all page objects including extensionless names, `text/css` for
CSS, `text/javascript` for JS, and `application/xml` for the sitemap. A generation
conflict requires a fresh download and reconciliation, never an unconditional
retry. Preserve DNS, certificates, URL maps and other bucket objects. Verify the
public routes, aliases, navigation, catalogue CORS, Calendly handoff, and privacy
signals after the separately authorized publish.

### 2027 card reservations (local implementation; publish separately)

Step 2 of `/buy` offers card checkout only for index-grade 1B-token lots, all
four grades AAA/A/B/C. The existing call path remains available for every
selection. Specific models, throughput and facilities have no card option.
The fixed card window is January 1 through December 31, 2027, with unused tokens
expiring December 31 and a full refund on request within 30 days of purchase.
The first successful-payment webhook records the purchase time durably; retries
preserve it. The ops notification includes that time and the 30-day deadline.
After 30 days, the only exit is NYTE’s resale program. NYTE is the only buyer of
listed lots; holders do not sell to or contract with each other, and there is no
peer-to-peer transfer. Reservations are transferable
only through that program: holders list lots, NYTE resells them to new buyers ahead
of new inventory, and each sale returns the resale price minus a 5% resale fee to
the original card, capped at what the holder paid for those lots. Until self-service
resale launches in a separate PR, refund and resale requests go to
`enterprise@trustedrouter.com`. The terms version is `2026-10-27-v3`.
Delivery is through a TrustedRouter account via the NYTE API. The terms live at
`/reserved-tokens-terms` and its `.html` alias, in the shared NYTE shell and New
York sitemap. These are prepaid inference services, not securities or investments.

The anonymous actions are `GET /token-exchange/reserve/quote?grade=&lots=` and
`POST /token-exchange/reserve/checkout` on trustedrouter.com. They reuse the
brief's canonical-origin CORS policy. Checkout uses the brief rate limiter;
quote reads use the bounded metadata collector limiter so editing the quantity
does not consume the five-per-hour checkout limit. No customer email is posted
by the page: Stripe-hosted Checkout collects it.

The quote returns the current discount, snapshot date, per-lot cents and total.
The page computes the discounted cents from its published index and the returned
discount, checks agreement with the authoritative quote and refuses stale pages.
This avoids embedding a potentially stale discount in a static build. The server
recomputes the authoritative price at session creation from `content/token_index.py`:
`ROUND_HALF_UP(index USD × 100 × (10000 − discount_bps) / 10000)`, then multiplies
by lots. There are no credit top-up processing-fee line items, promotions or
automatic tax additions; the exact displayed amount is charged. Stripe's final
checkout price is the price locked at purchase.

Configuration, kept dark in `scripts/deploy/rollout.sh`:

- `TR_RESERVED_TOKENS_CHECKOUT_ENABLED=true` enables sales.
- `TR_RESERVED_TOKENS_DISCOUNT_BPS=1000` records Joseph’s approved 10% discount.
  The checkout flag remains `false`; an absent discount still refuses sales.
- `TR_RESERVED_TOKENS_MAX_LOTS_PER_CHECKOUT` defaults to 100. Larger orders go to
  the call. Stripe amount limits also fail closed.
- `TR_RESERVED_TOKENS_MAX_PRICE_AGE_DAYS` defaults to 45 and must be at least 1.
  Invalid, future, or older snapshot dates (UTC) refuse quotes and checkout.
- The existing `TR_STRIPE_SECRET_KEY`, signed webhook secret and SES configuration
  are reused. The actions surface may now hold the Stripe key, but still no
  webhook signing secret, credit store or other billing credentials.

Before enabling, confirm Stripe's account settings for this 2027 pre-sale, customer receipts, statement descriptor and tax
handling, and confirm the existing webhook receives `checkout.session.completed`.
Keep release gates intact; this local implementation does not authorize publish.
The approved discount is already in config-as-code. Release both action paths
in the Python/Terraform URL maps, backend, growth worker/source filter and static assets/terms/sitemap together in their reviewed workflows.

The signed webhook intercepts `kind=reserved_tokens_2027` before all credit
handling, even if a session contains workspace/refill metadata. It verifies the
paid USD amount against locked metadata, logs `acquisition.reserved_tokens_recorded`
and emails the existing enterprise inquiry recipient: `TR_PARTNER_INQUIRY_EMAIL`
(`joseph@jperla.com` in rollout), falling back to `enterprise@trustedrouter.com`
just like the enterprise brief. No buyer email/name is included. Stripe remains
the order record; no token allowance, current balance credit or fulfillment is
created. Full refunds requested within 30 days of purchase are handled manually
in Stripe.
Checkout Session and PaymentIntent metadata lock `refund_window_days=30`, the
NYTE-only transfer policy, resale priority, 500-basis-point fee, original-card
refund method and original-lot-price cap; there is no fixed `refundable_until`.
The server computes `refund_deadline_at` from the signed Checkout Session’s
`created` timestamp plus 30 days, never webhook arrival time.

Every completed purchase is a separate position. Its stable ID is `nyte_pos_`
followed by the full SHA-256 hex digest of `nyte:reserved-position:v1:` plus the
Stripe Checkout Session ID. Domain separation avoids reuse across identifier
schemes; Stripe’s high-entropy session ID prevents guessing without adding a
rotating secret or a mapping store. The ID is an order reference, not an access
credential. It and the computed refund deadline appear in the structured log
and the ops notification email (the ID also appears in the subject). Retries and
different event IDs for the same session preserve the position ID. Stripe receipts
remain the buyer’s reference and operations can reconcile them with that email.
The static success page has no authenticated order lookup, so it stays generic:
no position/session ID or order details are added to the success URL or analytics.

Notification leases and completion markers are persisted by session ID in the
isolated `reserved_notification` entity kind, with an existing webhook dedup key
for the log. Concurrent/repeated events do not send another completed notification;
unpaid or invalid payments cannot claim it. Failed sends return a retryable 503;
an abandoned lease expires after 120 seconds. SES offers no idempotency key, so a
crash after email acceptance but before committing the sent marker can repeat an
email on retry. This cannot duplicate a charge or create credits. Stripe delivery
retries/manual replay recover temporary notification failures.

`nyte.reserve_checkout_started` carries only bounded unit/index/grade values.
`nyte.reserve_checkout_succeeded` is a browser-return funnel signal with no order
details; it is not payment verification. Both honor GPC/DNT and flow through the
collector, structured log fields and growth source filter. Only the signed
webhook establishes the paid reservation.

### Neocloud accelerator (local implementation; publish separately)

`/neocloud-accelerator` and `/neocloud-accelerator.html` render the NYTE shell
with page key `accelerator`. The navigation follows Reserved tokens; `/buy`’s
seller line and `/suppliers#open-stack` link here. Only New York’s sitemap lists
it. The page describes capacity reservations for inference service from planned
facilities: 1B-token lots of Grade B · Professional (DeepSeek-class models),
with delivery over the facility’s first twelve months of operation, not per month.
The sustained-rate equivalent is stated once per page. `facilities.py` contains
Joseph’s approved facts and renders the same cards on `/buy` and the accelerator.
Solar Powered Datacenter is planned with a targeted February 2027 launch;
Underwater Datacenter is planned with a targeted August 2027 launch. Power is a descriptor only, never a unit for sale.
Facility selection in `/buy` disables throughput and undecided units, hides editable
timing, caps quantity at the first offering, and hides all prices, totals and as-of
captions. Pricing is discussed on the call. Operator names are shared on the call.
Calendly receives the planned facility, targeted launch and lot count with the
twelve-month definition, without a price.

The linked Neocloud-in-a-box repository is the same Apache-2.0 stack referenced
by suppliers. It is pre-alpha. Operators handle power, permits, the building,
cooling and physical security. Self-run software and a discussion of remote
management are offered; remote management is not a live product. Hardware and
operating responsibilities are scoped together. Confidential serving still needs
independent hardware and software qualification.

All content and the plain Joseph Calendly link work without JavaScript. There
is no form, embedded booking widget or contact prefill on this page. The provider
deck remains request-only through `/providers`. Preserve the verbatim planned
offerings and not-a-securities language; use only Joseph’s approved facility facts in `facilities.py`; do not add funding
amounts, additional facilities or dates, service commitments, prices or assured outcomes.

A separately authorized narrow release needs both accelerator page objects and
`assets/nyte-v2/accelerator.css`, the shared stylesheet and `app.js`, every NYTE shell page
for the navigation update, `/buy` and `/suppliers` with their aliases for the
entry links, and New York’s sitemap. Use the backup, reconciliation and generation
preconditions above. These local changes do not authorize publishing.

### Public New York media kit

`https://nytokenexchange.com/media-kit` (also `/media-kit.html`) is the public
press resource. `press.json` is the shared source for page copy and the TXT/JSON
fact sheets. `press.py` builds a deterministic ZIP from an explicit public-asset
allowlist. It never includes the private commercial decks or the large demo MP4.
The video remains available separately at its existing public URL.

`press-assets/` holds the approved-mark derivatives (outlined SVGs and transparent
PNGs) and an unmodified 1920 x 1080 homepage capture from October 15, 2026.
Regenerate the logos with `uv run --no-project --with fonttools --with brotli
sites/token-exchange/export_press_logos.py` and `rsvg-convert` installed.
The normal build uses the checked-in exports and has no new dependencies.
The social card and video still reuse the existing public source assets.

Run `node sites/token-exchange/verify-press.cjs http://127.0.0.1:8089` after
building and serving the site. It checks 320/390/768/1440px layouts, images,
clipboard success/denial, FAQ, ZIP download, video metadata and no-JS access.

Publish only `media-kit`, `media-kit.html`, `assets/press.css`, `assets/press.js`,
the generated `assets/press/` allowlist, and New York's sitemap and footer link.
Back up existing objects and use generation preconditions. Both page objects
need `text/html`; all objects use a five-minute public cache. If live homepage
HTML differs from the build, change only its existing media-kit link, preserving
all other bytes and asset versions. Do not publish unrelated market/homepage
changes or alter DNS, certificates, routing, or inference services.

Keep company facts free of invented funding, launch dates, office locations,
customer endorsements or performance figures. Privacy properties are scoped to
routes; roadmap capacity derivatives are not live products. For interviews and
approved founder photography, the existing press contact is
`help@trustedrouter.com`.

### Private provider deck request page

`https://nytokenexchange.com/providers` is a public, request-only landing
page, rendered from `nyte-v2/providers.html` with the shared NYTE header,
footer, and design tokens. `/suppliers` is the supply-side overview; `/providers`
is its private-deck request destination. Its email link opens a request to `providers@trustedrouter.com`; it does
not submit a form, automatically send mail, or deliver a download. The team
reviews requests and shares the deck privately. Never add the confidential PDF,
slide images, or a publicly resolvable deck URL to the build or storage bucket.
The build includes this page in New York's sitemap only. Existing hosting's
default static route serves the HTML without DNS, certificate, or URL-map changes.

The October 10 replacement is seven pages (SHA-256
`3c8b888b92d1f63d64179a3927b845a03d766aa07aa963bd7359e7ffa3cf4849`).
It stays in the private outreach workflow, never in this source tree or build.
The request is reviewed manually; it is not an automated attachment-delivery flow.

For a narrow update, publish only the built `providers` and `providers.html` objects and
`new-york/sitemap.xml` and required `assets/nyte-v2/` styles/scripts after the
repository and site checks. Do not sync a stale
full build over other markets. Both objects use a five-minute public cache.

### NYTE Privacy Secured seal

`https://nytokenexchange.com/badge` is the canonical badge builder and usage page.
The shared `/providers` and `/badge` pages also work on the regional domains via
their existing default static routes. Every market links the badge in its buyer
section and the private deck in its supplier section; only New York lists these
pages in its sitemap, avoiding duplicate canonicals.
Keep `.html` aliases for older links. Extensionless objects require `text/html`.

`member_badges.py` generates two script-free SVGs in `assets/badges`: light and
dark lock-based “Privacy Secured · Confidential AI” seals. There is no provider
badge or role selector. NYTE requires confidential AI routes (owner-confirmed
product requirement); this is not an optional buyer tier. Enterprise buyers display
the seal to customers on NYTE-powered AI features or clearly scoped trust centers.
The linked page documents the required attested gateway, confidential routing floor,
verified confidential compute, provider-side E2EE, explicit ZDR and fail-closed behavior.
It distinguishes provider-side E2EE from client TLS termination inside the attested
gateway. The static image is not live verification or whole-product certification.
This website change documents the requirement; it does not change routing enforcement.
The builder previews, downloads, and copies accessible HTML linking to that scope.

For this content release, publish only the two shared pages and their aliases,
`assets/badge.js`, the two enterprise badge SVGs, and the 13 market
`index.html` files (buyer badge link and video). Retire the two old provider SVG
objects only after backing them up and recording their generations. No DNS,
certificates, URL maps, backend routes, PDF, slide images, or unrelated assets change.
Compare generated homepages with live objects first. If main includes unrelated
unpublished changes, apply only the buyer badge placement and demo to freshly downloaded live
HTML, preserving existing CSS/JS versions and all other content. Back up each
target, verify that removing the inserted line restores its original bytes,
and publish with the observed generation precondition to reject concurrent edits.

### Homepage routing demo

`media/nyte-routing-demo.mp4` is the user-supplied October 12 video, unchanged
(85.95 seconds, H.264/AAC, 1920×1080; SHA-256
`869f48547224c9524d556aed0cca77d09bd241866a7efaaf0410d24f317907de`).
`media/nyte-routing-demo-poster.jpg` is a
1280px still from that video. The build copies both into `assets/`. All market
homepages show native controls with `playsinline` and `preload="none"`, with no
autoplay. Captions are already burned into the supplied video. Publish the MP4
as `video/mp4` and the poster as `image/jpeg` before publishing homepage links.
Verify byte-range playback and mobile layout; the original video is not edited.

### Commands

```sh
python3 -m unittest discover -s sites/token-exchange -p 'test_*.py' -v
python3 sites/token-exchange/build.py --output /tmp/token-exchange-build
python3 -m http.server 8089 --bind 127.0.0.1 --directory /tmp/token-exchange-build
# In another terminal, with Playwright installed:
node sites/token-exchange/verify.cjs /tmp/token-exchange-build
```

The browser check exercises every market at 320, 390 and 1440 pixels, tests
overflow, loaded images, FAQ interaction and attribution, and verifies the saved
PNG social previews. Complete the visual review described in the market guide.
Inspect `/tmp/exchange-global-390.png` and
`/tmp/exchange-new-york-1440.png` before publishing.

## Launch Runbook

Use an explicitly selected authorized GCP account with `CLOUDSDK_CORE_ACCOUNT`.
The deployment service account can manage DNS and static hosting but currently
lacks `compute.sslCertificates.create`; certificate provisioning needs an
authorized operator. Do not broaden service-account IAM as part of a site deploy.

1. Inventory registrar ownership and **all** current records in Firefox,
   including custom subdomains, email records and DNSSEC. Read the authoritative
   zone if the registrar delegates to another service. A public apex query alone
   cannot discover all records. Save a deployment state directory outside git.
2. Run `deploy.py inventory --state /path/to/state`. It refuses to overwrite an
   existing backup. Inspect the saved DNS snapshot against the registrar UI.
3. Run `deploy.py dns --state /path/to/state`. Existing conflicting Cloud DNS
   records and active DNSSEC delegations fail closed. Add any additional
   registrar records to the target zone and verify them before delegation.
4. Run `deploy.py publish --state /path/to/state --output /tmp/token-exchange-build`.
   This uploads public assets, checks that the control load balancer routes
   every host and requests Google-managed certificates for every hostname that
   no managed certificate on the HTTPS proxy lists. Routing is Terraform's:
   `infra/control_lb_url_map.tf` reads `markets.json`, so a new market's routing
   is its entry in that file, merged and applied by infra before publish, which
   refuses while a host is unrouted and writes no routing itself.
   When the HTTPS proxy has a certificate map (`infra/control_lb_certificate_map.tf`),
   the proxy serves that map's certificates and ignores its classic ones. Then
   publish requests and attaches no certificate, and it refuses before any change
   unless every hostname has an `ACTIVE` map entry with an `ACTIVE` certificate.
   The order for a new domain is then: its nameserver change (step 5) first, then
   "Adding a Token Exchange domain" in `infra/README.md` until its certificate and
   its two map entries are `ACTIVE`, and only then publish. Certificate Manager validates through a CNAME in the
   domain's Cloud DNS zone, which resolves publicly only after delegation.
5. Without a certificate map, confirm all certificates were successfully
   requested and attached to the HTTPS proxy. Then change each domain's
   nameservers **in Firefox/Namecheap**
   to its exact four servers in `dns-manifest.json`. Do not assume all zones
   share the same server set. Certificate activation requires the new DNS.
   With a certificate map, this nameserver change comes before step 4's publish.
6. Verify delegation, apex A, www CNAME, unchanged mail records, HTTPS validity,
   canonical content, alias redirects, assets and intake links. Check the
   main TrustedRouter, trust and status sites still serve normally.

`python3 sites/token-exchange/smoke.py --staged` verifies host routing through
the existing load balancer before DNS cutover. It deliberately does not claim
new-domain TLS verification. Rerun without `--staged` after propagation to
check actual public HTTPS on every canonical hostname and alias.

For repeated content-only publishes, first run `deploy.py check-brochure`; it
refuses unless the live app accepts every market domain for the brochure form.
Then use `gcloud storage rsync` on the build directory. Objects have a five-minute browser cache. Update the versioned CSS/JS
references via the build step; invalidate the CDN only if an urgent correction
requires it.

## Rollback and Content Rules

Rebuild a prior reviewed revision and sync its assets for content rollback.
For routing rollback, remove only `exchange-*` matchers/host rules from a fresh
URL map. Never blindly import an old full map over concurrent production changes.
Retain DNS backups, generated manifests and certificate names in the ops record.

Each regional page has its own substantive workload guidance. Regional branding
does not imply local inference, a local office or a residency guarantee. Provider
ZDR policies and verified confidential inference are separate properties. Avoid
invented liquidity, customers, guaranteed savings, certifications or financial
exchange affiliation. Shanghai service availability requires explicit eligibility
and jurisdictional review.

## Live evidence contract

All 13 markets use `live-evidence.js` and the backend's `token_exchange.py`.
The feed reads the same effective catalog endpoints used by `/models`, selecting
exactly one Tinfoil Credits route with confidential compute and E2EE. It displays
exact USD/1M decimal prices and links to the model's provider table. The catalog's
headline minimum is a cross-provider minimum, not a Tinfoil quote. Prices follow
the existing catalog refresh/release cycle; the sites need no separate rebuild.

The backend reads the public status and release JSON for the market's established
GCP/Azure profile. Responses have a one-minute server cache, no stale-response
window, and `Cache-Control: no-store` to prevent additional browser/CDN caching.
Failed sources are absent; embedded/stale release records are not republished.
No checked-in evidence snapshot is used by the site build.

The landing page shows measured service details only when every supplied
component is operational, dated within one hour and complete. Degraded, failed, unknown or expired
checks quietly return the whole service panel to its existing source link;
there is no partial all-clear or warning badge. Measurements carry their original
last-checked timestamps; the one-hour display window is not a current-health guarantee. Historical
failure or missing-data buckets are never recolored or cherry-picked: the whole
chart is omitted while the accurate uptime percentage remains. Full incident
history stays available through the Service status link.

Attestation details require an operational check dated within one hour and valid published
release metadata; otherwise only the Published release link remains. Prices are
independently sourced and remain visible when valid. A failed fetch retains the
last response in the current tab only, bounded by its original timestamps.
After one hour, panels return to their source links. Successful refreshes restore
details automatically.
Responses or checks older than one hour, missing dates and future dates remain ineligible.
A successful newer response replaces the old response, including unavailable
or unhealthy components; those components use the existing source links.
The browser reassesses freshness every second and fetches every minute, matching
the shorter server cache. Published measurements do not establish location.

Regression checks:

```sh
uv run pytest tests/test_token_exchange_evidence.py -q
NODE_PATH=/path/to/node_modules node sites/token-exchange/verify-live.cjs /tmp/token-exchange-build
```

The browser test serves New York and London as two local staging hostnames,
stops the actual HTTP feed, and waits the real one-minute interval with pages
left open. It checks bounded retention after transport failure, quiet fallbacks, expired dates, rounding, state selection,
missing components, and responsive layout. This is staging verification, not
proof of production deployment. Deploy the backend through the reviewed release
workflow before publishing the static assets. Run all repository gates first.

For the upstream-outage check (backend remains healthy while status is killed),
serve the built site on port 8089, then run these in separate terminals:

```sh
uv run python sites/token-exchange/stage_evidence.py
NODE_PATH=/path/to/node_modules node sites/token-exchange/verify-upstream.cjs
```

This binds only loopback. It uses the production public route and its real
one-minute cache against a stoppable local HTTP status source. The test leaves
New York and London open, shuts that source down, and checks that uptime and
attestation disappear while independently sourced catalog prices remain.

The Token Exchange Sites workflow runs the stale/mixed-state browser checks and
the real one-minute upstream outage test on pull requests and main changes to
the shared site or evidence backend. It does not publish the sites.

## Hero artwork — October 15, 2026

`art/river-hero.webp` is the approved regenerated close aerial river image,
exported at 2880 × 1152 (WebP quality 88). The built-in image generator returned
1983 × 793; the export uses Lanczos resampling, without adding generated detail.
The hero uses a 30% black overlay on desktop and 20% at widths up to 600px.
The closing section retains `art/river-close.webp`.

The approved generation simplified the left half to one broad curving river
channel, retaining blue-hour basalt, forest-green ridges and cool blue water.
It is symbolic landscape artwork, not an identified location or company facility.
Generation source: `exec-91d974ff-c8fe-4501-8b28-253437e0dd7a.png`, built-in image tool,
October 15, 2026. Preserve this approved composition when optimizing exports.
