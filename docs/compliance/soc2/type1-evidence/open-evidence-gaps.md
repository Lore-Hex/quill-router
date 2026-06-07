# Open Type I Evidence Gaps

Review date: 2026-06-07

## Blockers

1. Enable GitHub branch protection on `main`.
2. Deploy legal/procurement pages and verify:
   - `https://trustedrouter.com/legal`
   - `https://trustedrouter.com/legal/procurement.json`
   - `https://trustedrouter.com/legal/soc2-readiness`
   - `https://trustedrouter.com/legal/hipaa-readiness`
   - `https://trustedrouter.com/legal/subprocessors`
3. Capture MFA evidence for GitHub, GCP, Cloudflare, Stripe, PayPal, AWS, Sentry, Axiom, DNS/admin accounts.
4. Export admin access lists for GitHub, GCP, Cloudflare, Stripe, PayPal, AWS, Sentry, Axiom.
5. Capture vulnerability, dependency, secret, and container scan evidence.
6. Capture backup/restore test evidence.
7. Review and reduce high-privilege GCP IAM roles where practical.
8. Triage recent Prod Smoke failures and record root causes.
9. Sign policy approval and management assertion.
10. Engage CPA/auditor to confirm scope and mapping.

## Nice To Have Before Auditor

- Compliance platform or lightweight evidence folder automation.
- Monthly access review calendar event.
- Quarterly vendor review calendar event.
- Incident-response tabletop record.
- Restore-test calendar event.
- Customer deletion/export runbook dry run.

## Public Claim Guardrail

Until an auditor issues the report, public language must remain:

> SOC 2 readiness documentation prepared. SOC 2 report pending.

Do not use:

- SOC 2 certified
- SOC 2 compliant
- SOC 2 Type I complete
- SOC 2 audited

