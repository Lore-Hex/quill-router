# Logging And Monitoring Policy

Status: draft for management approval.

Owner: Infrastructure Owner.

## Purpose

Detect and respond to security, availability, privacy, billing, and operational issues while avoiding collection of prompt/output content.

## Policy

- Logs should include request IDs, timestamps, service, region, status, error type, latency, and non-sensitive metadata.
- Logs must not include prompts, outputs, API keys, BYOK keys, payment secrets, or auth tokens.
- Sentry is used for control-plane error monitoring and is not configured in the attested prompt gateway.
- Axiom is used for operational metadata where configured.
- Synthetic monitoring collects metadata only.
- Error floodgates should prevent one defect from exhausting monitoring quotas.
- Alerts must be routed for router-core incidents, security incidents, billing integrity failures, and data-handling violations.

## Monitoring Domains

- Public health.
- Attestation.
- Chat/Responses PONG.
- Authorize/settle/refund.
- Provider fallback.
- Billing webhooks.
- Sentry errors.
- Cloud Run availability.
- Database failures.
- Queue backlogs.
- Rate limits and abuse.

## Review

Operational alerts are reviewed during incidents and periodically for noise, coverage gaps, and missing routes.

## Evidence

- Monitoring config.
- Alert rules.
- Sentry/Axiom project settings.
- Synthetic probe configuration.
- Redaction tests.
- Sample redacted logs.
