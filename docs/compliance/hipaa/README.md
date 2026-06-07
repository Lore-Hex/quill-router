# TrustedRouter HIPAA Readiness Binder

Status: readiness package, not a HIPAA certification or executed BAA.

Owner: Lore Hex Corp

Authorized Lore Hex Corp signatory: Joseph Perla, CEO.

Security contact: security@trustedrouter.com

Scope: TrustedRouter hosted service where a covered entity or business associate intends to route ePHI through TrustedRouter. PHI production traffic is not approved until a BAA is executed and routing/subprocessor restrictions are approved.

Important status:

- HIPAA certification is not obtained.
- BAA is draft-only until signed.
- PHI workloads require route restrictions and subprocessor approval.
- Default sensitive-workload routing is `trustedrouter/zdr`.
- Content export for PHI requires explicit written approval.
- Prompt/output content is not stored by TrustedRouter by default, but downstream providers may receive PHI when selected.

## Binder Contents

- [HIPAA Readiness Matrix](hipaa-readiness-matrix.md)
- [PHI Handling Policy](policies/phi-handling.md)
- [BAA Operations Policy](policies/baa-operations.md)
- [HIPAA Incident And Breach Response Policy](policies/hipaa-incident-breach-response.md)
- [HIPAA Risk Analysis Template](templates/hipaa-risk-analysis.md)
- [PHI Route Approval Template](templates/phi-route-approval.md)
- [BAA Execution Checklist](templates/baa-execution-checklist.md)

## Before PHI Production Traffic

1. Execute BAA with Lore Hex Corp.
2. Confirm the customer is a covered entity or business associate and defines permitted use.
3. Approve the exact route policy. Default to `trustedrouter/zdr`; use `trustedrouter/e2e` or a named provider allowlist only after written customer approval.
4. Review subprocessors and downstream model providers.
5. Disable content export unless specifically approved in writing.
6. Complete HIPAA risk analysis and record residual risks.
7. Verify trust page evidence at `https://trust.trustedrouter.com/`.
8. Confirm incident notification process and contacts.

## Route Restriction Meaning

Restricted routing means PHI/ePHI can only be sent to aliases or providers approved in the customer-specific route approval. Broad fallback aliases are not acceptable for PHI unless every possible downstream provider is approved in writing. The default starting point is `trustedrouter/zdr`.

## HHS References

- HHS Security Rule: https://www.hhs.gov/hipaa/for-professionals/security/index.html
- HHS Security Rule summary: https://www.hhs.gov/hipaa/for-professionals/security/laws-regulations/
- HHS business associate contract requirements: https://www.hhs.gov/ocr/privacy/hipaa/understanding/coveredentities/contractprov.html
- HHS model BAA: https://www.hhs.gov/sites/default/files/model-business-associate-agreement.pdf
