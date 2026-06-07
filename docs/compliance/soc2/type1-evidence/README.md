# TrustedRouter SOC 2 Type I Evidence Packet

Status: internal readiness packet, not an auditor report.

Candidate review date: 2026-06-07

Prepared for: Lore Hex Corp, TrustedRouter hosted service

Prepared by: Joseph Perla, CEO, with Codex assistance

Security contact: security@trustedrouter.com

## Bottom Line

TrustedRouter is not yet SOC 2 Type I complete. This packet prepares the evidence binder and grades the controls for an internal readiness pass.

The product is close enough to begin an auditor readiness review after the blockers below are fixed and evidence is exported. The current state should not be described publicly as SOC 2 audited, SOC 2 certified, or SOC 2 Type I complete.

## Type I Scope

In scope:

- Hosted TrustedRouter control plane.
- Billing, credits, payment-method management, API key management, workspace management, BYOK metadata.
- Public security, trust, status, legal, provider, and model transparency pages.
- Attested API gateway and internal authorize, settle, refund, and route-candidate callbacks.
- Processing integrity for billing, credit reservation, settlement, refund, idempotency, workspace scoping, and routing authorization.

Out of scope:

- Customer self-hosted deployments.
- Downstream model-provider internal systems, except as subprocessors and route-policy dependencies.
- Customer-owned BYOK provider accounts beyond encrypted storage and release into the attested gateway.
- Non-production experiments that cannot affect production.

Target Trust Services Categories:

- Security.
- Availability.
- Confidentiality.
- Privacy.
- Processing Integrity limited to billing, authorization, settlement, credits, refunds, and routing correctness.

## Readiness Grade

Current internal grade: not Type I ready yet.

Primary blockers:

1. GitHub `main` branch is not protected. This is a change-management blocker.
2. MFA evidence for GitHub, GCP, Cloudflare, Stripe, PayPal, AWS, Sentry, Axiom, and DNS/admin systems is not captured.
3. Formal management approval of policies is not signed.
4. Vulnerability, dependency, secret, and container scanning evidence is incomplete.
5. Alert-routing and incident-response evidence is incomplete.
6. Backup/restore test evidence is incomplete.
7. Auditor has not reviewed scope, criteria mapping, or evidence.

## Packet Contents

- [Control readiness assessment](control-readiness-assessment.md)
- [Evidence index](evidence-index.md)
- [CLI and production snapshot](cli-snapshot-2026-06-07.md)
- [Access review](access-review-2026-06-07.md)
- [Vendor review](vendor-review-2026-06-07.md)
- [Risk register](risk-register-2026-06-07.md)
- [Asset inventory](asset-inventory-2026-06-07.md)
- [Change and deploy record](change-record-2026-06-07.md)
- [Incident and operational event log](incident-log-2026-06-07.md)
- [Vulnerability review](vulnerability-review-2026-06-07.md)
- [Management assertion draft](management-assertion-draft.md)
- [Auditor PBC response draft](pbc-response-draft.md)
- [Open evidence gaps](open-evidence-gaps.md)

## Rules For Use

- Do not mark any control as implemented unless evidence exists.
- Do not use screenshots or exports that expose API keys, BYOK secrets, payment secrets, session secrets, webhook secrets, or prompt/output content.
- If evidence exists only as source code, mark the control as designed and code-supported, not auditor-verified.
- If a production setting is not yet deployed, mark it as pending deployment.
- If an auditor changes criteria mapping, update the control matrix before the audit date.
