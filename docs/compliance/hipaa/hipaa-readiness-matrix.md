# HIPAA Readiness Matrix

Status: draft readiness matrix.

This matrix is not legal advice and is not an HHS determination. It maps TrustedRouter controls to HIPAA Security Rule safeguard categories and business associate obligations for readiness review.

| Control ID | HIPAA Area | Requirement Theme | TrustedRouter Control | Required Evidence |
|---|---|---|---|---|
| HIPAA-ADM-001 | Administrative Safeguards | Security management process | Maintain HIPAA risk analysis for PHI use cases and route policies. | HIPAA risk analysis, risk register, mitigation records. |
| HIPAA-ADM-002 | Administrative Safeguards | Assigned security responsibility | Assign Security Officer responsible for HIPAA safeguards. | Owner list, policy approval. |
| HIPAA-ADM-003 | Administrative Safeguards | Workforce security | Limit PHI-related access to authorized personnel and revoke promptly. | Access reviews, JML records. |
| HIPAA-ADM-004 | Administrative Safeguards | Information access management | Enforce least privilege for systems containing PHI metadata or PHI-capable configs. | IAM exports, role reviews. |
| HIPAA-ADM-005 | Administrative Safeguards | Security awareness and training | Train personnel on PHI handling, safe debugging, incident reporting, and no prompt logging. | Training records. |
| HIPAA-ADM-006 | Administrative Safeguards | Security incident procedures | Maintain HIPAA-specific incident and breach assessment procedure. | Incident records, breach assessment records. |
| HIPAA-ADM-007 | Administrative Safeguards | Contingency planning | Maintain backup, disaster recovery, and emergency mode procedures. | Backup/DR evidence, runbook tests. |
| HIPAA-ADM-008 | Administrative Safeguards | Evaluation | Review HIPAA safeguards periodically and after major changes. | Review records. |
| HIPAA-ADM-009 | Administrative Safeguards | Business associate contracts | Execute BAA before PHI production traffic and require subcontractor restrictions where PHI may flow. | Signed BAA, route approval, subprocessor approvals. |
| HIPAA-PHY-001 | Physical Safeguards | Facility/device controls | Use cloud data centers and cloud provider physical controls; restrict operator endpoint access. | GCP compliance docs, endpoint inventory, access policy. |
| HIPAA-PHY-002 | Physical Safeguards | Workstation use/security | Production admin endpoints must use MFA, disk encryption where available, screen lock, and no local PHI storage. | Endpoint attestation/checklist. |
| HIPAA-TEC-001 | Technical Safeguards | Access control | Authenticate API/admin access, hash API keys, enforce workspace isolation, and restrict route policies for PHI. | API key tests, workspace scoping tests, IAM evidence. |
| HIPAA-TEC-002 | Technical Safeguards | Audit controls | Log metadata, request IDs, auth events, billing events, and operational events without prompt/output content. | Log samples, redaction tests. |
| HIPAA-TEC-003 | Technical Safeguards | Integrity | Protect credit ledger, route authorization, and PHI route policy from unauthorized changes. | CI/deploy logs, tests, access reviews. |
| HIPAA-TEC-004 | Technical Safeguards | Person/entity authentication | Require API keys/sessions and MFA for admin systems. | Auth config, MFA evidence. |
| HIPAA-TEC-005 | Technical Safeguards | Transmission security | Use TLS and attested gateway prompt path; downstream provider transmission follows route/provider contract. | TLS evidence, trust page, provider route approval. |
| HIPAA-BA-001 | BAA Required Terms | Permitted use/disclosure | BAA defines permitted uses and prohibits unapproved disclosures. | Signed BAA. |
| HIPAA-BA-002 | BAA Required Terms | Safeguards | BAA requires appropriate safeguards and Security Rule compliance for ePHI. | Signed BAA, policy binder. |
| HIPAA-BA-003 | BAA Required Terms | Reporting | BAA defines incident/breach reporting obligations. | Signed BAA, incident policy. |
| HIPAA-BA-004 | BAA Required Terms | Subcontractors | Subcontractors with PHI access must have equivalent restrictions. | Subprocessor approvals, vendor reviews. |
| HIPAA-BA-005 | BAA Required Terms | Return/destroy PHI | BAA defines return/destroy obligations at termination. | Signed BAA, retention/deletion records. |

## Key Limitation

A signed BAA is required but not sufficient. Customer must also approve downstream model providers that may receive PHI. If a provider cannot support the customer's PHI requirements, that provider must be excluded from the route policy.
