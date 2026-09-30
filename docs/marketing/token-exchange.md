# Enterprise Token Exchange

Public page: https://trustedrouter.com/token-exchange

Security resources: https://trustedrouter.com/token-exchange/security

The security page uses the same email gate and POST endpoint with
`resource: "security"` (the default remains `"brochure"`). Its prebuilt ZIP
contains the owner's original 18-slide security deck and 19-page whitepaper,
supplied September 28, 2026. No PDF text is rewritten. SHA-256 values:

- `TrustedRouter-Security-Deck.pdf`: `8108d1381a5ad0ff18936e1e55b0c332db829857c8d9fd21545f1befb079f55d`
- `TrustedRouter-Security-Whitepaper.pdf`: `1681a6726fa753f89a4630cf9065c3c414070a2a06c222e0b21cae12769a7232`

Only cover previews are static assets. The ZIP stays under `data/enterprise`;
downloads are private/no-store and require an accepted inquiry. The form is
explicit about security-review follow-up. Syntax validation does not verify
mailbox ownership. No email is sent to the visitor. The allowlist and shared
rate limit prevent arbitrary file selection or a per-resource limit bypass.
`acquisition.enterprise_security_pack_delivered` records delivery separately
from the existing brochure funnel, without the submitted address. The security
page is linked from Token Exchange, Security, Trust, and the core sitemap.
This is a marketing gate, not an NDA or private document vault; the documents
are included in the public source, as is the existing brochure.

The primary conversion is a request for the nine-page Token Exchange
brochure. Secondary calls to action open Joseph's existing sales calendar and
`enterprise@trustedrouter.com`. The page is in the core sitemap, resource hub
and shared footer.

## Download and lead handling

- `POST /token-exchange/brief` belongs to the **actions** service surface.
  Keep its exact path in `scripts/deploy/service_surface_url_map.py` during
  split-surface deployments. GET/HEAD rendering remains public.
- JSON contains `email` and the optional `website` honeypot. Email validation
  checks syntax, not mailbox ownership. The body is limited to 4 KiB.
- The existing SES service sends one inquiry to `TR_PARTNER_INQUIRY_EMAIL`
  (already `joseph@jperla.com` in production), with the visitor as Reply-To.
  Unconfigured environments fall back to `enterprise@trustedrouter.com`. The
  default SES sender/configuration set is used.
- The download is returned only after SES accepts that inquiry. A failed send
  returns a retryable error and never silently loses the lead. SES acceptance
  alone does not prove inbox delivery; verify forwarding in a production smoke.
- No account, credit grant, newsletter subscription or message to an arbitrary
  visitor mailbox is created. The form explicitly explains enterprise follow-up.
- Existing bounded, per-process inquiry limits apply: five per client/hour and
  60 total inquiries/hour across the inquiry routes. This is not a distributed
  organization-wide cap.
- The PDF is under `data/enterprise`, outside `/static`, and returned as an
  attachment with `private, no-store`. This is a marketing lead gate, not DRM or
  a claim that a publicly distributed brochure cannot be shared or found in source.
- The submitted address goes to the company inbox, not application logs or
  marketing event properties. `acquisition.enterprise_brief_delivered` records
  the existing consent-aware, pseudonymous first-party attribution context.
  It means the server offered the file, not that a human read it.

## Assets and copy

`data/enterprise/TrustedRouter-Token-Exchange-Brochure.pdf` is the owner-supplied
nine-page brochure (footer dated September 2026, supplied 2026-09-23),
published byte-for-byte. SHA-256:
`1d005ae234da1a68782e333023569b4f23653c78d081e05e4d2c2587ee54bbc7`.
Its content is not rewritten by the download endpoint.
The page uses Secure / Intelligent / Cheaper positioning and
distinguishes attested gateway protection, contractual provider ZDR and verified
downstream confidential inference. Following the owner's confirmation, the
page and linked SOC 2 HTML/JSON packet state that the Type II observation
window is in progress. Neither claims a completed audit or an issued report;
no auditor name or observation-period dates have been inferred.

`static/enterprise/token-exchange-hero.webp` is an optimized generated
illustration, 1536 x 1024. Built-in image generation prompt: architectural model
of silver server blocks from many suppliers, connected to one central gateway
and shield; dark green background, sage circuits, restrained gold, clean space
above for the headline; no labels or provider logos. It illustrates the
exchange rather than documenting an actual data center.

The social card is generated from this asset and native brand typography:

```sh
TR_PREVIEW_URL=http://127.0.0.1:8096 node scripts/marketing/render_token_exchange_og.mjs
```

## Verification

Run the repository's normal ruff, mypy and full pytest gates. Focused coverage:

```sh
uv run pytest -q tests/test_token_exchange.py tests/test_service_surfaces.py tests/test_service_surface_routing.py
```

Browser smoke requires Playwright installed in the local Node environment and
a local app with a **test-only fake SES sender**. It refuses remote hosts:

```sh
TR_PREVIEW_URL=http://127.0.0.1:8096 node scripts/marketing/token_exchange_browser_smoke.mjs
```

It checks five viewport/theme combinations, native email validation, delivery
failure/retry, overflow, loaded imagery, JavaScript errors and exact PDF bytes.
Do not install the fake sender into production. After deployment, submit one
clearly identified company test address, confirm the PDF and inbox receipt,
and verify homepage, status, page assets and the core sitemap.

## Release checks versus provider health

Release CI retains deterministic tests for routing, expired-catalog refusal,
privacy policy, billing, fallback, page rendering and downloads. A provider's
current model count or refresh availability is operational health, not evidence
that a website change is unsafe to ship. Time-sensitive tests carry the
`provider_health` marker and run hourly in `provider-catalog-health.yml`,
separately from the same-commit CI gate. Their failures remain visible in GitHub
Actions and never extend a provider's eligibility deadline. Social-card
structure, privacy labels and renderer idempotence remain release checks.
