# Vendor And Subprocessor Management Policy

Status: draft for management approval.

Owner: Privacy/Legal Owner.

## Purpose

Ensure vendors and subprocessors are understood, reviewed, and disclosed according to their data access.

## Scope

Platform vendors, payment processors, email providers, observability tools, code hosting, cloud providers, downstream model providers, and any vendor that may process customer data or operational metadata.

## Policy

- Maintain a vendor inventory.
- Maintain a public subprocessor list.
- Classify each vendor by data access.
- Review vendors before production use when they process customer data.
- Link public policy/security documentation where available.
- Track DPA/BAA/SOC status where relevant.
- Re-review critical vendors at least annually and after material service changes.

## Data Access Levels

- Level 0: No customer data.
- Level 1: Public website or operational metadata only.
- Level 2: Account, billing, or support metadata.
- Level 3: Secrets or security-sensitive metadata.
- Level 4: Prompt/output content in transit for selected routes.

## Model Provider Rule

Downstream model providers are subprocessors only for traffic routed to them. For legal, healthcare, or regulated workloads, customers should use `trustedrouter/zdr`, `trustedrouter/e2e`, or explicit allowlists approved in writing.

## Evidence

- Vendor inventory.
- Vendor review records.
- Public subprocessor list.
- Provider policy URLs.
- Contracts and DPAs where applicable.
