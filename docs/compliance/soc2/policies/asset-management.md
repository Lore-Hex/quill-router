# Asset Management Policy

Status: draft for management approval.

Owner: Infrastructure Owner.

## Purpose

Maintain an inventory of systems, services, repositories, domains, and data stores that support TrustedRouter.

## Asset Types

- Source repositories.
- CI/CD workflows.
- Cloud projects and services.
- Cloud Run services and jobs.
- Confidential Space/gateway workloads.
- Spanner databases.
- Bigtable instances/tables.
- KMS keys.
- Secret Manager secrets.
- DNS zones and domains.
- Payment/observability/email SaaS accounts.
- Service accounts and workload identities.
- Laptops or operator endpoints used for production administration.

## Policy

- Production assets must have an owner.
- Assets must be classified by criticality and data sensitivity.
- New production assets must be added to the inventory before or immediately after deployment.
- Assets storing secrets or customer metadata require encryption and access controls.
- Unused assets must be decommissioned.
- Asset inventory must be reviewed quarterly.

## Evidence

- Asset inventory.
- Cloud asset exports.
- Repository list.
- Domain/DNS inventory.
- Review records.
