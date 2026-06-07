# Change Management And Secure SDLC Policy

Status: draft for management approval.

Owner: Engineering Owner.

## Purpose

Ensure production changes are reviewed, tested, traceable, and reversible.

## Scope

Application code, gateway code, infrastructure, configuration, secrets, DNS, CI/CD, provider catalog changes, billing logic, security controls, and public legal/security claims.

## Policy

- Source code lives in GitHub repositories controlled by Lore Hex Corp.
- Production changes must be traceable to a commit, workflow run, or documented emergency record.
- CI must run relevant tests before merge or deployment.
- Deploys must use approved workflows or documented manual runbook steps.
- Production prompt-path changes require security-sensitive review.
- Public compliance claims require review by the Security/Legal owner.
- Rollback procedures must exist and be tested or exercised.

## Normal Change Procedure

1. Create a branch or commit with scoped changes.
2. Run local tests appropriate to risk.
3. Submit or merge via protected workflow where available.
4. CI runs linting, type checks, unit tests, and relevant integration tests.
5. Deploy workflow stages traffic by region and runs canary checks.
6. Prod smoke verifies critical surfaces after deploy.
7. Record evidence for audit-relevant changes.

## Emergency Change Procedure

1. Stabilize the incident using the fastest safe path.
2. Avoid destructive data operations without explicit approval.
3. Record reason, approver, commands, affected services, validation, and rollback status.
4. Create a follow-up issue or record within one business day.
5. Review emergency change in post-incident or weekly control review.

## Security Requirements

- Dependencies are pinned or locked where supported.
- Secrets are never committed.
- Prompt/output content must not be added to logs, tests, or fixtures unless synthetic.
- Money values are stored and tested as integers, not floats.
- Unsupported API features return explicit errors rather than silent behavior.
- Billing and settlement changes require idempotency and concurrency tests.

## Evidence

- Git commits and PRs.
- CI logs.
- Deploy workflow logs.
- Prod smoke results.
- Emergency change records.
- Branch protection screenshots.
