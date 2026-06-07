# Access Review

Review date: 2026-06-07

Reviewer: Joseph Perla, CEO

Status: draft, sign-off required.

## Scope

- GitHub organization and repository access.
- GCP project `quill-cloud-proxy`.
- Cloudflare DNS/admin.
- Stripe, PayPal, AWS SES/SNS, Sentry, Axiom.
- Production service accounts and deployment identities.

## Evidence Collected

- GitHub repository metadata confirms reviewer has ADMIN permission.
- GitHub branch protection check returned `Branch not protected`.
- GCP IAM summary was collected and shows human owner access plus multiple service accounts.
- GCP Secret Manager inventory was collected without secret values.

## Findings

| Finding | Severity | Action |
|---|---|---|
| GitHub `main` branch is not protected. | High | Enable branch protection requiring PR review or at minimum status checks, no force push, no deletion. |
| MFA evidence is not captured for GitHub, GCP, Cloudflare, Stripe, PayPal, AWS, Sentry, and Axiom. | High | Export screenshots or admin reports showing MFA enabled before audit date. |
| Project-level GCP IAM includes high-privilege roles. | Medium | Review whether each service account still needs its role. Remove broad roles or document compensating controls. |
| External admin access exports are missing. | Medium | Export Cloudflare, Stripe, PayPal, AWS, Sentry, and Axiom admin user lists. |

## Decision

This review is not ready for Type I sign-off. Complete the actions above, then rerun the access review and sign below.

Reviewer signature:

Date:

