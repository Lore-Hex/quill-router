# Access Control Policy

Status: draft for management approval.

Owner: Security Officer.

## Purpose

Ensure only authorized personnel and systems can access TrustedRouter production systems and customer data.

## Scope

Applies to Google Cloud, Cloudflare, GitHub, Stripe, PayPal, AWS SES/SNS, Sentry, Axiom, DNS, production databases, KMS, Secret Manager, CI/CD, and administrative SaaS.

## Policy

- Access must be role-based, least-privilege, and approved before grant.
- MFA is required for all administrative accounts where supported.
- Shared user accounts are prohibited except documented break-glass accounts.
- Break-glass credentials must be inventoried, protected, and reviewed.
- Access to production data must be limited to personnel with a business need.
- Access changes must be recorded.
- Departing personnel access must be removed promptly.
- Access reviews must occur at least quarterly.

## Joiner/Mover/Leaver Procedure

1. Request access with business justification, system, role, and duration.
2. Security or system owner approves access.
3. Admin grants least-privilege access.
4. Access is recorded in the access inventory.
5. Role changes trigger review and removal of stale permissions.
6. Departures trigger immediate revocation of production, source, payment, DNS, monitoring, and email access.

## Quarterly Access Review

The Security Officer reviews admin access to:

- Google Cloud project and service accounts.
- GitHub org/repositories and branch protection admins.
- Cloudflare DNS/edge account.
- Stripe and PayPal dashboards.
- AWS SES/SNS.
- Sentry and Axiom.
- Production databases and KMS/Secret Manager.

Review output must include reviewer, date, systems reviewed, changes made, exceptions, and sign-off.

## Service Accounts

- Service accounts must have a named owner.
- Permissions must be minimal for the workload.
- Keys are prohibited unless no workload identity alternative is available.
- Service account permissions are reviewed at least quarterly.

## Evidence

- IAM exports.
- GitHub access exports.
- SaaS admin screenshots/exports.
- Access review records.
- Joiner/mover/leaver tickets.
- MFA evidence.
