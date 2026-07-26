# Data Classification And Retention Policy

Status: draft for management approval.

Owner: Privacy/Legal Owner.

## Purpose

Classify TrustedRouter data and define retention expectations.

## Classification

- Public: public website, docs, status summaries, trust records, model/provider catalog.
- Internal: runbooks, internal procedures, non-sensitive operational metadata.
- Confidential: customer account data, workspace data, API key metadata, billing metadata, usage metadata.
- Restricted: API key raw values, BYOK secrets, payment tokens, auth session secrets, KMS keys, production credentials.
- Transient Sensitive: prompt and output content processed through the gateway and downstream providers.

## Handling Requirements

- Public data can be published intentionally.
- Internal data is limited to personnel and contractors with a business need.
- Confidential data requires access control and encryption in transit and at rest.
- Restricted data requires least privilege, encryption, redaction, and no source-control storage.
- Transient Sensitive data must never be stored durably or logged by TrustedRouter.

## Retention

- Prompt/output content: never logged or stored by TrustedRouter.
- Usage metadata: retained as needed for billing, security, abuse prevention, support, product analytics, and legal obligations.
- Synthetic monitoring raw samples: retained according to configured raw retention.
- Status rollups: retained according to configured rollup retention.
- Payment records: retained as required for tax, accounting, chargeback, and legal obligations.
- Logs: retained according to platform configuration and scrubbed of prompts/outputs/API keys.
- Deleted workspaces: metadata deletion/export handled according to customer agreement and legal obligations.

## Content Export

Prompt/output export to PostHog or webhook destinations requires explicit destination-level opt-in by the customer. TrustedRouter sends the content to the customer-selected destination without retaining it. Export destinations are customer subprocessors, not TrustedRouter storage.

## Evidence

- Storage schemas.
- Privacy/security tests.
- Log samples.
- Retention configs.
- Deletion/export records.
