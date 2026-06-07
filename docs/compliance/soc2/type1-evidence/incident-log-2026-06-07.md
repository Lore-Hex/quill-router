# Incident And Operational Event Log

Review date: 2026-06-07

Status: draft. Requires owner review.

## Open Events

| Event ID | Date/Time | Severity | Description | Status | Next Action |
|---|---|---|---|---|---|
| EVT-2026-06-07-001 | 2026-06-07 | Medium | Public `/legal` and `/legal/procurement.json` return 404 because new legal packet is local but not deployed. | Open | Deploy legal packet, verify HTTP 200, record change. |
| EVT-2026-06-07-002 | 2026-06-06 to 2026-06-07 | Medium | GitHub Actions Prod Smoke has recent failed scheduled/workflow runs. | Open | Root-cause failures, document whether provider/auth/status false positive or real production issue. |
| EVT-2026-06-07-003 | 2026-06-07 | High | GitHub `main` branch is not protected. | Open | Enable branch protection and record evidence. |

## Incident Classification

No confirmed customer data exposure was identified during this evidence-prep pass.

Any future event involving prompt/output leakage, signing/trust compromise, active attacker in production, PHI exposure, billing corruption, or global prompt-path outage should be classified through the incident response policy.

## Sign-Off

Reviewed by:

Date:

