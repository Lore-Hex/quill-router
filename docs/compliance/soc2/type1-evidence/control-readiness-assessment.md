# SOC 2 Type I Control Readiness Assessment

Review date: 2026-06-07

Status key:

- Implemented: evidence currently exists in this binder, source, tests, or production snapshot.
- Designed, evidence pending: control design exists but auditor-ready evidence needs export, screenshot, signature, or production verification.
- Gap: control is materially missing or fails the expected Type I design.
- Not applicable: not applicable to the scoped solo-founder environment on the review date.

## Summary

| Status | Count |
|---|---:|
| Implemented | 10 |
| Designed, evidence pending | 17 |
| Gap | 4 |
| Not applicable | 0 |

## Control Ratings

| Control | Status | Readiness Notes | Evidence |
|---|---|---|---|
| TR-GOV-001 | Designed, evidence pending | Responsibilities are documented, but policy owner approval and management sign-off are not yet signed. | SOC 2 binder, management assertion draft. |
| TR-GOV-002 | Designed, evidence pending | Solo-founder responsibilities are documented. Confidentiality/training evidence is still needed for any contractors or future employees. | Personnel policy, open evidence gaps. |
| TR-RISK-001 | Implemented | Risk register exists for current product, infrastructure, vendor, privacy, and availability risks. | Risk register. |
| TR-RISK-002 | Designed, evidence pending | Rate limiting, billing controls, API key controls, and abuse controls exist in code and tests. Need production config export and review record. | Tests, config, risk register. |
| TR-POL-001 | Designed, evidence pending | Policy binder exists locally. Public legal packet is not deployed yet, so production evidence is pending. | Policy docs, production snapshot showing `/legal` 404. |
| TR-ACCESS-001 | Designed, evidence pending | IAM evidence was collected at summary level. Need formal least-privilege review and admin access exports from all systems. | CLI snapshot, access review. |
| TR-ACCESS-002 | Designed, evidence pending | Access change process is documented. Joiner/mover/leaver evidence is not meaningful yet for a solo-founder company but should be recorded as not applicable or future-state with auditor agreement. | Access control policy, access review. |
| TR-ACCESS-003 | Gap | MFA evidence is not captured for admin systems. | Open evidence gaps. |
| TR-ACCESS-004 | Implemented | Secret Manager inventory exists and secrets are not committed as raw values. Need per-secret IAM screenshot/export for auditor evidence. | CLI snapshot, secret inventory. |
| TR-CHANGE-001 | Gap | CI exists, but GitHub reports `main` is not protected. Branch protection or a documented compensating workflow is required. | CLI snapshot, GitHub branch protection result. |
| TR-CHANGE-002 | Designed, evidence pending | Emergency change tracking process exists. Need completed change records for recent production fixes. | Change record template, change record draft. |
| TR-OPS-001 | Implemented | Status page, synthetic monitoring, and Cloud Run regional services are present. | CLI snapshot, status snapshot. |
| TR-OPS-002 | Designed, evidence pending | Sentry/Axiom/floodgate controls exist in configuration and docs. Need exported alert settings and review record. | Logging policy, open evidence gaps. |
| TR-OPS-003 | Designed, evidence pending | Incident response process exists. Recent prod smoke failures and legal route 404 need triage records. | Incident log. |
| TR-VULN-001 | Designed, evidence pending | CI runs tests, ruff, mypy, frontend checks, browser smoke, and coverage. Need dependency, secret, and container scanning evidence. | Workflow snapshot, vulnerability review. |
| TR-VULN-002 | Designed, evidence pending | Remediation policy exists. Need tracker export or risk acceptance records for open findings. | Vulnerability policy, vulnerability review. |
| TR-DATA-001 | Implemented | Data classification and retention policy exists. | Policy binder. |
| TR-DATA-002 | Implemented | Tests and schemas support metadata-only storage by default. Need sampled production log evidence. | Test evidence, storage schemas, logging policy. |
| TR-DATA-003 | Designed, evidence pending | BYOK encryption and secret handling are implemented in code/tests. Need KMS/envelope config evidence and per-secret access review. | Tests, Secret Manager inventory. |
| TR-PRIV-001 | Designed, evidence pending | Security and providers pages are live. Legal page is not deployed yet. | Production HTTP snapshot. |
| TR-PRIV-002 | Implemented | Routing preferences, BYOK, provider filters, and content export toggles are covered by tests and code. | Local tests. |
| TR-PRIV-003 | Designed, evidence pending | Retention policy exists. Need deletion/export procedure evidence and sample request record. | Retention policy, open gaps. |
| TR-AVAIL-001 | Implemented | Router-core, provider-effective, and control-plane SLO classes are documented and public status separates them. | Status page, status JSON. |
| TR-AVAIL-002 | Implemented | Multi-region Cloud Run services and synthetic status are present. | Cloud Run snapshot, status snapshot. |
| TR-AVAIL-003 | Designed, evidence pending | DR/BCP policy exists. Restore test evidence is missing. | Backup/DR policy, open gaps. |
| TR-PI-001 | Implemented | Billing, settlement, refund, and fallback billing tests pass locally. | Test evidence. |
| TR-PI-002 | Implemented | API input validation, compatibility, money parsing, routing, and workspace scoping tests pass locally. | Test evidence. |
| TR-PI-003 | Designed, evidence pending | Reconciliation behavior is designed. Need production runbook and evidence of reconciliation checks or compensating review. | Control matrix, open gaps. |
| TR-VENDOR-001 | Implemented | Vendor inventory and subprocessor list exist. Need dated vendor review sign-off. | Vendor review draft, subprocessor pages. |
| TR-VENDOR-002 | Implemented | Provider privacy posture is tracked in catalog and public provider pages. | Provider catalog, policy URLs. |
| TR-AUDIT-001 | Designed, evidence pending | Evidence index and packet now exist. Management review and auditor review are not complete. | Evidence index, management assertion draft. |

## Before Auditor Type I Fieldwork

Fix or collect:

1. Enable GitHub branch protection for `main`.
2. Deploy legal/procurement pages and verify public URLs.
3. Capture MFA and admin access evidence.
4. Export Cloudflare, Stripe, PayPal, AWS SES/SNS, Sentry, Axiom, and DNS admin access.
5. Capture vulnerability, dependency, secret, and container scan evidence.
6. Run and record restore/backup evidence.
7. Sign policy approval and management assertion.
8. Ask auditor to confirm Trust Services Criteria mapping.

