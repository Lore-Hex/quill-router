# SOC 2 Control Matrix

Status: draft control set for Type I readiness.

The initial report target is Security, Availability, Confidentiality, and Privacy. Processing Integrity is included for billing and routing correctness.

| Control ID | TSC Mapping | Control Objective | Control Activity | Primary Evidence |
|---|---|---|---|---|
| TR-GOV-001 | CC1.1, CC1.2 | Governance responsibilities are assigned. | Lore Hex Corp maintains owners for security, infrastructure, incident response, legal, and compliance policies. | Policy owner list, org chart, policy approval records. |
| TR-GOV-002 | CC1.4 | Personnel understand security responsibilities. | Security and confidentiality expectations are documented and acknowledged by personnel/contractors. | Signed confidentiality agreements, training records. |
| TR-RISK-001 | CC3.1, CC3.2 | Risks are identified and assessed. | Maintain a risk register for product, infrastructure, vendor, privacy, and availability risks. | Risk register, review notes, mitigation owners. |
| TR-RISK-002 | CC3.3, CC9.1 | Fraud and abuse risks are considered. | Rate limits, billing controls, API key controls, and abuse monitoring are reviewed. | Rate-limit settings, incident records, abuse control docs. |
| TR-POL-001 | CC2.1, CC2.2 | Policies are communicated. | Compliance policies are published internally and public legal posture is published externally. | Binder commit, public `/legal`, `/security`, `/providers`. |
| TR-ACCESS-001 | CC6.1, CC6.2 | Access is granted by role. | Production access uses least privilege and role-appropriate permissions. | IAM export, GitHub access export, access review. |
| TR-ACCESS-002 | CC6.3 | Access changes are controlled. | Access is approved, reviewed, modified on role change, and removed on departure. | Joiner/mover/leaver tickets, quarterly access review. |
| TR-ACCESS-003 | CC6.6 | Strong authentication is enforced. | MFA is required for GitHub, cloud provider, payment, DNS, and observability admin accounts where supported. | MFA screenshots/exports, admin account inventory. |
| TR-ACCESS-004 | CC6.7 | Secrets are protected. | Production secrets live in Secret Manager/KMS/envelope storage and are not committed to source. | Secret Manager inventory, secret scanning settings, BYOK tests. |
| TR-CHANGE-001 | CC8.1 | Code changes are reviewed and tested. | Production changes go through version control, CI, and deployment workflows. | PRs, CI runs, deploy logs, branch protection evidence. |
| TR-CHANGE-002 | CC8.1 | Emergency changes are traceable. | Hotfixes require a post-change record and test/deploy evidence. | Change records, runbook entries, incident links. |
| TR-OPS-001 | CC7.1 | System operations are monitored. | Health checks, synthetic probes, Cloud Run health, and status rollups monitor service health. | Status page, synthetic logs, monitor config. |
| TR-OPS-002 | CC7.2 | Anomalies are investigated. | Alerts and operational logs are reviewed for downtime, errors, and abuse. | Sentry/Axiom alerts, incident records, runbook actions. |
| TR-OPS-003 | CC7.3 | Security events are evaluated. | Events are triaged by severity with documented escalation and response. | Incident response records, postmortems. |
| TR-VULN-001 | CC7.1, CC7.2 | Vulnerabilities are identified. | Dependency, container, and code scanning are run before release and reviewed. | GitHub Dependabot/code scanning, container scan evidence. |
| TR-VULN-002 | CC7.4 | Vulnerabilities are remediated. | Critical/high issues are prioritized and tracked to closure or risk acceptance. | Vulnerability tracker, risk acceptance records. |
| TR-DATA-001 | C1.1, P6.1 | Data is classified. | Data classes and handling requirements are documented. | Data classification policy, storage schema review. |
| TR-DATA-002 | C1.2, P4.2 | Prompt/output content is not retained by default. | Durable storage paths store metadata only unless explicit observability content export is enabled. | Security tests, storage schemas, broadcast tests. |
| TR-DATA-003 | C1.1, P5.1 | Customer secrets are encrypted. | BYOK and destination secrets use envelope encryption/KMS and are redacted in responses. | BYOK crypto tests, KMS config, code review. |
| TR-PRIV-001 | P1.1, P2.1 | Privacy notices are available. | Legal/security/provider pages state data handling and provider boundaries. | Public `/legal`, `/security`, `/providers`. |
| TR-PRIV-002 | P3.1, P4.1 | Customer instructions are followed. | Routing preferences, provider filters, BYOK config, and content export toggles govern processing. | API tests, routing tests, broadcast tests. |
| TR-PRIV-003 | P6.4, P6.5 | Deletion and retention are controlled. | Metadata retention, synthetic retention, logs, and customer deletion processes are documented. | Retention policy, deletion request records. |
| TR-AVAIL-001 | A1.1 | Availability objectives are defined. | Router-core, provider-effective, and control-plane SLO classes are documented separately. | README, status page, synthetic monitor config. |
| TR-AVAIL-002 | A1.2 | Capacity and reliability are monitored. | Multi-region Cloud Run, synthetic probes, rollups, and status history are maintained. | Region config, status history, probe records. |
| TR-AVAIL-003 | A1.3 | DR/BCP procedures exist. | Backup, restore, regional drain, provider disable, and rollback runbooks are documented. | Runbook, backup evidence, restore test record. |
| TR-PI-001 | PI1.1 | Billing processing is complete and accurate. | Credit reservations, settlement, refund, and idempotency controls prevent double-charge and overspend. | Billing tests, gateway settlement tests, webhook idempotency tests. |
| TR-PI-002 | PI1.2 | Inputs are validated. | API inputs, unsupported endpoint behavior, money parsing, provider routing, and workspace scoping are tested. | Contract tests, API compatibility tests. |
| TR-PI-003 | PI1.4 | Processing errors are corrected. | Reconciliation handles missing activity rows, stuck reservations, and provider errors. | Reconciliation tests, runbook, error logs. |
| TR-VENDOR-001 | CC9.2 | Vendors are reviewed. | System vendors and model providers are listed, reviewed, and assigned data access levels. | Subprocessor list, vendor review records. |
| TR-VENDOR-002 | CC9.2, P6.1 | Provider privacy posture is tracked. | Provider ZDR/confidential/E2EE claims are sourced and shown publicly. | Provider catalog, policy URLs, review notes. |
| TR-AUDIT-001 | CC4.1, CC4.2 | Controls are monitored. | Control evidence is collected for Type I and periodically reviewed for Type II readiness. | Evidence index, management review records. |

## Auditor Notes

- Exact AICPA criteria mapping must be confirmed with the selected CPA firm.
- For Type I, evidence is point-in-time. For Type II, the same controls need operating evidence over the audit period.
- Public pages must continue to say “not yet obtained” until the auditor issues a report.
