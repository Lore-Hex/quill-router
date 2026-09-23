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

Routine workflow dispatches use `preserve` to keep the live issuance marker;
explicit `true` and `false` also override the code pin. Push-triggered deploys
(absent or empty input) use `REGIONAL_QUOTA_LEASE_ISSUANCE_PINNED=false` in
`scripts/deploy/rollout.sh` while the regional accounting version lands across
every serving revision and the reconciler job. A later change flips the pin
together with the cohort it enables, after all existing leases have drained and
the new code is fleet-wide. Capability, pilot workspace IDs, Bigtable app profiles,
TTL, caps, and shard count retain their existing live-primary preservation rules;
enabling still requires pilot IDs, profiles, and the fleet compatibility preflight.
The shell writes only a normalized boolean to the Cloud Run revision.

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

### Paid pilot repair

The original synthetic monitoring workspace is funded administratively, not by
a customer payment. It correctly fails the shared paid-workspace trust gate;
configured issuance flags alone are therefore not evidence of an exercised
regional lease. The rollout migrates only that exact legacy singleton allowlist
to the already-paid first-party smoke workspace used by the spend-lease pilot.
Empty lists, custom lists, explicit overrides and issuance-off state are
preserved. No payment record or trust tier is fabricated.

After rollout, verify an uncapped first-party request through the US Central
gateway actually reports regional settlement in its authorization record, then
verify the reconciler closes that lease and imports the exact usage without
counter drift. A successful HTTP response on the global fallback path does not
pass this check. Customer expansion and additional regional ledgers remain
separate gated steps.

## Accounting version and coverage evidence (R1)

New global leases persist `accounting_version=2`; authorizations copy it into
`regional_accounting_version` in their existing durable payload. Missing versions
mean **v1**, including leases already open and authorizations already in flight.
Do not rewrite existing leases to v2. V1 settlement books keys inline and the
reconciler imports workspace spend only. V2 healthy settlement writes neither
workspace nor key counters; reconciliation imports both once under its existing
watermarks. Missing-hold recovery still books workspace and key usage under the
typed reservation claim, because there is no Bigtable hold to reconcile.

Readers, settlement workers, and reconcilers must retain both contracts while
these records exist. For the mixed-version rollout, the operator must pause
issuance **before this change deploys**: the preceding deploy-script change pins
`REGIONAL_QUOTA_LEASE_ISSUANCE_PINNED=false` in `scripts/deploy/rollout.sh` for push
deploys, and the deploy workflow's `regional_quota_lease_issuance=false` input
forces the same. Immediately before this change merges, verify all three
preconditions: **(1) issuance is pinned off on every serving revision; (2) the
open-lease index (kind `regional_quota_lease_open`) is EMPTY; (3) there is NO
unsettled typed reservation with `hold_usage_type=RegionalCredits` and NO pending
or parked settle-outbox intent for a regional reservation.** Every v1 lease must
have been closed by the old reconciler; waiting for the lease TTL is insufficient.
In practice, (3) is satisfied once the reservation TTL (two hours) has elapsed
since the pause and every pre-pause reservation has either settled or been reaped,
with no pending or parked regional settle intents remaining. A reaped typed
reservation cannot finalize, so its retry cannot book the key inline. Verify
these conditions, rather than inferring completion from elapsed time alone.

With issuance off, the index cannot refill. Resume issuance only after every
serving revision and the reconciler job run this code. Every regionally served
request after resume is v2; reconciliation books both workspace and key usage
once even if the reaper wins while a request settles its regional hold and loses
Spanner finalization. V2 authorizations must never reach settlement workers or
serving revisions that predate version support. Reverting to such a revision
after v2 issuance is unsafe.

Residual v1 case: a request whose hold the old reconciler imported and whose
typed finalization then succeeds under R1 would book its key twice (the
pre-existing double-import). For example, the Bigtable hold settles for 7,500,
Spanner finalization fails, and the old reconciler closes the lease and imports
7,500 into both workspace and key counters while the typed reservation remains
unclaimed. A successful finalization retry under R1 then books another 7,500 to
the key inline. Precondition (3) rules this out at deploy time. This change does
not repair historical v1 key overcount already committed by old settlement and
reconciliation.

V2 holds record timezone-aware UTC `settled_at` when they transition to SETTLED;
replays retain that timestamp and reserved/refunded holds carry none. Lifetime
key usage retains the per-key total watermarks and the invariant that the sum
of lifetime key deltas equals the workspace spend delta. Each day/week/month
counter receives only spend settled at or after its own current UTC floor.
A settled hold missing `settled_at` retains import-time attribution for backward
compatibility (no such v2 holds are expected in production).

Window idempotency uses `reconciled_window_hold_ids` in the global lease record,
bounded by that lease's holds. The reconciler commits each newly visible settled
hold's ID and all three window increments in the same Spanner transaction,
including IDs whose amounts are zero or whose windows have expired. A retry
cannot add that hold again, even after a window boundary; an expired amount is
never carried into a newer window. This is an ID set, not a timestamp cursor,
so a hold becoming visible after a later-settled hold is still imported once.
Active leases retain the complete set; closing a lease clears it because closed
replays return before importing counters. Window floors come from a fresh clock
sample inside each transaction attempt, independently of the batch expiry cutoff.
The transaction checks the target key row's stored day/week/month floors; if any
is newer, it rolls back and retries with refreshed attribution.
The existing lifetime watermarks remain independent. No additional Spanner
reads are added to authorization or settlement, and reconciliation uses its
existing lease read for the ID set. Inline key settlement is unchanged.

Coverage uses the existing `spend_lease_shadow` event/outbox. Apply ClickHouse
migration **017** on the replicated cluster or **018** on a single node before
running the new event writer/ingester. The additions are nullable so historical
outbox rows still ingest. Each authorization attempt has a fresh `event_id`;
`authorization_id` joins accepted/replayed attempts to request records. Retries
therefore remain separate observations.

Observation runs when regional capability, regional observation, spend issuance,
or spend observation is enabled. The independent observation settings are
`TR_REGIONAL_QUOTA_OBSERVATION_ENABLED` and `TR_SPEND_LEASE_OBSERVATION_ENABLED`;
they do not enable issuance, initialize ledgers, or change eligibility. No deploy
settings are changed by R1.

`regional_predicate_reason` is the first failure; `regional_predicate_mask` has
one bit for every failure, starting at bit zero in this append-only order:

```
stage_c disabled issuance_disabled cohort backend estimate route_type
candidate_not_credits exact_global key_lifetime key_daily key_weekly key_monthly
custom_model user_model partner_mode additional_cost native_batch app_markup receipt_fee
```

A null mask means the request exited before predicate evaluation. A zero mask
means eligible. `regional_outcome` preserves the regional result before global
fallback; only an accepted authorization carrying `settlement="regional_lease"`
is `served`. `regional_unavailable_reason` distinguishes mapping, grant, fence,
exhaustion, expiry, pool cap, timeout and other failures using existing reads.
Requested and resolved regions are recorded separately.

The dispatcher exposes cumulative `attempted`, `enqueued`, `dropped`, `delivered`,
`failures`, and current `pending` through `stats()` and periodic
`spend_lease_shadow_dispatch_counts` logs. Logs carry a unique `dispatcher_id` so
process restarts do not combine incompatible counter sequences. `enqueued` counts
admission into the bounded queue, not durable delivery; an older queued event
can subsequently be dropped. At idle, attempted = delivered + dropped. At runtime,
include pending in that equation. Shutdown delivery failures count the discarded
active and queued events. Abrupt process death and a disabled downstream sink are
not proven delivered by these dispatcher counters; use downstream outbox/sink
health alongside them when interpreting coverage. Observation is evidence with
bounded, reported queue loss, not an exact census.
