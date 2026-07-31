# Regional quota leases

Status: **dark foundation, not production authority**

The current typed Spanner counter path remains TrustedRouter's only prepaid
billing authority. Regional quota leases are the planned way to remove a
cross-continent Spanner transaction from first-token latency without changing
the invariant that a workspace cannot spend more credit than it owns.

## Safety model

The global ledger grants a region a bounded amount of already-reserved credit.
Creating a lease and increasing the workspace's global `reserved` total happen
in one exact Spanner transaction. A region can authorize only against its local
durable lease. The maximum unreconciled exposure is therefore the sum of active
lease grants, which the global ledger has already removed from spendable
balance.

This is escrow, not an eventually consistent copy of account balance.

Every lease has:

- one workspace and one region;
- a monotonically increasing fencing token;
- an exact integer microdollar grant;
- a short expiration;
- durable reservation, settlement, and refund records;
- `active`, `draining`, `closed`, or `quarantined` state.

The regional ledger rejects stale fencing tokens, expired leases, duplicate
request IDs with changed fingerprints, settlement above the exact reservation,
and all work after drain begins. It fails closed if its durable store is
unavailable. In-memory quota is never authoritative.

## Intended flow

1. A global Spanner transaction computes a bounded grant and adds that amount
   to the workspace's exact global reserved counter.
2. The signed lease is written to a durable regional ledger with its fencing
   token. The gateway does not authorize from it until both records exist.
3. Regional authorization reserves against the lease in one local transaction.
4. Regional settlement or refund is idempotent and uses the same request ID.
5. A reconciler drains the lease, imports settled spend to Spanner, releases the
   unused global reservation, and closes the lease in one fenced operation.
6. Ambiguous or inconsistent leases are quarantined. They are never guessed
   back into service.

## Initial eligibility

The first pilot is limited to explicitly allowlisted workspaces using uncapped
prepaid API keys. BYOK, capped keys, hard workspace budgets, custom billing
arrangements, orchestration fan-out, and x402 funding stay on the exact global
path until each has an explicit accounting proof.

The grant is the minimum of:

- requested regional capacity;
- an operator-configured per-lease cap;
- a small percentage of current globally available credit.

All values are integer microdollars. No floating-point money enters this path.

## Durable schema sketch

The production implementation should use a regional strongly consistent store,
not process memory. A representative schema is:

```sql
CREATE TABLE tr_regional_quota_lease (
  lease_id STRING(64) NOT NULL,
  workspace_id STRING(64) NOT NULL,
  region STRING(32) NOT NULL,
  fencing_token INT64 NOT NULL,
  granted_micro INT64 NOT NULL,
  state STRING(16) NOT NULL,
  expires_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP OPTIONS (allow_commit_timestamp=true)
) PRIMARY KEY (lease_id);

CREATE TABLE tr_regional_quota_hold (
  lease_id STRING(64) NOT NULL,
  hold_id STRING(64) NOT NULL,
  fingerprint STRING(64) NOT NULL,
  reserved_micro INT64 NOT NULL,
  actual_micro INT64,
  state STRING(16) NOT NULL,
  updated_at TIMESTAMP OPTIONS (allow_commit_timestamp=true)
) PRIMARY KEY (lease_id, hold_id),
  INTERLEAVE IN PARENT tr_regional_quota_lease ON DELETE CASCADE;
```

The final regional store choice must preserve conditional writes and fencing.
Redis without durable, strongly consistent persistence is not acceptable.

## Rollout gates

`TR_REGIONAL_QUOTA_LEASES_ENABLED` defaults to false and production startup
currently rejects true. Ordinary deploys explicitly set it false so stale
configuration cannot activate a second billing authority.

Production activation requires all of the following:

1. Durable regional schema and transactional adapter.
2. Global grant and close transactions that escrow and release exact credit.
3. Reconciliation worker with drift, orphan, and stuck-lease repair.
4. Property tests proving aggregate spend never exceeds the escrowed grant.
5. Crash tests at every boundary, including grant-write ambiguity and regional
   settlement before reconciliation.
6. Fencing tests for region replacement and split-brain workers.
7. Shadow accounting with zero drift on a pilot workspace.
8. A one-workspace rollout with a hard exposure cap and automatic rollback.

Until those gates pass, the latency work in this change improves connection,
TLS, health, and region selection while billing remains exact in Spanner.
