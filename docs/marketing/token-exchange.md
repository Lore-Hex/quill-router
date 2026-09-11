# Enterprise Token Exchange

Public page: https://trustedrouter.com/token-exchange

The primary conversion is a request for the original, two-page enterprise
brief. Secondary calls to action open Joseph's existing sales calendar and
`enterprise@trustedrouter.com`. The page is in the core sitemap, resource hub
and shared footer.

## Download and lead handling

- `POST /token-exchange/brief` belongs to the **actions** service surface.
  Keep its exact path in `scripts/deploy/service_surface_url_map.py` during
  split-surface deployments. GET/HEAD rendering remains public.
- JSON contains `email` and the optional `website` honeypot. Email validation
  checks syntax, not mailbox ownership. The body is limited to 4 KiB.
- The existing SES service sends one inquiry to `enterprise@trustedrouter.com`,
  with the visitor as Reply-To. That mailbox must receive or forward company
  email. The existing default SES sender/configuration set is used.
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
  a claim that a publicly distributed brief cannot be shared or found in source.
- The submitted address goes to the company inbox, not application logs or
  marketing event properties. `acquisition.enterprise_brief_delivered` records
  the existing consent-aware, pseudonymous first-party attribution context.
  It means the server offered the file, not that a human read it.

## Assets and copy

`data/enterprise/TrustedRouter-Enterprise-Brief.pdf` is the original supplied
document, unchanged. Its content is not rewritten by the download endpoint.
The page independently distinguishes attested gateway protection, contractual
provider ZDR and verified downstream confidential inference. It links SOC 2
readiness without claiming completed certification.

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
