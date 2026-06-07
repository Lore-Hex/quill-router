# PHI Handling Policy

Status: draft for HIPAA readiness.

Owner: Privacy/Legal Owner.

## Purpose

Define how TrustedRouter handles protected health information (PHI) and electronic protected health information (ePHI) when a customer signs a BAA and routes PHI through the service.

## Policy

- PHI production traffic is prohibited unless a BAA is executed.
- PHI routes must be restricted to approved providers or aliases.
- PHI must not be sent through broad provider aliases unless the alias is configured to meet the customer-approved provider posture.
- Prompt/output content containing PHI must not be durably stored by TrustedRouter by default.
- Content export must remain disabled for PHI unless explicitly approved in writing.
- Personnel must not ask customers to paste PHI into support channels.
- Debugging PHI incidents must use request IDs and metadata unless customer counsel approves content review.
- Downstream providers that receive PHI must be approved as subprocessors under the customer BAA or equivalent arrangement.

## PHI Route Requirements

For PHI, use one of:

- `trustedrouter/e2e` when provider-side confidential compute/E2EE is required and approved.
- `trustedrouter/zdr` when zero-retention provider posture is required and approved.
- Explicit provider/model allowlist approved by customer counsel.

Do not use unrestricted `trustedrouter/auto` for PHI unless the customer policy explicitly approves every possible route.

## Support Procedure

1. Ask for request ID, timestamp, workspace, model/provider, and error.
2. Do not request PHI.
3. If content review is unavoidable, require written customer approval and use a secure channel defined by the BAA/support process.
4. Record all access and disposition.

## Evidence

- Signed BAA.
- Route approval record.
- Subprocessor approval record.
- Storage/no-logging tests.
- Support records without PHI.
