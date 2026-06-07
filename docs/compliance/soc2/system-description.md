# TrustedRouter System Description

Status: draft for SOC 2 Type I readiness.

## Company

Lore Hex Corp is a Delaware C Corporation operating TrustedRouter.

Address: 1111 Brickell Ave, Floor 10, Miami, FL 33131  
Telephone: +1-305-239-7350  
EIN: 41-5339728  
DUNS: 144992055

## Product

TrustedRouter is an OpenAI-compatible AI routing service. Customers use one API key and route requests to downstream model providers through TrustedRouter's hosted control plane and attested API gateway.

## Services In Scope

- Public website and dashboard at `trustedrouter.com`.
- Attested API plane at `api.trustedrouter.com`.
- Trust evidence page at `trust.trustedrouter.com`.
- Public status and synthetic monitoring.
- API key, workspace, billing, credits, routing, BYOK metadata, broadcast destinations, and model/provider catalog.
- Credit reservation, settlement, refund, and usage metadata.
- BYOK envelope encryption and controlled release to the attested gateway.

## Infrastructure

- Google Cloud Platform for Cloud Run, Confidential Space, Spanner, Bigtable, KMS, Secret Manager, Cloud Logging, and supporting services.
- Cloudflare for DNS, edge caching, and non-prompt site delivery.
- GitHub for source control, CI, and deployment workflows.
- Stripe and PayPal for payment processing.
- Amazon SES/SNS for transactional email and delivery notifications.
- Sentry and Axiom for control-plane errors and operational log visibility.
- Downstream model providers as request-specific subprocessors.

## Data Flow

1. Customer creates a workspace and API key.
2. Customer sends inference traffic to the API plane.
3. The production prompt path terminates TLS inside the attested gateway.
4. The gateway authorizes with the control plane using metadata.
5. The gateway routes to an allowed downstream provider.
6. The control plane records metadata, credit reservations, settlement, refunds, and activity.
7. Prompt and output content are not stored by default.

## Data Types

- Account identity: email, wallet address, session metadata, workspace membership.
- API key metadata: salted hash, display hint, limits, last use, disabled state.
- Billing data: Stripe/PayPal customer identifiers, payment method references, credit ledger, invoices, webhooks.
- Routing metadata: model, provider, usage type, region, token counts, cost, latency, finish reason, request status.
- BYOK metadata: provider, key hint, encrypted envelope, KMS/envelope reference.
- Optional broadcast configuration: endpoint, type, redacted headers, encrypted destination secrets.
- Prompt and output content: transient processing in the gateway/provider path only by default.

## Trust Boundary

TrustedRouter can provide evidence for the router code path and hosted gateway. It cannot make every downstream model provider confidential. Provider claims are tracked separately on provider and model pages, and customers must select routes that match their requirements.

## Commitments

- No prompt/output durable storage by default.
- No Sentry in the attested prompt gateway.
- API keys are hashed and never returned after creation.
- BYOK secrets are envelope encrypted and returned only to the gateway authorization path.
- Production prompt routes fail closed rather than falling back to a non-attested path.
- Public legal pages must not claim SOC 2, HIPAA, ISO 27001, or a signed DPA/BAA until obtained.
