# CLI And Production Snapshot

Review date: 2026-06-07

This file records non-secret evidence collected from local CLI tools. Do not paste secret values into this file.

## Repository Snapshot

Command:

```sh
git rev-parse HEAD
git branch --show-current
git remote -v
git status --short
```

Observed:

- Repository: `/Users/jperla/claude/quill-router`
- Branch: `main`
- HEAD: `2cf4578b232121d9e7a27f7b74bd6aad9ee04e96`
- Remote: `git@github.com:Lore-Hex/quill-router.git`
- Worktree: dirty. Compliance docs and unrelated product changes are uncommitted.

Type I implication: freeze the audit commit before the Type I report date. Do not use a dirty worktree as final audit evidence.

## GitHub Repository Snapshot

Command:

```sh
gh repo view Lore-Hex/quill-router --json nameWithOwner,url,visibility,defaultBranchRef,isPrivate,createdAt,pushedAt,viewerPermission
gh api repos/Lore-Hex/quill-router/branches/main/protection
```

Observed:

- Repository: `Lore-Hex/quill-router`
- URL: `https://github.com/Lore-Hex/quill-router`
- Visibility: public
- Default branch: `main`
- Created: 2026-05-02T17:12:42Z
- Last pushed: 2026-06-07T03:53:35Z
- Viewer permission during evidence collection: ADMIN
- Branch protection result: `HTTP 404 Branch not protected`

Type I implication: branch protection is a blocker for TR-CHANGE-001 unless a CPA accepts a compensating control. Enable branch protection before the audit date.

## GitHub Actions Snapshot

Command:

```sh
gh api 'repos/Lore-Hex/quill-router/actions/workflows'
gh api 'repos/Lore-Hex/quill-router/actions/runs?per_page=10'
```

Active workflows observed:

- CI: `.github/workflows/ci.yml`
- Deploy TR control plane: `.github/workflows/deploy.yml`
- Prod Smoke: `.github/workflows/prod-smoke.yml`
- Refresh upstream prices: `.github/workflows/refresh-prices.yml`

Recent workflow observations:

- Deploy TR control plane succeeded at 2026-06-07T04:25:44Z for SHA `2cf4578b232121d9e7a27f7b74bd6aad9ee04e96`.
- Refresh upstream prices succeeded at 2026-06-07T03:53:42Z.
- Several Prod Smoke scheduled/workflow runs failed on 2026-06-06 and 2026-06-07 and need operational triage records.

## GCP Project Snapshot

Command:

```sh
gcloud config list --format=json
gcloud run services list --platform=managed
gcloud spanner instances list
gcloud bigtable instances list
gcloud secrets list
```

Observed project:

- Project: `quill-cloud-proxy`
- Evidence collection account: `josephjavierperla@tt.live`

Cloud Run services observed:

| Service | Region | URL | Last deployed by | Last deployed at |
|---|---|---|---|---|
| quill-cloud | us-central1 | `https://quill-cloud-44325983244.us-central1.run.app` | `josephjavierperla@tt.live` | 2026-06-04T18:10:54Z |
| trusted-router | asia-northeast1 | `https://trusted-router-44325983244.asia-northeast1.run.app` | `tr-deploy@quill-cloud-proxy.iam.gserviceaccount.com` | 2026-06-07T04:23:11Z |
| trusted-router | asia-southeast1 | `https://trusted-router-44325983244.asia-southeast1.run.app` | `tr-deploy@quill-cloud-proxy.iam.gserviceaccount.com` | 2026-06-07T04:23:45Z |
| trusted-router | europe-west4 | `https://trusted-router-44325983244.europe-west4.run.app` | `tr-deploy@quill-cloud-proxy.iam.gserviceaccount.com` | 2026-06-07T04:10:09Z |
| trusted-router | southamerica-east1 | `https://trusted-router-44325983244.southamerica-east1.run.app` | `tr-deploy@quill-cloud-proxy.iam.gserviceaccount.com` | 2026-06-07T04:24:14Z |
| trusted-router | us-central1 | `https://trusted-router-44325983244.us-central1.run.app` | `tr-deploy@quill-cloud-proxy.iam.gserviceaccount.com` | 2026-06-07T04:01:36Z |
| trusted-router | us-east4 | `https://trusted-router-44325983244.us-east4.run.app` | `tr-deploy@quill-cloud-proxy.iam.gserviceaccount.com` | 2026-06-07T04:18:03Z |

Spanner instances observed:

- `trusted-router`, `regional-us-central1`, display name `TrustedRouter ledger`, 100 processing units, READY.
- `trusted-router-nam6`, `nam6`, display name `TrustedRouter (nam6)`, 100 processing units, READY.

Bigtable instances observed:

- `trusted-router-logs`, display name `TrustedRouter logs`, READY, PRODUCTION.
- `trusted-router-logs-v2`, display name `TR logs (HDD multi)`, READY, PRODUCTION.

Secret Manager inventory observed:

- Provider, billing, OAuth, monitor, internal gateway, Sentry, Axiom, and Cloudflare secrets are present as Secret Manager entries.
- Secret values were not accessed or recorded.
- Per-secret IAM bindings still need export and review for auditor evidence.

IAM observation:

- Project-level IAM includes a human owner binding and multiple high-privilege service-account bindings.
- Least-privilege review is required before Type I fieldwork, especially `roles/editor`, `roles/owner`, `roles/secretmanager.admin`, and broad deploy-service-account roles.

## Public HTTP Snapshot

Command:

```sh
python urllib checks against public URLs
```

Observed:

| URL | Status | Notes |
|---|---:|---|
| `https://trustedrouter.com/` | 200 | Homepage live. |
| `https://trustedrouter.com/legal` | 404 | New legal packet is not deployed. |
| `https://trustedrouter.com/legal/procurement.json` | 404 | New procurement JSON is not deployed. |
| `https://trustedrouter.com/security` | 200 | Security page live. |
| `https://trustedrouter.com/providers` | 200 | Provider page live. |
| `https://trust.trustedrouter.com/` | 200 | Trust page live. |
| `https://status.trustedrouter.com/` | 200 | Status page live. |
| `https://api.trustedrouter.com/v1/models` | 401 | API requires authentication. Expected for unauthenticated request. |

Type I implication: deploy the legal packet before using public legal posture as evidence.

## Test Snapshot

Commands:

```sh
uv run ruff check src/trusted_router/config.py src/trusted_router/legal.py src/trusted_router/dashboard.py src/trusted_router/routes/public.py tests/test_public_seo_pages.py
uv run mypy
uv run pytest tests/test_public_seo_pages.py tests/test_stubs_and_security.py tests/test_gateway_fallback_billing.py tests/test_core_api.py -q
uv run pytest -q
```

Observed:

- Ruff: passed.
- Mypy: passed, 130 source files.
- Focused compliance/security/API tests: 65 passed.
- Full Python test suite: 799 passed, 31 skipped.

