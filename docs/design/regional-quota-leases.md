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
treated as a fresh environment. With the normal ON pin, a fresh fleet must
explicitly request issuance=false for its first compatibility deployment.

Issuance requires accounting compatibility, independently of the git release:

1. `REGIONAL_QUOTA_ACCOUNTING_PROTOCOL=2` is a constant in
   `scripts/deploy/_lib.sh`, identifying R1/v2 tombstone-aware writers and
   reconciliation. `rollout.sh` sets this environment variable on every new
   serving revision; `regional_quota_reconciler.sh` sets it on the worker job.
   Bump the constant only for an incompatible accounting protocol change.
2. Before creating any issuance-enabled revision, every serving region must
   carry capability=true, a boolean issuance marker, and the protocol marker
   equal to that constant. The scheduled reconciler must declare it too.
   A missing/pre-R1 or incompatible marker refuses rollout with a non-zero
   exit, as do ambiguous traffic and read errors. These failures never silently
   choose issuance OFF. An older git release with protocol 2 passes the first
   rollout of a newer release with protocol 2. Only a fleet missing the protocol
   needs an explicit OFF compatibility rollout before re-arming, including all
   held regions and the worker; ordinary pushes do not require two deployments.
3. The stable Scheduler must be ENABLED and target the expected regional Cloud
   Run worker using POST and the worker OAuth identity. The worker must be ready,
   with its latest execution successful within five minutes, using its current
   configuration. Its actual reconciliation-complete log must also be present
   within five minutes (a single-flight skip exits zero but is not evidence).
   Missing, paused, failing, stale, or unverified workers refuse activation.
   The preflight is read-only and never resumes an operator pause. After an
   intentional pause, explicitly resume and verify reconciliation before enabling.

Operator commands (run only from the reviewed `main` commit) are:

```bash
gh workflow run deploy.yml --repo Lore-Hex/quill-router --ref main \
  -f regional_quota_lease_issuance=false
```

After the queued deployment is fully green in every region, reconciliation is healthy, and
the durable stop latch has been explicitly re-armed as described below:

```bash
gh workflow run deploy.yml --repo Lore-Hex/quill-router --ref main \
  -f regional_quota_lease_issuance=true
```

Routine workflow dispatches use `preserve` to copy the primary live issuance
marker; this is not per-region preservation. Push-triggered deploys (absent or
empty input) use `REGIONAL_QUOTA_LEASE_ISSUANCE_PINNED=true` in
`scripts/deploy/rollout.sh`. During an incident, the emergency containment recipe
is to change that literal to `false` and commit it through review so successor
pushes inherit OFF. Keep the incident pin off until compatibility and reconciler
readiness are verified, then restore `true` through review. The normal pin stays
true. A code change in an uncommitted checkout alone does not change production.

The dispatch kill switch `regional_quota_lease_issuance=false` now persists
`off` to `gs://tr-deploy-mutex-quill-cloud-proxy/controls/regional-quota-issuance.txt`
before CI/build/deployment gates. OFF dispatches use a unique workflow concurrency
group, so an ordinary pending deploy's replacement by a newer push cannot erase
the stop request. That parent only persists OFF and enqueues a normal `preserve`
deployment (forwarding the hotfix choice); all deployment jobs remain in the
ordinary serialized queue, including the public companion that does not hold the
GCS traffic mutex. If a newer push replaces the queued child, it still reads OFF.
The latch/dispatch job must succeed; a failed authentication or object write is
not a persisted stop, and a failed enqueue needs a normal deploy retried. No later
dispatch or push automatically clears it. Every rollout reads it afresh, and OFF overrides even explicit true or
preserve. The object is absent until the first OFF request; absence means no
containment and leaves the requested value unchanged. `allow` also leaves it
unchanged. `off` forces false; any other content (including empty) forces false
and logs malformed state. A successful structured listing establishes absence;
transport, permission, missing-bucket, malformed-listing and object-read errors
abort rollout loudly with a non-zero exit and never silently choose OFF. A push event cannot
enter the OFF persistence step; it requires workflow_dispatch and explicit false.
The deploy identity needs read/write access to this control object in the existing
mutex bucket; the preflight also needs Scheduler/Jobs and Cloud Logging reads.

Before the first OFF is persisted, apply the mutex bucket lifecycle migration:
replace the unconditional age-1 Delete rule with
`{"action":{"type":"Delete"},"condition":{"age":1,"matchesPrefix":["locks/"]}}`
(the policy emitted by `infra.sh`). Wait at least 24 hours after that update
before relying on a persisted OFF: Cloud Storage can continue applying the old
policy during propagation. Re-read the effective lifecycle and persist/verify OFF
only after this window. Do not treat the policy read alone as proof of propagation.
The deploy identity also needs `storage.buckets.get` for lifecycle inspection;
`infra.sh` grants bucket-scoped `roles/storage.legacyBucketReader` alongside
the existing object-admin role. Both persistence and rollout now refuse any Delete rule that could cover
`controls/`, including broad or overlapping prefixes. This migration has not been
applied by this code change. See [Cloud Storage lifecycle propagation](https://docs.cloud.google.com/storage/docs/lifecycle).

Latch reads use one bounded `controls/*` listing with
`gcloud storage objects list --raw --format=json`, then read the exact object only
if present. SDK 575 explicitly does not support `storage ls --format=json` and
its `ls --json` errors for an absent prefix; `objects list` provides a structural
JSON array (including `[]`). No stderr wording is classified as absence.
Nonzero listing status, malformed metadata, and read failures abort.

The build binds `com.trustedrouter.accounting_protocol` to the artifact with a
Dockerfile ARG/LABEL, supplied from `_lib.sh` by local and Cloud Build paths.
Deploy resolves the selected image to a digest and reads its config through
`docker buildx imagetools inspect IMAGE@DIGEST --format '{{json .Image}}'`.
Serving revisions and worker jobs receive that label value. Missing/unreadable
labels abort; issuance ON and worker deployment reject a label below the current
minimum. Docker Buildx is supplied by the Ubuntu deploy runner; registry auth is
configured before inspection. Existing unlabelled images cannot be redeployed
through these paths, even for OFF; use a labelled compatible build.

A running/pending scheduled execution permits a recent completed execution of
the current configuration, with reconciliation completion evidence. If there is
no recent completion, preflight polls for at most 90 seconds; explicit failed
completion still refuses issuance. Custom worker name/prefix configuration is
shared with worker deployment.

This remains a staged stop, not an immediate runtime revocation: a deploy that
already resolved its issuance state and held regions can still serve ON. Verify
OFF on **every serving revision**, complete the subsequent OFF rollout, and keep
capability and reconciliation enabled to drain existing work. Do not use a manual
Cloud Run env update as durable containment: config-as-code overwrites it.

To re-arm, resolve the incident and verify protocol compatibility on all serving
regions and the scheduled worker, plus healthy reconciliation. Restore the code
pin to true if incident containment changed it. An operator then deletes the
stop object or writes `allow` (choose one), and dispatches issuance=true:

```bash
# Option 1: remove containment.
gcloud storage rm \
  gs://tr-deploy-mutex-quill-cloud-proxy/controls/regional-quota-issuance.txt

# Option 2: explicitly allow the requested issuance setting.
printf 'allow\n' > /tmp/regional-quota-issuance.txt
gcloud storage cp /tmp/regional-quota-issuance.txt \
  gs://tr-deploy-mutex-quill-cloud-proxy/controls/regional-quota-issuance.txt
```

Then dispatch:

```bash
gh workflow run deploy.yml --repo Lore-Hex/quill-router --ref main \
  -f regional_quota_lease_issuance=true
```

No object initialization is required. Re-arming and OFF requests must not be
issued concurrently. The shell writes only a normalized boolean to each new Cloud Run revision. Direct
rollout.sh still requires the separate five-profile provisioning gate; this
preflight does not provision Bigtable or prove lease drainage.

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

Issuance remains pinned **true**. `TR_REGIONAL_QUOTA_RECONCILE_LIMIT` is
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
after the scan can still charge. Regional reconciliation repairs a local hold
only from matching, terminal typed authorization and reservation records.
Positive actuals require no guarded outbox work; a terminal zero outcome permits
refund even with pending/dead intents for that authorization.
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
usage import, and 16-way local sharding. Production issuance is pinned on with
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
above; explicit overrides remain supported and issuance follows the control above.
No payment record or trust tier is fabricated.

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
once. Settlement resolves the Spanner reservation claim before changing the local
hold; a reaper-winning free release stays zero and refunds unused local escrow. V2 authorizations must never reach settlement workers or
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
migration **017** on the replicated cluster (with
`scripts/deploy/clickhouse_replicated_migrate.sh --apply clickhouse/017_regional_coverage_replicated.sql`,
which refuses to run while the table is missing on any replica and fails on a
non-zero per-replica status — `spend_lease_shadow` existed only on node 1 until
2026-09-24) or **018** on a single node before
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


## Exact receipts and exceptional overruns (R3)

Receipt requests pass the regional eligibility predicate. The retired
`receipt_fee` telemetry bit stays at its original position and is always zero.
The gateway reserves the receipt-adjusted estimate and persists the receipt rate
on the authorization. Settlement uses the shared receipt pricing function:
`ceil(model_charge * 11200 / 10550)`, since catalog prices already include 5.5%.
The premium is workspace spend; it creates no beneficiary payout. App markup,
key caps, custom/user/partner models, additional costs, and native batch remain
excluded. No issuance, cohort, or region setting changes are part of R3.

The regional settle intent freezes `regional_local_microdollars` as the smaller
of the total charge and original estimate, and `regional_global_microdollars` as
the remainder. Regional intents preserve their first payload across retry
enqueues: local CAS may already have committed before a Spanner failure. Duplicate
HTTP deliveries defer to that intent's drain. Older intents derive the same split
from their frozen total and immutable authorization estimate; malformed explicit
splits fail before either ledger is changed.

Bigtable settles only the local component. The typed reservation claim commits
the full terminal total and books only the excess to workspace usage. V2 books
only excess key usage inline; reconciliation imports local workspace and key
usage once. V1 books the entire key charge inline and reconciles workspace only.
Missing-hold recovery drains the lease and books the entire charge under the typed
claim, as before. No request is reauthorized to fund an overrun. Terminal hold
recovery likewise restores only the local component to Bigtable.

The local CAS runs only after the reservation claim succeeds in the finalize
transaction. A reaper that won before enqueue therefore leaves **zero** usage,
matching GLOBAL, including late overrun intents and their drain/replay paths.
Reconciliation also refunds the bound terminal-zero hold when the process dies
before inline compensation or that compensation fails. Pending/dead late intents
cannot block this recovery: `settle_outbox_apply._apply_typed` skips finalization
of terminal regional reservations and classifies a positive late intent as
`ALREADY_RELEASED_FREE`; `typed_finalize_atomic` claims before local CAS; and
`RegionalQuotaLease.settle` rejects `REFUNDED` holds. Draining the intent to dead
therefore cannot revive a charge or strand the grant's unused escrow.
If the finalize transaction fails after the local CAS, the frozen durable intent
continues to block the reaper until replay finishes the global component.

The first successful local CAS's persisted `settled_at` is authoritative for
both components. Replays retain it. Inline excess (and the V1 full key charge)
uses `release_key.window_amounts` against current window floors, exactly like
R1's local reconciliation. A Sunday settlement replayed Monday contributes to
neither Monday's daily nor weekly window; lifetime and applicable monthly usage
still include the full charge. This matches a GLOBAL booking at that settlement
time, observed after the boundary. Missing-hold recovery has no local booking
and retains GLOBAL's inline timestamp for the whole charge.
Inline timestamped bookings use R1's stored day/week/month floor guard on both
the original key shard and shard-zero fallback. If another writer has advanced
any floor, the whole transaction rolls back and retries with fresh clock floors
while retaining the first local CAS's settlement timestamp.

Apply migration **019** (replicated) or **020** (single node) before the updated
telemetry writer/ingester. Settlement emits a best-effort R1 `spend_lease_shadow`
event after the successful claim, with stable ID `regional-settle:<authorization>`;
drained settlements emit the same event. `regional_outcome` is `settled` or
`refunded`, distinguishing these from admission observations. The nullable fields
`regional_actual_microdollars`, `regional_local_microdollars`,
`regional_global_microdollars`, and `regional_overrun_microdollars` report the
charge and split. Missing-hold recovery reports its full global booking but only
the estimate excess as overrun. Filter settlement outcomes for the denominator,
then use `countIf(regional_overrun_microdollars > 0)` and
`sum(regional_overrun_microdollars)` by workspace, region, and bounded time window.
As with R1 coverage, telemetry delivery is best effort and cannot block money.

### R2b: traffic allocation, global liquidity, and handoff

Issuance now sizes against a **workspace-wide** budget, across every region and
quota shard. With trust armed, its absolute ceiling remains the minimum of the
configured pool, the tier cap, and every outstanding generation's certified
pool. The existing available-balance fraction still caps each individual grant;
it does not shrink the workspace pool again. Neither a new region nor a
successor creates another pool.

At grant boundaries, the serializable transaction reads the bounded workspace
ownership index and the workspace's credit balances. Let `A` be current global
available balance (credits minus usage and all reservations), `E` unreconciled
regional escrow, and `f` the global floor in basis points. A grant `g` must obey:

```
E + g <= min(certified trust pool, floor((A + E) * (10000 - f) / 10000))
```

`TR_REGIONAL_QUOTA_GLOBAL_FLOOR_BASIS_POINTS` defaults to 5000 and accepts 1–10000.
Global reservations and debt on other credit shards reduce the denominator.
Every grant, including a replacement, checks this floor in its reserve
transaction; retries cannot use the earlier balance snapshot to bypass it.
This guarantees the retained share at each grant boundary. Subsequent global
spending may use that liquidity; it is not another reservation of global funds.
No deployment flag, cohort, region mapping, TTL, or shard count changes in R2b.

The router lazily starts a grant at up to four request estimates. The budget is
divided by the number of distinct `(region, quota shard)` slots with an unexpired
owned generation, including the requesting slot, and halved to leave overlap
capacity. Retired generations consume escrow but do not duplicate a slot. The
retiring shard's observed spent-plus-reserved amount seeds its next grant at
twice that demand, subject to the same share and pool bounds. Expired idle slots
stop diluting the share; their unreleased escrow still counts against the pool.

An unfunded or unusable hash-selected slot tries funded siblings in the same
region before global fallback. Search visits at most 64 slots, refreshes a stale
cached generation at most once per slot, and attempts at most four funded
siblings. The healthy cached path does not scan siblings. This adds no shared
counter write to successful local admission.

Before new admission, remaining quota below the larger of two request estimates
and 10% of the grant, or expiry within `min(5 seconds, TTL / 10)`, triggers
handoff. The writer first CAS-drains the regional row. A Spanner transaction
then persists a `regional_quota_lease_retired` pointer, retains both open indexes,
marks that separate canonical generation `retiring`, and clears only its own
fence. Only then can a successor reserve escrow and acquire the next fence
number. Settlement continues using the original lease ID and token; a hold
reserved before draining may finish its typed authorization record afterward.

Crashes after drain or retirement are retryable. A pending successor whose
issuer died before initialization/activation is resumed idempotently using its
already reserved escrow. Cached closed generations refresh to the current
fence. Reconciliation imports retired generations normally and closes them only
when no holds remain. Closing a retired generation requires its retirement
pointer and never clears a successor's fence. Retirement does not release
escrow, and insufficient overlap capacity falls back to the global path. No
finite pool can guarantee local admission when outstanding holds fill it.

Rollout requires the R2b reconciler to close retired generations. Older workers
fail closed on a detached fence; keep the compatible worker running until all
retired generations drain. The existing dual-read ownership index continues to
include legacy fenced leases during mixed-version issuance.

Regional shadow events add nullable `regional_selected_shard` and
`regional_sibling_served` fields through both ClickHouse adapters and forward-only
migrations 019/020. `pool_floor` identifies liquidity refusal; `pool_cap` also
covers a traffic share too small for the request. `sibling_exhausted` is the final
fallback classification when no more specific grant/lease reason is available.
The selected shard is the serving shard on success, or the requested hash shard
on failure. Existing charge calculation and settlement amounts are unchanged.
