# TrustedRouter SOC 2 Readiness Binder

Status: readiness package, not a SOC 2 report.

Owner: Lore Hex Corp

Authorized Lore Hex Corp signatory: Joseph Perla, CEO.

Security contact: security@trustedrouter.com

Scope: TrustedRouter hosted control plane, billing/key management, public status/trust pages, and the attested API gateway that routes model requests. The initial audit target is SOC 2 Type I for Security, Availability, Confidentiality, and Privacy, with Processing Integrity controls for billing, credit ledger, authorization, settlement, and refund correctness.

Out of scope for the first Type I report:

- Customer self-hosted deployments.
- Customer-owned BYOK provider accounts beyond secure storage and release to the gateway.
- Downstream model provider internal systems, except as subprocessors and route-policy dependencies.
- Non-production experiments unless they can affect production.

Important status:

- SOC 2 Type I is not yet obtained.
- SOC 2 Type II is not yet obtained and requires an observation period.
- HIPAA, ISO 27001, and formal privacy certifications are not yet obtained.
- DPA and BAA are drafts until executed.

## Binder Contents

- [System Description](system-description.md)
- [Control Matrix](control-matrix.md)
- [Evidence Checklist](evidence-checklist.md)
- [Information Security Policy](policies/information-security.md)
- [Access Control Policy](policies/access-control.md)
- [Change Management and SDLC Policy](policies/change-management-sdlc.md)
- [Incident Response Policy](policies/incident-response.md)
- [Risk Management Policy](policies/risk-management.md)
- [Vendor and Subprocessor Management Policy](policies/vendor-management.md)
- [Asset Management Policy](policies/asset-management.md)
- [Data Classification and Retention Policy](policies/data-classification-retention.md)
- [Backup, Disaster Recovery, and Business Continuity Policy](policies/backup-dr-bcp.md)
- [Vulnerability Management Policy](policies/vulnerability-management.md)
- [Logging and Monitoring Policy](policies/logging-monitoring.md)
- [Encryption and Key Management Policy](policies/encryption-key-management.md)
- [Personnel Security and Training Policy](policies/personnel-security-training.md)
- [AI Data Handling Policy](policies/ai-data-handling.md)
- [Audit Operations Policy](policies/audit-operations.md)
- [Risk Register Template](templates/risk-register.md)
- [Vendor Review Template](templates/vendor-review.md)
- [Access Review Template](templates/access-review.md)
- [Incident Record Template](templates/incident-record.md)
- [Change Record Template](templates/change-record.md)
- [Asset Inventory Template](templates/asset-inventory.md)
- [Evidence Index Template](templates/evidence-index.md)
- [Type I Evidence Packet](type1-evidence/README.md)

## Type I Readiness Procedure

1. Confirm audit scope and Trust Services Categories with the auditor.
2. Freeze this binder and assign policy owners.
3. Collect point-in-time evidence listed in [Evidence Checklist](evidence-checklist.md).
4. Export production IAM, GitHub, Cloud Run, Spanner, Bigtable, KMS, Secret Manager, DNS, monitoring, and deploy evidence.
5. Complete access review, vendor review, risk register, asset inventory, and incident/change records.
6. Run the SOC 2 Type I management assertion review with counsel and the auditor.
7. Do not publish “SOC 2 certified” language. Publish only auditor-issued report status when complete.

## Operating Rule

If a document says a control exists, evidence must exist or be collected before the Type I audit date. If evidence is missing, mark the control as “designed, evidence pending,” not “implemented.”

## Solo-Founder Operating Rule

Run the Type I readiness process internally before buying a large compliance stack. Keep evidence lightweight and repeatable: access review records, change records, vendor reviews, incident records, risk register updates, and deploy evidence. After funding or before a Type II observation period, move this into a compliance/evidence system such as Rippling, Vanta, Drata, Secureframe, or an equivalent workflow so recurring evidence is not manual.
