# Asset Inventory

Review date: 2026-06-07

Status: draft, owner approval required.

## Application And Code Assets

| Asset | Purpose | Owner | Notes |
|---|---|---|---|
| `Lore-Hex/quill-router` | TrustedRouter control plane, website, routing metadata, billing, API compatibility. | Joseph Perla | Public repo. Branch protection currently missing. |
| `Lore-Hex/quill-cloud-proxy` | Attested gateway/enclave prompt path. | Joseph Perla | Trust page references this gateway repo. |
| `Lore-Hex/quill-cloud-infra` | Infrastructure code. | Joseph Perla | Trust page references this repo. |

## Production Infrastructure Assets

| Asset | Purpose | Evidence |
|---|---|---|
| GCP project `quill-cloud-proxy` | Primary production project. | CLI snapshot. |
| Cloud Run `trusted-router` in multiple regions | Control-plane/site regional deployment. | CLI snapshot. |
| Cloud Run `quill-cloud` | Quill Cloud service. | CLI snapshot. |
| Spanner `trusted-router` | Ledger/database. | CLI snapshot. |
| Spanner `trusted-router-nam6` | Multi-region ledger/database candidate. | CLI snapshot. |
| Bigtable `trusted-router-logs` | Request/activity metadata logs. | CLI snapshot. |
| Bigtable `trusted-router-logs-v2` | Synthetic/activity log storage. | CLI snapshot. |
| Secret Manager secrets | Provider, billing, OAuth, monitor, internal, Sentry, Axiom, Cloudflare secrets. | Secret inventory in CLI snapshot. |
| Cloudflare DNS/CDN | Domain routing and public-site cache. | DNS/admin export pending. |
| GitHub Actions | CI, deploy, smoke, pricing refresh, embedding probe workflows. | Workflow snapshot. |

## Data Assets

| Data Asset | Classification | Storage |
|---|---|---|
| Account and workspace metadata | Confidential customer metadata | Spanner/control-plane storage. |
| API key hashes and hints | Secret-derived metadata | Spanner/control-plane storage. |
| BYOK provider secret material | Restricted secret | Envelope encryption/Secret Manager/KMS depending on path. |
| Billing and payment metadata | Confidential financial metadata | Spanner plus Stripe/PayPal. |
| Request metadata | Confidential operational metadata | Bigtable/Spanner as applicable. |
| Prompt/output content | Customer content, highest sensitivity | Transient processing only by default; not durable storage by TrustedRouter. |

## Approval

Owner signature:

Date:

