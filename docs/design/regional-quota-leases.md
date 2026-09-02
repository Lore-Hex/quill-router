# Regional quota leases

Status: **canary-capable, off by default, workspace allowlisted**

Global Spanner remains TrustedRouter's prepaid billing source of truth.
Regional quota leases remove the hot global counter mutation from eligible
authorizations without changing the invariant that a workspace cannot spend
more credit than it owns.

## Safety model

The global ledger grants a region a bounded amount of already-reserved credit.
Each active grant also has a transactionally maintained
`regional_quota_lease_open` index row ordered by expiry. Reconciliation reads a
bounded prefix of that index instead of scanning historical closed leases; the
same Spanner transaction removes the index row when it closes the grant.
Creating a lease and increasing the workspace's global `reserved` total happen
in one exact Spanner transaction. A region can authorize only against its local
durable lease. The maximum unreconciled exposure is therefore the sum of active
lease grants, which the global ledger has already removed from spendable
balance.

This is escrow, not an eventually consistent copy of account balance.

Every lease shard has:

- one workspace and one region;
- a monotonically increasing fencing token;
- an exact integer microdollar grant;
- a short expiration;
- durable reservation, settlement, and refund records;
- `active`, `draining`, `closed`, or `quarantined` state.

Each workspace-region pool is split across 16 independently fenced Bigtable
rows. The configured dollar cap and available-balance percentage apply to the
whole pool and are divided across those rows, so sharding does not multiply
financial exposure. A Spanner fence permits only one globally escrowed grant
per row until reconciliation closes it. The request idempotency fingerprint
selects a stable row.

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

## Durable storage

Global grants, fences, and compact reconciliation totals are stored in Spanner.
Each regional lease shard is one row in the
`trustedrouter-regional-quota` Bigtable table. A single-cluster transactional
app profile binds that row to exactly one physical regional writer, and every
transition uses compare-and-swap on a random version value. The table retains
only the latest cell version and expires rows after seven days.

The regional row stores authorization IDs, key hashes, integer amounts,
expiry, and terminal state. It never stores raw keys, prompts, outputs, or
request bodies. The normal typed Spanner authorization and reservation rows
still provide global idempotent replay, but carry a zero global counter hold;
the bounded regional escrow owns that hold until reconciliation.

The initial fixed profile exists only for `us-central1`. Bigtable warns against
transactional profiles that target separate clusters in one replicated
instance because they could write the same row concurrently. TrustedRouter
does not override that guard. A request served from Europe or another region
uses the exact Spanner path until that region has an isolated local ledger.

## Rollout gates

Two independent flags make a rolling deploy safe:

- `TR_REGIONAL_QUOTA_LEASES_ENABLED` is fleet capability. It keeps the fixed
  regional ledger available for settlement, refund, and reconciliation.
- `TR_REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED` is the traffic mutation switch.
  It defaults to false, requires capability, and is the only flag that lets an
  allowlisted authorization create a new regional hold.

Capability in production requires typed request records, the durable settle
outbox, a Bigtable instance, and fixed regional app profiles. Issuance also
requires a non-empty workspace allowlist. An issuance-off revision can still
finish a regional hold created by an issuance-on peer, which is the required
mixed-revision behavior during a ramp or rollback.

The rollout reads preserved quota state from the revision receiving exactly
100% of primary-region traffic. It never reads the service template, latest
created revision, or latest ready revision, because all three can name a failed
candidate after traffic has rolled back. An ambiguous traffic split or any
control-plane read error aborts. Only an exact missing-service response is
treated as a fresh environment, with issuance off.

Activation is intentionally two separate full-fleet deployments:

1. Compatibility phase — deploy every region with issuance explicitly false.
   Wait for the normal staged traffic, billing-path, and production smoke gates
   to complete everywhere.
2. Issuance phase — dispatch the same workflow with issuance true. Before
   creating any issuance-enabled revision, the rollout checks every active
   control-plane region for `TR_REGIONAL_QUOTA_LEASES_ENABLED=true` and an
   explicit boolean `TR_REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED` marker. Missing,
   split, unreadable, or incapable regions fail closed.

Operator commands (run only from the reviewed `main` commit) are:

```bash
gh workflow run deploy.yml --repo Lore-Hex/quill-router --ref main \
  -f regional_quota_lease_issuance=false
```

After that run is fully green in every region:

```bash
gh workflow run deploy.yml --repo Lore-Hex/quill-router --ref main \
  -f regional_quota_lease_issuance=true
```

Routine workflow dispatches use `preserve`; push-triggered deploys normalize an
empty input to the same behavior. The shell writes only a normalized boolean to
the Cloud Run revision.

Reconciliation is intentionally independent from traffic issuance. A
versioned one-shot Cloud Run Job continues draining leases that were already
issued even after operators disable the serving feature, so a kill switch
cannot strand globally reserved credit. Cloud Scheduler invokes the job with
Google OAuth; the deploy identity never reads the internal gateway token. A
new version must complete a real Spanner read and a Bigtable data read through
every fixed app profile before the stable schedule points to it. A private GCS
generation-guarded admission lease runs before importing Sentry, Spanner, or
Bigtable. Cloud Scheduler's `jobs:run` request completes when Cloud Run accepts
an execution, so a slow execution can overlap the next one-minute tick even
though Scheduler itself has no outstanding request. Overlaps exit before
opening database clients; the admitted worker then takes the existing Spanner
fencing lock before reconciliation. Clean runs publish the
`job:regional-quota-reconcile` heartbeat; failures reach Cloud Logging and
Sentry.

Production activation requires all of the following:

Implemented gates include the transactional adapter, exact global grant and
close transactions, a once-per-minute reconciler, integer-only property tests,
ambiguous Bigtable commit replay, fencing, concurrent idempotency, exact key
usage import, and 16-way local sharding. Production activation remains a
one-workspace canary. Any local read, conditional write, missing profile, or
initialization ambiguity falls back to exact Spanner authorization. Missing
lease state is quarantined and its global escrow is not guessed back into the
available balance.

Before expanding the allowlist, verify zero reconciliation errors, bounded
lease-row size and CAS retries under the canary's real concurrency, no global
counter drift, and successful failback when a Bigtable profile is disabled.
