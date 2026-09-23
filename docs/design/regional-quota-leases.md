# Regional quota leases

Status: **canary-capable, off by default, workspace allowlisted**

Global Spanner remains TrustedRouter's prepaid billing source of truth.
Regional quota leases remove the hot global counter mutation from eligible
authorizations without changing the invariant that a workspace cannot spend
more credit than it owns.

## Safety model

The global ledger grants a region a bounded amount of already-reserved credit.
Each owned grant has an expiry-ordered `regional_quota_lease_open` index
for the reconciler and a `regional_quota_lease_workspace_open` index keyed
`{workspace_id}#{region}#{lease_id}` for grants. Both inserts and both close
deletes commit with the canonical lease and credit reservation/release.
Quarantine, expiry, and drain retain ownership and both pointers; partial
reconciliation changes the canonical remaining amount, which grants point-read.

A grant reads only its workspace's new index prefix **and** its
`regional_quota_fence` prefix, then deduplicates their canonical lease pointers.
Fences already existed before this deployment: one row per regional quota slot,
with `active_lease_id` set by grant and cleared only by close in the same
transaction as the money change. Thus a legacy lease missing the new index is
still counted, including pending, expired, draining, and quarantined leases.
The dual read is permanent for now: a one-time backfill marker would miss an
older writer committing after the backfill. No migration-complete assumption
or lease-history read is needed, and a missing canonical target fails closed.
Older close writers can leave a stale new index pointer; canonical closed rows
contribute zero and the grant transaction deletes their stale pointers. Thus
old-writer closes cannot accumulate history across successive grants. Range locks, including empty ranges, cover only that workspace
and serialize concurrent grants across credit shards/regions. Cost is bounded
by workspace quota slots and indexed open grants, independent of fleet size
and historical lease count.

Authorization uses **option (a)**: no aggregate escrow read. It point-reads its
canonical lease and checks state, issuance tier, tier cap, fencing token, and
expiry as before. It additionally checks the recorded `issuance_pool_micro`
against the current configured pool and rejects an individually oversized
remaining grant. A missing issuance bound (pre-deploy lease) quarantines the
lease and refunds the new local hold; existing settlements still reconcile.
This is a conservative transition, not an assumption that legacy exposure is
zero.

Why the removed aggregate check is redundant for issuing revisions with this
protocol:

- Simultaneous grants cannot overfill a pool: the workspace ranges conflict,
  and the retry includes the winning grant. Each grant records its enforced
  pool bound. A later grant also honors the smallest recorded bound of all owned leases,
  so raising configuration or promoting a tier cannot invalidate an earlier
  lease's bound. Expansion waits for smaller-bound leases to close.
- Reconciliation only increases imported spend; close releases the remainder.
  Quarantine/drain/expiry release nothing. None increases total owned escrow.
- Tier or tier-cap changes invalidate the issuance metadata. A reduction of the
  independent regional maximum invalidates the recorded pool bound, even when
  the tier cap stays unchanged. Fence/state/expiry changes retain their direct
  rejection checks. These are the legitimate states the former aggregate
  recheck could detect after an initially valid grant.
- Unsupported direct edits inflating canonical money are not a grant
  transition. The individual oversize guard still detects an inflated current
  lease; arbitrary multi-row corruption requires reconciliation/audit rather
  than a request-time fleet scan.

For rollout, keep issuance off while replacing pre-protocol regional writers, using the
existing issuance/capability split below; capability remains on for settlement
and reconciliation. Re-enable issuance once every issuer and reconciler uses this protocol.
In particular, do not mix old issuers with changing pool/tier policy: old code
does not honor `issuance_pool_micro`. The **grant count itself remains correct
throughout mixed-version operation** because it always reads legacy fences,
including grants committed after an earlier read (which conflict and retry).
Legacy owned leases need no backfill to be counted and can drain normally.

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
(absent or empty input) use `REGIONAL_QUOTA_LEASE_ISSUANCE_PINNED` in
`scripts/deploy/rollout.sh`. It was `false` while the regional accounting
version, bounded escrow, exact regional charging and the five-region ledger
landed fleet-wide; it is now `true`, so every push deploy enables issuance for
the pinned cohort. Enabling still requires pilot IDs, profiles, and the fleet
compatibility preflight. The emergency off switch is the dispatch input
`regional_quota_lease_issuance=false` (or a Cloud Run env update); note that a
later push deploy turns issuance back on unless the pin itself is changed.
The shell writes only a normalized boolean to the Cloud Run revision.

R4 replaces the initial single-region configuration described above with these
code pins. An absent environment variable resolves to the pin; an explicit
value overrides it. All three scripts resolve these shared settings once in
`scripts/deploy/_lib.sh` and consume them unchanged. An explicit empty
pilot list means no cohort; empty table, timeout, cluster map, profiles, or
numeric lease settings are refused before any cloud mutation or revision write.
Capability retains its live-primary preservation rule.

| Setting | Pin |
|---|---|
| `TR_REGIONAL_QUOTA_CLUSTER_MAP` | `us-central1=trusted-router-logs-c1,us-east4=trusted-router-logs-c1,europe-west4=trusted-router-logs-c1,us-west1=trusted-router-logs-c1,southamerica-east1=trusted-router-logs-c1` |
| `TR_SPEND_LEASE_CLUSTER_MAP` | `us-central1=trusted-router-logs-c1` (independent of the regional map; decision 33) |
| `TR_REGIONAL_QUOTA_BIGTABLE_APP_PROFILES` | `us-central1=tr-quota-us-central1,us-east4=tr-quota-us-east4,europe-west4=tr-quota-europe-west4,us-west1=tr-quota-us-west1,southamerica-east1=tr-quota-southamerica-east1` |
| `TR_REGIONAL_QUOTA_LEASE_PILOT_WORKSPACE_IDS` | `358d80a4-2c9a-4479-92ea-a681f187477d,f46bf618-4c7c-4a35-afa0-8d48891bf7a5,1fa994e7-15b1-4e36-9c1c-51ba072d3060,c4ba9257-d212-4d7e-a5a1-989bceb7a1d8,45819281-0ce9-4811-a0cd-c660ab3a116d` |
| `TR_REGIONAL_QUOTA_LEASE_TTL_SECONDS` | `300` |
| `TR_REGIONAL_QUOTA_LEASE_MAX_MICRODOLLARS` | `10000000` |
| `TR_REGIONAL_QUOTA_LEASE_MAX_AVAILABLE_BASIS_POINTS` | `1000` |
| `TR_REGIONAL_QUOTA_LEASE_SHARD_COUNT` | `16` |
| `TR_REGIONAL_QUOTA_LEDGER_TIMEOUT_SECONDS` | `4` |
| `TR_REGIONAL_QUOTA_BIGTABLE_TABLE` | `trustedrouter-regional-quota` |

Issuance remains pinned **false**. `TR_REGIONAL_QUOTA_RECONCILE_LIMIT` is
unchanged. Every region, including Europe, routes its ledger to c1 for now:
Bigtable refuses a second transactional profile on a different cluster unless
its split-brain warning is forcibly bypassed. We keep that protection; an
EU-local ledger is separate, later work. The five keys cover enclave regions
and the control-plane-only region; unused keys are harmless.

The provisioner drift-checks existing profiles and creates missing profiles
with single-cluster routing and transactional writes. Unknown or unreadable
clusters fail closed. Its printed profile list must exactly match the configured
list (the pin by default), including order; provisioning, rollout, and
reconciliation refuse a mismatch. An operator changing the region set must override both maps of regions
and profiles consistently. The reconciler receives the same table, timeout,
profile list, and lease settings through the shared deploy configuration.

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

`TR_REGIONAL_QUOTA_RECONCILE_LIMIT` defaults to 500 (maximum 1,000), covering
400 open leases at five workspaces × five regions × 16 shards, versus 80
closures/minute at a 300-second TTL. Each invocation reads the open-index
metadata and processes a page round-robin across workspaces, then regions,
then lease IDs. A durable cursor advances on attempts, including errors and
live pending grants, so those entries cannot starve other groups. The existing
single-worker lock remains authoritative. No new work starts after 45 seconds
(including ledger verification); a shared 70-second Spanner RPC budget bounds
transaction retries, with existing per-operation Bigtable timeouts. The lease
count is a ceiling, not a throughput claim: monitor `backlog` (open rows at the
scan), `processed` (attempted rows), `remaining` (unvisited rows), `reconciled`,
`closed`, and `errors` to detect time-limited or failed progress. Each lease also
persists the last attempted hold ID, including guarded/unresolved/error holds.
Visits resume strictly after that ID and wrap, attempting at most eight expired
holds and admitting lookups for at most ten seconds or half the remaining work
budget, whichever is smaller. This reserves time for drain/import and other
leases; an in-flight RPC can overrun its admission slice. Transient hold lookup
failures are logged with lease/hold IDs and counted in `errors`; the reservation
stays untouched and the visit continues to drain/import within the remaining budget.
Non-transient lookup errors still fail the lease visit. Retired lease cursors
are pruned against the open index. Admission to another lease also reserves
the longest visit duration observed in this invocation. If that time no longer
fits, its lease cursor stays untouched and the next invocation starts there,
so a trailing slow lease cannot repeatedly receive only the leftover seconds.

Expiry alone never authorizes refunding a local hold. Live typed reservations,
including those with no outbox row yet, retain escrow so a settle intent arriving
after the scan can still charge. The settlement worker owns pending/dead
intents. Regional reconciliation repairs a local hold only from matching,
terminal typed authorization and reservation records with no guarded outbox
work: positive actuals settle the hold; a terminal zero outcome permits refund.
An expired hold with neither typed record can instead be cancelled: one Spanner
transaction reads the authorization PK, checks reservation absence through
`tr_reservation_by_authorization`, checks the outbox guard count, and inserts
`regional_quota_hold_cancellation` keyed by hold/authorization ID. The tombstone
carries workspace, lease, region, fence and cancellation time. Only after commit
does the worker refund locally. Every regional authorization writer reads that
same tombstone key in its transaction; cancellation refuses the write, triggers
idempotent local compensation (even after refund/close), and returns `unavailable`
so the gateway uses typed global authorization. A committed cancellation remains
authority to finish local compensation even if that global fallback now exists.
Partial/inconsistent typed records and guarded intents retain escrow for repair.

Cancellation tombstones are retained **indefinitely**, with no entity-retention
expiry. They are exceptional crash-recovery records, not per-request history.
There is no enforced maximum lifetime for a suspended in-memory writer holding
an ID, so deleting these records after a guessed TTL would reopen the race.
Any future TTL requires a writer-age fence first. Apply
`scripts/deploy/migrate_typed_counters.sh` and wait for the new authorization
index to be ready before releasing this reconciler. First-run DDL blocks
`migrate-schema` until its online, low-priority backfill completes: minutes to
hours for a roughly 1.2M-row table. Reruns also wait for `READ_WRITE`, since a
present `WRITE_ONLY` index is not ready. The index is safe to land ahead of
the code: the app starts without it; only orphan recovery needs it. Deploy
tombstone-aware writers everywhere before enabling orphan cancellation;
issuance remains off during this upgrade, and pre-upgrade in-flight writers
must finish/terminate.

Production activation requires all of the following:

Implemented gates include the transactional adapter, exact global grant and
close transactions, a once-per-minute reconciler, integer-only property tests,
ambiguous Bigtable commit replay, fencing, concurrent idempotency, exact key
usage import, and 16-way local sharding. Production issuance remains off with
the five-workspace cohort pinned above. Any local read, conditional write,
missing profile, or initialization ambiguity falls back to exact Spanner
authorization. Missing
lease state is quarantined and its global escrow is not guessed back into the
available balance.

Before expanding the allowlist, verify zero reconciliation errors, bounded
lease-row size and CAS retries under the canary's real concurrency, no global
counter drift, and successful failback when a Bigtable profile is disabled.

### Paid pilot repair

The original synthetic monitoring workspace is funded administratively, not by
a customer payment. It correctly fails the shared paid-workspace trust gate;
configured issuance flags alone are therefore not evidence of an exercised
regional lease. The earlier rollout migrated that exact legacy singleton allowlist
to the already-paid first-party smoke workspace used by the spend-lease pilot.
R4 supersedes live allowlist preservation with the five-workspace code pin
above; explicit overrides remain supported and issuance stays off. No payment
record or trust tier is fabricated.

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
