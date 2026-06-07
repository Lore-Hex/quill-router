# SOC 2 Type I Evidence Checklist

Status: evidence collection checklist. Do not mark an item complete until evidence is exported or linked.

## Governance And Policies

- Current policy binder commit hash.
- Policy owner list.
- Management approval record for all policies.
- Security responsibility matrix.
- List of employees/contractors with security responsibilities.
- Signed confidentiality agreements.
- Security training acknowledgements.

## Company And Legal

- Delaware C Corporation registration evidence.
- EIN evidence.
- DUNS evidence.
- Customer DPA/BAA drafts.
- Public legal packet screenshot or URL.
- Subprocessor list screenshot or URL.
- Privacy/security/public provider posture pages.

## Access Control

- Google Cloud IAM export.
- GitHub org/repo access export.
- Cloudflare account access export.
- Stripe, PayPal, AWS SES/SNS, Sentry, Axiom admin access export.
- MFA enforcement evidence for admin systems.
- Quarterly access review record.
- Joiner/mover/leaver tickets or records.
- Break-glass account inventory and access procedure.

## Change Management

- Branch protection settings.
- CI workflow configuration and recent successful runs.
- Deploy workflow configuration and recent deploy run.
- Prod smoke run evidence.
- Release rollback procedure.
- Sample change records for normal and emergency changes.

## Infrastructure And Security

- Cloud Run service config for all regions.
- Confidential Space/gateway attestation evidence.
- Trust page evidence: source commit, image reference, digest.
- Spanner database config.
- Bigtable tables/app profiles.
- KMS key and Secret Manager inventory.
- BYOK encryption test evidence.
- DNS records for `trustedrouter.com`, `api.trustedrouter.com`, `trust.trustedrouter.com`, and status domains.
- TLS certificate evidence.
- Rate limit configuration.
- WAF/Cloud Armor/Cloudflare protection settings if enabled.

## Logging And Monitoring

- Sentry project settings and scrubbing config.
- Evidence Sentry is not configured in the enclave/gateway prompt path.
- Axiom dataset/settings.
- Synthetic monitor configuration.
- Status page screenshot and JSON.
- Alert routes and paging/on-call configuration.
- Sentry floodgate/debounce config.
- Sample logs showing request IDs without prompts or outputs.

## Availability And DR

- Multi-region deployment evidence.
- Regional health check evidence.
- Backup configuration for Spanner/Bigtable, if enabled.
- Restore test evidence.
- Runbook for region drain, rollback, provider disable, Spanner degraded mode, Bigtable degraded mode, and DNS failover.
- Business continuity contact list.
- RTO/RPO targets approved by management.

## Vulnerability Management

- Dependency scanning evidence.
- Secret scanning evidence.
- Container/image scanning evidence.
- Recent vulnerability triage record.
- Patch SLAs.
- Risk acceptance records for unpatched findings.

## Vendor Management

- Vendor inventory.
- Vendor review records for GCP, Cloudflare, GitHub, Stripe, PayPal, AWS SES/SNS, Sentry, Axiom, and model providers.
- Subprocessor policy URLs.
- Data access classification for each vendor.
- Contract/DPA/BAA status for relevant vendors.

## Privacy And Confidentiality

- Data classification policy.
- Retention policy.
- Public privacy/security statements.
- Tests proving prompts/outputs are not stored in Spanner, Bigtable, Sentry, logs, dashboard payloads, or broadcast metadata by default.
- Content export opt-in tests.
- BYOK redaction tests.
- API key one-time display/hash tests.
- Customer deletion/export procedure.

## Processing Integrity

- Billing unit tests.
- Stripe and PayPal webhook idempotency tests.
- Credit reservation/settlement/refund tests.
- Workspace scoping tests.
- API route compatibility tests.
- Reconciliation worker evidence or documented compensating procedure.

## Management Assertion Package

- Final scope statement.
- Final system description.
- Control matrix.
- Exception list.
- Open risk register.
- Evidence index.
- Management representation letter draft.
- Auditor-provided PBC list response.
