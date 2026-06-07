# Information Security Policy

Status: draft for management approval.

Owner: Security Officer.

Review cadence: at least quarterly and after material product or infrastructure changes.

## Purpose

Protect TrustedRouter systems and customer data through documented security responsibilities, access control, encryption, monitoring, incident response, vendor management, and continuous improvement.

## Scope

This policy applies to Lore Hex Corp personnel, contractors, systems, repositories, cloud infrastructure, third-party services, and production environments supporting TrustedRouter.

## Security Principles

- Least privilege.
- Defense in depth.
- Fail closed for prompt-path trust boundaries.
- No prompt/output durable storage by default.
- Explicit customer opt-in for content export.
- Open-source, auditable implementation where practical.
- Evidence-backed claims. No compliance claim is public until obtained.

## Roles

- Executive Owner: approves risk posture and compliance scope.
- Security Officer: owns security policies, risk register, incident response, and control evidence.
- Infrastructure Owner: owns cloud configuration, deployment, backups, availability, and monitoring.
- Engineering Owner: owns SDLC, tests, code review, release quality, and secure design.
- Privacy/Legal Owner: owns DPA/BAA drafts, subprocessors, privacy statements, and customer legal requests.

## Mandatory Controls

- Production access requires approval, MFA where supported, and least-privilege roles.
- Production changes require source control, CI, and deployment evidence.
- Secrets must not be committed to source and must be stored in approved secret systems.
- Customer API keys must be hashed and raw values shown only once.
- BYOK keys must be encrypted before durable storage.
- Prompt/output content must not be logged or stored by default.
- Security incidents must be recorded, triaged, escalated, and remediated.
- Vendors that process customer data must be listed and reviewed.
- Public trust/security pages must state limitations accurately.

## Exceptions

Exceptions require documented risk acceptance, owner, expiration date, and compensating controls.

## Enforcement

Violations may result in access removal, incident investigation, contract termination, or disciplinary action.
