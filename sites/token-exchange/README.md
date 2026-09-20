# Regional Token Exchanges

Static enterprise acquisition sites for 12 markets and 29 owned domains.
The global site is **thetokenexchange.com**; New York is **nytokenexchange.com**.
`markets.json` is the source of truth for canonical hosts, aliases and local copy.

## Architecture

- Public GCS bucket `quill-token-exchange-public`, containing only generated site assets.
- Cloud CDN backend bucket `token-exchange-static`.
- Existing global HTTPS load balancer, address `35.241.14.18`.
- URL map matchers prefixed `exchange-`. Other host rules, services, gateway
  routes and certificates are preserved. Normal four-service deploys preserve
  unrelated matchers through `scripts/deploy/service_surface_url_map.py`.
- One canonical root per market; alternate names and `www` redirect permanently,
  retaining query parameters. Canonical URLs, sitemaps and OG images are generated.
- Buyers book an enterprise pilot or visit the existing email-gated brief flow.
  Suppliers use the existing provider marketplace application. Credentials are
  handled separately by that onboarding flow.
- No application server, Spanner access, inference, cookies or third-party pixels
  on these sites. Allowlisted UTM fields pass to intake. Initial site visits are
  not funnel events; central intake and signup use TrustedRouter's existing tracking.

## Build and Test

```sh
python3 -m unittest discover -s sites/token-exchange -p 'test_*.py' -v
python3 sites/token-exchange/build.py --output /tmp/token-exchange-build
python3 -m http.server 8089 --bind 127.0.0.1 --directory /tmp/token-exchange-build
# In another terminal, with Playwright installed:
node sites/token-exchange/verify.cjs /tmp/token-exchange-build
```

The browser check exercises all 12 markets at 390, 768 and 1440 pixels, tests
overflow, loaded images, FAQ interaction and attribution, and generates 12 PNG
social previews. Inspect `/tmp/exchange-global-390.png` and
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
   This uploads public assets, validates a merged URL map and requests certificates.
   The pre-deploy URL map is saved once for rollback; existing routing remains.
5. Confirm all certificates were successfully requested and attached to the
   HTTPS proxy. Then change each domain's nameservers **in Firefox/Namecheap**
   to its exact four servers in `dns-manifest.json`. Do not assume all zones
   share the same server set. Certificate activation requires the new DNS.
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
