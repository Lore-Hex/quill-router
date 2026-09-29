# PR 5: regional asynchronous settlement journal

This supersedes the single-workspace-row proposal in `/private/tmp/async-settle-build-plan.md`.
Storage only: no route, importer, reaper, enclave, or runtime factory calls this code.
Provisioning has not been run. No production state changes are part of this PR.

## Chosen layout and the accounting proof

One active workspace grant, pinned to one region and epoch, with older sealed
epochs allowed to drain concurrently. The registry persists immutable **K shards
and S lifetime slots per shard** in each Grant (defaults **128 × 4,096**).
Integer shard capacities sum exactly to that grant’s reserved remainder. A hash
prefix spreads row keys; the authorization allocator hashes authorization IDs to
shards and assigns an unused ordinal. It never probes another shard for money,
reuses a slot, or independently grants the whole cap to each region.

```
row: hash-prefix#settlement#region-hex#hash(workspace,region,epoch)#shard
family journal:
  version     random 128-bit CAS token
  cap         immutable shard capacity
  outstanding accepted, not-yet-acknowledged charge
  sealed      irreversible 0 (open) -> 1 (sealed) -> 2 (payload purge started)
  shards      immutable K (1..4096)
  slots       immutable S (64..4096)
  s/0000..s/0fff immutable binding + terminal payload + acknowledgment
```

Acceptance reads **only six metadata cells and its own slot**, then atomically
updates that slot, outstanding, and version in one CheckAndMutateRow. No hot
workspace row or cross-row transaction. The body stores the exact verified
frozen-snapshot charge; storage never computes prices. The independent PR 6
caller must authenticate every Ticket field and verify the frozen-price result
before calling `accept`. A Python constructor is not an authenticated ticket.

The binding covers authorization, prospective generation, workspace, key, nonce,
region, epoch, shard/slot, snapshot hash/version, authorization idempotency deadline, and billing authority. The
immutable canonical hash covers that binding, endpoint, settle/refund kind,
normalized usage, and exact amount. No prompts, completions, arbitrary metadata,
or credentials are journal fields. Refunds have zero charge.

* **I1/I4:** the registry binds an authorization once, even across epochs. Slot
  changes share a row/version, compare the frozen binding, and cannot replace a
  terminal choice. Identical accepted retries return the original outcome,
  including after acknowledgment; changed payload/kind conflicts. A payload that
  selected `sync_required` is bound too. A prior recovery fence returns its winner.
* **I2:** accepted intent and increment are one atomic mutation. No speculative
  increment and no counter/intent crash leak. `outstanding` equals the sum of
  accepted, unacknowledged slots, not just a lower bound.
* **I3:** every acceptance checks its shard's capacity. Summing shard capacities
  proves the grant cap, without reading all shards on the handoff path. A new
  grant reserves tier_cap minus the sealed outstanding bounds of ALL unclosed
  older epochs, in one idempotent registry transaction. Thus the sum of older
  actual debt plus the new grant cap never exceeds the workspace tier cap.
  Reuse `trust_eligibility.tier_cap()`; defaults 5M/25M/100M microdollars. Invalid
  or absent tier refuses a grant. Do not use legacy `spend_cap()` fallback.
* **I5:** a fence compares the slot's exact pending value, writes the fence and
  a fresh shard version atomically. If it wins, every stale acceptance CAS fails.
  If acceptance wins, the pending predicate fails and the fence returns that
  accepted result. Even a zero-cost refund is a terminal choice.
* **I6:** a verified final-ledger receipt atomically marks the slot acknowledged
  and subtracts its accepted amount. Duplicate acknowledgment is a read-only
  replay. Import receipts cannot release money. Receipts must match the ticket
  and payload hash. Synchronous/fenced slots require ledger completion evidence
  for cleanup too, but have no asynchronous counter to decrement.
* **I7:** no age GC on any journal cell. Purge requires a closed epoch, an expired
  idempotency window, an acknowledged slot, and a sealed row. Pending, parked,
  dead, and accepted-unacknowledged slots block closure indefinitely.

Actual ledger-uncommitted money can be less than journal outstanding after a
ledger commit loses its receipt delivery. This conservative overcount is at most
the grant. Re-delivering the durable finalization receipt reclaims it exactly
once. Sealing **does not zero debt**. An epoch cannot close or transfer its grant
until all of its obligations have finalization evidence. The unused portion
can fund a successor after the seal-and-bound barrier described below.

### Explicit tradeoffs and bounds

Static partitions sacrifice utilization: admission tests `charge <= cap - outstanding`
for the assigned shard. Spare capacity elsewhere is never borrowed. The supplied
3.07M-generation, seven-day aggregate distribution (through 2026-09-28) gives
p50/p90/p99/p99.9 = 9/129/10,461/161,836 µ$, maximum 2,521,708 µ$;
1.7% exceed 1,220 µ$ and 0.02% exceed 312,500 µ$. These are aggregate bounds,
not measured fallback percentages for any workspace or tier.

| Default K=128 | Full shard capacity µ$ | Empty-shard size fallback bound | Remaining at 75% occupancy µ$ | Size fallback bound at that occupancy |
|---|---:|---:|---:|---:|
| Tier 1: 5M | 39,062–39,063 | approximately 0.1–1% | 9,765–9,766 | approximately 1–1.7% |
| Tier 2: 25M | 195,312–195,313 | approximately 0.02–0.1% | 48,828–48,829 | approximately 0.1–1% |
| Tier 3: 100M | 781,250 | at most 0.02% | 195,312–195,313 | approximately 0.02–0.1% |

Actual fallback also depends on arrival/charge correlation, drain lag and hashing.
In particular an overlap grant has less capacity than a full tier grant, so the
full-cap table must not be reused blindly for it.

`choose_sizing(cap, peak_rate, drain_lag, minimum_shard_cap=32_000)` is pure.
Rate is the workspace/region’s observed peak completions per second; lag is
measured seconds from journal acceptance to final-ledger acknowledgment:

* Unknown rate **or** lag: K=min(128, floor(cap/minimum)), S=4,096.
* Known measurements: K=min(4,096, floor(cap/minimum),
  max(1, ceil(3 × peak_rate × 0.010 / -ln(0.95)))). This targets at most 5%
  Poisson interference from create/accept/ack during a modeled 10ms CAS window.
* S is the next power of two above max(64, ceil(2 × peak_rate ×
  max(3,600 seconds, 4 × drain_lag) / K)), capped at 4,096. This targets
  an hour or four drain lags before the 50% usage trigger, subject to the cap.
  The cap can make that horizon unattainable; rotation then happens sooner.
* Cap below the configured minimum refuses sizing. Registration and successor
  reservation enforce the same minimum; callers pass the setting into the pure
  function. Reduced overlap capacity may require fewer shards and a fresh
  sizing attempt before a successor has been reserved. A committed successor's
  K/S can never change, including on replay.

At tier 1, rate=70/s and lag=1s selects **41 × 4,096**; 200/s, lag=1s selects
**117 × 4,096**. Rate=0.1/s selects **1 × 1,024** at lag=1s and **1 × 4,096**
at lag=10,000s. The minimum capacity caps K at **156 / 781 / 3,125** for the
three tiers. The minimum is a planning floor around the requested charge-tail
scale; the supplied percentiles do not establish an exact p99.5 value.

The default 524,288 nominal slots last **2.08 hours at 70/s**, **43.69 minutes
at 200/s**, or **2.76 days at 190k/day**. The 50% rotation trigger halves these
times to **62.4 minutes / 21.85 minutes / 1.38 days**, before counting hash skew.
Rotate when total allocated slots reach **50% of K×S OR any shard reaches 90%
of S**. Allocations are lifetime counts; acknowledgment does not recycle slots.

Under uniform independent hashing, per-shard allocation attempts are approximately
Poisson with λ=N/K. Next-allocation slot fallback is `P(Poisson(λ) >= S)` and
expected full shards is K times that probability. For the old 4,096×64 shape,
N=190k gives λ=46.39, about **33.47 full shards and 0.82% fallback**. At nominal
capacity λ=S, roughly half the shards are full. For the new shape at its 50%
trigger λ=2,048, S=4,096, the Chernoff bound is
`(e/4)^2048 < 3e-344` per shard (less than 4e-342 over 128 shards).
At 190k attempts it is even smaller. These models exclude adversarial IDs,
nonuniform hash input, CAS contention, and debt occupancy.

Identifiers now have an explicit **256-byte UTF-8 limit AND 258-byte canonical
JSON-string limit** (including quotes). Unconstrained escaping within the old 256-byte limit
could not meet the requested row budget. Regex punctuation, backslashes, and
bounded control characters remain legal. Tickets cap at 2,500 bytes, payload
bodies at 3,000 bytes; timestamps/usage/charges are bounded int64 values.
The maximal legal test uses 256 ASCII characters or 128 backslashes for each identity, maximal decimal
integers, and every normalized usage field. Real canonical serialized sizes are:

| Slot state | Bytes |
|---|---:|
| Pending | 2,184 |
| Accepted (charge fits maximal grant’s shard) | 2,753 |
| Recovery fenced | 2,183 |
| Fenced with a submitted payload | 2,765 |
| sync_required | 2,772 |
| Acknowledged (largest, sync_required + maximal receipt) | 3,040 |

The enforced slot bound rounds to **3,072 bytes**. At S=4,096 that is **12 MiB**
of values, plus roughly 24 KiB of qualifiers and small metadata. This measures
serialization, not Bigtable physical storage or RPC latency; those need a real
benchmark. It is below the requested 16 MiB target and Google's [100 MB row
recommendation](https://docs.cloud.google.com/bigtable/docs/schema-design).
Delete-before-set keeps exactly one physical version per replacement.

Closure first checks each shard's metadata and zero debt, then reads fixed-width
slot qualifiers in **60-slot column-range pages**, each with the six metadata
cells. S=4,096 needs 69 pages per shard plus one metadata read. Missing or
unregistered slots, malformed state, or unacknowledged obligations block closure.
The adapter's explicit-column point-read limit stays **68**, and handoffs read
only seven cells. No whole-shard read or unbounded regex union is introduced.

Slot parsing validates status-specific fields, strict boolean ack, envelope
field types, canonical payload hash, receipt identity when acked, grant K/S/cap,
and accepted-unacknowledged charge <= the observed counter. A local read cannot
prove the sum over unread slots under arbitrary corruption. All terminal outcomes
from reads pass these checks; malformed state raises and is never sync permission.

## Overlapping epoch rotation

`rotate` retires allocation, seals every shard of each older unclosed epoch,
then reads the sealed counters and persists conservative bounds. Retirement is
not the acceptance barrier: a previously read acceptance can still win until
its shard's seal changes the row version. No successor is permitted until all
shards have sealed. After that barrier, outstanding can only decrease through
verified acknowledgments, so sequential counter reads safely overestimate debt.

The registry transaction checks every unclosed epoch for this workspace, requires
its sealed bound, sums **all** B values, and atomically persists both the new Grant
with `tier_cap - ΣB` and `previous → successor`. At most one successor can be active;
competing transitions observe it or conflict. Replay with identical epoch/K/S
returns the original grant even if debt has since fallen; changed parameters
conflict. Zero remainder means no grant yet. Region remains pinned.

Proof of I3 by induction: prior to a grant, actual older debt Dᵢ <= Bᵢ. After
reservation, D_new <= cap_new = tier_cap - ΣBᵢ. Thus ΣDᵢ + D_new <= tier_cap.
Sealed epochs cannot add debt, while acknowledgments only reduce it. Repeat this
argument for every successor, retaining all older bounds until safe closure.
A refresh can decrease a bound; it does not enlarge an already committed grant.

| Crash point | Durable state | Recovery |
|---|---|---|
| Before/after retirement | No successor; old active or retiring | Replay retirement |
| Between any shard seals / lost seal reply | No successor; some shards sealed | Replay all seals; never reopen |
| Between counter reads | No new complete bound | Re-read all shards; every one must be sealed |
| After bound persistence, before grant | Conservative B; no new reservation | Replay bound and registry transaction |
| During registry transaction | Neither grant nor link, or both | Atomic persistence is the registry port contract |
| After grant commit, reply lost | One grant and immutable successor link | Return that same grant, never reserve again |
| Before/during successor initialization | Grant already reserved, some rows absent | Idempotently initialize; never reset existing rows |
| During old-epoch acknowledgments/closure | Bound may overcount | Refresh on later rotation; release closed epochs only after proof |

This removes waiting for *every* old obligation to drain before granting a
remainder; it is not a zero-latency cutover. Seal/bound RPCs still impose a gap,
and full outstanding debt or a regional outage can prevent safe new capacity.
Truly gap-free overlap requires capacity reserved ahead of time or a separately
proven incremental shard-transfer protocol.

The registry here is an **in-memory reference implementation plus a production
port contract**, as in round 1. A production implementation must persist grants
(including K/S), tickets, bounds, successor links, states and retention deadlines;
commit reservation/link atomically; and accept bound evidence only from the trusted
journal coordinator. Tests retain the durable-state model while replacing the
Journal process facade. This PR does not wire or activate a production registry.

Slot recycling is out of scope. It would require incarnation identifiers, atomic
replacement of an acknowledged slot, recovery of interrupted replacement, and
retaining original terminal outcomes throughout the retry window. Binding memory
alone cannot protect an old ticket from a newly exposed empty slot. Purge keeps
sealed row and registry tombstones; no age-based GC may reclaim obligations.

## Alternatives and conflict/latency model

Numbers below are models, not measured Bigtable latency or throughput. At 70/s
and 20ms per handoff, mean concurrency is about 1.4. At the binding 200/s rate
it is about four, not 150 concurrent requests. With K=128, four synchronized
handoffs produce `4 - 128×(1-(127/128)^4) = 0.04663` expected first-wave CAS
losers, about **1.17%**. At 200/s each shard sees **1.5625 handoffs/s**. Including
create/accept/ack, a modeled 10ms CAS window has
`1-exp(-3×200/128×0.010) = 4.58%` interference. It excludes tablet skew, SDK
and network tails, row size, and rotation work.

The historical 4,096-shard synchronized 10/50/150/200-request test remains a
state-machine stress test, with a deliberately reduced test-only minimum capacity;
it does not justify production sizing. The real load probe uses four concurrent
workers, is opt-in, and accepts `TR_JOURNAL_TEST_K` / `TR_JOURNAL_TEST_S` (defaults
128/4096). Its 600 handoffs do not establish sustained throughput through debt
accumulation and epoch rotation. **>=200 accepted handoffs/s remains unverified**.

**Rejected per-authorization intent + shared RMW counter:** RMW is server-atomic
and returns its post-increment cell value, avoiding optimistic version retries
on the counter. But a shared row still serializes increments (200/s is not proven
safe), and an absent-intent CAS plus RMW plus publication consumes the three-RPC
budget before reading/reconciling any ambiguous result. Concurrent retries can
increment twice. A crash before publication leaks capacity; refunding a claimed
increment while a delayed publication can still land undercounts accepted debt.
A crash after decrement but before recording compensation can decrement twice.
For a three-RPC candidate and counter service time `s=1 ms`, a synchronized
burst's optimistic mean is `3r + (C-1)s/2`: **34.5, 54.5, 104.5, 129.5 ms**
at C=10/50/150/200. With `s=0.1 ms` those become **30.45, 32.45, 37.45,
39.95 ms**. Neither service time is measured. At 200/s, a counter service time
of 5 ms saturates that row. There are no optimistic CAS conflicts on the RMW
counter, but server queueing and ambiguous increment ownership remain. These
numbers exclude cap compensation and every retry/reconciliation read.

One cannot repair this by merely adding a post-increment cap check and a negative
increment. An attempt-token/escrow allocation and reconciliation protocol would
be needed, with more RPCs and durable metadata. The wire tests demonstrate RMW
atomic return values and the compensation required even in its non-crashing
counter example. This candidate is **not** used in production.

**Selected K-sharded rows:** static escrow makes intent and counter colocated,
so cross-row compensation and uncertain increment ownership simply disappear.
Only the authorization's assigned shard can accept its intent. The price is
fragmentation, bounded epoch capacity, and safe fallback for a skewed shard.

## RPC inventory (Bigtable data calls)

Construct and validate a reusable adapter at startup (three Admin RPCs), never
on each handoff. Every data RPC disables automatic transport retries and has a deadline (default
250 ms). Two CAS attempts maximum, each with its own point-read. There is no
sleep/backoff or unbounded loop. `cas_conflicts`, `contention_fallbacks`, and
`rpc_calls` count local events. Registry and receipt-verification calls below
are separate ports, never disguised Bigtable RPCs.

| Operation | Normal calls | Conflict / replay / failure |
|---|---:|---|
| Initialize one shard | 1 absent-version CAM | Existing row unchanged; lost reply replay safe |
| Create registered slot | read + version CAM = 2 | Existing slot 1; maximum 4, then RetryLater; do not issue ticket |
| Accept | read + version CAM = 2 | Replay 1; retry success 4; two losers then fence 5, or 6 if fence loses |
| Cap/sealed rejection | read + pending-slot CAM = 2 | Fence loses: another read = 3; return immutable winner |
| Reaper fence | pending-slot CAM = 1 | Predicate miss adds 1 read; absent slot is unknown/error |
| Ack ledger receipt | read + version CAM = 2 | Duplicate 1; maximum 4 then RetryLater; retain debt |
| Seal shard | initialize-if-absent CAM + sealed=0 CAM = 2 | Both idempotent; second updates version without shared CAS retries |
| Recover registered missing slot after retirement | seal (2) + read + version CAM = 4 | Existing slot: fence, plus read if terminal = 4–5; contention returns RetryLater |
| Bound sealed epoch | K metadata point-reads + registry persistence | All shards sealed; concurrent acknowledgments only lower actual debt |
| Rotate | 3 × sum(K over older unclosed epochs) data RPCs + registry transaction | Includes idempotent seals and bound refreshes; successor initialization is separate |
| Close epoch | K × (1 + ceil(S/60)) bounded reads | Registry retirement required; durable registry close after verification |
| Purge slot | read + exact-slot CAM = 2 | Already absent 1; closed registry and deadline checked first |

After two **known** contention misses, the slot-specific fence wins independently
of unrelated version churn, or a competing terminal choice already won. A lost
transport reply is different: it raises an error and requires identity replay.
No false `sync_required` is returned on timeout, missing row, malformed state,
or regional outage. The enclave must withhold successful completion until an
accepted or durably synchronous outcome is known. This last-byte integration is
PR 6/9, not implemented here.

## Crash matrix

`R` = bounded point read, `C` = atomic conditional mutation. RPC execution is a
single per-row step; a crash during it is either the before or after case with
an unknown response. The tests abandon generators after RPC application before
consuming the reply, retry against surviving cells, and check invariants at
every scheduling step. No operation holds a multi-RPC lock.

| Operation and crash boundary | Durable state | Recovery action | Invariant |
|---|---|---|---|
| Missing-slot repair between seal RPCs / before missing-slot version CAM | Registered binding, sealed or partly initialized shard | Replay seal then create a durable absent-slot fence; absence itself is not permission | I1/I2/I5 |
| Missing-slot repair after missing-slot version CAM / lost reply | Durable fence or existing immutable winner | Retry returns the fence/winner without increment | I1/I4/I5 |
| Registry grant before shard init | Enumerated grant, zero or some rows | Initialize missing shards; no tickets before slot/guard | I2/I3 |
| Init before C | Row absent/unchanged | Replay absent-version C | I2/I3 |
| Init after C / lost reply | Initialized or already-existing row | Replay cannot reset version/counter/seal | I2/I3 |
| Slot binding before create R | Durable registered identity, no slot yet | Create it while active; after retirement use recover_slot to seal and durably fence it | I1/I5 |
| Create after R before C | No slot or old slot | Replay R; version invalidates competing creation/seal | I1/I5 |
| Create after C / lost reply | Bound pending slot or CAS loser | Replay returns existing binding; changed binding conflicts | I1/I4 |
| Accept before R / after R before C | Pending or competing winner | Replay identical envelope | I1/I3/I4 |
| Accept after winning C / lost reply / before response last byte | Intent AND counter durable together | Return same result; worker uses registry enumeration | I1/I2/I4 |
| Accept after losing C before retry R (both attempts) | Another operation's state, no partial increment | Bounded retry then slot fence | I2/I3/I5 |
| Accept after retry R before next C | Same as first read boundary | Version protects against concurrent ack/seal/fence | I2/I3/I5 |
| Accept cap check or exhausted attempts before fence C | No new terminal decision from this attempt | Replay intent; never assume synchronous permission yet | I1/I5 |
| Fence before C | Pending or immutable winner | Replay slot-pending C | I1/I5 |
| Fence after winning C / lost reply | Durable fenced or sync_required slot and new version | Replay returns fence; no late acceptance | I1/I4/I5 |
| Fence after losing C before result R | Competing terminal winner | Read winner; changed attempted payload conflicts | I1/I4/I5 |
| Fence after result R | Winner unchanged | Resume recovery or drain; missing remains unknown | I1/I5 |
| Import before/after transfer (future PR 7) | Accepted intent and full outstanding remain | Reimport idempotently; import is not release authority | I2/I6/I7 |
| Ledger commit before receipt delivery (future PR 7) | Final ledger + durable receipt; journal overcounts | Deliver same verified receipt; do not infer from absence | I2/I6 |
| Ack before R / after R before C | Original counter + ack bit | Replay verified receipt | I2/I6 |
| Ack after winning C / lost reply | Ack bit AND decrement durable together | Duplicate read returns without another decrement | I2/I6 |
| Ack after losing C, between retry R/C | Winning counter/slot state | Bounded replay; RetryLater preserves occupied capacity | I2/I6 |
| Registry retirement before/between shard seals | No new bindings; some shards still open | Resume K seals, then bound every older open epoch before successor grant | I3/I7 |
| Seal between initialize C and seal C | Existing row or new empty row, possibly open | Replay both; acceptance may win before its shard seals | I2/I3/I5 |
| Seal after seal C / lost reply | Permanent sealed bit + fresh version; debt untouched | Resume; delayed create/accept cannot reopen | I2/I3/I7 |
| Close between any shard reads | All read shards sealed/resolved, registry not yet closed | Recheck; sealed/resolved facts are monotonic | I2/I3/I7 |
| Close after final read before registry close | All obligations resolved, grant still unavailable | Replay validation and durable close | I3/I7 |
| Close after registry close / lost reply | Closed epoch, persistent deadline | Idempotent replay; later rotations may omit its zero debt | I3/I7 |
| Purge after registry check / R before C | Closed, retained slot still present | Recheck deadline and ack; exact slot predicate | I1/I7 |
| Purge after C / lost reply | Payload absent; sealed=2 and a fresh version prevent repair resurrection | Repeat is no-op; old create/retry fails closed | I1/I4/I7 |

A trust downgrade invokes registry retirement and finishes every shard seal.
Retirement alone is not a claim that all handoffs are fenced: already-issued
requests may win until their shard seals. Sealing is the barrier that the future
control-plane caller must await. Regional outage freezes responsibility and
prevents completion of that barrier; it does not permit grant migration.

## Bigtable semantics and configuration

The adapter uses generated requests, explicit app-profile IDs, one exact row,
and bounded column filters. In predicates the order is family, qualifier,
**newest cell**, then expected value. Matching any historical version is unsafe.
CAM true mutations execute when the filtered row is nonempty; false mutations
implement create-if-absent. Delete-old-version plus set-new-value is itself part
of the same atomic mutation. All malformed/missing data fails closed.

Sources reviewed for this design:

* [Bigtable conditional writes](https://docs.cloud.google.com/bigtable/docs/writes#conditional-writes):
  predicates select an atomic true/false mutation branch; response follows mutation completion.
* [Single-row transactions and routing](https://docs.cloud.google.com/bigtable/docs/routing#single-row-transactions):
  transactions cover one row; multi-cluster and row-affinity failover are unsuitable here.
* [Bigtable Data API](https://docs.cloud.google.com/bigtable/docs/reference/data/rpc):
  CheckAndMutateRow, ReadRows and ReadModifyWriteRow contracts.
* [Python RMW API](https://cloud.google.com/python/docs/reference/bigtable/latest/row#google_cloud_bigtable_row_AppendRow_commit):
  increments operate on signed big-endian int64 cells and return modified cells.

Startup `connect()` reads the real app-profile routing oneof, exact profile name,
cluster ID, transactional-write permission, cluster location, and table GC rule.
Multi-cluster, wrong cluster/region/name, missing family, or age-based GC refuses
construction. Do not edit profiles or GC policies under an active epoch; the
runtime service identity must have metadata-read/data permissions, **not routing
or table-admin mutation permissions**. No region failover is supported. Cloud
semantics are additionally checked by opt-in tests; a failed probe blocks rollout.

Provision `scripts/deploy/settlement_journal.sh --dry-run` to review commands.
This PR did not execute that script, including its dry-run. It creates one
`${BASE}-${region}` table with `journal:maxversions=1` (no maxage), validates
cluster placement, and creates/verifies `tr-settlement-${region}` with
single-cluster transactional routing. Existing unsafe policies fail instead of
being silently overwritten. Explicitly select the same region-suffixed table
when constructing the adapter.

Settings (all environment names start `TR_`):

| Name after TR_ | Default |
|---|---|
| ASYNC_SETTLEMENT_JOURNAL_MINIMUM_SHARD_CAP_MICROS | 32,000 |
| ASYNC_SETTLEMENT_JOURNAL_ENABLED | false; no wiring even if set in PR 5 |
| ASYNC_SETTLEMENT_JOURNAL_BIGTABLE_TABLE | trustedrouter-settlement-intents (provisioning base) |
| ASYNC_SETTLEMENT_JOURNAL_BIGTABLE_APP_PROFILES | empty; comma-separated unique region=profile |
| ASYNC_SETTLEMENT_JOURNAL_RPC_TIMEOUT_SECONDS | 0.25; bounded 0.01..10 |

Provisioning-only `ASYNC_SETTLEMENT_JOURNAL_CLUSTER_MAP` is required and has no
default. Project and Bigtable instance are explicit environment inputs.

## Verification and mutation reproduction

The in-memory backend only makes each row RPC atomic. Exhaustive 256 schedules
cover accept/fence, and 50 seeded schedules combine accept/retry/fence/ack/seal,
crash after RPC execution, and eventual replay. Crash-point parametrization
covers initialize/create/accept/fence/ack/seal/recover/close/purge; deterministic tests
cover cap races, a partial epoch seal, missing rows, replay after ack, pending
retention, and bounded contention. The wire fake evaluates actual protobuf
filters against multiple cell versions and applies the chosen mutation branch
atomically; RMW returns the updated signed integer. It does not simply return a
mocked Boolean.

Run the real tests only with an explicitly disposable table/family/profile:

```bash
export TR_JOURNAL_BIGTABLE_INTEGRATION='project,instance,table,profile,region,cluster'
PYTHONPATH=src /Users/jperla/josh/repos/tr/quill-router/.venv/bin/python -m pytest \
  -q -s -p no:cacheprovider tests/test_settlement_journal_bigtable.py
```

They do not provision resources. Unique scratch rows are cleaned after the CAS,
RMW, and 600-handoff load tests (K/S parameterized). They require Admin metadata read privileges and
Data read/write privileges. No instance/table/app profile was configured in this
session, so all three real-service tests are skipped; production throughput and
20–60 ms handoff latency are **not yet demonstrated**.

`tests/settlement_journal_mutations.py` copies src/tests to a temporary directory,
runs a green baseline, applies one mutation at a time, requires an assertion
failure (not a collection error), and restores each copy. No worktree edits or
git operations. It covers the full round-2 mutation list plus the retained round-1 split-write,
unconditional increment, refund-overwrite, and rejected-RMW compensation probes.
Every reported kill must be a named test failure on a temporary copy after a
passing baseline; collection errors do not count. See the verification report
for commands, actual results, and remaining integration gates.
