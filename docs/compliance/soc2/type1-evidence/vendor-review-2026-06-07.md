# Vendor Review

Review date: 2026-06-07

Reviewer: Joseph Perla, CEO

Status: draft, sign-off required.

## Platform Vendors

| Vendor | Purpose | Data Access | Status |
|---|---|---|---|
| Google Cloud Platform | Hosting, Confidential Space, Cloud Run, Spanner, Bigtable, KMS, Secret Manager. | Metadata, secrets, infra logs; prompt path designed to terminate in attested gateway. | In use, policy linked in public subprocessor list. |
| Cloudflare | DNS, public-site caching, edge protection for non-prompt surfaces. | DNS and public website metadata. | In use, policy linked. |
| Stripe | Card, stablecoin checkout, billing records, payment methods. | Billing identity and payment metadata. | In use, policy linked. |
| PayPal | Optional prepaid payment processing. | Billing identity and payment metadata. | In use, policy linked. |
| AWS SES/SNS | Transactional email, bounces, complaints. | Email address and email metadata. | In use, policy linked. |
| GitHub | Source control, CI, release workflows. | Source code and CI metadata. | In use, branch protection gap noted. |
| Sentry | Control-plane exception monitoring. | Scrubbed control-plane errors only. | In use, scrubbing evidence pending. |
| Axiom | Operational log search and alerting. | Structured operational metadata only. | In use, log sample evidence pending. |

## Model Providers

Model providers are subprocessors only when customer traffic is routed to them. Provider ZDR, confidential compute, and E2EE posture is maintained in the provider catalog and displayed on public provider/model pages.

Sensitive legal/HIPAA workloads default to `trustedrouter/zdr`. `trustedrouter/e2e` or named provider allowlists require written customer approval.

## Evidence Needed Before Type I

- Export vendor admin/account screenshots where available.
- Attach vendor DPA/BAA/security documentation where applicable.
- Attach public subprocessor page verification and any vendor-specific DPA/BAA evidence.
- Record customer-specific provider approvals for regulated workloads.

Reviewer signature:

Date:
