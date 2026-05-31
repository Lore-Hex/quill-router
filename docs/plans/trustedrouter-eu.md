# eu.trustedrouter.com — independent EU-sovereign stack on AWS Frankfurt

**Status:** Phase 1 in progress (storage_aws.py)
**Decided:** 2026-05-24
**Owner:** Joseph

## TLD decision (2026-05-24)

**Shipping V1 on the `eu.trustedrouter.com` subdomain.** The original
plan was `trustedrouter.eu` but two friction points pushed us to defer:

1. The `.eu` TLD has an EU-residency requirement (EURid post-Brexit
   rules) that Lore Hex Corp doesn't satisfy directly. A nominee
   service (Gandi "EU Trustee", ~€20/yr) solves it but adds a
   third-party touch-point on the registrar contact.
2. Time-to-ship matters more than the marketing-positioning delta for
   V1. Subdomain is available immediately.

The subdomain approach is **reversible**: once V1 has paying EU
customers asking for a `.eu` URL (or once regulated-industry
procurement specifically requires it), we can register `trustedrouter.eu`
via Gandi Trustee, add it as a Route 53 alias to the same AWS Frankfurt
ALB, and serve both URLs in parallel. No code changes needed.

For V1 the subdomain shape is:
  * `eu.trustedrouter.com` — marketing + console root
  * `api.eu.trustedrouter.com` — OpenAI-compatible API endpoint
  * `trust.eu.trustedrouter.com` — sovereignty/compliance page

DNS: the existing Cloud DNS zone `trustedrouter-com` gains a new CNAME
`eu` → AWS Frankfurt ALB hostname. TLS via Let's Encrypt for
`eu.trustedrouter.com` and `*.eu.trustedrouter.com`.

**Marketing copy** still positions this as "TrustedRouter EU" or "TR
Sovereign EU" — the `.com` ancestry is honest but de-emphasized.

## Why this exists

EU enterprise procurement (financial services, healthcare, public sector)
increasingly *requires* an "EU-only data path" — no US-cloud touchpoint,
no transatlantic data flow, Schrems-II-defensible. Cross-region Spanner
still legally counts as "global Google" so the existing
`trusted-router-nam6` instance does not satisfy this.

eu.trustedrouter.com is an **independent product on independent
infrastructure**:

| Property | trustedrouter.com | eu.trustedrouter.com |
|---|---|---|
| Cloud | GCP | AWS |
| Region | nam6 multi-region | eu-central-1 (Frankfurt) |
| Storage | Spanner | Aurora PostgreSQL Serverless v2 |
| Compute | Cloud Run (6 regions) | ECS Fargate |
| Enclave | Confidential GKE (existing) | AWS Nitro Enclaves |
| DNS | GCP Cloud DNS | AWS Route 53 |
| Secrets | GCP Secret Manager | AWS Secrets Manager |
| KMS | GCP Cloud KMS | AWS KMS |
| LLM providers | OpenAI, Anthropic-direct, Bedrock-US, Cerebras, Gemini, etc. | Mistral, Aleph Alpha, Bedrock-EU, Anthropic-via-Vertex-EU (when launched) |

**Critical property: ZERO shared dependencies.** A GCP-global outage cannot
take down eu.trustedrouter.com. An AWS-global outage cannot take down
trustedrouter.com. Reliability for clients using BOTH is multiplicative:
~99.99% × ~99.99% = **~99.99999999%** (effectively perfect) — without
any failover orchestration on our side.

This is strictly better than Stage 5a (eur3 hot replica) which is still
single-cloud-Spanner-capped.

## Identity / billing model

**Decided: Federated identity, separate billing.**

- Single OAuth signup (Google/GitHub) creates a workspace on the side
  the user landed on
- Webhook fires to the other side to create a shadow workspace with the
  same `oauth_sub` claim
- Two independent credit pools — customer tops up each side separately
  (V1 — credit-transfer operator script handles edge cases)
- Failover from client side: customer's SDK has two API keys, falls
  through on 5xx from primary
- Console on each side shows a "you have a workspace on the other
  regional stack" callout

Federation surface:
- `POST /internal/federated-signup` on each side, called by the other's
  signup flow, signed with `TR_FEDERATION_HMAC_SECRET`
- `federated_identity` table (kind = `federated_identity`, id =
  `oauth_sub`, body = `{remote_side_workspace_id, last_sync_at}`)
- Eventually consistent — no synchronous cross-cloud calls in the
  signup hot path; webhook fires fire-and-forget after local signup
  commits

Customer-facing rule: **a workspace's data NEVER leaves its region.**
Only the OAuth `sub` claim (which is opaque) crosses. PII, prompts,
responses, credits, usage — all region-local. This is what makes the
sovereignty story defensible.

## Build phases

### Phase 0 — Foundations (USER ACTION REQUIRED)
1. Buy `eu.trustedrouter.com` (Namecheap, ~€10/yr)
2. Create a separate AWS account for the EU stack (AWS Organizations
   sub-account preferred; standalone OK for MVP). Clean blast radius +
   billing isolation + IAM trust boundary.
3. File AWS support ticket for service quota raises in eu-central-1:
   Aurora Serverless v2 ACUs ≥ 32, ECS Fargate vCPU quota ≥ 50, Nitro
   Enclaves instance type quotas (m5n.xlarge / m5n.2xlarge) ≥ 50
4. AWS budget alarm at $500/mo as runaway-cost backstop

### Phase 1 — Storage layer port (CAN DO ALONE)
1. `src/trusted_router/storage_aws.py` — PostgreSQL-backed Store
   implementing the same `store_protocol.Store` Protocol as
   `storage_gcp.py`. 80 methods to port.
2. `src/trusted_router/secrets_aws.py` — AWS Secrets Manager wrapper
3. Schema migration: `infra-aws/migrations/0001_initial.sql` — same
   `tr_entities` shape as Spanner but as a Postgres table with JSONB
   body
4. Local dev: `docker-compose.yml` with Postgres so the port can be
   developed against without touching AWS
5. New env var `TR_STORAGE_BACKEND=aws-postgres` routes to the new
   implementation
6. Existing 705-test suite runs against `[memory, spanner-bigtable,
   aws-postgres]` backends — proves API parity

**Schema notes:**

```sql
CREATE TABLE tr_entities (
  kind VARCHAR(64) NOT NULL,
  id VARCHAR(512) NOT NULL,
  body JSONB NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (kind, id)
);
CREATE INDEX idx_tr_entities_updated_at ON tr_entities(updated_at DESC);
-- Match Spanner's commit-timestamp semantics via a BEFORE-UPDATE trigger
CREATE OR REPLACE FUNCTION tr_entities_touch_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER tr_entities_touch_updated_at_trg
  BEFORE UPDATE ON tr_entities
  FOR EACH ROW EXECUTE FUNCTION tr_entities_touch_updated_at();
```

Postgres transactional model is identical to Spanner's read-write
transactions, so the existing transactional code in `storage_gcp.py`
ports almost line-for-line — just swap `database.run_in_transaction()`
for SQLAlchemy or psycopg's transaction context manager.

### Phase 2 — AWS infrastructure (USER ACTION + ME)
1. Terraform module `infra-aws/`:
   - VPC with 3 AZ subnets in eu-central-1
   - Aurora Serverless v2 PostgreSQL cluster (min 0.5 ACU, max 8 ACU,
     auto-pause off — we need sub-second cold-start)
   - ECS Fargate cluster + service + ALB
   - KMS keyring `trustedrouter-eu-byok`
   - Secrets Manager namespace `trustedrouter-eu/*`
   - Route 53 hosted zone `eu.trustedrouter.com`
   - CloudWatch + CloudWatch Logs for observability (vs Sentry/Axiom on
     .com)
2. Cross-region Aurora backups to eu-west-1 (DR)
3. Initial deploy of TR control plane to ECS Fargate
4. Synthetic monitor running against `https://eu.trustedrouter.com/v1/healthz`

### Phase 3 — Federation layer (CAN DO ALONE; after Phase 1+2)
1. `federated_identity` entity kind on both sides
2. `POST /internal/federated-signup` handler on each side
3. HMAC-signed webhook trigger in the signup flow (fire-and-forget +
   retry queue if delivery fails)
4. Console UI: "you also have a workspace on eu.trustedrouter.com" banner
5. Operator script: `scripts/transfer_credits.py wid amount --from .com
   --to .eu`

### Phase 4 — Nitro enclave on AWS (Stage 4 from old plan, EU edition)
1. `tools/deploy-aws-nitro.sh` — provisions m5n.xlarge ASG (1 prewarm,
   max 50) in eu-central-1
2. Parent vsock-relay process per Nitro instance
3. AWS NLB on `enclave.eu.trustedrouter.com`
4. Same `cloud_aws,llm_multi` build of `enclave-go/` — the AWS-side
   build already exists; this is operational work

### Phase 5 — EU-specific provider mix (CAN DO ALONE)
1. New provider adapters: Mistral (already done), Aleph Alpha,
   Bedrock-EU
2. Move provider mix config to be per-stack — `.com` and `.eu` ship
   different catalog snapshots
3. Bedrock-EU integration in the enclave (Bedrock is AWS-native, fits
   the Nitro path)

### Phase 6 — Marketing + cross-promotion (USER + ME)
1. Landing pages on `eu.trustedrouter.com`:
   - "Sovereign EU AI Gateway"
   - GDPR / Schrems II compliance page
   - Data-flow diagram showing no US-cloud touch
2. Cross-promotion banners on both sides
3. "Mirror my workspace to .eu" one-click migration

## Estimated timeline

- Phase 1: 2 weeks (storage_aws.py is mechanical but large; 80 methods)
- Phase 2: 1-2 weeks (Terraform module + initial deploy)
- Phase 3: 1 week (federation webhook + UI)
- Phase 4: 2 weeks (Nitro enclave deploy + provider EU mix)
- Phase 5: 1 week
- Phase 6: 1 week

**Total to V1 public launch: 6-8 weeks.**

## Cost (steady-state)

| Component | Monthly |
|---|---|
| Aurora Serverless v2 (0.5-8 ACU) | ~$200 average |
| ECS Fargate (0.5 vCPU, 1GB × always-on) | ~$15 |
| ALB | ~$25 |
| Nitro enclave (1 m5n.xlarge prewarm + ASG burst) | ~$140 |
| KMS | ~$5 |
| Route 53 + queries | ~$3 |
| CloudWatch | ~$20 |
| Egress (cross-cloud for federation webhook, EU residency) | ~$5 |
| **Total** | **~$413/mo** |

Roughly 1/3 of the .com infra cost. Offset by EU residency pricing
premium — enterprise customers pay 2-3× for sovereign deployments.

## What we explicitly are NOT doing

- **Replication between .com and .eu storage** — defeats sovereignty
- **Synchronous cross-cloud calls in hot path** — defeats reliability
- **Pooled credits across stacks** — defeats blast-radius isolation
- **Identical provider mix on both sides** — .eu picks EU-hosted
  providers by default; .com keeps US-hosted; explicit choice required
  for cross-region provider use

## What we'll publish for credibility

- Architecture diagram showing no US-cloud touch on .eu data flows
- Bedrock-EU + Mistral + Aleph Alpha provider list with their data-
  residency commitments
- Independent auditor SOC2 Type II / ISO 27001 path within 6 months of
  launch
- DPA (Data Processing Agreement) template ready at launch for EU
  customer procurement

## Decisions log

- 2026-05-24: Frankfurt over Ireland/Stockholm (residency optics)
- 2026-05-24: Aurora PostgreSQL over DynamoDB (closer to Spanner shape,
  less porting friction)
- 2026-05-24: Federated identity + separate billing over fully-separate
  accounts (better UX, marginal complexity)
