# Backup, Disaster Recovery, And Business Continuity Policy

Status: draft for management approval.

Owner: Infrastructure Owner.

## Purpose

Maintain service continuity and recoverability for TrustedRouter systems.

## Availability Classes

- Router core: API reachability, attested gateway, auth/authorize, fallback, settlement/refund durability.
- Provider-effective: successful model response after fallback.
- Control plane: dashboard, billing UI, keys, credits, docs, status.

## Targets

Initial public claim remains conservative. Internal router-core SLO targets may be higher than public contractual claims only after measured performance supports them.

Draft targets for Type I evidence:

- Router core RTO: 1 hour.
- Control plane RTO: 4 hours.
- Billing ledger RPO: governed by Spanner durability and backup/export configuration.
- Public website RTO: 4 hours.

Final RTO/RPO must be approved before audit.

## DR Controls

- Multi-region Cloud Run/control-plane deployment for warm regions.
- Attested gateway regional endpoints.
- Provider fallback and provider disable runbooks.
- Spanner/Bigtable backup or export evidence.
- DNS/Cloudflare failover procedure.
- Rollback workflow.
- Synthetic probes and status page.

## Business Continuity

- Maintain contact list for incident response.
- Maintain runbooks for regional outage, provider outage, billing degradation, DNS outage, and deploy rollback.
- Review continuity procedures at least annually and after major incidents.

## Testing

At least annually or before Type I evidence collection:

- Verify backup configuration.
- Test a restore or documented restore dry run.
- Execute or simulate region drain.
- Execute or simulate deploy rollback.
- Confirm alert routing.

## Evidence

- Backup configuration.
- Restore test record.
- Region/deploy evidence.
- Synthetic status.
- Runbook links.
