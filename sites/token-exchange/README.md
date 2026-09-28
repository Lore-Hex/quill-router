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
- Buyers book an enterprise pilot or visit the existing email-gated brochure flow.
  Suppliers use the existing provider marketplace application. Credentials are
  handled separately by that onboarding flow.
- Static pages fetch the public `/token-exchange/evidence/{profile}.json` feed
  from TrustedRouter on load and every minute. There is no direct Spanner
  access, inference, cookie, or third-party pixel on these sites. Allowlisted UTM fields pass to intake. Initial site visits are
  not funnel events; central intake and signup use TrustedRouter's existing tracking.

The HTTPS proxy also retains independent flagship certificates
`token-exchange-global-20260920` and `token-exchange-new-york-20260920` for each
site's apex and `www`. These isolate their availability from the larger regional
certificate batches. Preserve them on future publishes. A hostname marked
`ACTIVE` inside a batch is not sufficient: the certificate itself must be
`ACTIVE`, and a normal public HTTPS request must pass without disabling validation.

## Build and Test

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
   This uploads public assets, validates a merged URL map and requests Google-managed
   certificates for every hostname that no managed certificate on the HTTPS proxy lists.
   The pre-deploy URL map is saved once for rollback; existing routing remains.
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

For repeated content-only publishes, use `gcloud storage rsync` on the build
directory. Objects have a five-minute browser cache. Update the versioned CSS/JS
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
component is operational, fresh and complete. Degraded, failed, unknown or stale
checks quietly return the whole service panel to its existing source link;
there is no partial all-clear, warning badge or stale percentage. Historical
failure or missing-data buckets are never recolored or cherry-picked: the whole
chart is omitted while the accurate uptime percentage remains. Full incident
history stays available through the Service status link.

Attestation details require an operational, fresh check and valid published
release metadata; otherwise only the Published release link remains. Prices are
independently sourced and remain visible when valid. A failed feed returns every
panel to its source links. Successful refreshes restore details automatically.
Checks older than six minutes, missing dates and future dates remain ineligible.
The browser reassesses freshness every second and fetches every minute, matching
the shorter server cache. Published measurements do not establish location.

Regression checks:

```sh
uv run pytest tests/test_token_exchange_evidence.py -q
NODE_PATH=/path/to/node_modules node sites/token-exchange/verify-live.cjs /tmp/token-exchange-build
```

The browser test serves New York and London as two local staging hostnames,
stops the actual HTTP feed, and waits the real one-minute interval with pages
left open. It checks quiet fallbacks, stale dates, rounding, state selection,
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
