# Risk Register

Review date: 2026-06-07

Status: draft internal risk register.

| Risk ID | Risk | Likelihood | Impact | Current Controls | Residual Status | Owner |
|---|---|---|---|---|---|---|
| RISK-001 | Branch protection disabled allows unreviewed or force-pushed changes to main. | Medium | High | CI exists, deploy workflow exists. | Open gap. Enable branch protection. | Joseph Perla |
| RISK-002 | Public legal packet could drift from procurement evidence after future legal changes. | Low | Medium | Legal pages are deployed, tests cover the public packet, trust page links the packet, and production URLs were verified 200 on 2026-06-07. | Monitor on future legal changes. | Joseph Perla |
| RISK-003 | Admin MFA evidence unavailable during audit. | Medium | High | Personal MFA may exist, but not exported. | Evidence pending. | Joseph Perla |
| RISK-004 | Prompt/output content accidentally enters durable logs or monitoring. | Low | High | Metadata-only policies, Sentry excluded from enclave path, security tests. | Continue tests and log sampling. | Joseph Perla |
| RISK-005 | Provider routes with unknown retention posture are used for legal/HIPAA work. | Medium | High | `trustedrouter/zdr`, provider transparency, route approval policy. | Customer-specific approvals required. | Joseph Perla |
| RISK-006 | Billing settlement/refund logic double-charges or fails to settle correctly. | Low | High | Billing and gateway fallback tests. | Continue CI and reconciliation review. | Joseph Perla |
| RISK-007 | Secrets or BYOK material exposed through repo, logs, or API responses. | Low | High | Secret Manager, envelope encryption, API key hashing, BYOK tests. | Per-secret IAM review pending. | Joseph Perla |
| RISK-008 | Multi-region status/control-plane deployment drifts between regions. | Medium | Medium | Cloud Run multi-region deploys, status checks. | Add regional deployment verification evidence. | Joseph Perla |
| RISK-009 | Sentry or log flood loses important incident evidence. | Medium | Medium | Sentry floodgate and Axiom logging policies. | Alert/export evidence pending. | Joseph Perla |
| RISK-010 | Vendor or model-provider policy changes are not reviewed. | Medium | Medium | Refresh pricing workflow, public provider catalog. | Add periodic vendor review cadence. | Joseph Perla |

## Review Notes

This register is enough for internal readiness, but the auditor will likely expect owner, treatment decision, due date, and status for each open risk before the Type I report date.
