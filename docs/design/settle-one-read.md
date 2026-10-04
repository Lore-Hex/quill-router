# S2 — settle: one read

Design only, 2026-10-04. Source baseline: `5c6091f12d7ecccdc382c6ca6eec01d6a344e816`
on `latency/s2-settle-one-read`. All line ranges below refer to that checkout.
No implementation, schema migration, wire change, or release is included.

**Recommendation: GO-WITH-CHANGES.** Combine the authorization and reservation
lookup in the first statement of the money transaction. This removes one client
round trip from the ordinary one-commit Credits settle: **4 → 3 operations**.
It requires extracting settlement preparation, preserving request-scoped inputs
across retries, and adapting per-key admission. It is not a two-line SQL change.
Keep compatibility paths and refund/recovery release algorithms intact. Replay
becomes more expensive unless a separate, correctness-preserving optimization
is subsequently justified.

## 1. Baseline and inventory

Source abbreviations in the tables:

* **G**: [`routes/internal/gateway.py`](../../src/trusted_router/routes/internal/gateway.py)
* **A**: [`storage_gcp_authorize.py`](../../src/trusted_router/storage_gcp_authorize.py)
* **S**: [`storage_gcp.py`](../../src/trusted_router/storage_gcp.py)
* **R**: [`storage_gcp_request_records.py`](../../src/trusted_router/storage_gcp_request_records.py)
* **M**: [`storage_models.py`](../../src/trusted_router/storage_models.py)

The real HTTP entry is G:519–533 → G:3126–3165. It reads the authorization at
G:3135 for `authorization.key_hash`, acquires `_SETTLE_ADMISSION`, and passes
`_authorization` to the helper. G:3183 therefore does **not** read again on that
path. Merely deleting the helper's lookup saves nothing for HTTP. The production
configuration specifies a limit of 16 (scripts/deploy/rollout.sh:399).

The local authorization snapshot at G:3206–3208 also avoids wrapper rereads at
S:4777–4781 and S:4955–4959. The ordinary one-commit sequence is consequently
authorization read → transactional reservation read → Batch DML → commit.
Cold outbox-availability probes, compatibility lookups, post-settle work, and
retries are additional operations, not hidden parts of this four-operation claim.

Inventory units below are deliberately named blocks, not individual expressions.
**a** means inputs need only the request and the explicit `success` argument;
**b** means the authorization row is needed, often together with request,
settings, or process catalog inputs. These are exclusive classifications.
**c** is an overlapping flag for an observable effect in today's block, including
store I/O, logging, admission, scheduling, and default clock/random generation.
Pure local object construction is not c. The last rows include the immediately
downstream consumers that must stay outside a retryable preparation callback.

| # | Block and exact current behavior | Source lines | a | b | c |
|---|---|---|:---:|:---:|:---:|
| 1 | Lookup by request `authorization_id`; 404 if absent; HTTP admission by the resulting key, with warning/503 and finally-release | G:3134–3165, 3182–3185 | — | ✓ | ✓ |
| 2 | Report successful route fallbacks to Sentry before helper lookup/terminal checks | G:3178–3181; reporter G:3039–3075 | ✓ | — | ✓ |
| 3 | Already-settled response from authoritative finalization fields; release user-model slot; omit timing log | G:3186–3190; response helper G:4314 onward | — | ✓ | ✓ |
| 4 | Retired settlement check, error log and 409; happens after already-settled check | G:3192–3202 | — | ✓ | ✓ |
| 5 | Deep-copy local authorization for finalize; retain frozen money inputs while settled UPDATE merges heartbeat fields | G:3203–3208 | — | ✓ | — |
| 6 | Compare supplied tags to authorization tags; reject neither invalid nor mismatched tags, only warn | G:3210–3227 | — | ✓ | ✓ |
| 7 | Dump request, strip tags, soft-validate attribution and client telemetry/correlation ID; log invalid attribution/client context | G:3229–3230, 4415–4480 | ✓ | — | ✓ |
| 8 | Force synthetic metadata/app from authorization tags; later combine with body synthetic markers for benchmark exclusion | G:3231–3236, 3637–3647, 4507–4523 | — | ✓ | — |
| 9 | Refund client-context info log | G:3237–3253 | ✓ | — | ✓ |
| 10 | Strip caller-supplied operator-cost and user/app/custom-model payout fields | G:3254–3266 | ✓ | — | — |
| 11 | Reconstruct user-model pair from frozen revision/prices/owner; live model lookup supplies display name; endpoint selector calls this helper again | G:3268, 4605–4641, 4701–4716 | — | ✓ | ✓ |
| 12 | Select only an authorized endpoint/model; preserve old Gemini endpoint aliases and old selector 0/0 fixed fee; catalog model missing is 500 | G:3269–3298, 4717–4762 | — | ✓ | — |
| 13 | Output count; actual service tier; provider-specific cached/uncached prompt accounting; user-model prompt special case; Fugu tier basis | G:3301–3315, 3333–3339 | — | ✓ | — |
| 14 | Resolve partner mode from requested model, route and idempotency key; reject user-model partner and partner BYOK combinations | G:3316–3332 | — | ✓ | — |
| 15 | Video snapshot validation/cost; Stage D `billing_pricing_snapshot`; user-model/partner cost or frozen endpoint document, else effective-dated catalog pricing at authorization creation | G:3340–3387; stage_d.py:25 onward; G:4988–5048 | — | ✓ | — |
| 16 | Refund cost zeroing; user-model hold cap and warning; native batch price validation/adjustment | G:3388–3423 | — | ✓ | ✓ |
| 17 | Receipt fee, custom-model markup, hosted additional-cost validation, video hold validation, app markup, selector final cap, collected markup recomputation | G:3424–3507 | — | ✓ | — |
| 18 | Operator COGS/owner share; warning for Credits charge above hold | G:3508–3542 | — | ✓ | ✓ |
| 19 | Generation model/provider identity (including top-level Liberty); provider display name | G:3543–3549 | — | ✓ | — |
| 20 | Success-only `Generation.from_settle_body`: stable authorization-derived generation/request IDs, usage, costs, tags, app, region, metrics, attribution/client metadata | G:3551–3568; M:1169 onward | — | ✓ | ✓ |
| 21 | User-model owner payout: frozen owner/model/workspace, share after app markup; zero on refund | G:3570–3581 | — | ✓ | — |
| 22 | App owner payout and defensive clamp/error log | G:3583–3603 | — | ✓ | ✓ |
| 23 | Custom-model owner payout from collected markup, frozen recipient/workspace; zero on refund | G:3605–3618 | — | ✓ | — |
| 24 | Typed-store capability, settle/refund intent kind, internal-partner exclusion, internal-surface Credits refill requirement | G:3620–3634 | — | ✓ | — |
| 25 | Refund benchmark construction/exclusion; helper catches construction failures and warns; provider-error sample allocates UUID and timestamp | G:3635–3648, 3078–3108; M:1503–1538 | — | ✓ | ✓ |
| 26 | Freeze repair whitelist, synthetic bit and control-owned payouts/COGS into compact JSON; construct intent with resolved cost/endpoint/reservation and refill attachment; catch/log failure | G:3649–3703, 4483–4495; M:562–599 | — | ✓ | ✓ |
| 27 | One-commit attempt or durable pending enqueue; preserve enqueue's actual frozen intent, refill attachment and `intent_durable` responses | G:3705–3834 | — | ✓ | ✓ |
| 28 | Finalize dispatch and fallback; generation/payout parameters, outbox done-mark, post-commit defer; non-typed payout writes | G:3836–3955 | — | ✓ | ✓ |
| 29 | Slot release after finalize; claim-lost disposition reread; replay/durable-intent response; user-model outcome | G:3961–3990, 4269–4300 | — | ✓ | ✓ |
| 30 | Committed-only route learning/affinity, outbox disposition, refill/API-call/budget tasks, durable broadcast enqueue, refund benchmark fallback and final timing/response | G:3991–4192 | — | ✓ | ✓ |

**Counts: a = 4, b = 26, c = 18, total = 30.** Row 1's lookup key is request-only,
but the combined lookup/admission block needs the authorization's key hash.
Row 20's IDs are deterministic but Generation
has a timestamp default; row 26's intent also has timestamp defaults. Counts
describe current code, before the purity extraction.

Important negative findings: `_authorized_user_model_pair` is not pure despite
its frozen-money contract. `_refund_benchmark_sample_safely` is not pure on its
error path, and its success path allocates random identity. The safe-attribution
helpers log. Moving the helper wholesale into a transaction is unacceptable.

## 2. Proposed transaction and preparation boundary

### First statement and key selection

The existing wire supplies **authorization_id**, not reservation_id
(schemas.py:551–555). Do not assume a deterministic reservation-ID spelling:
old writers used random IDs. No request additions are needed.

The simple inner join suggested by the optimization loses information on
missing/expired reservations and on terminal authorizations. Use an
authorization-anchored LEFT JOIN, with a full projection of both existing
readers' columns. A concrete index-backed candidate is:

```sql
SELECT
  a.authorization_id, a.workspace_id, a.key_hash, a.reservation_id,
  a.model_id, a.provider, a.usage_type, a.estimated_microdollars,
  a.settled, a.created_at, a.payload,
  -- all AUTHORIZATION_TYPED_COLUMNS, in their existing order
  r.reservation_id, r.workspace_id, r.key_hash, r.ws_shard,
  r.credit_shard, r.key_shard, r.credit_reserved_micro,
  r.key_reserved_micro, r.hold_usage_type, r.settled_usage_type,
  r.actual_micro, r.authorization_id, r.settled, r.expires_at
FROM tr_gateway_authorization a
LEFT JOIN tr_reservation@{FORCE_INDEX=tr_reservation_by_authorization} r
  ON r.authorization_id = a.authorization_id
 AND r.authorization_id = @authorization_id
 AND r.reservation_id = a.reservation_id
WHERE a.authorization_id = @authorization_id
```

The comment is a projection abbreviation, not permission to omit typed fields.
Use the exact 24-column authorization projection from R:219–224 plus the
14-column reservation projection from storage_gcp_counter_dml.py:685–687.
Split by explicit offsets/names; never `SELECT *` or ambiguous duplicate names.

`a` is a primary-key lookup. `tr_reservation_by_authorization` is an existing
**NULL_FILTERED, non-unique** index on `(authorization_id)`, without STORING
columns (scripts/deploy/migrate_typed_counters.sh:156–161). Reservation primary
keys are index row locators; the additional reservation-ID equality prevents
multiple reservations for one authorization from multiplying this result. The
index needs a base-table fetch for the holds/shards. This is server work inside
one statement, not another client RPC. Never use `LIMIT 1` to choose an arbitrary
reservation from this non-unique index.

An equivalent and potentially cheaper plan is the same authorization point
lookup LEFT JOINed to `r` **by its primary key** `r.reservation_id=a.reservation_id`,
without the index hint or authorization-FK predicate. Prefer that plan if the
staging optimizer trace confirms two bounded keyed accesses: the authorization
already supplies the reservation primary key inside the SQL statement. It also
handles historical NULL reservation authorization FKs. No index migration is
needed for either plan. Benchmark both; the acceptance criterion is bounded
keyed access and one client statement, not mandatory use of a secondary index.

Typed payloads can contain fields that differ from base columns: today's mapper
uses payload identity/reservation values when payload exists. If decoded
`credit_reservation_id` disagrees with the SQL join key, or the index candidate
misses a historical NULL/different authorization FK, exit before DML and use
the existing keyed compatibility path. Do not reinterpret this as a missing
authorization, silently switch the charged reservation, or introduce a new
customer error. The oracle must cover these historical combinations.

The SELECT is the **first statement on the same read-write transaction handle**
that subsequently executes the batch and commits. Consume its result and obtain
the transaction ID before issuing DML. No explicit BEGIN, pretransaction JOIN,
parallel independent snapshot reads, or batch-first inline begin. A lost read
response can leave request-row read locks, but cannot strand credit/key locks
that have not yet been taken. Preserve bounded cleanup when an ID is available.

### Rebuild the existing objects, not an approximate authorization

Extract R:233–283 verbatim into a row-to-authorization mapper used by both
`read_gateway_authorization` and the new joined reader. It is currently inline,
not an existing separately callable mapper. Preserve
`merge_authorization_typed_columns`, `_authorization_from_payload`, typed
non-NULL precedence, payload fallback for typed NULL, timestamp spelling,
`UsageType` coercion, and typed-only construction when payload is absent.
Preserve the typed `settled` and `created_at` overrides on payload-backed rows.
Likewise share reservation decoding from storage_gcp_counter_dml.py:695–706,
especially `credit_shard` → historical `ws_shard` → UNSHARDED fallback.

Build a detached `GatewayAuthorization` for the preparation input. Build a
different mutable copy for `record_finalization` (M:923 onward), then pass that
full object to `gateway_authorization_settled_statement(pt, authorization)`.
Keep R:345–386's guarded UPDATE and live heartbeat JSON merge unchanged.
`billing_pricing_snapshot(authorization)` receives exactly its current inputs,
including Stage D eligibility and the full snapshot; do not substitute catalog
prices or treat any non-NULL snapshot as eligible.

### Pure preparation and retry lifetime

Introduce a backend-independent `prepare_settle` function, receiving request
values, authorization, settings/catalog inputs, and explicit identity/time
inputs. Return a detached plan: selected endpoint/model, usage/cost, generation,
payouts, benchmark description, frozen intent, response inputs, and diagnostic
descriptors. It performs computation and validation only. A GCP-specific store
entry accepts this preparer; storage must not import FastAPI routes. Existing
memory/Postgres and worker finalize contracts continue to work.

Split the route into request preparation, transaction orchestration, and the
existing outcome dispatcher. Normalize request-only data outside retries, but
emit its diagnostics at the same logical eligibility point as today (for
example, not on an initial already-settled early return). Capture settings and
catalog references for the request. Keep process configuration stable across
attempts; no RPC/file loading may be introduced by pricing helpers.

Default clocks and UUIDs must become explicit inputs, not repeated constructor
effects. Preserve deterministic generation/request IDs. Allocate a refund
benchmark UUID once per request and retain it through fallback; do not change
it to a different durable ID scheme. Preserve the semantic sampling points for
generation/intent timestamps and existing per-attempt outbox timestamp/window
sampling. The transaction executor owns these samples; builders consume them.
The current resolved-intent builder samples clocks at
storage_gcp_settle_outbox.py:296–298 and needs a pure, explicit-time building
seam without changing its public default behavior.

After the first valid active observation, keep the request's original detached
authorization money snapshot and prepared values stable, as main does today.
Request-local memoization must be idempotent and bounded, never a global cache.
Reconstruct fresh mutable attempt objects from that plan. Each ABORTED attempt
still executes the joined read first and uses its fresh reservation state for
claims/releases; no cached row may bypass transactional arbitration.

This distinction matters for a competing settle/refund: **an initial terminal
observation may return replay, but a terminal observation after an active plan
was prepared must retain main's claim-lost/decline/fallback path.** Otherwise S2
could skip a durable sibling intent or pending-row refresh that main creates.
Do not throw away a prepared plan on commit timeout or exhaustion: the outer
request context retains the same frozen intent for the durable fallback.

If J fails before any authorization/plan is obtained, there is no frozen intent
to enqueue. After cleanup, the outer dispatcher may run the existing lookup and
preparation under the remaining shared billing budget; if that also fails,
propagate the existing failure and rely on enclave redelivery. Do not report
`intent_durable` or synthesize a cost from the body alone. This error path is
outside the successful three-operation count.

User-model preparation currently performs up to two live model reads. Initially
leave this cohort on the existing preparation/finalize path: discover it in the
joined read, roll back before writes, and continue using the detached
authorization through `_authorization`. Preserve both existing lookups and
their error tolerance. This costs an extra cancellation round trip but preserves
its display/metadata behavior and avoids external reads inside retries. A later
pure extraction may prove those lookups unnecessary; S2 must not assume it.
App/custom-model payouts without that live-read dependency can use pure
preparation, but retain their existing transactional payout statements and
non-folded releases. They are not a three-operation cohort.

### Per-key admission is required work

G:3135 must be replaced by the new store entry for eligible HTTP requests.
Acquire admission only after the joined row reveals the authoritative key, and
before preparation or DML. The existing `try_acquire` increments on every call
(services/keyed_admission.py:24–40), so calling it on every retry would leak
slots and spuriously reject requests.

Use a **request-owned acquire-once lease** in transaction orchestration: remember
the acquired subject; subsequent callback attempts with the same subject are
no-ops; hold it across rollback and durable fallback; release exactly once in
the outer HTTP `finally`. A different subject on retry must abort/exit before
money writes and be explicitly classified, never inherit another key's lease.
This is the narrowly scoped idempotent local effect permitted at the runner
boundary; the business preparer remains pure. Rejection is nonblocking and
rolls back the read transaction before emitting the existing warning/503 and
Retry-After. Check admission before initial settled/retired processing, as HTTP
does today. Direct refund/helper callers must not acquire a new gate that main
does not use. Compatibility dispatch must reuse the lease, not acquire twice.

There is no cold-cache way to preserve admission *before all transactional reads*
without either the old authorization RTT or a new trusted identity input. S2
moves this existing gate past one read, not past any hot-row write. Measure
overload behavior; disabling admission or trusting request-supplied key hashes
is not an alternative. If even idempotent local acquisition inside orchestration
is ruled out, this three-operation HTTP design is NO-GO under the current wire.

### Early outcomes and cleanup

Prefer typed control-flow signals with protected rollback to a bare return from
`run_in_transaction`: an ordinary return can trigger a gratuitous empty commit.
Adapt the existing protected cleanup pattern (storage_gcp_io.py:369–419 and
A:1641–1658); distinguish early outcomes from `OneCommitSettleDeclined`.

| Observation | Action outside the callback after rollback |
|---|---|
| No typed authorization | Perform the existing legacy authorization lookup by complete entity key. Only report 404 if that too is absent. S:4646–4657 currently has this fallback. |
| Initial `authorization.settled` | Return `_already_settled_gateway_data`; release user-model slot once outside the transaction; no intent insert, pricing, or timing line. Reservation may be absent. |
| Initial unsettled retired settlement | Emit existing error and return 409; no generation/intent/releases. Settled check still wins over retired check. |
| Authorization present, reservation absent | Preserve one-commit `OneCommitSettleDeclined("not_found")`; valid prepared intent still follows durable enqueue then existing finalize/disposition. Do not turn it into authorization 404. |
| Reservation claimed after initial active observation, typed UPDATE zero, done-mark zero, or release guard failure | Preserve `_RetrySequentialFinalize` rollback, then one-commit decline to durable fallback; sequential retry only where main allows it. |
| Invalid endpoint, pricing input, or model | Return the same API error after rollback; no partial batch. |
| Unsupported record/backend/preparation capability | Roll back before writes and use the existing path with the available detached authorization. |

A runner with a verified explicit no-write close could return a typed outcome
instead; do not assume that close has zero RPC cost. The count table below uses
one Rollback for early exits. Never interpret an early replay sentinel as a
one-commit failure that should enqueue an intent.

### Batch and effects

Reuse existing batch construction and all count checks. Its ordinary order is
reservation claim → authorization settled UPDATE → resolved settle intent and
retention → generation/activity → benchmark → credit release → current/stale
key release forms → commit (A:1769–1859). Credit stays before key and both stay
last. No new shared SELECT of `tr_credit_balance` or `tr_key_limit` is allowed.
No eligibility probe against these rows; retain conditional DML guards.

Refunds, payouts, debt recovery and `_RetrySequentialFinalize` recovery keep
their current algorithms. Main has existing exceptional recovery reads (for
example its lazy debt-recovery shard enumeration); S2 does not add, broaden, or
move them into the first read. The three-operation cohort performs none of
those shared counter reads. Changing recovery to satisfy a stronger blanket
prohibition would be a separate money-code project, not a read-fold refactor.

Effects have distinct destinations; they cannot all become background work:

* Route-fallback Sentry stays once at its current request-dispatch point. Tag,
  attribution, cost-cap/overrun and refund-context diagnostics become values,
  emitted once by the outer dispatcher when that main-equivalent branch ran.
  Failed-attempt diagnostic lists do not accumulate. Failure diagnostics need
  an after-rollback hook as well as a post-commit hook.
* Admission is the acquire-once local lease described above. Slot release,
  model outcome recording, route learning and affinity run after the final
  outcome, respecting replay/claim-lost behavior.
* Money, generation, activity intent, one-commit benchmark intent, retention,
  and refill attachment remain in their current durable transactions. Pending
  settle enqueue for the two-commit fallback executes outside the abandoned
  transaction **before** fallback finalize. It is not a post-commit best-effort
  task. Existing outbox/refill failure responses at G:3754–3834 stay intact.
* Optional analytics uses existing `defer_post_commit` only after confirmed
  commit (post_commit.py:126–135; S:4835–4853). Register tasks once, with deep
  copies; synchronous direct callers keep their current behavior. One-commit
  benchmarks must not also run as post-commit duplicates.
* Auto-refill scheduling, API-call counters, alerts and refund benchmark
  fallback keep their present dispatch conditions. Broadcast enqueue remains
  durable **before the HTTP reply** (G:4094–4126); moving it to background tasks
  would change crash durability. Preserve fallback legacy payout writes too.

## 3. Operation sequences and RPC counts

Count sequential client operations, including commit/rollback; a Batch DML is
one operation regardless of its statement count. This is not a count of internal
Spanner messages or streamed response frames. Assume warm schema capability,
no retry, and the same post-finalize work. Let A = external authorization read,
R = transactional reservation read, J = joined first read, B = Batch DML,
C = commit, X = rollback, E = durable enqueue transaction. A fresh E is B+C = 2
(storage_gcp_settle_outbox.py:486–511). E on duplicate/leased/terminal rows is
larger: keep its existing INSERT failure, rollback, conditional refresh,
classification and optional reporting/refill reads.

| Shape and assumptions | Main | S2 | Main → S2 operations |
|---|---|---|---|
| Ordinary one-commit Credits, positive hold, cost ≤ hold, no payouts, successful guards | A; R B C | J B C | **4 → 3** |
| One-commit refund, positive credit hold, existing key, no recovery debt | A; R B credit-UPDATE debt-SELECT key-UPDATE C | J B credit-UPDATE debt-SELECT key-UPDATE C | **7 → 6** |
| Initial typed replay/already settled, no user-model side effects | A | J X | **1 → 2** |
| Initial typed retired authorization | A | J X | **1 → 2** |
| One-commit attempt fails, then durable two-commit fallback | A + P + E + F | P(J for R) + E + F | **1+P+E+F → P+E+F** |
| Example: benchmark INSERT error rolls back attempt; fresh E; ordinary fallback finalize succeeds | A; R B X; B C; R B C | J B X; B C; R B C | **9 → 8** |
| One-commit unavailable by configuration before any read; durable two-commit path selected directly | A + E + F | Existing path | **unchanged** (ordinary example 6 → 6) |
| User-model/other unsupported cohort discovered by J, then existing path using detached authorization | A + H | J X + H | **1+H → 2+H** |
| Legacy-only authorization / truly missing ID | Typed A + keyed legacy lookup + H | J X + keyed legacy lookup + H | **2+H → 3+H**, assuming one legacy lookup |

P includes all failed-attempt RPCs/cleanup, F includes all fallback finalize RPCs,
and H is the rest of the unchanged path. Post-commit benchmark delivery,
broadcast, refill attachment/reporting and final disposition lookups are excluded
from both columns; they remain and must be counted in a full HTTP trace. If
enabled synchronously, add the same applicable operations to each side.

**Do not quote 6 → 5 for an unconditional two-commit fallback.** To enqueue a
frozen intent first, S2 needs authorization-dependent preparation. If one-commit
is unavailable before starting, retain main. If discovered inside J, close that
transaction before E, and pay X. Where a real one-commit attempt already occurred,
S2 saves its initial A, then retains the independent F reservation read. Carrying
J's reservation across rollback or running E while J remains open is unsafe.

Refunds are not 4 → 3: `fold_tail` requires `success` (A:1870–1879).
Their credit release invokes recovery lookup even when no debt exists
(storage_gcp_counter_dml.py:294–367; storage_gcp_trust.py:261–291). Positive debt,
deleted keys, over-hold usage, zero holds, BYOK and payouts have variable extra
operations. Leave these unchanged. Underflow/window rollover/claim races can
make F include another speculative rollback plus fresh sequential transaction.

## 4. Risks and required controls

| Risk | Required control / acceptance evidence |
|---|---|
| CPU and larger row decoding inside transaction | Measure decode/prepare time and bytes. Pre-batch work lengthens reservation/authorization read-lock lifetime and total transaction duration; it does **not** extend credit/key hot-lock hold time, because those locks are first acquired by the batch tail. Authorization read locking can increase heartbeat conflicts. |
| ABORTED re-execution | Fresh J each attempt; fresh mutable objects; stable request plan/entropy; attempt-local statements and diagnostics; once-only lease. Exercise both SDK inner and wrapper outer retries, including abort at commit. Preserve live heartbeat merge and window-boundary resampling. |
| Concurrent settle/refund and early replay | Distinguish first observation from a later terminal observation; preserve main's fallback/sibling intent effects. Do not infer authorization settled from reservation settled. Compare commit histories, not just final balances. |
| Twenty-second billing budget | G:290, 3126, 3168 establish 20 s; retain the nested 5 s one-commit allowance (A:125; S:4970), now including J/preparation. No fresh budget per callback/compatibility retry. Check remaining budget after CPU work and before writes. Preserve protected rollback's independent bounded floor; cleanup can exceed the billing deadline by that existing bounded allowance. |
| Lost batch/commit response | First J supplies ID before hot writes; retain `_dispose_open_transaction` for the last attempt, including commit errors. Do not proceed to durable fallback with a known live transaction holding locks. Unknown commit may already have landed; retain claim-first idempotence. |
| Duplicate outbox INSERT | `ALREADY_EXISTS`, not count zero, is expected (A:1785–1794; S:5027–5034). Roll back the entire attempt and use existing pending refresh/lease/terminal classification. Never change INSERT to upsert or swallow duplicate inside the transaction. Benchmark failures also fall back; analytics must not compromise money. |
| Sentry/log/task duplication | No emission or task registration in business preparation. Emit once for the selected request outcome, including error paths; preserve refund benchmark identity and distinguish it from success's deterministic benchmark. |
| JOIN access plan and memory | Index is non-covering and non-unique. Verify seek predicates, base fetch, rows scanned, response bytes and two-row decode allocation in staging; include worst supported snapshot/payload sizes and multiple reservations per authorization. No full scans, wildcard entity search, unbounded aggregation, or new covering index carrying full payloads. |
| Historical JSON and typed column drift | Shared mapper plus tests for payload-only, typed-only, NULL typed fallback, conflicting typed heartbeat, missing reservation, NULL FK, mismatched pointer and retired kinds. Compatibility path stays reachable. |
| Admission moved after read | Same per-key cap and immediate rejection, idempotent lease through retries and fallback, all exception paths release. Measure read pressure under rejected-request bursts; ensure no DML before admission. |
| Replay latency regression | J+X is two RTTs versus one point read; report replay separately and retain timing-log exclusion. Estimate traffic-weighted benefit before rollout. Do not hide X or substitute an unbounded/stale authorization cache. |

## 5. Proof and test plan

### Frozen-main differential

Create a new `tests/fakes/settle_s2_main.py`, sourced from the exact baseline SHA
above (or explicitly repin to the eventual implementation base). The existing
`settle_c1_main.py` is pinned to **c5486e78**, before the present folded tail,
and is not the S2 oracle. Follow `tests/test_settle_c1_oracle.py:14–58` for AST
provenance pins, but extend the frozen surface to the HTTP admission wrapper,
full settle orchestration, preparation helpers being extracted, store wrappers,
authorization decoder, finalizer, statement builders, serialization and clock/
identity dependencies that change. Redirect only documented imports. Sharing
the changed preparer or mapper between oracle and S2 would share their bugs.

Run both HTTP entry paths against independent cloned stores with identical
request, settings, catalog, entropy and semantic clock events. Require exact
HTTP status/body and byte-identical stored payload/intent/generation JSON,
plus equality of all typed money/payout/retention/activity/benchmark/refill and
legacy rows. Check durable state after each successful commit and at crash
boundaries. Supply identical commit timestamps rather than stripping arbitrary
fields from comparisons. Align injected times by semantic event; separately
exercise real advancing clocks at day/week/month boundaries. Byte identity is
the assertion, not an excuse to normalize away changed money or audit fields.

The matrix must cover:

* One-commit success, direct two-commit, all declines, absent/missing outbox and
  activity capabilities, benchmark construction/INSERT failure, duplicate
  pending/leased/done/dead/release-approved intents and sibling settle/refund.
* Credits/BYOK, zero/exact/under/over holds, debt and no debt, deleted key,
  missing shard and shard-zero recovery, underflow, NULL/old/current window
  starts, all window transitions during batch, claim and typed UPDATE misses.
* All price sources: Stage D snapshot and ineligible/malformed snapshot,
  legacy effective-dated catalog, direct OpenAI priority, Anthropic cache
  accounting, Fugu tier basis, partner top-level/internal, native batch,
  hosted search/image/video additional cost and video snapshot limits,
  selector legacy 0/0 tariff/new-meter clamp, receipt fee, and all combinations
  of app/custom-model/user-model payouts and caps.
* Frozen owner/model revision with deleted/changed live user model; malformed
  frozen user-model fields; preserve live-name lookup failure behavior. Test
  backend memory/Postgres compatibility as well as the GCP typed path.
* Initial replay with/without reservation, settled retired versus unsettled
  retired, missing authorization versus legacy-only authorization, reservation
  NULL FK/random ID/multiple same-authorization rows, mismatched payload pointer,
  typed-only/payload-only and typed-NULL fallback, heartbeat before read/between
  attempts, generation/intent exact tags/attribution/synthetic/client filtering.
* Concurrent settle/settle and settle/refund; reaper winning after first active
  observation; ABORTED before/after batch and at commit; batch response loss,
  known failed commit, unknown successful commit, budget exhaustion and rollback
  failure. After restart, run the durable drain and verify no double charge,
  payout or lost repair/refill work.
* Admission limit and rejection at 16, inner/outer retries with one lease,
  cancellation/exception release, subject mismatch, refund bypass compatibility,
  and late terminal observation without suppressing main's fallback effects.

### Operation and hold-time fake

Extend the transaction trace fake used by the C1 oracle and
`tests/fakes/spanner_order.py`. Observe the **HTTP admission entry**, not only
`typed_finalize_atomic`. Model separate network operations for SELECT, batch,
commit and rollback; batch-contained statements are not separate network hops.
Record transaction IDs, first statement, all reads, batch SQL/counts, first hot
lock, last hot lock, release on commit/rollback, and complete attempt histories.

For eligible no-retry settle assert exactly **J, B, C**, no external A, no
explicit BEGIN, and no counter SELECT. Inject arbitrary preparer CPU delay:
request-row lock duration grows, but credit/key hold duration stays batch tail
through commit. Enforce credit-before-key and every non-hot batch statement
before both. A lost first read response must execute no DML; a lost batch
response must have a known transaction ID and protected rollback before fallback.
Assert the table's refund/replay/fallback traces too. The fake proves client
shape/order, not real distributed lock latency; validate plans and contention
on staging Spanner with normal trace tags before any production rollout.

### Mutation audit

Each mutation must fail a behavioral assertion, not fail collection/setup:

1. Restore the admission wrapper's separate authorization read; or move J to
   a snapshot outside the transaction; or insert an explicit BEGIN.
2. Execute the batch before receiving J's transaction ID; omit cleanup after
   lost batch/commit response; reuse J's reservation after rollback.
3. Add a shared balance/key SELECT; move hot releases before generation/outbox;
   swap credit/key; split reservation claim and releases into separate commits.
4. Replace LEFT JOIN with INNER JOIN; pick an arbitrary indexed reservation;
   assume deterministic reservation IDs; drop pointer compatibility handling.
5. Decode only JSON, ignore typed overrides, lose pricing snapshot, or replace
   frozen pricing with current catalog; drop shard fallback or heartbeat merge.
6. Reuse a mutated settled authorization on retry; return early on a later
   terminal observation; regenerate refund benchmark UUID per attempt.
7. Emit Sentry/logs/register background tasks or increment admission per retry;
   release admission per attempt rather than once per request; skip gate entirely.
8. Swallow `ALREADY_EXISTS`, upsert an intent, omit refill/retention writes,
   acknowledge a durable intent before commit, or mark a claim-lost intent done.
9. Change refund to folded-tail release; suppress debt recovery; drop row-count,
   no-debt, window boundary or payout atomicity guards.
10. Grant each retry a new 20 s/5 s allowance; acknowledge before broadcast
    enqueue; duplicate a benchmark already inserted in the money commit.

Keep the existing C1, speculative batch, one-commit, deferred settlement,
pricing, money and conformance coverage. Patch backend classes, not the STORE
proxy. Implementation acceptance requires the full `uv run ruff check .`,
`uv run mypy`, and `uv run pytest -q` gates, coverage ≥70%, the differential and
mutation audit. No conformance relaxation. This design pass performs source
inspection only; those are future implementation gates, not claimed results.

## 6. Size, expected gain, and decision

Estimate **7–10 production files, 800–1,300 changed production lines**, of which
roughly 450–650 are extracted/moved preparation code; **5–8 test files,
1,500–2,500 test lines**, plus a **1,800–2,800-line frozen source oracle**.
The largest changes are gateway orchestration/preparation, the GCP store and
transaction adapter, shared row decoders, request admission lease, and explicit
clock/entropy seams. Model/outbox constructors may need small pure-builder
adapters. No schema or enclave changes. This is several days of implementation
and proof plus staging contention/latency validation, not an afternoon patch.

Using the supplied 2026-10-04 measurements, the first-order gain is one RTT:

| Control-plane region | RTT to nam6 | Current settle p50 | Predicted eligible p50 | Reduction |
|---|---:|---:|---:|---:|
| us-central1 | ≈10 ms | 111 ms | ≈101 ms | ≈9.0% |
| us-east4 | ≈35 ms | 272 ms | ≈237 ms | ≈12.9% |
| europe-west4 | ≈110 ms | 697 ms | ≈587 ms | ≈15.8% |

These are estimates, not measured S2 results: net gain is saved RTT minus
additional JOIN execution/decoding and changed contention/retry cost. Preexisting
pricing CPU moves inside the transaction rather than disappearing. Measure
eligible and compatibility cohorts separately, including p95/p99, attempts,
deadline/fallback rates, admission rejection, request-row read locks and hot-row
hold duration. Initial replay can lose approximately one RTT. A simple mean
traffic estimate is `(eligible_fraction - extra_RTT_fraction) × RTT`, before
JOIN/CPU/retry overhead; p50 cannot be obtained by averaging cohort p50s.

Proceed only with the admission, mapper, retry-lifetime and early-out changes
above, verified against frozen main. Keep unsupported cohorts on compatibility
paths and disclose their costs. If staging shows join-plan overhead, use the
authorization-PK → reservation-PK LEFT JOIN variant first: it is the smallest
alternative, still one statement and still establishes the transaction ID before
hot locks. A pretransaction JOIN followed by batch-first execution, an explicit
BEGIN, or a required reservation/key field in the enclave request does not meet
S2's constraints.
