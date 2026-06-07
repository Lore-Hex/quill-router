# Incident Response Policy

Status: draft for management approval.

Owner: Security Officer.

## Purpose

Provide a consistent process for identifying, triaging, containing, resolving, and communicating security, availability, privacy, and billing incidents.

## Scope

Security events, data exposure, prompt-path trust failures, service downtime, billing errors, lost credits, provider routing failures, key compromise, account takeover, infrastructure compromise, and privacy incidents.

## Severity Levels

- SEV0: Confirmed customer data exposure, prompt/output leakage, signing/trust compromise, active attacker in production, or global prompt-path outage.
- SEV1: Regional prompt-path outage, billing integrity issue, unauthorized admin access, or material privacy/security control failure.
- SEV2: Degraded service, provider-fallback failure, customer-impacting dashboard issue, or suspected but unconfirmed security incident.
- SEV3: Low-risk bug, isolated support issue, monitoring noise, or non-production incident.

## Response Procedure

1. Detect and create incident record.
2. Assign incident commander and severity.
3. Preserve request IDs, logs, traces, deployment references, and affected customer/workspace IDs.
4. Contain the issue.
5. Eradicate root cause.
6. Recover service.
7. Notify affected customers if required.
8. Complete post-incident review with action items.

## Prompt-Path Special Rules

- If attestation verification fails, production prompt traffic must fail closed.
- Do not route prompt traffic to a non-attested fallback.
- Do not add prompt/output content to durable logs during debugging.
- Use request IDs, metadata, synthetic probes, and customer-provided reproduction inputs only with explicit approval.

## Notification

Security or privacy incident notifications must be approved by the Security/Legal owner. Contractual notification windows are governed by the signed customer agreement. If no agreement exists, notify without undue delay once facts are reasonably confirmed.

## Evidence

- Incident record.
- Timeline.
- Logs and request IDs.
- Root cause analysis.
- Customer notification record.
- Remediation and verification evidence.
