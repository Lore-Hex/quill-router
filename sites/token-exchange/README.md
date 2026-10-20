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
  is the only form. Allowlisted UTM fields pass to intake. The first-party
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

New York's homepage includes `spot-api.html`, a streaming quickstart using
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
quickstart only after both API origins pass production tests. For a narrow
release, back up and update only New York's index and the isolated
`assets/spot-api.css`, preserving concurrent asset versions and unrelated market
pages. If the live homepage differs from the build, insert only the new
stylesheet link, navigation link, hero action and spot API section into fresh
live HTML. Verify reversing those four changes reproduces the original bytes.

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
page. Its email link opens a request to `providers@trustedrouter.com`; it does
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
`new-york/sitemap.xml` after the repository and site checks. Do not sync a stale
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
