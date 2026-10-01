# TrustedRouter homepage

The marketing homepage ported into the application, served at `/` when `TR_HOMEPAGE_LANDSCAPE_ENABLED=true`. The setting defaults to false; `scripts/deploy/rollout.sh` enables it for production. Alternate brands keep the legacy page. The reference design lives in the marketing handoff (`SPEC.md`, `DECISIONS.md`, competitor research) and is not a second implementation.

## Where things live

| Path | Role |
|---|---|
| `src/trusted_router/templates/homepage/*.html` | One include per module: top (header + hero), request, models, migrate, verify, pricing, customers, faq, closing, footer. `index.html` composes them and includes the shared `public/_signin.html` dialog. |
| `src/trusted_router/static/homepage/homepage-modules.css` | Module layout from the marketing mock. |
| `src/trusted_router/static/homepage/homepage-landscape.css` | River landscape, Archivo type, hero sizing modes, responsive rules. |
| `src/trusted_router/static/homepage/application.css` | Application integration and every decision made after the port. Appended in layers; a consolidation into one sheet is planned after launch. |
| `src/trusted_router/static/homepage/components.css` | Primary/secondary buttons and `.action-link`. |
| `src/trusted_router/static/homepage/homepage.js`, `status.js` | Catalog filters, model search dialog, migration tabs, copy buttons, disclosures, tooltips, analytics events; live status dot. `dashboard.js` (shared) handles sign-in and returning-user chrome. |
| `src/trusted_router/homepage.py` | Template context: live catalog rows and prices from the model directory, pricing comparison, asset digest for cache busting. |
| `src/trusted_router/dashboard.py` | Chooses the template by flag and brand, sets the social image. |
| `src/trusted_router/routes/acquisition.py` | Allowlist of `home.*` analytics event names. |
| `docs/homepage/social-card.html` | Source of `static/homepage/social-card-v1.jpg` (1200×630). |
| `docs/homepage/evidence/2026-09-30-gateway/` | Gateway attestation capture behind the request module. |
| `docs/homepage/MOCKUP-AUDIT.md` | September 29 audit of the port against the mock. |

Static files are loaded into memory at process start, so restart the server after any CSS or JS change.

## Design rules in force

- **Placement.** Tools (search, filters, tabs) sit in the row of content they operate on. Actions (buttons, `.action-link`) close their module at the copy's left edge, after the evidence. The hero and closing band center theirs. Nothing sits beside an H2.
- **Supporting links** are underlined with a trailing arrow added by CSS (`.action-link`).
- **Header.** Model search is in the header row at every width ("Search N models", live count). Below 600px the wordmark drops to the mark and Menu is an icon; below 480px Sign in moves into the drawer. Short landscape phones keep a 64px header.
- **Hero ticker.** Items are buttons that open the model search prefilled. The marquee pauses for pointer hover and keyboard focus only. On phones and tablets it follows the hero actions on a gradient band; on desktop it sits at the hero's bottom.
- **Status dot** reflects `/status.json` (`status.js`, 60s refresh): green pulse when fresh, amber degraded, red down, grey unknown or stale. Locally it is grey because the memory backend has no probes.
- **Sign-in** reuses the shared dialog unchanged: Google, GitHub and MetaMask with their provider badges, labels per Google's sign-in branding guidance.
- **Social proof** is a customer story card followed by a sibling press card (Featured in · Axios).
- **No-JS:** `<html class="no-js">` is cleared by the first inline script; without scripts the phone header shows its links inline.

## Running it

```bash
TR_ENVIRONMENT=local TR_STORAGE_BACKEND=memory TR_RATE_LIMIT_ENABLED=false \
TR_HOMEPAGE_LANDSCAPE_ENABLED=true \
TR_GOOGLE_CLIENT_ID=preview-only TR_GOOGLE_CLIENT_SECRET=not-a-real-secret \
TR_GITHUB_CLIENT_ID=preview-only TR_GITHUB_CLIENT_SECRET=not-a-real-secret \
uvicorn trusted_router.main:app --app-dir src --host 127.0.0.1 --port 3001
```

Dummy OAuth IDs are required to render all three sign-in options; without them the dialog shows MetaMask only, which hid a regression once. Rate limiting is off because a shared tunnel bridge makes every visitor one client. For an external preview, put a read-only bridge in front (serve `/`, `/static/*` and `/v1/models/picker` from the app, forward `/status.json` to production, redirect other GETs to production, reject writes) and point ngrok at the bridge, never at the app.

## Tests

```bash
uv run pytest tests/test_homepage_landscape.py tests/test_homepage_analytics.py tests/test_public_surface_deploy.py
node --test tests/js/homepage_*.test.cjs
npx playwright test --config playwright.homepage.config.js
```

QA rule: compare against main under equivalent configuration (providers enabled, same cookies). A dialog opening is not proof that sign-in works.

## Open items

- Terminology: the team proposed filters All / ZDR / Confidential and badges ZDR / Confidential; the page still says Private and E2EE. Not implemented; routing rules unchanged.
- Not certified locally: production OAuth round trips, analytics ingestion, deployed status behaviour.
- A real-device report of the hero ticker shifting vertically has not been reproduced in Chromium.
- Stylesheet consolidation (one sheet, one breakpoint ladder) is planned for after launch, gated by computed-style and screenshot comparison.
