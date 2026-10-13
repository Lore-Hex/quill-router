# Async settle shadow evidence policy v2: certifiable on a real fleet

Status: **design amendment, 2026-10-11; not implemented or enabled by this document**.
Joseph selected revised A: amend the evidence gate, preserve correctness, and
measure a new window. PR names below are work packages, not existing PR numbers.
No code, deployment, money changes or Git writes belong to this authoring pass.


> **Amendment A1 — round 2, 2026-10-12, DESIGN TEXT ONLY; proposed, not activated.**
> This amendment answers G-A/R2 (health eligibility), G-B/R5 (authorization
> obligations), and G-C/R6 (original-writer close acknowledgement). Round 2 adopts
> S1–S3: Spanner observation/read timestamps, terminal-only report discharge,
> and the same service-identity provenance as other router evidence. It was
> written against two adversarial design reviews of 2026-10-12 (the review that
> rejected the first proposed rules, and the review of amendment round 1); their
> reported executions are not executions by this author. A third review approved
> round 2 and supplied two bounded corrections, applied verbatim: the literal
> serialization of report obligations and close-result fields (§§7.3, 7.4) and the
> claim query's result-row accounting (§3.2).
>
> **Change log:** §2 states the evidence threat boundary; §3 separates freshness
> from the 500 ms availability bound, replaces host-clock qualification with
> exact Spanner snapshot metadata and bounded monotonic elapsed time, and fairly
> costs immediate confirmation. §§6/7 distinguish writer closure from report
> discharge, commit report obligations through the receipt chain, retain strict
> terminal completion context, and keep an insert-only post-ack close-result row.
> §7.1 defines the P1-A1 fixture release and enclave pin-only follow-up; §7.2
> removes ownership/origin export infrastructure and completes remaining health
> attachment operation/churn budgets. §§8–12 propagate gates, dependencies,
> adversarial proofs and costs; §13 gives real operator choices; §14 records
> source verification. CLOCK_QUALIFICATION, owner fields/acceptances/pages,
> ownership lanes, per-boot signing, P0-A1 and behavioral P2-A1 are removed.
> Money AdmissionCache/fleet-v1, DDL, indexes and request money behavior are unchanged.
> P1 remains the frozen baseline; the proposed A1 contract is separately pinned.
> BEFORE/AFTER labels describe changes to the pre-amendment design; round-1
> protocols survive only where explicitly labelled as alternatives.

> **A2 ADD — Amendment A2, round 3, 2026-10-12, DESIGN TEXT ONLY; proposed, not activated.**
> Round 3 closes only A2-R1–R3: failure identities/counting units, conflict-entry
> preimages and report metric populations; the accepted round-2 decisions remain.
> Closes the five P5 contract questions with an A2 report behind the unchanged
> A1 gate envelope, all 58 exact failure counts/blocked days and bounded subject prefixes,
> an offline verifier port, explicit digest preimages and bounded proof envelopes.
> §§7.5/8 define the contract; §§7.1/10 select one combined A1+A2 fixture release
> and exactly one enclave pin-only follow-up; §§11/12 add proof/cost obligations.
> Round 2 adopts one dedicated eligible probe workspace (owner decision 2026-10-12),
> splits P4a compared integration from deferred P4b exclusion issuance, and makes
> `shadow-probe-workspace` a P6 prerequisite. No exclusion issuance is needed for
> the first window. P1-A2 is pure/additive; P3/P4a/P4b depend on A1, not A2. Baseline and A1
> behavior/bytes remain frozen; no DDL, index, money, wire, capability, enclave
> behavior or request-path change. §14.3 verifies code claims in the supplied
> read-only P1-A1 tree. No tests or production measurements were executed; local catalog arithmetic is labelled.
> A2 BEFORE/AFTER passages supersede only the quoted text; A1 is not reopened.
> The earlier A1 note's separate enclave pin is consolidated as specified below.

## 1. Evidence, authority and amendment scope

**D-V2-1 — adopt revised A.** The normative policy ID is `shadow-v2`.
All “must”, schema names and algorithms below describe proposed behavior until
the named PR lands and its activation gates pass. “Current” claims are limited
to the source ledger in §14. `R/` means this router tree at
`876f3c7b1ad0450181b05020d3a7a6634a5bf275`; `E/` means the read-only
`/Users/jperla/josh/repos/tr/wt/qcp-ref/` checkout at
`08986d4057350166b0261336672775ee2d0dade0`. Citations use those roots.

Inputs read in full: `/tmp/shadow-v2-inputs/gate-findings.md` and
`/tmp/shadow-v2-inputs/verification-report.md`. Production observations below
are **gatekeeper-reported**, not independently queried or established by source
inspection. The verification report's seven witnesses are reported offline
results, not rerun results from this authoring pass.

| Finding | Evidence and qualification | Classification against v1 |
|---|---|---|
| F1: missing shadow health prerequisite | Gatekeeper: health row absent at 14:51Z; all 34 samples unknown/cache_missing, 0 yes, 0 no, 34 unknown. Current installed publisher is in fast drain after its flag-off return; the helper itself is callable without that flag. Observer returns unknown for absent health; installation only supplies readers (C1–C3). | Missing publisher integration is an implementation defect. Blocking missing health/unknown seeds is **required** by shadow-v1 §§5–6; its prohibition on shadow publication must be amended. Outbox-v1 §10/order and runbook defer fast drain until after PASS. |
| F2: mixed traffic lacks terminal proof | Gatekeeper: 69/69 writers with envelope-less settles have observed_partition_gap, settle:phase_coverage_gap and terminal_coverage_gap; all 141 opted authorizations were adapter=other. Projection/authorize collapses missing snapshot or signer, and capture can drop snapshot_size before reading headers (C4–C5). | Blanket envelope/comparison requirement is an **implementation choice beyond intended eligible scope**, made concrete in shadow-v1 §§3–4/6. Blocking these ambiguous existing rows is **required**. Verification's signed observed service-tier exclusion passes: “any out-of-boundary request” is too broad (C6–C7). |
| F3: every writer needs a known prediction | Gatekeeper: 86/172 writers had no opted traffic; all 86 counter_gap, 74 solely from known>0. Some boots lasted under four minutes; 172 boots in 15 hours. Predictions accrue on booked terminal processing, including refund; idle writers are registered (C5). | **Required explicitly** by shadow-v1 §5 per-counter known>0; report and pilot share it (C7–C8). Authorize-only and excluded-only writers need the same amendment as idle writers. |
| F4: every failed checkpoint becomes loss | Gatekeeper: 339 store drops, 333 worker and 6 settle; 59 on otherwise idle writers; previous day 52,035. flush_failed emits a sticky worker/store_unavailable drop and the report also treats it as terminal failure (C9, C7). | Zero actual loss, unknown coverage and missing closure are **required** by shadow-v1 §§4.2/6/7. Permanently vetoing a fully recovered cumulative checkpoint is an **implementation choice**, stricter than §7's “flush later” versus missing flush distinction. |
| F5: two-workspace budget fails | Gatekeeper: N=1 pre-enable PASS at 30 reads/s and 6,006 pending-row visits/s per region versus allowances 36 and 7,208. N=2 gives 36 and 12,012: 66.6482% above pending allowance. C8 verifies the formulas at cap 24 and exact pre/post N matching. | Budget enforcement is **required** by shadow-v1 §5 and outbox runbook. Those allowances are operational choices. Reported CPU 26.5% peak + projected 23.6%=50.1% is unverified sizing, not a calculation implemented by the tool; tool only checks supplied allowance and strict <45% (C8). |
| Historical boots summed as simultaneous load | Verification witness: eight nonoverlapping one-day boots, cap one, coverage PASS but fleet BLOCKED at 10 reads/s and 2,002 pending-row visits/s. Code takes each boot's peak and sums all boots; compares caps with historical distinct counts (C8). | Originally conservative **implementation choice**, now expressly prescribed by outbox runbook step 5 (“instances are summed conservatively”); explicitly superseded here, never silently weakened. All historical boots still need coverage. |

The gatekeeper also reports #1667 region labels and #1678 reset sanitization
merged at 14:46Z with deploy queued. That is not proof of serving deployment.
The actual #1667 per-writer regional-map check is present in this tree (C12).

### 1.1 Exact precedence over v1

The following table is exhaustive. Within a named section, only the listed rule
is superseded; every other sentence remains normative verbatim. Historical
source/measurement assertions remain historical, not re-certified by v2.
Forward pointers in the originals are conditional on the named PRs.

| v1 location | Superseded rule | Replacement / landing package |
|---|---|---|
| shadow-v1 §1 D-S2, §2.1, §2.2, §2.3 | Only a price binding can carry terminal diagnostic provenance; replay/header absence only aggregate; snapshot cap precedes any exclusion path; single v1 wire | §4/§7, P1 + P2 + P4; legacy body bytes and off guards retained |
| shadow-v1 §3 steps 1–5, 8 and exclusion paragraph | Every terminal requires price-comparator envelope; all exclusion proof follows that path | §4/§5 closed partition, P4; §3.1 arithmetic and explanation equations unchanged |
| shadow-v1 §§4.1–4.2 | v1 keys/schema; only sample existence backs retry; checkpoint failure equals sticky loss; no explicit durable recovery/closure ledger | §§5–7, P1 + P3 + P4; true loss remains sticky |
| shadow-v1 §4.3 | Exactly three evidence kinds | §7 bounded v2 kind list, P3; retention, indexed pagination and cleanup bounds unchanged |
| shadow-v1 §5 | No shadow health publisher; money worker heartbeat as shadow source; positive known predictions for every writer; observer-only load accounting; all failed flushes are drops; historical one-second worker budget prose | §§3/5/6 and G5–G7, P3 + P5; request CPU, observer cadence/failure/freshness limits unchanged; current storage budgets explicitly accounted below |
| shadow-v1 §6 | Envelope/comparison-only partition, v1-policy/report fields, fleet-wide moving prerequisite timestamp on new boot | §§5/7/8, P5; positive seed, 604,800 seconds, zero disagreement, reset/fix provenance retained |
| shadow-v1 §7 | Store-unavailable/no-binding/size/restart rows insofar as they exclude certificates or checkpoint reconciliation | §§4/6, P3 + P4; every actual loss/crash gap retained |
| shadow-v1 §§8–10 | Original F2 scope/wire/schema and proof tables as complete implementation checklist | §10/§11 add v2 obligations, P1–P5; all old money/isolation/arithmetic mutation obligations retained |
| shadow-v1 §11 and §12 answered shadow-workspace/landing choice | F2 router-first emission order and old shadow start procedure/cohort assumption | §9/§10/§13, P6; dormant PR G overlap remains allowed |
| outbox-v1 §8 | Exclusions represented only through comparisons; evidence-policy implementation of shadow exit | §§2/4–8, P4 + P5; money/external proof exit criteria retained |
| outbox-v1 §9 row F and row G | Original list sufficient for shadow certification/enablement | §10, P1–P6; original A–E money scope unchanged |
| outbox-v1 §10 Decisions item 1 and its seven-step enablement order | Operator workspace automatically also the shadow cohort; old health/clock order | §9/§13, P6; **admission pilot identity is not changed by default**; §10 decisions 2–5 kept verbatim |
| outbox-v1 PR G opening cohort paragraph, “Evidence bundle and read boundaries”, “Serving-region label transition and the clean window”, “Exact enablement order and gates” steps 1–8 | Shared shadow/pilot identity, v1 evidence kinds/proof, earliest October 11 start, historical-boot sum, known>0 for every writer and old prerequisite order | §§5/7–10, P5 + P6; #1667 true-region maps and full-SHA rules retained; step 9 unchanged |
| outbox-v1 “Rollback and truthful status”, final clock-prerequisite paragraph only | One global maximum of refreshed gate completion times for all boot traffic | §5.3, P5; rollback order and accepted-work protection remain verbatim |

Kept verbatim in particular: shadow-v1 D-S1 and D-S3–D-S6 (except additive
versioned evidence names), §3.1 exact arithmetic, authenticated S0/full/hash-only
rules for eligible work, body/response/stream isolation, bounds except explicit
v2 additions, §6 correctness reset history and external gates, §6.1 logs not
being evidence, and §11 non-goals. Outbox-v1 §§1–7, §3 wire/ticket authority,
PR C/PR D/F1 appendices, measured-drain selection, key placement and money
rollback rules are not amended. The old ticket and billing fixtures remain
unchanged. V2 does not claim their historical implementation citations describe
this checkout; §14 is the current-code claim ledger.

A2 round 2: references to P4 in the precedence table mean P4a for compared
integration and P4b for exclusion issuance/worker integration. P4b is deferred
for the first eligible-only window; §10 gives the controlling dependencies.

### 1.2 Decision alternatives

| Decision | Rejected alternative and reason |
|---|---|
| D-V2-1 | Engineer fixed boots/eligible-only traffic around v1: violates fast-drain-off constraint and leaves crash/checkpoint obstructions. |
| D-V2-2 | Percentage comparator/evidence loss allowance: cannot prove every eligible request or a closed partition. |
| D-V2-3 | Reuse money fleet health schema: falsely asserts worker liveness; per-router fleet scans multiply node load. |
| D-V2-4 | Unsigned reason/body flag or authorize aggregate: proves neither immutable facts nor this terminal; global missing-envelope exemption hides eligible losses. |
| D-V2-5 | Condition coverage on prediction count: broken instrumentation would erase demand; sum historical boots: mistakes sequential capacity for concurrency. |
| D-V2-6 | Forgive every store failure, or block every recovered checkpoint forever: respectively loses proof or rejects fully reconciled evidence. |
| D-V2-7 | Reinterpret v1 rows/add nullable recovery fields: old evidence cannot establish new facts; signed literals in prose duplicate fixture authority. |
| D-V2-8 | Different pilot/report predicates: allows contradictory PASS results and unchecked deployment gaps. |
| D-V2-9 | Continue the October 11 clock or reuse old N=1 budget: neither has the v2 publisher/partition proof or incremental load measurements. |
| D-V2-10 | Router emits before enclave support, or one large activation PR: loses terminal certificates and combines independently reviewable proof boundaries. |
| D-V2-11 | Fake-only storage tests or mutants that merely fail import: do not prove transaction/unknown-commit behavior or kill the intended defect. |
| D-V2-12 | Predict success from reported transient drops: conflates recoverable failures with residual loss and ignores correlation. |
| D-V2-13 | Automatically replace the admission pilot when selecting a shadow cohort: overrides the existing exposure/side-effect decision without pilot measurements. |

## 2. Goals, non-goals and invariants

**D-V2-2 — closed evidence, unchanged money.** For each terminal HTTP attempt
(settle or refund, including retries), in each writer/day/phase/adapter/route/
stream bucket, require a disjoint partition:

`T = A + B + C`

- A: eligible and compared, with durable authenticated S0, raw-body agreement,
  normalized Python/Go/legacy usage and actual confirmed booking comparison.
- B: authenticated out-of-cohort and not price-compared, backed by a verified
  exclusion certificate or the retained v1-style authenticated observed-exclusion
  proof; the latter verifies provenance/usage but makes no price equality claim.
- C: unresolved or lost, including inability to decide eligibility, missing
  comparison or terminal proof, unknown booking, failed persistence or conflict
  needing review. **C must equal zero** for a certifiable interval/day.

A known eligible attempt that fails comparison remains C with
`eligibility=eligible`; this matters for demand (§5). Classification exact or
explained must cover all A; a computed mismatch remains visible and resets even
if its attempt is C. B is never exact, an admission yes, or a clock seed.
Zero unexplained price/normalization/signature/hash/identity disagreement applies
to **every eligible opted request**, including retries, not a selected sample.
No loss percentage or money tolerance is introduced. The existing 2% admission
unknown allowance concerns counterfactual prediction availability only, never
comparator availability or evidence loss.

Throughout the entire shadow window: router admission **false**, protection
**false in this pre-admission deployment**, enclave negotiation **off**, money
fast drain **false** on every serving revision. Never turn protection off if
accepted async work exists: first apply the retained rollback/drain procedure;
such a deployment is not eligible to begin this shadow phase. Shadow changes
no hold, amount, winner, outbox intent, side effect, response, retry policy or
stream frame. Legacy synchronous recovery remains independent.

**Residual limit:** a hard crash, missing final closure, or unrecoverable evidence
loss still blocks the affected day, including an apparently idle boot. A final
absence cannot prove zero requests. Graceful churn and recovered checkpoints
can certify; arbitrary loss cannot. Removing this limit requires a separately
reviewed durable pre-response attempt/evidence handoff: router authorization and
terminal receipt commitments, replayable inputs/booking references, sequenced
journal and acknowledgements, lost-ack/idempotency proof, and enclave retry/spool
integration. An enclave spool alone cannot recover undelivered router authorizes.
That would change latency/isolation assumptions, must remain outside money
transactions, and is out of scope.

**A1 threat boundary (S3):** router evidence is produced by correctly executing
reviewed code under the router service identity, with actual boot/day in keys and
bodies and independent G2 inventory/coverage. Faults, restarts, missing/late replies,
conflicts and evidence loss are in scope. A malicious router deliberately forging
another boot's pages, counters, closure or acknowledgement is not covered by this
design. The inventory detects missing/mismatched boots; it is not cryptographic
proof of which process issued a service-account write. A1 gives CLOSE_RESULT the
same provenance as its closure. Signing that row alone would not authenticate
all its underlying evidence. Existing certificate/binding signatures still authenticate their
original purposes; they do not identify the router process that observed a close
reply. They remain mandatory and unchanged. Signed per-boot origin is a stronger alternative
requiring its own reviewed protocol and consistent scope (Q-A2), not an A1 gate.

Rejected alternatives: unchanged gate plus fast drain before shadow violates
Joseph's order; fixed instance counts do not fix boot churn; traffic sent just
to fill every idle denominator fabricates no missing prerequisite and consumes
caps; deleting failures or allowlisting missing envelopes destroys the partition.

## 3. Change 1 — independent shadow health

**D-V2-3 — observation publisher, no money worker impersonation.** Add
`services/async_settle_shadow_health.py`, owned by router application lifespan.
All shadow-v2 opted router processes in the authority's **primary serving region**
are publisher contenders; all opted processes in all regions are readers.
Primary-region identity and maximum contender count K are explicit reviewed
config/proof, not inferred from a health row. No dedicated money drain is started.
Require at least one primary-region contender throughout collection. Empty opt-in,
policy disabled or admission enabled stops new claims/reads, joins bounded work,
invalidates caches and closes its evidence; no request starts a publisher.

Use proposed pin `TR_ASYNC_SETTLE_SHADOW_POLICY=shadow-v2` (default empty) and
`TR_ASYNC_SETTLE_SHADOW_HEALTH_REGION=us-central1` for this authority, reviewed
against actual placement. Pin equality plus nonempty workspace list selects the
contenders. Code loading with the policy empty is pure relative to v1. The
publisher has one dedicated execution slot per contender, no task queue, no
catch-up and no automatic retry. Always-allocated CPU and real timer progress
must be measured; a configured thread is insufficient. Current rollout requests
no CPU throttling, not proof it is serving (C13).

### 3.1 Record, cadence and indexed operations

New health kind `async_settle_shadow_health_v2`, key `local/fleet`; cadence kind
`async_settle_shadow_health_control_v2`, key `local/publish`. Neither uses
`settle_drain_control`. Exact health body, ≤4,096 UTF-8 bytes:

`v=2, policy_version="shadow-v2", authority="local", source="shadow_observation",
publisher_boot_id, publisher_region, router_revision, slot, observed_at_ns,
publisher_heartbeat_ns, backlog_age_at_seconds, complete, sample_count, backlog_count, frozen_micro,
p50_age_us, p95_age_us, oldest_unresolved_age_us, dead_count`.

All counts/slot and nanosecond/microsecond fields are nonnegative int64;
backlog_age_at_seconds is a finite nonnegative IEEE-754 binary64 JSON number
(exact round-trip representation, bool/string reject). Revisions full 40-hex, IDs bounded
as §7. A1 replaces the two P1 health `_us` freshness timestamps with exact `_ns`
Spanner timestamps. `observed_at_ns` is the read timestamp of the winning cycle's
strong unresolved snapshot, returned in result metadata even for an empty result.
`publisher_heartbeat_ns` equals that same timestamp: it labels the successfully
completed observation, not a later wall-clock completion or worker progress.
The trace separately records actual cycle duration. `backlog_age_at_seconds`
is the publisher's local wall sample used only to reproduce money's backlog-age
calculation (§3.2); it never enters freshness, slot or commit checks. Keep ordered
ages, dead ≤ backlog = sample_count, empty implies zero amount/ages. Missing,
malformed, incomplete or >10,000 observed backlog is unknown. Never omit leased,
dead or unknown-created-at rows.

**A1 AFTER (S1):** contenders attempt once per two-second slot with Spanner boundary
B=`slot*2,000,000,000` ns. A host timer proposes the slot; it is not time authority.
The successful observation timestamp must lie in `[B,B+250,000,000]`. Thus the
250 ms start allowance now includes claim acquisition through snapshot selection;
this is a tighter availability gate than round 1. Early/late or skewed host wakes
can miss a slot, never make an old snapshot current. Recheck before submission
and after waits, no catch-up or retry; account every scheduled opportunity. The
claim remains ≤1,024 bytes, with A1 fields
`v,policy_version,authority,slot,claim_at_ns,owner_boot_id`. The existing complete-key
claim read also returns server `CURRENT_TIMESTAMP()` S; **before mutating or
winning**, require the proposed slot=floor(S/2,000,000,000) and
B≤S≤B+250,000,000. Store exact S as claim_at_ns. A skewed host cannot reserve a
far-future slot, multiply winning slots or trigger extra unresolved scans. A
mismatched proposal is a failed attempted opportunity; do not retry with a new
slot in that invocation. One attempt/2 s monotonic/contender still applies.
Only genuine server-slot winners scan. Increasing slot and same-slot owner
transactions remain mandatory. Publish only after a won claim and one
strong unresolved read; increasing observation and slot fences prevent a delayed
winner overwriting new data. Claiming never leases money outbox work.

| Statement name (proposed except reused unresolved query) | Exact access / index / maximum rows |
|---|---|
| `shadow_health_claim_read` | `SELECT (SELECT body FROM tr_entities WHERE kind=@kind AND id=@id) AS body, CURRENT_TIMESTAMP() AS claim_at`; scalar subquery uses complete PK `(kind,id)`, ≤1 table row/one result row even when absent; same transaction checks server slot then inserts/updates only that key on winning slot |
| `unresolved_statement` (reused) | `SELECT unresolved_at, actual_cost_micro, status FROM tr_settle_outbox@{FORCE_INDEX=tr_settle_outbox_unresolved} WHERE unresolved_at IS NOT NULL ORDER BY unresolved_at LIMIT @limit`; `@limit=10001`; covering sparse index, ≤10,001 entries including sentinel; no base-body lookup |
| `shadow_health_publish_fence` | `SELECT body FROM tr_entities WHERE kind=@kind AND id=@id`; claim complete PK, ≤1 row |
| `shadow_health_previous` | `SELECT body FROM tr_entities WHERE kind=@kind AND id=@id`; health complete PK, ≤1 row; fenced `insert_or_update` only this health key |
| **A1 BEFORE → AFTER:** `read_shadow_health` | Previously body only; now `SELECT body, updated_at FROM tr_entities WHERE kind=@kind AND id=@id`, plus that same single-use strong result’s `metadata.transaction.read_timestamp`; same complete PK, ≤1 row, once/second/router, no extra statement/RPC. Return `{body, committed_at_ns, read_at_ns}`. No `CURRENT_TIMESTAMP()` is needed (§3.1.1). |
| `read_admission` (retained observer) | C1's `admission_statement`: workspace equality/status pending or dead on forced `tr_settle_outbox_workspace_status`, inner LIMIT 1001 before SUM/COUNT, and complete `(workspace_id,shard=0)` trust read, ≤1,001 pending entries +1 trust row; N workspaces/4 seconds/router |

C1 and C14 verify the existing statement/index definitions; existence in production
must be checked in the deployment artifact. No LIKE/regex/JSON predicates,
unbounded aggregate, entity-kind discovery or due-index substitution. All new
health operations are LOW priority, no retries, each RPC ≤500 ms; **whole winning
cycle from claim start through publication ≤500 ms**. It is a measured gate,
not a guarantee implied by RPC deadlines. Control transactions use the shared
remaining deadline, including SDK streaming restarts/commit; no SDK retry sleep.
**A1 BEFORE:** “A missed bound invalidates that publication for predictions and
consumes the availability budget.” **A1 AFTER (D-A1-A):** prediction validity is
§3.1.1 observation freshness, not proof of publisher acknowledgement. A missed
500 ms cycle bound consumes the publisher availability budget even when a landed
row remains fresh enough for prediction. This is an explicit weakening of that
sentence; Joseph accepted the Q-A1 default on 2026-10-12 (§13). It does not relax
observation completeness, money isolation, no-retry, load or certification
requirements beyond the explicitly stated timestamp/availability changes below.
The cycle ends at the original publisher's observed successful commit reply,
not at server commit time. Acquisition, session creation/pool wait, iteration,
cleanup, locks and serialization all consume one monotonic deadline. No shared
money-client wrapper may be changed to implement this dormant feature.
This primary-region design still requires measurement.

### 3.1.1 A1 ADD — G-A, freshness and availability are different predicates

**Gap:** two histories with identical HEALTH body and commit timestamp can have
an on-time reply, a reply at .8 s, or no reply. Neither a timestamp envelope nor
counters consulted elsewhere can tell this reader which happened (A1-C1–C3).
A1 deliberately selects option (2), not a purported proof of option (1).

The complete P1-A1 predicate accepts validated HEALTH plus the exact metadata
pair `{committed_at_ns=C, read_at_ns=R}`, and monotonic `read_elapsed_ns=q` and
`since_reply_ns=e`. All are exact nonnegative int64, rejecting bool, float,
overflow, missing values and lossy conversion. Obtain timestamps from protobuf
seconds/nanos or lossless SDK nanosecond values; never `datetime.timestamp()`
floats or microsecond truncation. P1's old `health_usable` alone is insufficient.
Only the shadow HEALTH writer uses native `COMMIT_TIMESTAMP` for `updated_at`;
all fence reads precede mutation. DDL already permits this (A1-C11/A2-C1).

**Timestamp meanings and feasibility.** O=`observed_at_ns` names the exact strong
snapshot of the unresolved index, not query start, receipt or publication commit.
C is the HEALTH write's serialization timestamp, not its acknowledgement. R is
the reader's fresh, single-use strong snapshot timestamp, paired with that row
version. The installed client requests returned read timestamps by default and
exposes result metadata (A2-C2); retain the stream object through full consumption,
including zero rows, then read its metadata. No private SDK attribute is required.
`CURRENT_TIMESTAMP()` is stable within a statement, but documents statement time,
not equality to that statement's snapshot timestamp. In a reused/stale snapshot
it can accompany old data. Even in a strong statement it is not a reply timestamp.
A1 chooses returned read metadata for O/R rather than assuming those values
coincide. The claim uses statement-current S solely to fence a proposed slot
before winning/scanning, never as the unresolved snapshot timestamp.
[Timestamp functions](https://docs.cloud.google.com/spanner/docs/reference/standard-sql/timestamp_functions),
[read timestamps](https://docs.cloud.google.com/spanner/docs/reference/rest/v1/Transaction),
and [strong reads](https://docs.cloud.google.com/spanner/docs/reads) establish these
separate contracts. No statement-start/current-time scalar is substituted for O.

Capture m0 before snapshot/session acquisition, m1 after complete iteration,
conversion and cleanup; q=m1−m0. At every prediction take m2 under the cache lock,
e=m2−m1. Charge **the whole read interval**, not merely time since reply: the
server selected R before delivery completed. Require q≤250,000,000 ns and
q+e≤1,500,000,000 ns; discard delayed results, negative intervals, timer regression,
process resume with an unaccounted pause, or expired cache. Every RPC and stage
uses the same remaining deadline; no fresh budget after checkout/restart. The
shadow-health read/install deadline is tightened from 500 to **250 ms**; the money
reader, workspace observer and publisher cycle remain unchanged. Poll every
1 s on monotonic time, with at most 250 ms scheduling delay; a delayed poll does
not extend cache lifetime. Use an elapsed clock that includes descheduling and
platform pauses (Linux CLOCK_BOOTTIME for suspend-aware elapsed timing); no
wall-time subtraction or cross-boot monotonic values. This relies on functioning
platform elapsed timers, as existing deadline enforcement does, not a measured
absolute UTC offset or a new clock-qualification service. Short timer/rate and
pause behavior belong in G6/G7's actual adapter measurements; unexplained timer
behavior is a failure, never free time.

Require P1 shape/completeness/count/order conditions, plus
`B <= O = publisher_heartbeat_ns <= B+250,000,000`,
`O <= C <= B+750,000,000`, `C <= R`, and
**`A = (R−O) + q + e < 5,000,000,000`**.
The publisher also requires claim_at_ns≤O from its won claim. The snapshot O
must be from this won cycle; reuse/relabel is forbidden and the
trace binds its digest. This is freshness in Spanner's timestamp domain with a
conservative elapsed delivery/cache charge. Spanner handles TrueTime uncertainty
when serving strong reads and committing; A1 invents no fixed TrueTime epsilon,
no zero-uncertainty assertion, and no Cloud Run UTC accuracy certificate.
Its external-consistency/strong-current-read contract, not synchronized host
clocks, is the time authority. Backend uncertainty/wait consumes the RPC/cycle
budget. This is not a new physical-age theorem about timestamps originally
written by money hosts or about instantaneous backlog (§3.2).
[Spanner time semantics](https://docs.cloud.google.com/spanner/docs/true-time-external-consistency)
and [read/write execution](https://docs.cloud.google.com/spanner/docs/whitepapers/life-of-reads-and-writes)
explain that boundary. The commit envelope is only a sanity check, never Dp proof.

Reader state is EMPTY → INSTALLED(body,C,R,m0,m1) or UNUSABLE. Each successful
poll atomically replaces the complete tuple, including a malformed/incomplete
new row; invalid newer data invalidates old cache, with no fallback. A failed
read makes the source unavailable under retained observer rules. Re-reading the
same row cannot rejuvenate O: a later R increases its age. Same-slot no-overwrite,
same-slot claim owner, increasing slot AND observation, and body/version pairing
are independent mandatory fences. Future/inverted metadata yields unknown.

This explicitly weakens §3.1's old cycle-validity wording: start 0/observation .10/
commit .501 can be fresh while failing the 500 ms cycle; commit .40/reply .80 or
lost has the same validity but failed/unknown publisher accounting. Start .25/
commit .90 fails the .75 envelope (and usually observation jitter). A current-slot
commit with a four-second-old O rejects. A host clock +250 ms cannot turn actual
Spanner age 5.10 into 4.85 because neither freshness timestamp comes from that
host. Delayed read delivery is covered by q. Availability failures remain visible
and can block G6/G7 even when the observation supports a yes/no prediction.

**Alternative, not adopted: completion-evidence protocol (R3).** DD1's envelope
cannot distinguish lost replies; a prefrozen success field asserts a future fact.
Next-slot confirmation adds up to 2 s to the new conservative 4.50 s chain.
Immediate post-ack confirmation is not shown infeasible. With the adopted
component maxima its additional visibility delay H must satisfy **4.50 s + H <
5 s**; alternatively the original publish and confirmation phases must share a
tightened .5 s allowance. A separate eligibility-row design would add up to .5
confirmation transactions and mutations/s fleet-wide, up to I returned point
rows/s at I readers, associated read/write bytes and native RPC overhead, and a
new missing-proof availability failure surface. A same-snapshot two-key read
could avoid a second poll RPC, but requires a reviewed reader/fencing protocol:
boot/slot/body binding, no eligibility before observed success, immutable same-slot
proof, cache invalidation and missing-proof rejection. Q-A1's freshness-only
default was accepted on 2026-10-12 (§13); this stronger alternative is not an
activation dependency. A1's narrower claim does not assert that immediate
completion evidence has been proved impossible.

**Durable prediction inputs retained:** `basis_sha256` resolves to exact canonical
HEALTH_BASIS ≤6,144 bytes in the existing bounded proof attachments:
`{v,policy_version,contract_revision,reader_boot,source_state,body,
committed_at_ns,read_at_ns,read_elapsed_ns,since_reply_ns}`.
source_state=`installed|unusable|empty|read_failed`; body/C/R are nullable only
for absent/failed source, elapsed values are u. Installed requires all metadata
and the composed predicate. No wall timestamp, uncertainty assertion or clock
digest is an input. P5 replays it and checks the trace/body digest. One object per
eligible prediction remains bounded by capture limits, carries only a digest in
the ≤2,048-byte receipt, and may never be lost silently. Batch ≤10 bases plus
≤4,096 wrapper bytes in ≤65,536 bytes, schema
`{v,policy_version,contract_revision,boot,day,sequence,previous_sha256,bases}`,
ordered by digest; complete key `health-basis-batches/<day>/<boot>/<sequence>`.
Retention 30 days; health_basis_root covers all receipt references. P3/P5 own the
ordinary export path and its full operation/churn budget (§7.2); no independent
origin service or per-prediction RPC is introduced. Queue ≤4 batches/256 KiB,
including the open batch; 2/s sustained plus burst ten fits nominal batching,
not arbitrary export stalls. Overflow blocks. CPU/serialized/native copies count.

**Emulator boundary:** the same native API, existing commit-enabled DDL and
strong snapshots are available in the tree's emulator harness (A2-C3). Its shim
drops staleness options, so a stale-read test must not claim to exercise them
through that shim. No exact-image A1 execution is claimed: P3 must verify empty
and nonempty returned read metadata, nanosecond conversion, sentinel roundtrip,
version pairing and the changed complete-key scalar claim query (including
absent row and far-future host proposal) against the CI-pinned image.
Emulator timing is not TrueTime or regional latency evidence. Upstream explicitly
lists commit timestamps and timestamped reads as supported, but ignores gRPC
deadlines/cancellation; use controlled transport faults and production-compatible
elapsed enforcement to prove deadlines, never an emulator timeout claim.
[Emulator support and limits](https://docs.cloud.google.com/spanner/docs/emulator),
[upstream feature matrix](https://raw.githubusercontent.com/GoogleCloudPlatform/cloud-spanner-emulator/master/README.md).

### 3.1.2 A1 ADD — publisher conservation and durable traces

For each contender/day, all nonnegative int64, extend the ≤2,048-byte publisher
counter object with `scheduled_slots, lost_claims, unknown_outcomes, late_cycles`.
Retain `claim_attempts, claims, scans, publications, failures, missed_slots` and
histograms. Final counters obey:

`scheduled_slots = missed_slots + claim_attempts`

`claim_attempts = lost_claims + publications + failures`

`unknown_outcomes <= failures; late_cycles <= failures; scans <= claims <= claim_attempts`.

Unknown and late are overlapping diagnostic subsets, never additional outcomes.
`publications` means original successful reply within 500 ms; a landed late row
is not counted here. `failures` includes every attempted claim's error/unknown,
won-cycle failure, deadline miss and publication fence rejection. `lost_claims`
is only a definitive healthy contention loss, not a failed/unknown claim.
`missed_slots` means no claim attempted (busy, unscheduled, late start, disabled
before attempt). Register the owning day before executor submission and hold it
through final accounting; midnight cannot close ahead of queued jobs. Recheck
local scheduling after lock waits; validate server observation-slot alignment after the read. Intermediate
counters may have outstanding slots; closure requires none. The scheduled set
comes from independently rostered contender lifetimes/slots, not successful jobs.

Define `publisher_slot_trace` entries (≤1,024 canonical bytes each): exact
`v=2, policy_version="shadow-v2", contract_revision="a1", day, boot, slot,
claim_start_us, claim_at_ns, observed_at_ns, publisher_heartbeat_ns, body_sha256,
ack_at_us, cycle_us, outcome, unknown_outcome, late_cycle, rpc_count,
rpc_elapsed_us`. Times/duration/count are int64;
claim_at_ns/observed_at_ns/publisher_heartbeat_ns/body_sha256/ack_at_us/claim_start_us/cycle_us
are nullable only when that stage was never reached; unknown_outcome/late_cycle
are exact booleans; digests are 64-hex, boot/day as §7; outcome is
`missed|lost_claim|published|failed`. `ack_at_us` records an observed reply only;
unknown/late do not get invented success times. Each missed slot has an entry.
Wall claim_start_us/ack_at_us are diagnostics only; cycle_us is monotonic.
Trace totals SHALL equal the counters, including failures and unknowns; a missing
trace blocks G6/G7, never reconstruct it from histograms. Export hash-addressed
pages ≤64 KiB/≤60 entries with exact
`{v,policy_version,contract_revision,day,boot,first_slot,last_slot,
previous_sha256,entries}`; slot bounds=u, previous=null only for the first page,
entries ordered/contiguous and matching boot/day/bounds, with independent lifetime
coverage; reserve ≤4 KiB for wrapper. Maximum at K
contenders is K/2 entries/s and 512K entry-payload bytes/s. Export full or partial
immutable pages with one PUT-or-reconciliation-GET token/5 s/boot: ≤K/5 operations/s, ≤65,536K/5 peak transport
bytes/s, with actual wrapper overhead ≤4,096K/5 bytes/s in addition to entry
payload. This steady-state rate includes day/shutdown partial pages; §7.2 adds finite-interval
startup/in-flight terms, all offline operations and cleanup. No extra final burst.
Use §7.2 ordinary export accounting, no extra health/database query or mutation.
Queue ≤4 pages/256 KiB per exporter, no catch-up burst, overflow blocks coverage.
Trace hash/copy/serialization CPU is measured in publisher/export budgets.

P5 reconstructs outcome partitions and exact snapshot/slot coverage. G6 applies the
retained failure/streak/degraded limits to publisher opportunities as well as
observers: failed+missed ≤max(3,1% of scheduled slots), consecutive affected slots
≤2, degraded time ≤1%; healthy lost claims are not failures. Additionally evaluate
fleet winning-slot absence so contenders' losing claims cannot hide no publisher.
G7 requires each configured component bound and includes every failed/unknown
attempt in measured load; it cannot call an over-budget execution compliant.
A failed opportunity may remain within G6's allowance; missing evidence never does.
**End A1 additions to §3.1.**

### 3.2 Load and meaning

For K simultaneous contenders, steady upper bounds **in addition to** all
observer/evidence load: K/2 claim reads/s; 0.5 unresolved queries/s;
1 publication read/s in aggregate (half a fence/previous-read pair/s);
≤0.5 claim-row mutations/s and ≤0.5 health-row mutations/s. Thus `(K/2 + 1.5)` SELECTs/s,
5,000.5 unresolved index entries/s, and `(K/2 + 1)` control-row reads/s.
Budget transaction begin/commit/rollback separately: ≤K/2 claim transactions/s
(including losers) and ≤0.5 publication transactions/s. No “one transaction =
one RPC” assumption. Burst budget includes K contenders at a slot boundary and
rollout overlap. The observer remains I×(N/4+1) reads/s; publisher traffic is
charged to its actual primary region **and** the single authority database,
not once per reader region or omitted because it is not a request RPC.

CPU budget: retain ≤1 ms p95/≤2 ms p99 authorize CPU and ≤2 ms p99 wall;
≤100 μs p99 capture and ≤5 ms p99 comparison CPU. Propose publisher summary CPU
≤5 ms p99 at 10,001 entries, with measured startup/canonicalization costs.
Fleet CPU approval must include publisher contenders, retries avoided, certificate
verification/writes, receipt pages, closure and surge. Require measured baseline
peak + reviewed incremental allowance **strictly <45%**; an unmeasured allowance
blocks. No guessed CPU slope or assumption that the old 7,208-row allowance can
absorb an extra 5,000.5 visits/s. Record separate pending/unresolved/control/trust
row budgets and total RPC/write budgets in pre/post artifacts (§8 G7).

**A1 round-2 freshness derivation:** use B/O/C/R in one Spanner domain.
For compliant successive slots, old O≥B; the next observation is at most
B+P+Jp=B+2+.25. Its complete cycle/reply takes at most Dp=.5 s, charged
conservatively from that observation although the cycle started earlier. The
next reader poll starts within T+Jr=1+.25 s and returns/installs within Dr=.25 s.
Thus at replacement the old sample's domain age is bounded by
`P+Jp+Dp+T+Jr+Dr=2+.25+.5+1+.25+.25=4.25 s`.
To avoid assuming R was selected at reply, the predicate additionally charges the
whole read interval q≤Dr. Its conservative acceptance quantity is therefore
**`4.25+.25=4.50 <5 s`**. This double charging is intentional. With the former
Dr=.5 ceiling the same conservative chain would be 5.00, insufficient for strict
<5; hence the explicit 250 ms shadow-only read deadline, not a hidden assumption
of instantaneous replies. Pauses, late polls, TrueTime wait and slot misses spend
availability; the actual predicate still rejects A≥5 independently of continuity.
No unused jitter can turn an over-budget publisher cycle into a successful one.
Workspace chain stays 4+.25+.5≤4.75. Regional measurement must establish every
component and the conservative turnover bound; the arithmetic is not a latency
measurement or continuous-availability guarantee.

The health SELECT adds one 8-byte logical commit timestamp per poll (8I bytes/s)
and consumes already-returned read metadata (another 8I logical bytes/s handled,
not newly added wire bytes). The claim SELECT adds a server-current timestamp,
≤K/2 scalars/s or 4K logical bytes/s. Native encoding is measured. There is no additional SQL statement, RPC,
table-row visit, stored row, mutation or index. The claim statement returns
exactly one result row even when its key is absent, versus zero in that case
previously; its worst-case result-row rate remains K/2 per second. Account for
the NULL body, added timestamp column and result metadata in native bytes. The
health query still returns at most one row and adds only the commit-timestamp
column; its read timestamp was already requested by the SDK. The publisher
consumes its existing unresolved result's metadata, including empty results. Body maxima stay 4 KiB/claim 1 KiB. Conversion/predicate/copy work must
fit existing capture/authorize CPU budgets, with trace/basis costs in §7.2.

**Money backlog ages are a different input.** The installed money publisher takes
`observed=time.time()` before reading, subtracts stored `unresolved_at` timestamps,
sorts ages and computes ceiling-rank p50/p95; it records a later local heartbeat
(`src/trusted_router/storage_gcp_async_admission.py:189–221`). Admission tests
cached freshness and p95≤5 (`services/async_settle.py:98–105,166–175`). A1 keeps
that sampled wall-time backlog-age calculation and completeness rules in its
shadow summarizer; `backlog_age_at_seconds` records that separate wall input. Use the
same binary64 wall sample and age subtraction as the baseline calculation,
recorded without precision loss in backlog_age_at_seconds; round derived
nonnegative age seconds up to integer microseconds using exact rational
conversion of the binary64 result (not a float multiply that could round down); never round a value >5 s down to pass the threshold.
Tests must compare exact yes/no threshold results to the unchanged money
summarizer, including future/epoch/invalid creation timestamps. O/R freshness
never substitutes Spanner time into money's age subtraction. A host offset can
still change nominal backlog ages near the p95 threshold; this predicts what
admission would compute on those samples, not exact physical-age parity across
hosts. Requiring that stronger claim would need a separately reviewed money-age
policy and uncertainty handling; no such change is authorized here. Missing
progress is unknown; a fresh complete backlog above p95=5 s gives known no;
empty backlog is an actual observation, never fabricated worker health.

Prediction means “the tier/cap/backlog-age predicate would say yes/no on these
fresh observations **if** a proven fast-drain service were installed.” Label
`prediction_basis=counterfactual_backlog_v2`; it proves neither worker liveness,
future throughput nor permission to admit. The actual money `AdmissionCache`
must continue reading only `settle_drain_control/fleet-v1` and reject shadow
kind/source/schema even if supplied by mistake. Its existing strict schema and
reader separation support this boundary (C1, C3). Do not rename publisher
heartbeat to worker_heartbeat or convert records between schemas.

At later pilot activation re-prove real worker heartbeat and empty/nonempty
backlog semantics, health-source isolation, scheduling, freshness, stale/unhealthy
admission closure, regional capacity/burst/lease/crash/repair SLOs and **combined**
load during health-source handover. A seven-day counterfactual yes is not that
proof. Rejected: writing a fake fleet-v1 heartbeat, enabling fast drain early,
or running the fleet scan on every request.

## 4. Change 2 — authenticated cohort exclusion

**D-V2-4 — authorize facts, terminal proof.** Add an optional authorize response
field `billing_shadow_exclusion`, mutually exclusive with
`billing_shadow_binding`. Sign only after successful authorization, from already
available server facts. Never call the money ticket signer API, add a money
transaction field or synchronous evidence RPC. Missing facts mean unresolved,
not ordinary defaults. This fixes the current projection collapse and cap ordering
(C4–C5), without changing existing snapshot metadata for nonmembers.

**A2 round 2 — first-window scope:** §§4.1/4.3 continue to specify the full
exclusion path, but live certificate issuance/verification-worker integration is
P4b and **DEFERRED**. P4a issues only an eligible v1 binding/snapshot under the
existing v2 outer policy. A request that is not demonstrably eligible receives
**no certificate and no eligible binding**; its decision/receipt remains durable
and unresolved, each terminal attempt is C and blocks its UTC day (and any issue
day required by G3). Do not omit it from T, invent B, suppress E, or treat missing
issuance as an exclusion. Every attempted terminal still has one receipt.

The dedicated §13 cohort must yield T=A and B=C=0 for its certified window. B
from certificates is expected to be zero; no issuance is required by any of
G1–G11 when B=0. G3 still reconciles every authorize, G4 proves the complete
partition, G5 derives E independently, and all other gates run unchanged.
Existing observed-binding exclusion semantics remain frozen for the general
contract; an unexpected valid observed B in this eligible-only cohort is a
cohort-policy defect (G1 evidence_schema_gap), not a seed or permission to certify
this cohort. It cannot rescue an ineligible authorize without a valid binding.
P4a must reject/unresolve unexpected certificate traffic as C; the deferred live
worker is never replaced by claims-only acceptance. P5 retains the existing
certificate verifier port/pin, with zero calls when there are no applicable proofs.

Owner reports P2 already merged. Its inspected retention code branches on an
actually present certificate; absent one, a binding/snapshot follows the compared
path (`P2/enclave-go/internal/trustedrouter/async_settle_shadow_v2.go:162–193`).
It echoes an exclusion only from retained exclusion state (`:217–238`). Thus
P2 needs **no behavior change** for P4a or later P4b; the single combined fixture
pin-only follow-up remains. Merged status is owner-supplied, not a Git/deployment
verification by this author.

### 4.1 Certificate and exact qualifying reasons

Ed25519 compact canonical JWS: `typ=tr-async-settle-shadow-exclusion-v2`, purpose
`async-settle-shadow-exclusion`, audience `router-shadow-exclusion`, header
exactly alg/kid/typ. Reuse loaded key material only through a distinct trusted
purpose descriptor. Certificate, shadow-binding validator, fresh settlement-ticket
validator, expired-ticket lookup validator and Go ticket binder must each reject
both foreign types/purposes/audiences. Existing ticket/binding types are distinct
(C10, C15); this adds a third, not an alias.

Exact claims: `v=2, policy_version, iss, aud, iat, exp, authorization_id,
generation_id, workspace_id, key_id, invocation_nonce, reservation_id,
billing_authority, journal_region, epoch, settle_origin, requested_route,
requested_streamed, reason, facts_sha256, candidate_set_sha256,
router_revision, original_receipt, async_eligible=false`.
Identities must match server-owned authorization; deterministic generation;
positive epoch; no invented reservation/nonce. `settle_origin` and authority are
bounded enums of actual facts, not forced typed/local for nonlocal exclusions.
Unsupported identities that cannot be represented remain unresolved.
`exp-iat=172800`, inclusive issuance/exclusive expiry checked at terminal receipt,
no renewal on replay. Certificate ≤2,048 ASCII bytes; claims strings use native
64-byte identity limits, generation/region/issuer/audience ≤128; hashes 64 lowercase
hex. Combined maximum must fit, not each field independently assumed to fit.
Oversize is a blocking diagnostic. Retain verification keys through 48 hours plus
in-flight verification; fixtures describe keys, documents contain no signed tokens.

`original_receipt` is mandatory for **every** exclusion reason, with exactly
`{day,boot,first_seq,event_seq,event_sha256}`. `day` is the issuer's UTC observation
date, exactly `YYYY-MM-DD` (valid calendar date, equal to the authorize event's
date); `boot` is the actual issuing boot ID, 1–64 ASCII `[A-Za-z0-9_-]` characters,
never the revision or terminal boot. Both sequences are canonical zero-padded
20-digit decimal strings representing integers 1 through 2^64−1, with
`first_seq <= event_seq`; `event_sha256` is 64 lowercase hex. No optional fields,
unknown keys, aliases, escapes, path separators in boot, or coercion. The kind is
implicitly and exclusively `async_settle_shadow_receipts_v2`; the complete id is
`day + "/" + boot + "/" + first_seq` (≤96 ASCII bytes). The signed claim supplies
the entire locator; neither a certificate row, money field, cache nor catalog is
needed to discover it. A verifier must not search other pages if it fails.

Before signing, reserve the event sequence and stable page key using §7's bounded
append protocol, prepare the complete authorize event, and hash its canonical
bytes into `event_sha256`. The event includes `receipt_locator` with exactly the
four coordinate fields above (without `event_sha256`). Its decision reference
contains reason and facts/candidate digests, **not** a certificate digest, signed
locator, self-digest or forward reference to the later verified certificate row.
This avoids a hash/signature cycle; the terminal/completion event later records
the certificate SHA and full signed locator. Include the coordinate member in
the complete 2,048-byte authorize-event fit check and the full locator in all
certificate/terminal-receipt checks. After signing, finalize those exact hashed
bytes in the reserved slot before exposing the certificate. Durability may follow issuance;
a signature is not a flush acknowledgement. Reservation/signing/size failure
emits no certificate and leaves unresolved evidence, never an exclusion default.

`facts_sha256` commits to canonical content-free requested facts (typed origin,
usage class, authority, route/stream, fixed feature booleans/fees/tier sentinel,
commercial program and reason). For endpoint-dependent exclusions,
`candidate_set_sha256` commits to the complete ordered candidate identity/adapter/
feature projection. The 128-candidate/65,536-canonical-byte input ceiling is not
permission to issue a certificate: **the entire serialized authorization receipt
event**, including all metadata, requested facts and that complete ordered
projection, must fit the §7 **2,048-byte** event limit before issuance. No truncation,
digest-only substitution, chunking or external candidate-proof object is allowed
in v2. For an immutable request-level condition that excludes every fallback
regardless of endpoint, this field is explicitly null: no candidate hashing or pricing-snapshot
retention is necessary. Null is forbidden for adapter-only/candidate-dependent
reasons. Over-bound candidate-dependent proof remains unresolved: emit no exclusion
certificate; retain a bounded unresolved authorization receipt with diagnostic
`candidate_receipt_size`, measured required bytes and candidate count, and classify
its terminal as C (blocking), never B. This diagnostic is not factual exclusion
backing. Do not hash prompts, credentials or arbitrary model parameters. Store
bounded canonical decision facts, including the full ordered candidate projection
when required, in the authorization receipt ledger (§7), outside the money
row/transaction. This stricter fit requirement uses the existing receipt storage,
reads, retention and closure contract rather than adding a new proof storage path.
Expected overflow frequency is unknown and must be measured before P6 by cohort,
reason and candidate count using complete serialized receipt sizes; include its
blocking-day impact in §12, never assume large projections are rare.
Router terminal validation independently recomputes any available server facts
and checks that ledger commitment for facts not retained by the money authorization.
A signature proves issuance, not truth of missing inputs; absence of that durable
backing blocks closure. The certificate's own facts/candidate digests alone are
never durable factual backing. No new terminal-path synchronous lookup is allowed.

Only this closed allowlist qualifies, with the accompanying proof condition:

| Reasons | Sufficient immutable requested fact |
|---|---|
| `untyped`, `non_credits`, `settlement_authority` | Server origin, usage class or authority fixed for the entire authorization, with identities still provable |
| `unsupported_route` | Actual requested logical route outside chat.completions/responses, cannot change during fallback |
| `service_tier` | Explicit requested nondefault tier excluded by the policy, fixed across candidates; absent/default does not qualify |
| `app_markup`, `custom_markup`, `receipt_fee`, `custom_model`, `user_model`, `native_batch`, `fusion`, `polyphemus`, `liberty` | Explicit server-owned immutable program/feature; receipt request qualifies even with zero fee; never infer from failure |
| `tool_cost`, `search_cost`, `image_cost`, `video_cost`, `request_fee`, `private_tier_basis` | Positive requested feature/fee/pricing rule excludes **all** possible terminal candidates; if only endpoint-dependent, require complete candidate-set proof |
| `partner`, `unsupported_adapter` | Immutable commercial program excluding the request regardless of adapter, **or** every allowed fallback candidate proven outside supported adapters; initially selected `other` or a model-prefix heuristic alone is insufficient |
| `unknown_parameters` | Positively identified requested feature outside the frozen ordinary-parameter allowlist, with a bounded feature-enum commitment and policy saying it excludes all candidates; inability to decode/discover parameters is missing context, not this reason |

Never qualifying: `snapshot_unavailable`, `missing_envelope`, `snapshot_size`,
missing signer/context, transport failure, local-work overflow, usage missing/
estimated/malformed, arithmetic overflow, failed Go evaluator, unknown booking,
ambiguous fallback, replay or header_absent **by themselves**. These are failures
or provenance states, not immutable out-of-cohort facts. Unknown features with no
bounded authenticated representation remain unresolved. Do not widen
`COHORT_EXCLUSIONS` with those three diagnostic failures: each can hide an
otherwise eligible request and cannot discharge its terminal proof obligation.

If fallback can reach an eligible endpoint, do not issue an adapter-only
certificate. Use an eligible shadow binding with the complete supported candidate
context, then compare the actual endpoint; if that complete projection cannot be
built under the frozen cohort, the attempt remains unresolved. No candidate
trimming or declaring mixed fallback ineligible to pass. A certificate whose
immutable request-level condition still applies can cover a supported adapter
(e.g. requested receipt), but actual dimensions must agree and remain out of
cohort under that same condition. A contradictory eligible terminal produces
`exclusion_conflict` and a correctness reset, never B.

The existing authenticated **observed** exclusion path (e.g. provider reports
unsupported tier) remains valid under its full binding/S0/raw-body verification
(C6–C7). It becomes B, with `proof_kind=observed_binding`; no new certificate is
minted after the fact. Missing exact usage on an otherwise eligible refund stays
C; a proven requested exclusion may legitimately cover a usage-less refund.

### 4.2 Wire, capture ordering and enclave seams

New terminal variant in the **same one** `X-TR-Settlement-Shadow` header:
`{v:2, policy_version:"shadow-v2", variant:"excluded", certificate,
attempt:{authorization_id,terminal_kind,selected_endpoint,adapter,route_type,
streamed}, go_revision}`. Endpoint/adapter may be null only when no endpoint was
invoked (e.g. pre-invoke refund); signed requested route/stream remain explicit.
No prices, snapshot, usage, charge or async status. Exactly these keys; ordinary
strict JSON/base64 bounds apply. Maximum decoded exclusion variant 4,096 bytes,
encoded ≤5,462 bytes, certificate ≤2,048; both remain below existing 8,192-decoded/
12,288-header caps. `original_receipt` travels only inside the opaque certificate,
not as an unsigned outer locator; retention and retries echo it byte-for-byte.
No locator is synthesized by the enclave. Locator-inclusive arithmetic is in §7;
all complete-object limits still apply, and oversize leaves the terminal C/BLOCKED
without changing the legacy request. Eligible variant uses
`v:2,policy_version,variant:"compared"` plus existing v1 comparison fields; binding type/lifetime and billing DTO stay
v1, while the outer policy/version pins its evidence interpretation. Re-measure
full/hash-only fit with new outer fields; never exceed existing 6,144 inline,
65,536 local or 128-candidate bounds. Existing limits are C11/C16.

At terminal capture: count T/unknown immediately; collect only a bounded header
and compact authorization/terminal identities under the shared nonblocking
rate/queue guard. Dispatch by a **nontrusted** bounded variant discriminator.
Excluded variant does not copy/serialize a pricing snapshot or candidate list;
it retains only already-available immutable references/compact commitments.
Worker strictly parses and verifies before moving C→B. Invalid exclusion never
falls through as a clean size exemption. The compared-path local cap remains
mandatory and sticky. Thus large irrelevant partner pricing snapshots do not
trigger today's premature `settle/snapshot_size`, but size cannot exclude eligible
traffic. Certificate processing shares 2/s burst 10, 32 tasks/256 KiB limits and
daily terminal permits; no unlimited cheap-verification lane.

Enclave implementation seams, verified in this checkout:

- `E/enclave-go/internal/trustedrouter/client.go:482` authorize metadata;
  `:840`/`:858`/`:871` authorize mode/retention; `:1081` Settle;
  `:1227` refundDetailed; `:1378` HTTP header send (C16).
- `E/enclave-go/internal/trustedrouter/async_settle_shadow.go:185` retention,
  `:278` builder, `:358` encoder, `:372` request-local header/retry path (C11).
- `E/enclave-go/internal/trustedrouter/async_settlement.go:79`/`:83` async
  provenance/binding guards (C17): exclusion must never bind `Authorization.async`.

**A1 round-2 wire boundary:** no owner header, A1 capability header, response
locator, acceptance, ownership export or new enclave behavior is introduced.
Baseline P2 read/echo remains required; its follow-up copies/pins every file in
the new A1 manifest, including router-only schemas (§7.1). Authorize/certificate/
terminal wire bytes and original receipt hashes are unchanged from baseline P1.

P2 must extend authorize retention **before snapshot parsing**, and header echo
for settle and refund, including supported transport for out-of-cohort routes
whose authorizes currently do not request shadow (C16/C17). Preserve opaque token
bytes; bounded local claims parsing is not cryptographic acceptance. Router remains
the verifier. Keep exact legacy JSON body, pinned authority, existing retry policy,
no terminal mode header, no shadow-dependent error returned. Tests must drive
real chat/Responses streams and unsupported-route/refund send seams, not only a
builder with prefilled facts.

### 4.3 Durable verification, duplicates and rollout

Router checks certificate signature/type/purpose/issuer/audience/lifetime,
all authorization identities/epoch/authority, commitment to durable authorize
facts, actual endpoint membership when the reason depends on candidates (otherwise
independently validate any reported endpoint against server-owned selection), phase from actual server entry and actual
adapter/route/stream. Preserve unknown adapter/stream explicitly; do not replace
refund placeholders with asserted actual facts. Current comparator already treats
refund stream and absent route specially (C6); v2 uses signed requested facts plus
known actual facts, and requires all known actual facts agree. If the absent fact
is needed to prove exclusion (e.g. endpoint-dependent reason), absence blocks.
Record booking outcome and confirmed server winner; unknown/deferred booking
remains C until independently resolved, never “refund means zero.”

For every exclusion certificate, first authenticate and validate its bounded
`original_receipt` claim. The asynchronous worker constructs the complete §7
`(kind,id)` solely from that claim, then performs **one complete-primary-key page
read**, ≤1 row/≤65,536 body bytes, under the existing 1-second point-read ceiling.
There is no preliminary certificate-row/locator-index read, page scan, fallback
key, or request-path lookup. A fresh boot with no verified certificate row follows
the same path. Any later reconciliation pass repeats only that same bounded read
under the existing worker/cadence budgets; a miss while commit may still land is
unresolved, never proof that no authorization occurred.

Strictly validate page policy/schema/size, returned key and body boot/day/first
sequence against the signed coordinates, ordered unique contiguous event
sequences and first/last bounds, and select exactly `event_seq`. Require an
authorize event, its coordinate member equal to the signed coordinates, its date
and authorization ID equal to the signed day/authorization ID, and recomputed
whole-event SHA-256 equal to signed `event_sha256`. Match all authorization
identity fields (or their specified content-free commitments), authorizing
revision, requested dimensions, decision reason and facts/candidate digests to
the claims and server-owned authorization. Recompute those digests from the
complete stored canonical facts/projection; for candidate-dependent reasons,
verify ordering, actual endpoint membership and adapter/features from that
backing, including after catalog changes. Matching the event hash alone is not
factual verification. Page-chain/HWM and cross-writer closure reconciliation
remain required under §6; this one-page lookup does not itself certify a chain.

Malformed/over-bound coordinates give `original_receipt_locator_gap`; a wrong
boot/page/event or identity/sequence/key/body mismatch gives
`original_receipt_identity_gap`; an event/facts/candidate digest mismatch gives
`original_receipt_digest_gap`; an absent page gives `original_receipt_missing`;
oversized/schema-invalid backing gives `original_receipt_backing_gap`. Each
leaves C and blocks closure (and preserves any required conflict/reset), never B.
For A2 reporting these five original_receipt_* worker diagnostic subcodes are
failures of `exclusion_proof_gap` on the affected terminal object; they are not
additional GAP_NAME literals. The exact source diagnostic remains in raw evidence.
The worker must not rebuild facts from the live catalog or accept digest-only
backing. Missing durability after issuer death stays C/BLOCKED. Persist the exact
signed locator in the verified certificate projection and terminal receipt;
§6 closure reconciliation must resolve it and the complete facts under §7
retention/read bounds. The verified projection is a result of this flow, never
its locator prerequisite. The original authorize event is not rewritten.

Persist a compact verified certificate projection/hash (§7), retain the original
authorization receipt, and persist one receipt for **every terminal attempt**,
referencing either that certificate or a compared sample. Do not persist raw JWS,
body, prompts, keys or outputs. Exact retry retains the original logical sample/certificate; each new
HTTP attempt has its own writer sequence receipt and fresh prediction if eligible.
Same proof hash, authority, phase, identities, dimensions and valid original
lifetime must match; writer/serving region may change. Conflicting same-kind
payload/facts/hash resets; opposite settle/refund winner polarity stays a coverage
gap with original winner preserved. No latest-write-wins. Never let an aggregate
authorize exclusion substitute for terminal evidence.

Replay authorize gets a replay receipt linking the original decision; it must
not re-freeze prices, renew expiry or mint a new exclusion based on current catalog.
An enclave retaining the original unexpired proof can echo it. If the original
proof cannot be recovered byte-identically by the existing response mechanism,
legacy continues but its terminal is C. A header_absent authorize is counted and
cannot certify itself as an exclusion: a terminal must supply its original valid
proof, or independently have the new fresh-authorize certificate (issuance requires
v2 capability); otherwise C. Authorize-only writers may have zero predictions,
but every authorization must have durable decision evidence and an explicit
report obligation, discharged only by an independently reconciled terminal. Unknown lost invocation
is not a harmless authorize-only interval. The window cannot hide a missing terminal
by ending while its status is unknown (§6).

Mixed versions:

| Serving pair | Behavior / certifiability |
|---|---|
| New enclave, old router | P2 sends v2 capability marker, tolerates absent v2 fields and uses old v1 behavior; v1 data never certifies v2 |
| Old enclave, new dormant router | Policy gate empty: byte-identical baseline; no v2 emission |
| Old enclave, v2-active router | No verified v2 capability: no certifying v2 emission (including P4a compared policy); preserve legacy result, record unresolved C/protocol coverage gap; cannot certify |
| New enclave, P4a-active router | Eligible binding/snapshot under v2 policy; all noneligible/missing-proof terminals C. No certificates issued. Full per-boot marker/attested-image evidence required |
| New enclave, later P4b-active router | Adds certificate issuance and durable worker verification under §§4.1/4.3; excluded-traffic opt-in waits for P4b proofs/budgets. Same P2 protocol, no further enclave change or pin-only follow-up |

Proposed marker: `X-TR-Settlement-Shadow-Policy: shadow-v2` on authorize only;
optional response marker `billing_shadow_policy:"shadow-v2"`. It is a protocol
capability, not opt-in or authentication. Shadow workspace ownership remains
server-side. Deployment evidence pins build capability
`shadow-v2-read-echo` before P4a compared emission (and later P4b certificate emission); an arbitrary caller marker
cannot bypass the reviewed serving-roster gate. Enclave must **land and deploy
read/echo support before router emission**. No clock during mixed-policy rollout.
Rejected: relying on old enclaves silently carrying unknown metadata, or excluding
transport failures to make mixed rollout look complete.

## 5. Change 3 — demand and simultaneous fleet accounting

**D-V2-5 — measure eligible demand independently of prediction instrumentation.**
For writer/day/phase/dimension j let E_j be the number of terminal receipts whose
requested and observed facts establish eligible comparison, **including eligible
C failures**, derived from reconciled authorize/terminal facts. On a certifiable
partition C=0, E_j=A_j. Until then demand is `unknown` if any terminal lacks an
eligibility decision, never default zero. Total known yes+no+unknown predictions
must equal E_j; every eligible receipt carries its prediction reference/value,
even if sample persistence or booking fails. Duplicate attempts count separately;
a reused sample cannot invent a second writer's cache observation.

| Writer interval | Prediction requirement | Other requirements |
|---|---|---|
| Idle: no authorize or terminal | Exactly zero, `not_exercised` | Full roster, cadence/failure budgets, counters/closure |
| Authorize-only | Exactly zero, `not_exercised` | Complete authorize receipt/decision partition; no missing terminal hidden as open |
| Excluded-only terminals | Exactly zero, `not_exercised` | Every B backed by valid durable proof and attempt receipt; no C |
| E>0, including any eligible failed attempt | Y+N+U=E; Y+N>0; 100U≤2E | Every comparator/booking/provenance obligation, no C |
| Eligibility unknown | BLOCKED | Never use zero predictions to infer idle/excluded |

Every writer still satisfies observer cadence N/4 workspace reads and 1/s health
reads, no catch-up, read/install limits, failures ≤max(3,1% of reads), max
consecutive failures ≤2 and degraded time ≤1% of interval. Count failures on
idle and excluded writers. Exact comparison remains available independently
of admission unknown; unknown never seeds. One unknown among fewer than 50
eligible predictions fails; 1/50 passes the availability fraction only.
At least one **real durable eligible positive seed** is required per 604,800-second
window; no generated prediction/counter seed. Default proposed regional policy:
one such positive comparison per traffic-serving router region in the window,
not per boot/day. §13 asks Joseph to ratify; all regions still need observer,
transport and fixture proof if he selects fleet-only seeds. The first seed starts
time; missing regional seeds blocks final PASS without shifting its timestamp.

### 5.1 Simultaneous-load sweep

Treat lifetimes as half-open `[start,end)` with verified boot/revision/region.
Sweep all starts/ends, day boundaries, rate-segment changes, and publisher roles.
At equal timestamps remove ending intervals before adding starting ones. For
each segment sum conservative rates of **active** boots only. A boot's rate is
max(nominal N/4+1, its peak measured interval reads/duration); pending rate is
max(nominal 1001N/4, peak measured workspace_reads×1001/duration). Preserve
separate trust, health and publisher terms. Include startup/tick bursts as measured
one-second peak buckets (bounded export) and projected limiter burst envelopes;
long intervals must not average away startup overload. No overlapping duplicate
intervals for the same boot; contradictions block. Missing end means potentially
still active for budget computation and incomplete for certification.

Check regional peak simultaneous count ≤ configured regional cap, fleet peak
count ≤ fleet cap, and sum of configured regional caps=fleet cap. Independently
check conservative configured-cap projections including rollout surge and
publisher maximum K against approvals/headroom, even if observed fleet is small.
Compute peak **fleet sum at each instant**, not sum of unrelated regional historical
peaks; retain the sum-of-cap projection as an independent worst case. Eight
sequential boots at cap one are one capacity, two overlapping boots are two.
All eight still appear in completeness, region maps and closure.

### 5.2 Budgets are not expanded by policy

F5's observer formulas and exact workspace-count/cap/allowance matching stay.
Replace only the historical summation; add publisher and v2 evidence costs. A
second workspace needs fresh approval and pre/post measurements for N=2.
**A1 ADD:** §7.2–§7.4 retained health attachment transport/verification and
post-close rows are included in actual and configured-cap/surge projections.
They are not free because they use an export sink rather than the money database. Lowering
router caps needs a separate serving-capacity decision. No unexplained CPU
extrapolation, 45% alarm change, or quota increase to manufacture PASS.

### 5.3 Per-boot prerequisite ledger

Hash-linked, reviewed ledger entry per role/boot/revision/true-region with
policy/fixture/config hashes, boot time, collection interval, traffic-ready time,
original transport, size and regional load artifacts, per-boot bounded transport
check, observer warm/read/freshness check, marker/key installation check, each
`completed_at_us`, and previous-ledger hash. Regional stress evidence may be
shared by reference within its declared configuration envelope; **per-boot
readiness/transport/freshness proof cannot be borrowed from a predecessor**.
New boot extends the ledger without rewriting earlier completion times or moving
the whole fleet's clock. Its opted traffic may count only after its own prerequisite
maximum. Provision startup/readiness integration so it accepts no opted requests
before ready; do not turn workspace opt-in off locally to erase a denominator.
If safe readiness cannot be enforced, count early traffic and block that day.
Startup observer intervals and resource load remain in exports even when there
was no traffic. Idle boots still need closure; no revision-only substitution.
Readiness uses a bounded boot-local timer/transport check plus the already approved
regional capacity artifact, not a closed daily counter from that new boot. Its
subsequent daily closure independently validates the whole interval. Initial
post-opt-in calibration uses closed warm-up-day counters before the fresh window;
new boots do not need to close themselves before serving or restart the fleet gate.

## 6. Change 4 — checkpoint failure is not necessarily evidence loss

**D-V2-6 — cumulative recovery with a durable high-watermark.** Current
`flush_failed()` emits an unconditional drop (C9); current storage only compares
flush sequence (C18). V2 uses distinct counters:
`checkpoint_attempts`, `checkpoint_failures`, `checkpoint_recoveries`,
`unknown_commits`, `unreconciled_commits`, `irrecoverable_losses`.
Never translate a worker checkpoint failure into a terminal failure. Never clear
an actual drop/reset because a later checkpoint succeeded.

Assign monotonically increasing `event_seq` per boot/day to every successful
authorize receipt, every terminal HTTP attempt (including retry), and completion
update. Number before expensive work. Bounded append-only receipt pages record
these events, references and results (§7); cumulative counters retain totals.
`checkpoint_seq` increases on every attempted cumulative checkpoint.
`durable_event_hwm` is the largest contiguous sequence proven durable with
reconciled references, not the largest sequence observed in memory. A final
same-transaction counter/closure update commits `hwm=final_event_seq` only after
all relevant pages, samples, certificates and locally owned outcomes are
acknowledged; pending report terminal obligations are represented as §7.3, never
mislabelled reconciled terminal outcomes. Earlier
pages are immutable and hash-linked; exact-key reads resolve unknown commits.
An equal-sequence different-body write is a conflict, never an acknowledged retry;
older snapshots cannot lower any count/HWM or erase a loss/reset.

Recovery is permitted only if a later cumulative checkpoint from **the same writer**
includes every event through the failed checkpoint, all authorization decisions
and terminal A/B/C partitions reconcile with durable referenced objects, every
retry/pending comparison/booking is accounted, no unknown commit remains, and
closure confirms no in-flight request, retained receipt or observer fold was lost.
Failed attempts remain in availability diagnostics. Sample/certificate writes
can be recovered only using retained bounded data, identical durable identity
and exact read-back verification; a later unrelated sample is not recovery.
No unbounded retry queue or request wait. Failed work retains its existing
32-task/256-KiB allocation; a scheduled later checkpoint may retry exact retained
batches within the normal cadence and budget. Overflow immediately blocks.

**A1 AFTER (D-A1-B / S2): terminal-only REPORT discharge.** A writer may close
when its own complete decision/terminal/evidence work is durably acknowledged
and all ordinary closure conditions hold. Its HWM means local durable evidence,
not that every issued authorization has finished. Every authorize receipt lacking
a reconciled terminal remains an explicit obligation of the REPORT, committed
by the closure's complete receipt chain and §7.3 interpretation marker. Closing
the writer never discharges that obligation. This replaces the former owner-link
permission; it does not assert that a durable receipt proves response delivery,
retention, an owner or completion. `complete()` with empty backing cannot discharge
it, and no authorize-original completion is permitted.

Only independently reconciled durable terminal evidence for that authorization,
from any router boot and any enclave, within its original ≤48-hour horizon may
discharge it. All attempts still need §2's T=A+B+C partition with C=0; no terminal,
a terminal failing reconciliation or a conflict blocks the **authorization's
issue day**, plus any affected terminal day. Before complete horizon/forward
coverage and reconciliation, the report is PROVISIONAL, never PASS; at the horizon
an absent terminal blocks, and incomplete export cannot prove absence or success.
No nonterminal disposition, expiry-as-discharge, reaper terminal, or backdated
positive sample exists in A1. Unknown lost invocation is not a harmless
authorize-only interval: a lost response with no terminal remains open then blocks.

Daily closure body (§7) fixes interval end, final event/checkpoint sequences,
durable HWM, receipt-chain digest, cumulative counter digest, retained-work=0,
pending-comparisons=0, unresolved-commits=0, loss count and observer fold-through
time. It must be independently matched to deployment inventory. Midnight seals
only after prior-day in-flight work releases, with later physical commit time
recorded; no closing yesterday before its observer fold. A still-serving boot
closes its day, starts the next, and links them. Closing a boot requires ingress
quiescence before freezing its final event sequence.

Blocking always: lost sample/certificate/receipt without exact durable recovery,
queue/rate/daily-cap drops (including excluded work), dimension/byte overflow,
missing writer/counter/page/closure, unknown commit not reconciled, failed final
close, hard kill, or any comparator unknown/disagreement. V1-policy
`store_unavailable` rows lack these recovery facts; they are **never retroactively
forgiven**, relabelled or used to certify v2.

### 6.0.1 A1 ADD — disposition and final-close state machines

Writer evidence states are RESERVED → DECISION_DURABLE → COMMITTED_REPORT_OBLIGATION;
report states separately are OPEN → TERMINAL_RECONCILED or BLOCKED. No state
OWNER_PROVEN_OPEN exists. P3 can release local retained authorize work after its
exact immutable decision page and references are durable, while P5 must still
enumerate the obligation even after the boot exits. The closure/acknowledged
result certifies only that writer's durable evidence boundary, not the report.
Issuer-owned requests, comparisons, observer folds and evidence queues still
must drain; lost pages, hard kills and failed final close remain blocking.
A fresh router's first terminal is legitimate and distinct from same-writer
checkpoint recovery. H1/E1 and H2/E2 from review R2 both discharge only if the
same authorization's terminal independently reconciles; A1 makes no stronger
inference from their indistinguishable evidence.

**Explicit policy limitation:** independently reconciled terminal evidence may
discharge the authorization regardless of which enclave sent it. A1 makes no
claim that an accepted owner continuously retained the invocation or sent its
terminal. Reports must state that limitation and must not describe terminal
reconciliation as proof of same-enclave continuity. There is no accepted-owner
route or “no live transfer” rule in A1. Compared with round 1, A1 gives up marking
a still-running call benign before the horizon; a report sealed after that horizon
already needed the terminal, so this loses little for final certification.

**Completion correction (R4 context, no new authorize completion):** every route
through finalize/reconcile/HWM SHALL resolve an exact durable original page and
terminal event with explicit `(day,boot,page,event)` context. Original phase must
be settle/refund, original and completion actual writer boots must match, original
receipt time/event time cannot be after completion, and identities/proof/result
preservation follow P1. On the same day require original seq < completion seq;
across days require original day < completion day (equal/reset sequence numbers
are legal only there). `completion_of` matches the original seq; `retry_original`
is mandatory `{day,boot,event_seq}` for cross-day completion. Its page is resolved
from the verified bounded receipt-page chain/export, never by arbitrary body
search or a guessed current page. If not already indexed in the writer's bounded
retained metadata, defer to bounded export reconciliation; no unbounded journal
scan/RPC burst. An absent in-memory original is not success. Unknown reference
keeps HWM blocked. Completions never erase original mismatch or prediction time.

**G-C / D-A1-C:** a final counter/closure transaction cannot describe its own later
reply; read-back proves only landing, and P1 SEALED_CLOSURE has no producer-to-export
transport (A1-C6, A1-C10). Writer/day states SHALL be OPEN → DRAINING → FROZEN →
FINAL_ATTEMPTED exactly once → ACK_OBSERVED or FINAL_FAILED. Only an original
success reply within both its 2 s transaction deadline and remaining flush/shutdown
deadline enters ACK_OBSERVED. Timeout, unknown outcome, error or late success enters
FINAL_FAILED permanently, even if exact bytes later land. No final-close retry
or new boot may rehabilitate that writer/day. Freeze the outcome after accounting;
a late reply following a timeout cannot revise it.

Only ACK_OBSERVED may emit the single immutable close-result row (§7.4). It
then becomes HANDOFF_LANDED (including exact-read recovery of a landed handoff)
or HANDOFF_MISSING. Only HANDOFF_LANDED with a valid acknowledged result permits
P5 to construct SEALED_CLOSURE. FINAL_FAILED emits no certifying result. Missing
result yields a durable report `writer_closure_gap` after complete inventory/export
coverage; it is not evidence of a particular count of failed original commits.
The row and its producer provenance, not logs/in-memory last-ack, carry the fact.
The original final transaction and later handoff have deliberately different
semantics: failed final close is irreparable; unknown delivery of an already true
acknowledgement fact is a recoverable export/checkpoint outcome. §7.4 defines a
bounded, conservative accounting rule without an acknowledgement-of-acknowledgement.

### 6.1 Shutdown is a measured boundary

**A1 BEFORE:** this paragraph called 2/1/4/9 seconds “current” (C18/C5).
**A1 AFTER:** the supplied P1 tree has v1 write/read/flush/observation ceilings
4/1/8/17 seconds; P3's distinct proposed v2 adapter uses 2/1/4 seconds
(A1-C12). A1 retains the **v2 design** ceilings 2 s/transaction, 1 s/point read,
4 s/flush; it does not change v1 or claim those ceilings prove shutdown feasibility.
Reserve up to 2 s for the final transaction and up to 2 s for the separate
close-result transaction inside the same final 4 s flush pass. Pending required slot/basis
exports must also land before closing. Their cadence wait is part of shutdown,
not a free final burst or an extension to the 9 s local cap. All earlier draining,
page passes, hashing, allocation and cleanup must fit the measured remaining
termination grace (proposed local cap 9 s inside the platform interval). If not,
leave the writer blocked; never extend grace or cancel money work. Those ceilings are not
hard scheduler
bounds and do not guarantee that queued work plus close fits termination.

Cloud Run services send SIGTERM before a 10-second termination interval, then
SIGKILL; exceptional shutdown may begin with requests still active. Idle CPU
allocation depends on billing configuration. See the official
[container runtime contract](https://docs.cloud.google.com/run/docs/container-contract#instance-lifecycle)
(checked 2026-10-11). V2 must trap/propagate shutdown, quiesce ingress, finish
owned request/comparison work, fold observers, stop publisher, and persist closure
within the measured remaining platform grace. Reserve final-flush time; do not
cancel a money operation to make shadow close. If work cannot complete, leave
writer unclosed and BLOCKED. SIGKILL is not recoverable by a finally block.
Fault-inject kills before first flush, during commit, before/after closure and
under full queues. A graceful SIGTERM success is evidence for that execution,
not proof of losslessness under arbitrary kills.

## 7. Evidence schema, bounds and versioning

**D-V2-7 — separate policy namespaces and literal contracts.** Every new record,
wire outer object, report, prerequisite and gate artifact has exact
`v=2,policy_version="shadow-v2"`.
**A1 ADD:** A1 is a separately pinned contract revision `a1`, selected by the
new fixture-manifest hash and boot prerequisites, not a default for baseline P1.
All new A1 objects below also require `contract_revision="a1"`; changed manifests,
closure/counter/proof/gate schemas carry that field. HEALTH time fields change
as §3 specifies, and HEALTH_CLAIM replaces claim_at_us with server claim_at_ns;
authorize/terminal receipt/certificate/wire
fields and bytes remain baseline P1. No owner field changes an event hash or
embedded certificate signature. Selection uses exact A1 manifest/boot prerequisites. Baseline P1 fixtures
and hashes remain historical/frozen; never recertify baseline observations under
A1 by attaching a new manifest. No reason membership or money policy changes.
Unknown/mixed versions reject; no coercion or
missing-field defaults. V1 billing snapshot/terminal DTO and price binding retain
their own v1 versions; their **v2 outer receipt/policy** is mandatory. Old rows
stay queryable as historical diagnostics/reset provenance but contribute zero
certifiable v2 seconds or positive seeds. Never combine old drops with new
recovery fields. Any subsequent change to reason membership/predicates requires
a new policy ID/fixtures, not a silent report upgrade.

| Kind / complete key (proposed) | Fields and serialized bound |
|---|---|
| `async_settle_shadow_sample_v2`, `<authorize-day>/<authorization-id>` | Existing sample fields/math plus policy, proof kind, original terminal receipt identity, health source/slot and prediction basis; ≤8,192 bytes. First insertion immutable; conflicts sticky. No JWS. |
| `async_settle_shadow_certificate_v2`, `<authorize-day>/<authorization-id>` | Verified claims projection with raw workspace/key/nonce/reservation replaced by content-free commitments; reason, facts/candidate digests, certificate SHA, validity, issuer/keyring artifact digest, authorizing revision, verification result/time, `original_receipt` as the exact signed authorize receipt reference (no duplicate copy); ≤4,096 bytes. No signed token. |
| `async_settle_shadow_receipts_v2`, `<observation-day>/<boot>/<first-seq-padded-20>` | Immutable pages: policy, boot/day, first/last seq, previous-page SHA, ≤128 ordered events. Event: seq, event/receipt time, phase, authorization day/id, decision/proof reference, actual finite dimensions, compact content-free requested facts and full ordered candidate projection when required for authorize plus `receipt_locator` coordinates (§4.1; entire event ≤2 KiB) and terminal payload/certificate digest and full signed `original_receipt` for excluded terminals, A/B/C and eligibility status, comparison/booking outcome/reference, prediction for eligible terminal, retry-original reference. Completion events link original seq; no overwrites. ≤64 KiB/page and ≤2 KiB/event. |
| `async_settle_shadow_counter_v2`, `<day>/<boot>` | Existing fixed dimensions/phase arrays and diagnostics; new A/B/C, eligible_demand (nullable when unknown), proof-present by variant, requested decisions, checkpoint diagnostics, event/HWM/checkpoint sequence, receipt count/hash, loss/reset fields, publisher operations/budget histograms. ≤64 KiB; no per-authorization maps. |
| `async_settle_shadow_closure_v2`, `<day>/<boot>` | §6 exact closure fields plus policy, boot/day/revision/region, closure reason `utc_boundary|shutdown|opt_out`, physical commit acknowledgement time stored in subsequent sealed export, previous-day closure hash; ≤4 KiB. Own commit duration not known inside its row. |
| `async_settle_shadow_control_v2`, `<day>/manifest-v2` | v1 manifest scope plus policy/config/fixture hashes, counter and closure roots, receipts root, prerequisite-ledger root, independent inventory hash, all four disabled-flag intervals, completeness/gaps. ≤256 KiB, ≤4,096 writers/day, ≤64 revisions/role, ≤128 gap intervals. |
| Same control kind, `<day>/cap-v2`, `retention-v2` | Daily terminal reservations limit 100,000 in blocks of 100, no crash reclamation; receipt event reservations limit 400,000/day in blocks of 100 (authorizes, terminals, completions and retries all consume them), at most one allocation attempt/5 seconds/boot. Overflow/daily-cap is blocking. Retention fence mirrors v1 with exact new kind list. ≤4 KiB each. |
| Health and health control kinds/keys | §3 exact schemas, ≤4 KiB/≤1 KiB; source is non-authorizing; latest record only. Export slot/timing evidence each day, since latest record alone cannot prove a week's progress. |
| **A1 ADD:** `async_settle_shadow_close_result_v2`, `<day>/<boot>` | §7.4 immutable original-boot observed-success fact, ≤4,096 bytes, separate post-close transaction; explicitly added to kind/retention/export allowlists. |
| External proof/gates | ≤256 KiB manifest plus hash-addressed bounded attachments; policy, source SHAs, evaluator/binding/v2 wire fixture hashes, per-day router/enclave rosters, **instance_regions_by_day**, ledger root, shutdown/inventory evidence, cohort/seed policy, pre/post budgets, publisher slot trace, independent external proofs and resolution DAG. No secret material. |

**A1 ADD:** closure adds exact `authorization_obligations="receipt_chain"`.
Its existing receipt_chain_sha256, final_event_seq and durable_event_hwm commit
all authorize receipts and thus every open obligation (§7.3); no duplicate
unbounded authorization list is added. Day manifest adds `close_result_root`,
`publisher_trace_root`, `health_basis_root`; proof manifest adds the same plus
`handoff_accounting_root` and `authorization_obligation_root` (P5's bounded
reconciliation export). Each root is a 64-hex digest with §7.2 coverage. No owner,
boot-key or clock-qualification root remains. All complete-object limits remain
conjunctive, including the 25-byte contract_revision member where specified.

These are new kind names under existing `(kind,id)` `tr_entities` (C14), no new
money DDL. A page counter/closure transaction touches at most 4 new pages plus
counter and closure: **≤6 row bodies, ≤324 KiB (331,776 bytes)**, derived as
`4×65,536 + 65,536 + 4,096`. Keys and RPC/transaction encoding are additional,
not included in that body bound. **A1 ADD:** the later close-result is **not**
a seventh row in that transaction. Two sequential transactions may carry
`331,776+4,096=335,872` body bytes total in the final pass, plus separate key/native
encoding overhead; their deadlines share that pass's 4 seconds. P3/P6 must measure and record their separate
maximum byte overhead using maximum supported keys and native serialization,
and measure RPC count, peak resident memory (including retained pages, counter,
closure and encoded copies), flush and shutdown budgets at the full 324 KiB body
bound **plus** that overhead. A failed budget blocks activation; no 258 KiB sizing
assumption remains. Never assume a transaction-count bound is an RPC bound.
Flush receipts asynchronously at the five-second checkpoint cadence; authorize adds bounded in-memory evidence
only, **no per-authorize background RPC**. At most 4 unacknowledged pages/256 KiB
per boot **in aggregate across** at most three day buckets, within separately
measured memory and worker budgets. A full page queue causes sticky loss,
not request blocking.
Terminal sample/certificate persistence uses the existing bounded worker budget;
all page/counter transactions obey the same 2-second ceiling, 1-second point-read
ceiling and 4-second flush pass ceiling proposed in §6.1/A1-C12. Extra pages may take
later passes; closure cannot get ahead of them. Cap allocations and receipt
writes are additional load in §3/§5, never hidden under “zero observer writes.”

Byte/row bounds are conjunctive: `128×2 KiB=256 KiB` does not fit a 64 KiB
page; page admission must include its wrapper and reject an event before either
limit is exceeded (with 2 KiB events and nonempty metadata, at most 31 fit).
Four pages retain at most 256 KiB of serialized page bodies total, not per day;
counter/closure bodies account for the additional 68 KiB per transaction. The
400,000-event/day cap bounds event bodies alone to 819,200,000 bytes (781.25 MiB),
not total page/storage bytes; wrappers and all other kinds must be charged in
measured write/storage budgets. The 100,000 terminal and 400,000 event caps admit
at most 1,000 and 4,000 reservations of 100 respectively, including stranded
reservations; allocation cadence does not raise either cap. Likewise 4,096 writers,
64 revisions/role and 128 gaps do not override the manifest's 256 KiB serialized
limit: whichever bound is reached first applies; inability to represent required
coverage blocks, never truncates. These limits allocate no new candidate-proof rows.

**Stable reservation before issuance.** All events use one ordered bounded
append allocator per boot/day. Before signing/expensive work, reserve the next
`event_seq` and a slot of up to 2,048 bytes in the current page. Fix its observation
time/day at reservation, even if signing finishes after midnight. Reserve page
wrapper/separators and maximum-width last-sequence metadata too; the page must
remain ≤65,536 bytes and ≤128 events with every unfinished slot at its full
reservation. If it cannot fit, stop appends to that page and reserve the next
page at this event's sequence without waiting for earlier slots; freeze the old
page only after its slots finish. Each `first_seq` and key is fixed at creation,
before any certificate uses it. A completed shorter event
can free unused bytes for later appends **to the same page**, never repack an
assigned event into a different page, renumber it, merge pages or reuse a key.
Sequence reservation consumes the existing daily event permit; it creates no
storage row or RPC. Saturation/sequence exhaustion leaves sticky unresolved loss
under the existing bounds, never waits on storage or invents a locator.

Finalize each reserved slot as immutable bytes before exposing its certificate;
slots may finish out of order, but their reserved page/sequence order never
changes. On failure before finalization/exposure, use a bounded unresolved
diagnostic in that same slot/sequence. A certificate or locator overflow is
`receipt_locator_size`; full candidate-event overflow keeps the accepted
`candidate_receipt_size` diagnostic. Neither can become B. If even the diagnostic
cannot be retained, keep the existing sticky loss and unclosed writer. Freeze a
page when full, at checkpoint, day boundary or shutdown, once all its slots and
the previous-page hash are final; first stop appends while finalization is pending.
Never flush unfinished slots, append after freezing, or change a frozen page's
key/body/hash on retry.
The next page links the finalized previous-page hash. Reserved, open, frozen and
unknown-commit pages all count toward the **same four-page/256-KiB aggregate**;
slot reservations include their worst-case bytes in that limit. No fifth page,
new storage kind, per-authorize flush or extra transaction row is introduced.

The ordinary five-second flush writes frozen pages with the counter (and closure
only when §6 permits), at most four pages plus counter/closure per transaction.
An in-memory reservation or signed certificate does not advance durable HWM;
a hole/unfinished slot/unknown commit prevents it passing that sequence. Only
acknowledged contiguous pages and reconciled references advance HWM. Authorize
receipts have no forward reference to a not-yet-created verified certificate, so
**A1 clarification:** an issuer may close with complete durable local facts
and the §7.3 receipt-chain commitment to report obligations under §6, without
waiting for a first terminal on another boot. This does not prove delivery or
ownership. Issuer-owned work must drain; report sealing still requires actual
terminal reconciliation of every authorization. Keep exact frozen bytes/keys through unknown commit and use
§6's exact-key reconciliation, never relocate on retry. If the boot dies before
its page is durable, the echoed locator still names that missing page: terminal
C/`original_receipt_missing`, unclosed writer/BLOCKED, no alternate lookup. A later
commit acknowledgement can resolve durability only by §6's existing rules; it
cannot forgive a hard kill or actual loss. All locator references inherit the
same 30-day retention, 48-hour horizon and closure/export coverage below.

**Locator-inclusive byte derivation (P1 fixtures, P3/P6 measurement).** Canonical
ASCII JSON without whitespace has a maximum 246-byte `original_receipt` object;
adding its member to nonempty claims/terminal-event JSON costs at most 266 bytes.
The four-coordinate object is ≤164 bytes; its `receipt_locator` member adds at
most 183 bytes to an authorize event. These are **inside**, not in addition to,
the accepted complete-event 2,048-byte bound. Required facts cannot be trimmed to
make room. Invalid field lengths/sequence encoding or any complete-object overflow
remains unresolved. Preflight the complete authorize event, certificate and
maximum-metadata verified row before issuance; terminal receipt/wire fit is also
enforced at capture/verification, never by truncating the locator.

With `kid` 1–64 bounded ASCII identity bytes, the exact §4.1 protected header is
69–132 bytes (92–176 unpadded base64url bytes); the 64-byte Ed25519 signature is
86 encoded bytes and the two dots cost 2. Let `U(n)=ceil(4*n/3)`. Enforce
`U(header_bytes)+U(claim_bytes)+86+2 <= 2,048` on the **whole** JWS, including
the locator. Maximum header leaves 1,338 raw claim bytes; the smallest permitted
header allows at most 1,401. The added 266 raw claim bytes can cost up to 355
encoded bytes; there is no promise that previously fitting claims still fit.
For the ≤4,096-byte stored verified row, all claims including the locator occupy
≤1,401 bytes; replacing four identity values by 64-hex commitments adds at most
252, leaving at least 2,443 bytes for all remaining row fields/wrappers. Enforce
that combined serialized limit with maximal verification metadata, not individual
field maxima alone; no raw JWS or extra candidate storage goes in the row.

The excluded wire adds **zero outer locator bytes**: the locator is within its
still-≤2,048-byte certificate string. The complete variant still must fit 4,096
bytes, with `U(4,096)=5,462` header bytes, below C11's 8,192 decoded and 12,288
header limits (and 65,536 local limit). No snapshot is added; the compared path's
6,144 inline/65,536 local limits are unchanged. Certificate/locator overflow
never promotes C to B. Receipt pages remain ≤65,536 bytes because locator bytes
consume their event budgets; the six-row transaction remains exactly bounded by
`4×65,536 + 65,536 + 4,096 = 331,776` body bytes, plus separately measured key/native
encoding overhead. The now-explicit page id ceiling is 96 bytes; native key/RPC,
retained-copy, flush and shutdown measurements still gate P3/P6, not an assumed
new load allowance.

Keep 30-day retention; read report days plus two prior authorization days for
48-hour proof lifetime, plus explicit same-key references. **A1 ADD:** sealing
open authorizations also consumes complete exports for up to two following UTC
terminal days through each original expiry, and any explicitly referenced later
durable reconciliation. A terminal's actual receipt must meet the original proof
lifetime; later export/commit cannot renew it. Join by full identity across these
bounded exports, never search production entity bodies to find a terminal.
Until forward coverage and terminal reconciliation are known, the original
day is provisional; expiry or end of the requested report window is not absence
proof. Retain v1 ≤200 rows/page,
LOW-priority 200 ms operator read, no retries, complete day/boot indexed ranges,
cleanup ≤200 complete keys/transaction, ≤5 transactions/s, ≤30-second pass. New
kinds require explicit allowlisting; health reads use complete keys. Live pilot
≤10,000 explicit keys per bounded bundle; larger windows require separately
reviewed paginated exports with coverage hashes. Never call a truncated export
complete. Receipt-cap budget is a proposed bound, not approval to increase load.

#1667 semantics stay: `instance_regions_by_day[observation_day][boot]` must match
counter, sample, certificate-terminal receipt and serving lifetime; authorize-day
sample key does not change observation region. Keep short-revision/wrong-region
historical rows visible as legacy diagnostics; never expand/relabel them (C12).

### 7.1 Cross-repository byte-identical fixtures

P1/P2 copy exactly the same bytes (including final LF), pin SHA-256 independently
and check both parsers' accept/reject results. New fixtures live under
`tests/fixtures/async_settlement/shadow_v2/` and
`E/enclave-go/internal/trustedrouter/testdata/async_settlement/shadow_v2/`:

- `authorize_compared.json`, `authorize_excluded.json`, `exclusion_certificate.json`:
  complete optional authorize projections, protected headers/claims and real
  deterministic test signatures **only in fixture files**, never this document.
  P1 freezes `original_receipt` coordinates/event hash, cycle-free authorize-event
  bytes and reservation/key invariants **before P2 builds echo support**. Include
  maximum locator/claim/verified-row/receipt/wire sizes and one-byte-over rejects.
- `terminal_compared_full.json`, `terminal_compared_hash_only.json`,
  `terminal_compared_failure.json`, `terminal_excluded_settle.json`,
  `terminal_excluded_refund.json`: decoded object and canonical/header bytes,
  including ordinary missing-usage refund failure and excluded pre-invoke refund.
- `capability_headers.json`, `purpose_rejections.json`, `wire_rejections.json`:
  exact markers, ticket/binding/certificate cross-type rejection, duplicate keys,
  expiry, sizes, booleans/numeric lexemes, identity, fallback contradictions;
  locator missing/extra/invalid fields, wrong boot/page/event and event-digest
  mismatch, plus byte-identical locator echo through settle/refund and retries.
- `health.json`, `health_claim.json`, `receipt_pages.json`, `sample.json`,
  `certificate_record.json`, `counter.json`, `closure.json`, `controls.json`,
  `proof_manifest.json`, `prerequisite_ledger.json`, `gate_outputs.json`:
  exact new stored/export objects. Enclave need not consume router storage
  schemas; its fixture byte-pin test still detects cross-repo contract drift.

Keep original billing-v1, ticket, binding and legacy response fixtures byte-for-byte.
Fixture manifest enumerates **every** variant and policy hash. Freeze independent
expected amounts (including ±1 negative controls); do not generate expected output
with the evaluator being tested. No invented final hashes before fixture PRs land.

**A1 ADD — fixture revision, proposed files only:** keep baseline P1 bytes and
manifest frozen. P1-A1 releases separate `shadow_v2_a1/` copies in both repos with
a new independently pinned manifest (no invented hash). Changes: `health.json`
(new timestamp meanings/types and backlog wall input), `health_claim.json`
(server claim_at_ns replaces host claim_at_us), `counter.json` (conservation),
`closure.json` (obligation marker and sealed projection), `controls.json`
(close-result kind/roots), `proof_manifest.json`, `prerequisite_ledger.json` and
`gate_outputs.json` (A1 interpretation). Add `health_read_metadata.json`,
`health_basis.json`, `publisher_slot_trace.json`, `close_result.json`,
`handoff_accounting.json`, `authorization_obligations.json`, `completion_context.json`
and all positive/negative boundary variants. Completion references retain P1
terminal-only wire shape; contextual rules become explicit and shared.

Receipt pages, authorize projections, capability headers, certificate
claims/signatures/verified records, excluded and compared terminal variants,
legacy billing/ticket/binding/money-oracle bytes stay baseline byte-identical.
No owner, clock, boot-provenance fixture or A1 wire negotiation is needed.
The enclave follow-up is **pin-only**: copy and check every fixture, manifest and
variant inventory, including router-only additions, as its existing test does
(A1-C9). No P2-A1 behavior package remains. If implementation discovers a wire
change, that is a new reviewed amendment, not implied permission here.

### 7.1.1 A2 ADD — one combined fixture release, one enclave pin

**A2 BEFORE:** A1 described an independently pinned `shadow_v2_a1/` release and
its own enclave pin-only follow-up. **A2 AFTER:** that release is not yet pinned
in the enclave. Freeze its files/manifest as historical A1 bytes; P1-A2 creates
a **new superseding release for the pin**, `shadow_v2_a2/`, containing exact
copies of every A1 fixture file and its manifest under `a1/`, plus the new A2
files below and a new top-level `manifest.json`. Do not edit/extend the frozen
A1 manifest in place. The new manifest enumerates every nested file, variant,
source A1 manifest hash and baseline wire manifest hash. Router and enclave copy
identical bytes and independently pin **that one combined manifest SHA-256**.
Keep the baseline P1 pin and A1 historical identity checks; add the combined pin.
Perform exactly **one** enclave pin-only follow-up after P1-A2 release; do not
land an intermediate A1-only enclave pin. No A2 wire/capability/schema consumer
is added to enclave runtime. This changes release packaging, not A1 semantics.
P3/P4a/P4b's code dependencies remain A1; P4a activation still waits for the combined
fixture pin and baseline P2 deployment. No final A2 hash is invented here.

P1-A2 adds exactly these **16** fixture files (one fewer than round 1; no
index fixture/schema), with positive, negative, byte/count boundary
and preimage variants in each file, not separate unlisted files:
`gate_report.json`, `gate_outputs_a2.json`, `gate_failures.json`,
`failure_pages.json`, `writer_reports.json`, `region_reports.json`,
`input_inventory.json`, `root_preimages.json`,
`independent_inventory.json`, `shutdown_inventory.json`, `cohort_seed_policy.json`,
`budget_envelopes.json`, `external_proofs.json`, `resolution_dag.json`,
`verifier_ports.json`, `delegated_payloads.json`. Add top-level `manifest.json`;
all other release files are the byte-identical `a1/` copies just specified.
No signed token or key is printed in this design. Existing fixture signing
material remains confined to its already approved fixture files.

P1-A2's pure implementation surface is proposed, not created by this amendment:
`async_settle_shadow_v2_a2_evidence.py` owns all schemas defined in §7.5:
GATE_REPORT, GAP_NAME, RATIO, STAT, RATIONAL, PEAKS, WRITER_REPORT, REGION_REPORT,
FAILURE_SUBJECT, GATE_FAILURE, DETAIL, FAILURE_PAGE, WRITER_REPORT_PAGE,
REGION_REPORT_PAGE, VERIFIER_USE, FILE_REF, APPROVAL, INPUT_LEAF,
INPUT_INVENTORY_ENTRY, INDEPENDENT_INVENTORY, SHUTDOWN_INVENTORY, SHUTDOWN_ENTRY,
COHORT_SEED_POLICY, DELEGATED_PAYLOAD, BUDGET_ENVELOPE, LOAD_EVIDENCE,
LOAD_SEGMENT, EXPORT_OPERATION, EXTERNAL_PROOFS, EXTERNAL_REQUIREMENT, RESET_ENTRY,
RESOLUTION_DAG, PROOF_RESULT, REVIEW_RECORD, SHUTDOWN_MEASUREMENT, ROOT_ENTRY_PAGE,
ATTACHMENT_PAGE. ROOT_ENTRY aliases A1 COVERAGE_ENTRY; it is not a replacement.
`async_settle_shadow_v2_a2_roots.py` adds exactly
`file_digest(raw: bytes) -> str`,
`entry_root(entries: Iterable[dict[str, Any]], *, stream: str) -> str`,
`validate_detail(detail: dict[str, Any], pages: Iterable[dict[str, Any]],
*, stream: str) -> None`;
stream is a closed enum selecting the key/type rules above, not arbitrary code.
`async_settle_shadow_v2_a2_contract.py` adds exactly
`validate_gate_output(output, report, *, failures, writers, regions, inputs,
reset_history, verifiers) -> None`,
`validate_preimages(manifest, proof, *, artifacts, roots) -> None`,
`display_failure(failure) -> str`; arguments are supplied validated JSON objects/
byte iterables, no file/network access. It declares the two callable port types
with §7.5.3 signatures, without importing binding/storage. Canonical-object
hashing reuses the existing sha256 helper, never a fourth construction.
P5 supplies evaluation and I/O adapters; P1-A2 does not implement G1–G11 or copy
certificate, binding, retained-tool, A1 completion/health/obligation validators.

### 7.2 A1 ADD — existing evidence provenance and complete export accounting

Normative types: `u`=exact nonnegative int64; `boot`=1–64 ASCII `[A-Za-z0-9_-]`;
`day`=valid YYYY-MM-DD; `sha`=lowercase 64-hex; `rev`=full 40-hex;
`seq`=canonical zero-padded 20-digit positive uint64. New A1 objects have literal
v=2/policy_version=shadow-v2/contract_revision=a1. Strict canonical JSON, unknown-
key and duplicate-key rejection remain. No credentials, prompt, raw token or key.

S3 uses router service identity and ordinary immutable evidence/export controls,
actual boot/day in keys and bodies, and independent G2 inventory. It introduces
no per-boot key registry, attestation binding, signed-fact wrapper, independent
origin collector or P0-A1. Exports copy original rows/bytes; P5 never originates a
writer's acknowledgement. Inventory/key/body/revision/lifecycle agreement is
mandatory but does not resist deliberate cross-boot forgery by a malicious
router with shared write authority (§2). That limit also applies to pages and
closures; stronger origin must be reviewed consistently across evidence.

Round-1 enclave ingress, OWNER_PAGE generations, owner polling, ownership-root
pages and provenance/qualification resolution lanes and their budgets disappear.
Health bases and publisher traces still require durable export; removing those
would weaken an unchallenged evidence requirement. They use P3/P5's ordinary
bounded attachment exporter, under the same service identity, not a new trust
domain. Their explicit transport budget follows; byte limits alone are insufficient.

Per router boot and each of the two streams (basis; trace if contender), permit
one **PUT or exact-key reconciliation GET per 5 seconds**, not one of each. Keys
are `health-basis-batches/<day>/<boot>/<sequence>` and
`publisher-traces/<day>/<boot>/<first-slot>`. Create immutable objects with a
create-if-absent condition; no mutable cumulative generations or collector.
An identical existing body is landing evidence, a different body is conflict.
On unknown PUT retain exact bytes/key; next token may GET, without an immediate
retry loop. Missing objects cannot be called durable. Queue ≤4 objects/256 KiB
per stream including open/in-flight bodies, ≤512 KiB combined per contender;
parser/native/encoded copies are measured separately. Restart does not adopt a
dead boot's unfinished queue: its missing export/closure blocks. The surviving
boot needs no prior-generation fetch/cache to construct immutable next pages.
At close all required objects must be durably established, including cadence
wait, inside remaining shutdown grace; there is no extra final token/burst.

For any interval, lane operations ≤ sum over participating boot-lanes of
`1+floor(active_seconds/5)`, plus operations already in flight at interval start
(at most one per lane). Include newly started AND retired boot-lanes, not just
simultaneous R/K. Each operation moves ≤65,536 body bytes. Steady simultaneous
ceilings are (R+K)/5 operations/s and 65,536(R+K)/5 bytes/s, with writes and GETs
reported separately. These are lane ceilings, not the total export budget.
Basis input is at most 2/s steady with burst ten under existing capture guard;
ten bases/5 s drains steady input with bounded burst space. Reconciliation GETs
spend service capacity; sustained errors eventually overflow and block. Trace
input is K/2 entries/s, ≤512K payload bytes/s; wrappers/partial pages still count.
No independent enclave/export-verifier signature operations remain.

P5 processes only manifest/inventory/reference-derived complete keys and ranges,
never LIST/body search as absence proof. Each export pass has an approved finite
key list ≤10,000; larger windows require separately paginated reviewed bundles.
Fetch each source object/result at most once per pass; cache exact bytes only
within the pass, ≤one 256 KiB decode buffer plus bounded page/manifest state.
A restarted pass is another fully charged pass, not free recovery. Write each
coverage/report/root page once with create-if-absent; unknown writes remain
provisional and require a separately charged exact-key read in the next pass.
No recursive immediate retries. Let L=source object/result GETs, W=coverage,
root-list and report-page PUTs in a pass: count L+W storage operations, actual
bytes (≤262,144(L+W) coarse body bound), every native RPC and all decoding/hashing.
Original Spanner exports additionally retain ≤200 rows/page, 200 ms LOW/no-retry
operator reads and actual row/key bytes. Result point reads are counted once,
not again as source attachment reads. P6 must approve pass frequency and full
finite-interval totals including re-runs before activation. No unlimited offline
export work is hidden in a producer per-second rate.

Retention cleanup includes every retained generation/object and root: enumerate
from the coverage manifests, charge ≤one deletion/key, batch only where supported;
Spanner retains ≤200 keys/transaction, ≤5 transactions/s, ≤30 s/pass. Object-sink
cleanup follows the same ≤1,000 keys/s and ≤30 s/pass ceilings, with actual
batch/native request counts measured. Missing cleanup coverage blocks retention
claims. No provenance-resolution, collector-consumption or cumulative-page-fetch
operation exists in this branch; all remaining producer, reconciliation, export,
root and cleanup work above counts in G7 and §5's churn/burst sweep.

`COVERAGE_PAGE` ≤262,144 bytes/≤1,000 entries is exactly
`{v,policy_version,contract_revision,day,stream,page_seq,previous_sha256,entries}`;
stream=`close_result|publisher_trace|handoff_accounting|health_basis|authorization_obligation`,
page_seq=seq, previous nullable sha; entries sorted by complete logical key,
each `{key,sha256,bytes,state}` with ASCII key≤512, nullable sha, bytes=u,
state=`present|missing|conflict`. Missing has null hash/0 bytes. Conflicts retain
all conflicting digests in bounded attachments, never latest-write-wins. Root
hashes the canonical ordered page-digest list, itself paginated if needed.
Expected coverage comes from inventory, receipt HWM/references and scheduled
slots, not uploads found. All objects/roots retained ≥30 days. Limits never permit
truncation. A trace/basis/root transport failure is a visible evidence gap.

### 7.3 A1 ADD — G-B report obligations without an owner claim

The smallest commitment is the closure's **existing complete receipt chain**:
all immutable ordered pages through final_event_seq, hash-linked to
receipt_chain_sha256 with durable_event_hwm=final_event_seq. Add the literal
`authorization_obligations="receipt_chain"` so no baseline closure is interpreted
under this new rule. No owner field, count-only summary, new page kind or
per-authorization row is needed. P5 must actually read/verify the whole bounded
chain and derive the exact set of authorize receipts; it cannot treat the hash
alone as enumeration. This is already required closure evidence, not a new scan.

Every authorize receipt enters that report set. Fresh/replay counters must equal
the enumerated receipt counts; replay references resolve the same original
identity/decision without creating a renewed horizon. P5 groups by the full
committed authorization identity, preserving each issuance/replay receipt and
its issue day. It joins independently reconciled terminal receipts from the
report days, two prior authorization days, up to two following terminal days
through each original expiry and explicit later durable-reconciliation references.
All terminal attempts retain their independent partition obligations. A terminal
can discharge the original/replays of the same authorization only with exact
identity/proof/result matching, immutable factual backing and original lifetime.
It cannot discharge a different nonce/reservation/generation or cure a conflict.

At any report cutoff, explicitly enumerate the authorize receipts without such
a linked terminal as OPEN, with `original_receipt`, original horizon and status
`open|terminal_reconciled|blocked`, plus terminal references or gap reason.
Define exact `AUTHORIZATION_OBLIGATION` ≤2,048 bytes:
`{v,policy_version,contract_revision,original_receipt,identity_sha256,
horizon_at_us,status,terminal_reference,gap}`. original_receipt is the existing
full LOCATOR; identity_sha256 hashes the canonical committed identity;
horizon_at_us=u is original issuance plus the existing maximum horizon, capped
by the original proof expiry where applicable (never renewed by replay).
terminal_reference is nullable `{day,boot,first_seq,event_seq,event_sha256}`;
gap is nullable enum `authorization_link_gap|authorization_horizon_gap|authorization_terminal_conflict`.
`open` requires null terminal/gap; terminal_reconciled requires resolved reference
and null gap; blocked requires a named gap, retaining a reference if one exists.
Multiple terminal attempts are enumerated in the already-required receipt export,
not truncated into this summary; the chosen reference does not excuse any C.
Use exact page `{v,policy_version,contract_revision,day,boot,page_seq,
previous_sha256,entries}` with ≤256 KiB and ≤1,000 entries, ordered by original
(day,boot,event_seq); all whole-object limits are conjunctive. A report's exact
writer/day obligation projection is `{v,policy_version,contract_revision,day,boot,
closure_sha256,open_obligation_count,open_obligations_root,reconciliation_root}`
(≤1,024 bytes). Its count is u and roots sha. Coverage entries under
`authorization_obligation_root` name these exact pages/projections. P5 derives and emits
`open_obligation_count` and `open_obligations_root` over the sorted canonical
unresolved entries for every writer/day, including canonical empty-list digest.
These are report fields, **not an unbounded local map or a claim by the closing
writer**. At writer close every authorization not yet linked remains committed
by the receipt chain regardless of the writer's knowledge of another boot's
terminal; later evidence can only discharge via the independently verified join.
Missing chain/receipt/counter coverage makes enumeration impossible and blocks.

In each AUTHORIZATION_OBLIGATION, original_receipt locates the individual
fresh-authorize or replay receipt being projected: derive day, boot, first_seq
and event_seq from its verified immutable page and event_sha256 from that
event's canonical bytes. For a replay, this is the replay receipt's own locator,
not the fresh authorization's locator. Resolve the original decision and
original horizon through the replay's existing verified reference; do not
change any receipt or certificate field. There is exactly one obligation entry
per fresh-authorize or replay receipt, uniquely keyed by these receipt
coordinates. For each writer/day, reconciliation_root is SHA-256 of the
canonical JSON array of all its AUTHORIZATION_OBLIGATION entries, sorted by
(original_receipt.day, original_receipt.boot, original_receipt.event_seq).
open_obligations_root uses the same construction restricted to entries whose
status is open or blocked; open_obligation_count is that restricted array's
length. Both roots use SHA-256 of the canonical empty array when their input is
empty. Page partitioning does not change either root. Existing bounded pages
carry the entries, and P5 recomputes both roots from the verified entries
rather than accepting producer summaries.

The old DD5 witness may now obtain a closed *writer evidence boundary* with its
explicit obligation commitment, but can never obtain a certified report from
its empty `complete()`. Retained versus lost-response histories stay provisional;
only the former's eventual reconciled terminal can discharge. No terminal at
horizon blocks the original day even if every local counter/close result is clean.
Response lost but a later legitimate proof/retry produces a reconciled terminal
is allowed; A1 proves terminal reconciliation, not uninterrupted retention.
The R2 E1/E2 witness is deliberately indistinguishable and both acceptable under
that narrower claim. No same-enclave continuity or no-transfer assertion survives.

**Stricter alternative, not adopted:** identifying a still-running call as benign
before the horizon needs enclave-attested post-response retention **and** a
reviewed terminal binding to the exact enclave boot/invocation, loss/release and
transfer coverage, replay rules, bounded transport/operation/churn costs and
fixtures. A signature on historical acceptance alone fails R2. Nonterminal
dispositions also require a separate reviewed protocol; none exist in A1.

The new closure marker consumes 44 compact JSON bytes in a nonempty object,
inside 4,096 (plus contract_revision where added); no authorize/wire/hash delta.
P5 terminal joins and reconciliation pages remain bounded by existing event and
export caps, with actual CPU/bytes/GET/PUT/retention in §7.2. They add no live
router/enclave ownership work, new synchronous RPC, DDL or index.

### 7.4 A1 ADD — G-C original-writer close-result row

`CLOSE_RESULT` is one exact canonical object ≤4,096 bytes:
`{v,policy_version,contract_revision,day,boot,router_revision,closure_sha256,
final_checkpoint_seq,attempt_started_at_us,observed_at_us,elapsed_us,
physical_commit_ack_at_us,outcome,flush_remaining_us,shutdown_remaining_us,
handoff_attempt_limit}`.
Types as §7.2; outcome is literal `acknowledged`, handoff_attempt_limit=1;
ack equals observed_at_us and is nonnull. Wall timestamps record the original
process's observed reply for diagnosis; they are not UTC-accuracy evidence or
freshness inputs. The success/timeliness proof within §2's trusted-code boundary
is the recorded monotonic elapsed time and original shared remaining deadlines,
not wall subtraction. A wall step cannot turn a late operation timely. Preserve
wall anomalies as diagnostics; never invent an acknowledgement from server time.
Day is the closed logical day even when the reply is after midnight.

Original elapsed≤2,000,000 us and ≤flush_remaining_us and, during termination,
≤shutdown_remaining_us; remainders are captured at original attempt start from
the existing deadlines. P5 checks exact closure/checkpoint/day/boot/revision
binding and G2 lifecycle coverage; fault proofs establish correct producer state
transitions and deadline capture. Inventory is not an independent observer of a
reply, and no per-boot signature claims otherwise. Key is exactly
`(async_settle_shadow_close_result_v2, <day>/<boot>)`, id≤75 ASCII bytes.

final_checkpoint_seq, attempt_started_at_us, observed_at_us, elapsed_us and
flush_remaining_us are u. shutdown_remaining_us is nullable u: null exactly when
no termination deadline applies at original-attempt start, otherwise the
captured remaining termination budget. P5 applies the shutdown comparison
whenever the field is nonnull and requires nonnull for a shutdown closure. A
null value never disables an independently applicable termination deadline.
physical_commit_ack_at_us is u and equals observed_at_us; handoff_attempt_limit
is the exact integer literal 1. Add positive and negative fixtures for ordinary
UTC-boundary/opt-out closure, termination, and missing or falsely omitted
termination budgets.

Only the original closing boot, **after directly observing timely success** of
its final transaction, may freeze and insert this row under the router service
identity. Boot identity is bound to the live journal/lifecycle object, never a
caller-supplied boot or one reconstructed by revision equality. No exporter,
P5, replacement process or read-back path may originate it. Failed, timeout,
unknown or late original outcome permanently vetoes the writer/day, emits no
CLOSE_RESULT, and cannot retry into a clean state. No failed/unknown result enum
is needed; retained diagnostics and coverage-derived absence remain blocking.

One separate insert-only transaction reads the retention fence and exact result
key (≤2 point rows), inserts ≤1 row/4,096 bytes, with total ≤2 s and each point
read≤1 s under the remaining shared flush/shutdown deadline. Same key/different
body is conflict. Identical pre-existing bytes establish landing only; never
mint a new acknowledgement or retry final close. No automatic or scheduled
handoff-write retry. P5 may read the exact key once per bounded export pass to
find late landing; while outcome can still land absence is provisional, thereafter
complete coverage records writer_closure_gap. Frozen closure/counter digests are
never edited to include their own later result. Ordinary service identity and
insert-only code do not cryptographically prevent a malicious authorized writer
from bypassing these rules; that is the explicit §2 limit, Q-A2's alternative.

**No acknowledgement-of-acknowledgement:** row landing preserves a fact created
*after* the original successful reply. Its validity does not depend on whether the
producer received the result-row write's reply. Lost handoff reply + landed row is
recoverable export/checkpoint delivery, not a failed original close. A second
marker saying the first marker was acknowledged is neither required nor permitted.
The producer's exact response diagnostics, when observed, may be included in its
bounded exporter trace, but A1 cannot guarantee survival of the last local reply
fact under a kill. It therefore SHALL NOT claim exact zero handoff failures from
row presence or absence.

Define coverage-derived `HANDOFF_ACCOUNTING` ≤1,024 bytes per writer/day, emitted
by P5 in bounded attachments, exact `{v,policy_version,contract_revision,day,boot,
expected_handoffs,attempts_lower,attempts_upper,landed,reply_status,
failures_lower,failures_upper,unknown_reply_upper,result_sha256}`.
expected_handoffs=1 for each expected closing writer/day; result_sha256 nullable
sha; reply_status literal `not_proven`; landed bool; all other counts u.
If valid result exists: attempts_lower=attempts_upper=1, landed=true,
failures_lower=0, failures_upper=unknown_reply_upper=1. If missing: attempts_lower=0,
attempts_upper=1, landed=false, failures_lower=0, failures_upper=unknown_reply_upper=1.
Key/body/inventory mismatch or conflict also blocks and is explicitly reported, not counted
as a successful landing. These are **bounds**, not invented observations of error.
Charge the upper bounds to availability/load diagnostics even on successful
handoffs; any actually observed failed attempt is thus preserved in the conservative
charge, never erased by a later read. Result-row write is a separate export budget,
not folded into frozen checkpoint counters or publisher counters. There is only
one possible attempt, so no failure can escape this bound. P5 outputs exact
original-close success when a valid result exists, otherwise unknown; missing
result is not a proved count of failed final closes. Requiring exact last-write
reply counts instead is impossible under these crash constraints without an
additional witness; Q-A3 records that tradeoff explicitly.

P5 creates A1 SEALED_CLOSURE ≤4,096 bytes with exact
`{v,policy_version,contract_revision,day,boot,closure_sha256,
physical_commit_ack_at_us,close_result_sha256}` solely from
verified acknowledged result, matching closure/counter digest, checkpoint seq,
logical day, boot and covered lifecycle. Export every expected result key, including
absence/conflict, under close_result_root and handoff_accounting_root; expected
set derives from independent inventory and day boundaries, not rows found.
The exact original row, inventory binding and diagnostic bounds are retained
≥30 days with the closure and daily roots; next-day export includes references to
the original day, not a renamed result. Add this kind to validate_row, cleanup,
retention fence and operator allowlists. Use existing complete-PK/day range access
and pagination; no new DDL/index. SEALED_CLOSURE remains an export projection.

Cost for C closing writer-days/s: ≤2C additional exact-key row reads/s,
≤C transactions/s and ≤C mutations/s, ≤4,096C body bytes/s plus native overhead;
P5 adds one point row per expected writer/day per complete export sweep and
one bounded schema/digest/inventory verification/result; no signature operation. At ≤4,096 writer-days/day: ≤4,096 result rows,
≤8,192 write-path point-row reads and ≤16 MiB body/day, plus keys/encoding,
≤4 MiB handoff-accounting bodies/day before wrappers. Burst is simultaneous closes,
not the daily average. The final six-row transaction is still ≤331,776 body bytes;
its separate handoff makes ≤335,872 per final pass, each transaction≤2 s, shared
pass≤4 s. Hashing/export preparation/cleanup count in that deadline; no ownership reads exist. CPU/memory/native RPC overhead and
actual final-pass latency are proposed measurement gates, not existing results.

Rejected: DD6 “SEALED_CLOSURE exists, therefore transport exists” (P1 has none);
readback/commit timestamp/P5 time as acknowledgement (lost original reply);
new boot/same revision as original writer (wrong observer); result inside the
six-row transaction (future fact); retrying final close clean (erases failure);
permanently vetoing every unknown marker reply (recursive last-write problem).
A1 instead bounds one handoff attempt, permits recovery of its durable landing,
and conservatively charges its unprovable reply. It never recovers a failed
original final close.

### 7.5 A2 ADD — offline gate report and complete failure contract

**D-A2-1 through D-A2-5, design only.** P1-A2 `shadow-v2-contract-a2`
adds an offline report interpretation to A1. Stored rows, A1 schemas/functions,
baseline fixtures, signature purposes, wire and enclave behavior remain frozen.
A2 objects have exactly `v=2, policy_version="shadow-v2",
contract_revision="a2"`. The unchanged A1 GATE_OUTPUT is the outer envelope
(`contract_revision="a1"`); `coverage_sha256` selects and commits the A2 report.
Do not relabel stored rows or A1 GATE_OUTPUT as revision a2. A2's validator first
calls A1's validator, then enforces the relationships below. A1's standalone
acceptance of an object is not A2 certification.

All schemas below are closed (no extra/default members), JSON-native, duplicate-
key rejecting and bounded by **both** cardinality and serialized bytes. Reuse §7.2
`u`, `boot`, `day`, `sha`, `rev`, `seq`, baseline `name` (1–128 ASCII), `boolean`,
`DIMENSIONS` and `COUNTER_DIMENSIONS`; `s` is signed int64 in
[-9,223,372,036,854,775,807, 9,223,372,036,854,775,807]. Nullable means explicit
JSON null, never absence. All object hashes use `detached_jws.canonical` (sorted
keys, compact separators, ASCII escaping, no NaN, no final LF). Source-file/row
hashes instead include **every original byte**, including whitespace/final LF.
These are proposed definitions, not a claim that the schemas exist (A2-S1–S8).

#### 7.5.1 A2 ADD — Q1: the single GATE_REPORT

`GATE_REPORT` ≤65,536 bytes has exactly the common A2 tags and these fields.
Reserve ≤49,152 bytes for failure_counts and ≤16,384 for all remaining fields/
wrappers together; both and the total are conjunctive. Maximum-value fixtures
must establish fit; these are design limits, not measured serializer results:

| Field | Exact type, bound and meaning |
|---|---|
| `gate`, `completed_at_us`, `proof_manifest_sha256`, `prerequisite_ledger_root` | Same enum/u/sha/sha as outer GATE_OUTPUT; exact equality required. |
| `days` | Sorted unique day array, 1–32. Includes forward-join dates; `window` is INTERVAL for the requested observation window. |
| `window` | Baseline INTERVAL; start < end. Supporting older reset history is not restricted to these dates. |
| `rules` | Exactly keys G1…G11; each `pass\|fail\|unevaluable\|not_required`. Evaluate every rule; stage alone determines not_required by §8. Unevaluable required rules emit their named coverage failure; they cannot PASS. |
| `comparator` | Exactly `{eligible,compared,exact,explained,mismatch,unevaluable,coverage}`. First six are nullable u; coverage is RATIO below. Counts include original and retry attempts, with unchanged comparator arithmetic. |
| `admission` | Exactly `{E,Y,N,U,coverage}`: nullable u, u, u, u, RATIO; all four counts nullable. Independently derived E; Y+N+U=E is tested, never assumed. |
| `partition` | Exactly `{T,A,B,C}` of nullable u, T=settle+refund. |
| `attempts` | Exactly `{authorize_fresh,authorize_replay,terminal_original,terminal_retry,completion}` of nullable u. Authorize/replay/completion are not extra terminal predictions. |
| `checkpoint` | Exactly `{attempts,failures,recoveries,unknown_commits,unreconciled_commits,irrecoverable_losses,evidence_loss}` of nullable u. Recovered failures remain in failures. |
| `handoff` | Exactly `{expected,landed,attempts_lower,attempts_upper,failures_lower,failures_upper,unknown_reply_upper}` of nullable u; sum A1 HANDOFF_ACCOUNTING without claiming exact last-reply outcome. |
| `publisher` | Exactly `{scheduled_slots,claim_attempts,claims,lost_claims,scans,publications,failures,unknown_outcomes,late_cycles,missed_slots}` of nullable u, plus `peaks` of PEAKS type. |
| `fleet_peaks` | PEAKS. Simultaneous sweep and configured-cap projection remain separate; no historical-boot sum. |
| `timing` | Fixed map of STAT objects for `authorize_shadow_us,handoff_prepare_us,router_settle_us,booking_confirm_us,comparator_us,evidence_write_us,workspace_age_us,health_age_us,publisher_cycle_us,publisher_slot_rpc_elapsed_us,close_us,handoff_us,drain_us`. |
| `deltas` | Exactly `{count,null_count,min_micro,max_micro,bins}`: nullable u/u/s/s; bins has the eleven fixed keys `0,+1,-1,+2-10,-2-10,+11-100,-11-100,+101-1000,-101-1000,+>1000,->1000`, each nullable u. Retain signed booked-minus-frozen deltas, including mismatches. |
| `null_counts` | Exactly `{eligibility,comparison,prediction,booking,timing,delta}` of nullable u. `timing` sums null observations over the thirteen named timing series; detailed null counts remain in each STAT. |
| `first_seed_at_us`, `first_reset_at_us`, `first_gap_at_us`, `clock_start_us`, `elapsed_seconds` | Nullable u. First seed is the first qualifying original sample time; clock start obeys §8, not verification time. First reset includes retained history; first gap is earliest affected evidence time (day start for a day-wide gap). Unknown time is null, not completion time. |
| `failure_counts` | Exactly all 58 GAP_NAME keys, each `{count:u,listed:u≤16,complete:boolean,day_counts:array(u,32)}`; array length equals days, position i counts assertions blocking days[i]. Exact distinct (gap,subject) totals; semantics in §7.5.2. Each value ≤768 bytes; the full map including names ≤49,152 bytes. |
| `writers`, `regions`, `failures`, `input_inventory`, `reset_history` | DETAIL references below. All streams are complete except failures, which commits only the deterministic listed prefix; failure_counts commits the exact totals. No per-authorization maps in this object. |
| `verifiers` | Exactly `{certificate,binding,retained_tools}`; certificate and binding are nullable VERIFIER_USE, retained_tools is a sorted array of ≤8 VERIFIER_USE entries. Null/missing required verifier blocks G1. |
| `complete` | Boolean; true only after full evaluation, input closure, output-chain and exact-set checks. False always entails artifact_gap. |

`RATIO` ≤256 bytes is exactly `{state,numerator,denominator}` with state
`evaluated|not_exercised|unevaluable|not_required`, nullable u numerator/denominator.
Evaluated has known denominator >0 and 0≤numerator≤denominator; comparator is
A/E, admission is (Y+N)/E. `not_exercised` has 0/0 **only after** closed known E=0
and the §5 conditions; unevaluable/not_required have both null. No float ratio.
`STAT` ≤512 bytes is exactly `{count,null_count,min,p50,p95,p99,max}` of nullable u;
count counts known observations, null_count counts explicitly null observations.
Percentiles use ceiling rank on sorted known values, as retained in
`shadow_report.percentiles` (A2-S6). With a completely enumerated empty series,
count=null_count=0 and extrema/percentiles null. If enumeration is incomplete,
all fields are null. Histogram bounds alone do not establish exact percentiles:
where only histograms survive, percentiles are null, never bin edges presented
as exact times. Counts/min/max may be supplied only when independently exact.

`PEAKS` ≤4,096 bytes has exactly nullable u fields
`simultaneous_routers,simultaneous_publishers,configured_routers,configured_publishers,
startup_operations_1s,close_operations_1s,export_operations_1s` and the nullable
RATIONAL fields `reads_per_second,pending_rows_per_second,trust_rows_per_second,
health_rows_per_second,claim_operations_per_second,scan_operations_per_second,
publication_operations_per_second,export_operations_per_second,export_bytes_per_second,
configured_reads_per_second,configured_pending_rows_per_second,cpu_percent`.
RATIONAL is `{numerator:u,denominator:u>0}`, ≤128 bytes, reduced by gcd; no float
rounding changes comparisons. Each peak is a maximum over §5.1 sweep segments;
CPU is the measured peak. Missing components stay null.

`WRITER_REPORT` ≤8,192 bytes has common A2 tags, `day,boot,role:router|enclave,
revision:rev,region:nullable(name)`, `interval:INTERVAL`, plus `partition,admission,
comparator,attempts,checkpoint` of the same types above and
`first_seed_at_us,first_reset_at_us,first_gap_at_us:nullable(u)`.
Exactly one per expected role/day/boot (≤32×4,096=131,072); unknown inventory
cannot invent a writer. Enclave comparator/admission values are not_required.
`REGION_REPORT` ≤16,384 bytes has common tags, `region:name`, `writer_days:u`,
`peaks:PEAKS`, `timing` as above, `seed_at_us:nullable(u)`; one per distinct actual
region, ≤131,072, external pages rather than a 128-character-name list in the
report. Unknown region stays on the writer and raises G2; never create an
"unknown" actual region or silently omit a known region. Both detail streams
are complete fixed-shape aggregates, not per-authorization maps.

**Metric observation contract (A2-R3).** These populations apply identically to
GATE_REPORT and REGION_REPORT.timing; regions filter the same observations by
the original producer's validated actual region. A repeated file/export is not
an observation. Deduplicate by the identities below, accepting only identical
bodies; conflicting bodies make the dependent series unavailable, never
latest-write-wins. Do not sum per-region percentiles to form fleet percentiles.
No timing/delta series is a reconstruction of the attempt/comparator counters.

- `S` is the complete retained SAMPLE population in the approved cohort whose
  **original** observed_at_us is in [window.start_us,window.end_us), keyed by
  (authorization_day,authorization_id). Include every classification, including
  mismatch/unevaluable/observed exclusion, with no positive-seed filter. Receipt
  references and sample persistence accounting establish the expected retained
  set; absent required samples make its enumeration unavailable. A proven
  unsampled attempt is not a null sample. The immutable first insertion counts
  once; original, retry and completion references to it add no observations and
  do not replace its fields/time. A distinct first-inserted sample from a retry
  counts once at its own original observed_at_us. Forward reconciliation and
  old reset-support samples outside the interval add no metric observations.
  Region is SAMPLE.deployment.region, checked against original_terminal_receipt
  and independent writer inventory.
- `T` is every original **and retry** settle/refund receipt with event_at_us in
  that same half-open interval, keyed by (day,boot,event_seq). No authorize,
  replay or completion receipt is an extra T observation. For the null counters
  below read the original receipt fields, before completion overlay; later
  completion cannot erase an originally null field. Its independent partition
  reconciliation and the comparator counters still obey §§6/8. Region is the
  receipt writer's actual inventory region.
- `J` is every independently scheduled publisher slot for each contender whose
  slot*2,000,000 microseconds lies in the half-open window, keyed by (day,boot,slot)
  under the A1 pre-submission day-ownership rule. Include missed/lost/failed slots,
  not just publications. Slot-trace batches are containers, not observations.
  All native RPCs/retries in a slot contribute only to its recorded aggregate;
  request originals/retries/completions do not add J observations. Region is the
  contender's actual inventory region.
- `H` is every expected closing router writer-day with
  window.start_us < SHUTDOWN_ENTRY.interval_end_us <= window.end_us, keyed by
  (day,boot), independently derived from shutdown/lifetime inventory. Ordinary
  UTC close, opt-out and shutdown each have one expected close, never one per
  recovered checkpoint or request. `Hterm` is H with termination_applies=true.
  Missing close/result/measurement is unavailable, not an omitted observation.
  Region is the closing writer's actual inventory region.

In the table, `nullable value` means count a present explicit JSON null in
null_count and known values in count (or count nulls for a null_counts field).
`required value` has no legal explicit null: a missing/invalid field is
unavailable, not zero and not a known-null observation. Unavailable enumeration
or required source makes all STAT fields null, all delta fields/bins null, or
the individual null_counts scalar null; keep independently available series.
Known enum values `unknown`, `pending` and `unevaluable` are never JSON null.
The last column names the failure emitted for a required unavailable source,
using its §7.5.2 constructor (original producer W/B/E, or N when unidentifiable),
plus the underlying G1 failure; independently failing predicates still run.
The corresponding rule is unevaluable. At a stage that does not require the
population, its fields are null; this does not change §8's rule requirements
(e.g. G7 still evaluates projected budgets before opt-in). S/T are required
when G5 requires actual evidence, J when G6 does, H/Hterm when G8 or actual
close-budget G7 does. Pre-enable invents none of these observations. A closed
empty required population has
count=null_count=0, zero bins, null extrema/percentiles. Histogram-only inputs
cannot replace any population below or create individual observations.

| Metric (fleet and, for timing, region) | Source object and exact field | Population / original-retry-completion treatment and window | Null/unavailable handling; failure |
|---|---|---|---|
| timing.authorize_shadow_us | SAMPLE.timing.authorize_shadow_us | S | nullable value; eligible_coverage_gap |
| timing.handoff_prepare_us | SAMPLE.timing.handoff_prepare_us | S | nullable value; eligible_coverage_gap |
| timing.router_settle_us | SAMPLE.timing.router_settle_us | S | nullable value; eligible_coverage_gap |
| timing.booking_confirm_us | SAMPLE.timing.booking_confirm_us | S | nullable value; eligible_coverage_gap |
| timing.comparator_us | SAMPLE.timing.comparator_us | S | nullable value; eligible_coverage_gap |
| timing.evidence_write_us | SAMPLE.timing.evidence_write_us | S | nullable value; frozen self-timing field is always null; eligible_coverage_gap |
| timing.workspace_age_us | SAMPLE.admission.workspace_age_us | S | nullable value; eligible_coverage_gap |
| timing.health_age_us | SAMPLE.admission.health_age_us | S | nullable value; eligible_coverage_gap |
| timing.publisher_cycle_us | PUBLISHER_SLOT_TRACE.cycle_us | J | nullable value (missed slot is null); publisher_trace_gap |
| timing.publisher_slot_rpc_elapsed_us | PUBLISHER_SLOT_TRACE.rpc_elapsed_us | J, one **per-slot aggregate elapsed** observation, irrespective of rpc_count | required value (missed slot's recorded zero is a known zero); publisher_trace_gap |
| timing.close_us | CLOSE_RESULT.elapsed_us | H, original close only | required value; writer_closure_gap |
| timing.handoff_us | SHUTDOWN_ENTRY.measurement → SHUTDOWN_MEASUREMENT.result_us | H, the one result handoff | required value; close_handoff_budget_gap |
| timing.drain_us | SHUTDOWN_ENTRY.measurement → SHUTDOWN_MEASUREMENT.final_pass_us | Hterm; whole shadow final pass, including preparation/export/close/result, not a money-drain or individual RPC duration | required value; close_handoff_budget_gap |
| deltas.count, null_count, min_micro, max_micro and all eleven bins | SAMPLE.booked_minus_frozen | S; one signed microcredit delta per retained sample, unchanged by retry/completion references | nullable value; eligible_coverage_gap |
| null_counts.eligibility | SAMPLE.eligibility.observed | S; count exactly explicit null observed-eligibility booleans | nullable value; eligible_coverage_gap |
| null_counts.comparison | TERMINAL_EVENT.comparison | T; count exactly explicit null comparison fields in original receipt bodies | nullable value; terminal_coverage_gap |
| null_counts.prediction | TERMINAL_EVENT.prediction | T; count exactly explicit null prediction objects, including noneligible attempts; unknown prediction enum is not null | nullable value; eligible_coverage_gap |
| null_counts.booking | SAMPLE.booked_micro | S; count exactly explicit null booked amounts, not booking.outcome enums or missing receipts | nullable value; eligible_coverage_gap |
| null_counts.timing | Each of the thirteen timing STAT.null_count fields above | Sum their null observations, with the populations above (not a union of subjects) | null if any summand is unavailable; corresponding source failure |
| null_counts.delta | deltas.null_count | S, same observations as deltas | exact equality including null; eligible_coverage_gap |

`publisher_slot_rpc_elapsed_us` replaces the misleading name `publisher_rpc_us`
in the proposed A2 report only. The frozen A1 trace supplies rpc_count and
rpc_elapsed_us per slot (`A2R/src/trusted_router/async_settle_shadow_v2_a1_health.py:133–138`);
neither divide its total into per-RPC samples nor substitute native-RPC histogram
percentiles. This does not rename A1 fields or alter its RPC-budget checks.

For closed available populations, each S timing count+null_count=|S|,
deltas.count+deltas.null_count=|S|, sum(deltas.bins)=deltas.count, and
null_counts.delta=deltas.null_count. Both J timings have count+null_count=|J|;
the slot-RPC series has count=|J| and null_count=0 (never sum(rpc_count)).
close and handoff count=|H|, drain count=|Hterm|, with zero null_count when
available. SAMPLE's frozen self-timing rule gives evidence_write_us.count=0
and null_count=|S|. If deltas.count=0, min_micro=max_micro=null and all bins=0,
including when every S delta is explicitly null; otherwise the extrema are the
minimum/maximum known signed deltas. null_counts.eligibility and .booking are each ≤|S|;
.comparison and .prediction are each ≤|T|. The latter two need not equal
comparator.unevaluable or admission.U. All-known regional identities partition
each fleet timing population: fleet count and null_count equal the sums of the
corresponding regional counts, and fleet STAT is recomputed from their union.
An observation with unknown region makes that series unavailable in every
REGION_REPORT (G2 regional_roster_unknown at its writer W); known fleet values
remain. It cannot create a fictional region or silently shrink a regional series.
All sum equalities use exact integers; unavailable operands propagate null,
and overflow blocks.

A required rule that cannot be evaluated makes **every dependent aggregate**
null/unevaluable; independently evaluated aggregates remain. For example missing
one writer's counter invalidates fleet T/A/B/C/E totals, not the known writers'
detail. Pre-enable has no invented traffic/cadence counts: those are null and
not_required. Known zero, absent observation and uncomputable total are distinct.
GATE_OUTPUT.elapsed_seconds must remain an integer: use 0 when report elapsed
is null, but only with null clock_start and BLOCKED when clock is required; it
is an envelope sentinel, never a measured zero. Otherwise equal floor of known
continuous microseconds/1,000,000. A2 consumers must read the report state.

#### 7.5.2 A2 ADD — Q2/Q3: exact failure totals, bounded subjects and flat pages

`GAP_NAME` is the **58** distinct literals in the amended §8 table, including
G9's explicitly enumerated retained reset names; 58<128. `GATE_OUTPUT.gaps`
is their sorted unique set for **active failures**, never contextual strings.
G5/G9 share one producer for exclusion_conflict. Resolved historical resets stay
in reset_history; they become active failures only when the retained reset/clock
predicate fails. This does not turn an old resolved mismatch into a permanent
veto or erase its provenance. Verdict is BLOCKED iff gaps is nonempty, including
unevaluable-required-rule and incomplete-run failures; otherwise PASS under the
stage-specific §8 predicate. Complete iff all required evidence and evaluation
are present, independently of whether the verdict passes.

`FAILURE_SUBJECT` ≤768 bytes has exactly
`{scope,day,role,boot,phase,dimensions,object,at_us,revision}`. Scope is
`run|day|writer|bucket|object|reset`; day/role/boot/phase/dimensions/object/at_us/
revision are nullable. Non-null day/boot/rev use existing schemas; phase is
`authorize|replay|settle|refund|completion|worker`; dimensions is baseline
DIMENSIONS for both new bucket subjects and event objects. Retained fixed-array
diagnostics keep their original values; their containing writer-day assertion
is selected by the table below, never a converted DIMENSIONS bucket. No
conversion between a literal "unknown" and JSON null is allowed.
`object` is exactly `{kind:name,key:ASCII[1..512],event_seq:nullable(seq)}`.

**Normative constructors and assertion populations (A2-R1).** The constructors
below set only the listed fields; **every other FAILURE_SUBJECT field is null**.
`L(...)` is the §7.5.4 length-prefixed ASCII tuple encoding, also used here for
virtual keys. No optional enrichment from diagnostics is permitted.

- `N`: scope=run. `D(d)`: scope=day, day=d.
- `W(d,r,b)`: scope=writer, day=d, role=r, boot=b, from independent inventory.
  Writer populations are the union of independently expected and observed
  role/day/boot identities with independently valid keys; disagreement remains G2.
- `B(d,b,p,x)`: scope=bucket, day=d, role=router, boot=b, phase=p,
  dimensions=x. Iterate the union of expected receipt-derived and counter-declared
  baseline DIMENSIONS buckets, including declared zero buckets; p is authorize,
  settle or refund as selected below. Fixed retained COUNTER_DIMENSIONS arrays
  are not converted: array equations use phase_coverage_gap at W, and loss
  diagnostics use evidence_loss_gap at W; a malformed counter uses G1 at O.
- `O(k,x)`: scope=object, object={kind:k,key:x,event_seq:null}, using the
  independently expected complete row kind/key. A non-row file uses the sole
  virtual file kind `a2_file`, key=its exact input-inventory path. Aliases to a
  row use its row identity, not a second file subject. No body-derived owner
  fields are added. Missing/conflicting candidates share the expected identity.
- `E(d,b,f,q)`: scope=object, day=d, role=router, boot=b,
  object={kind:"async_settle_shadow_receipts_v2",key:d+"/"+b+"/"+f,event_seq:q}.
  f and q are 20-digit sequences from verified page/event coordinates; phase and
  dimensions are exactly those of the validated event. E requires independently
  validated schema and coordinates; otherwise use I for that expected position
  (or O if no position is enumerable), including for a dependent rule's failure.
  This is the receipt's producer, never its verifier. Obligations use their own original_receipt
  (including a replay's own receipt), not the fresh authorization's locator.
- `I(d,b,f,i)`: invalid receipt at a known page, scope=object, day=d,
  role=router, boot=b, object={kind:"a2_invalid_receipt",
  key:L(d+"/"+b+"/"+f,"event",decimal(i)),event_seq:null}. i is the **zero-based**
  array position, unpadded decimal with no leading zero except `0`. Page identity
  comes only from its independently expected key. phase/dimensions remain null;
  never take seq, owner or other identity from the invalid event. If the events
  array cannot be enumerated, use `O` of the expected page instead.
- `V(r)`: scope=object, object={kind:"a2_region",key:L("region",r),event_seq:null};
  r is the exact actual region from inventory/policy. This is the REGION_REPORT
  logical child, not a hash of a possibly unavailable report.
- `K(t,r)`: scope=object, object kind `a2_budget`, event_seq=null,
  key=L(t,"fleet","cap") when r is null, otherwise
  L(t,"region",r,"cap"). t is the required BUDGET_ENVELOPE stage `pre` or `post`.
  All limits for that scope form one cap assertion, not one per PEAKS field.
  `A(t)` uses the same kind and key=L(t,"approval") for the envelope approval
  child, including its prerequisite measurement/configuration checks.
- `Q(z)`: scope=reset, at_us=z.at_us, revision=z.revision,
  object={kind:"a2_reset",key:L(time,z.revision,z.reason,z.support.root),event_seq:null},
  where time is `null` for unknown time, otherwise unpadded decimal microseconds.
  z is the normalized RESET_ENTRY after the existing complete-support union;
  its expected support.root identifies even absent support bytes. No source
  file chosen from that support changes this subject. Unknown time is permitted
  only for the retained unresolved counter-mismatch case.
- `P(g,n)`: scope=object, object={kind:"a2_proof",key:L(g,n),event_seq:null};
  g is required stage (gate enum), n is the exact EXTERNAL_REQUIREMENT.name.
  The pilot prerequisite child uses n=`pilot`.

These six virtual kinds (`a2_file`, `a2_invalid_receipt`, `a2_region`, `a2_budget`,
`a2_reset`, `a2_proof`) are exhaustive; no virtual health-basis, slot or batch
subject exists. All existing subject/key byte bounds still apply; inability to
encode a required identity blocks with G1 evidence_schema_gap at N, never a
truncated or hashed substitute.

In the following **one row per name** table, the producer is exclusive. The
population is what is iterated, not just the objects that happened to fail.
`self` in the last column means an unevaluable required assertion emits that
row's name at the same constructor. If its population/identity itself cannot
be enumerated, emit that name once at N in addition to every independently
identified failing subject; counts remain provisional and complete=false under
the existing incomplete-input rule. G1 additionally reports the underlying
missing bytes/schema defect; this is not a second producer of the affected
name. `support` means G9 missing_support at Q (N if no reset can be identified),
not an invented correctness mismatch. Stage-not-required assertions emit none.
Within each row, OR all failing subchecks for a subject into **one** assertion;
finer basis/slot/batch/field detail is diagnostic only and is not counted.
Health-reader subchecks belong to each referencing receipt's reader writer-day;
publisher subchecks belong to the trace/claim producer's scheduled writer-day.
Several uses of a basis in one writer-day still count once for each failing name.
A valid recorded empty/unusable source is evaluated under the unchanged §3
predicate, not automatically an unevaluable assertion merely for being unusable.
For G1, validate a page container and its independently enumerable events
separately: a bad child emits E/I only, not another page failure merely because
recursive page validation also rejected that child. A separately bad page
header/chain still emits O. Unidentifiable run artifacts use N.

| GAP_NAME | Single producer | Assertion population and constructor | Unevaluable |
|---|---|---|---|
| policy_version_gap | G1 | Each expected source/artifact logical object O, or receipt E/I, for its own policy/cohort check; cohort configuration uses its O | self |
| evidence_schema_gap | G1 | Each expected source/artifact logical object O, or receipt E/I, for schema/identity/commitment checks | self |
| artifact_gap | G1 | Each expected source/artifact logical object O for availability/digest/provenance; interrupted/incomplete run N | self |
| writer_roster_gap | G2 | Each scoped day D; all manifest/inventory writer-set disagreements that day | self |
| regional_roster_unknown | G2 | Each inventory/observed writer-day W | self |
| legacy_region_label | G2 | Each inventory/observed writer-day W | self |
| legacy_sample_region | G2 | Each retained sample logical key O | self |
| flag_interval_gap | G2 | Each inventory/observed writer-day W; all four flags/lifetime segments together | self |
| boot_prerequisite_gap | G2 | Each traffic writer-day W; all readiness/ledger prerequisites together | self |
| authorize_coverage_gap | G3 | Each router writer-day W; fresh/replay counts and receipt-chain coverage together | self |
| authorization_link_gap | G3 | Each receipt-derived authorization obligation E at its original_receipt | self |
| authorization_obligation_gap | G3 | Each router writer-day W; projection/count/root completeness together | self |
| authorization_terminal_conflict | G3 | Each receipt-derived authorization obligation E at its original_receipt | self |
| authorization_horizon_gap | G3 | Each receipt-derived authorization obligation E at its original_receipt | self |
| observed_partition_gap | G4 | Each settle/refund bucket B; partition equation and C=0 | self |
| terminal_coverage_gap | G4 | Each original or retry settle/refund receipt E; proof/booking/once-only coverage after valid completion reconciliation | self |
| phase_coverage_gap | G4 | Each router writer-day W; phase/aggregate/retained-array equations together | self |
| exclusion_proof_gap | G4 | Each original or retry settle/refund receipt E; applicable exclusion proofs and original-receipt worker subcodes together | self |
| eligible_coverage_gap | G5 | Each router writer-day W; independent eligible-attempt enumeration and comparator coverage together | self |
| prediction_demand_gap | G5 | Each settle/refund bucket B; independent E and Y+N+U equation/known-demand checks | self |
| prediction_unknown_gap | G5 | Each settle/refund bucket B; unchanged unknown fraction | self |
| observer_cadence_gap | G6 | Each router writer-day W; all observer intervals together | self |
| observer_budget_gap | G6 | Each router writer-day W; all observer read/failure/streak/degraded limits together | self |
| shadow_health_gap | G6 | Each router writer-day W; all required health coverage/usability checks together | self |
| health_source_gap | G6 | Each router writer-day W; all source/isolation checks together | self |
| health_timestamp_gap | G6 | Each router writer-day W; all referenced health bases' O/C/R timestamp checks together | self |
| health_elapsed_gap | G6 | Each router writer-day W; all referenced bases' q/e/deadline checks together | self |
| health_slot_gap | G6 | Each router writer-day W; all reader references and publisher claim/slot/version fences together | self |
| publisher_accounting_gap | G6 | Each router writer-day W; all scheduled/attempted/outcome and operation conservation checks together | self |
| publisher_trace_gap | G6 | Each router writer-day W; all required slots/batches/chains/histogram reconciliation together | self |
| simultaneous_capacity_gap | G7 | Each required budget stage and fleet/actual-region scope K; all simultaneous/configured/startup/churn cap checks together | self |
| pre_enable_budget | G7 | Required pre-enable budget approval/prerequisites A(pre), once per run | self |
| publisher_budget_gap | G7 | Each router writer-day W; all contender/publication RPC/read/write limits together | self |
| fleet_headroom_gap | G7 | Each required budget stage and fleet/actual-region scope K; CPU/headroom/approval/pre-post matching together | self |
| freshness_budget_gap | G7 | Each router writer-day W; all conservative-chain/component-deadline evidence together | self |
| evidence_export_budget_gap | G7 | Run N; complete approved plan, every pass/rerun/cleanup and envelope approval together | self |
| close_handoff_budget_gap | G7 | Each closing router writer-day W; final-pass/result/burst/grace measurements together | self |
| checkpoint_reconciliation_gap | G8 | Each router writer-day W; every checkpoint/page/HWM/recovery check together | self |
| evidence_loss_gap | G8 | Each router writer-day W; all loss/overflow diagnostics together | self |
| writer_closure_gap | G8 | Each expected closing router writer-day W; original close/closure/acknowledgement together | self |
| retry_original_gap | G8 | Each retry or completion receipt E; all original/context/order checks together | self |
| close_result_identity_gap | G8 | Each expected close-result row O(async_settle_shadow_close_result_v2,day+"/"+boot) | self |
| close_result_conflict | G8 | Each expected close-result row O(async_settle_shadow_close_result_v2,day+"/"+boot); all conflicting bodies together | self |
| handoff_coverage_gap | G8 | Each closing router writer-day W; expected result/coverage/accounting together | self |
| hash | G9 | Each retained reset fact Q of reason hash, subject to retained active/reset predicate | support |
| identity | G9 | Each retained reset fact Q of reason identity, subject to retained active/reset predicate | support |
| normalization | G9 | Each retained reset fact Q of reason normalization, subject to retained active/reset predicate | support |
| evaluator_disagreement | G9 | Each retained reset fact Q of that reason, subject to retained active/reset predicate | support |
| counter_mismatch | G9 | Each retained reset fact Q of reason counter_mismatch, including unsampled history | support |
| reviewed_resolution | G9 | Each retained reset fact Q of reason reviewed_resolution; all resolution checks together | support |
| revision_rollback | G9 | Each retained reset fact Q of reason revision_rollback; all rollback checks together | support |
| missing_support | G9 | Each retained reset fact Q whose full provenance cannot be established | self |
| exclusion_conflict | G9 (including G5 detection) | Each retained reset fact Q of reason exclusion_conflict; G5 forwards evidence to this producer | support |
| positive_seed_gap | G10 | Run N | self |
| regional_seed_gap | G10 | Each policy-required traffic-serving router region V | self |
| continuous_window_gap | G10 | Run N | self |
| external_proofs_missing | G11 | Each stage-required external proof P(stage,name); all checks/dependencies/approval together | self |
| pilot_prerequisite_gap | G11 | Each required pilot stage P(stage,pilot); all retained pilot prerequisites together | self |

The table fixes counting units only; §8's predicates, stage requirements and
conservative affected-day rules are unchanged. A coalesced writer-day assertion
blocks the union of the days affected by its failing subchecks, including
forward effects, not just the date displayed in W. Every applicable receipt
predicate still runs; grouping diagnostics does not forgive an invalid receipt.

`GATE_FAILURE` ≤1,024 bytes is exactly common A2 tags plus
`{gap:GAP_NAME,subject:FAILURE_SUBJECT,count:1}`. The counting unit remains one
assertion per distinct (gap,complete subject). Several bad fields within the
same assertion count once; different subjects never collapse. Bucket equations
count assertions, not an inferred number of offending receipts; each applicable
receipt predicate is separately evaluated. G5/G9's shared exclusion_conflict
producer avoids double counting the same assertion.

**Round 2 simplification:** §8's “retain all failures” now means **retain every
failing name, exact counts and exact blocked days; list a deterministic bounded
prefix of subjects**. Evaluate every rule over the complete input, count every
assertion, and retain only the first **K=16** subjects per name in canonical
subject-byte order. Sixteen gives an operator a useful batch of independent
repairs per name while keeping all 58 names' diagnostics in four pages. K is a
fixed listing constant, unrelated to publisher contender K; no verdict depends
on its value. It is not a cap on evaluation, counting, raw evidence or history.

For each name, `failure_counts.count` is the exact total; `listed=min(count,16)`;
`complete=(count==listed)` describes **that subject listing only**. Zero count
has listed=0 and complete=true. `day_counts[i]` is the exact number of distinct
assertions of that name that block `days[i]` under §8, including issue-day and
forward-coverage effects. One assertion can block several days, so their sum
need not equal count. Unknown affected time blocks every possibly affected day;
persistent missing support blocks all scoped days. Run-only stage failures still
block the run; counts are never inferred from the listed subjects. The complete
blocked-day set is exactly the days with any positive day_count (plus no unstated
exceptions). Derive days and day attribution from full evidence and §8, never
from the prefix. Older reset support remains in the complete reset-history stream.

`GATE_OUTPUT.gaps` equals exactly the names with count>0, independently of listed
subjects; PASS requires all 58 counts zero and all stage predicates evaluable.
Each listed entry must be one of the actual first min(count,16) assertions, with
no duplicates. Recompute counts, day counts and prefixes from the complete
input when verifying; a claimed summary alone is not proof. Thus truncating the
**diagnostic list** cannot change BLOCKED to PASS or hide a failing rule. A report
can be complete=true with some listing complete=false: all rules were evaluated,
only diagnostic subjects were capped. An operator loses direct subject detail
beyond K until listed defects are fixed and the gate is rerun; the exact totals,
blocked days and immutable source evidence are retained. This is the smaller
mechanism because a PASS has no failures to list.

For humans only, display a subject in this fixed order as
`gap=<gap>;scope=<scope>;day=<day>;role=<role>;boot=<boot>;phase=<phase>;adapter=<a>;route=<r>;streamed=<s>;kind=<kind>;key=<key>;event_seq=<seq>;at_us=<time>;revision=<revision>`.
Null prints `~`, booleans lowercase, numbers decimal; values are UTF-8 percent-
escaped using uppercase hexadecimal with only ASCII alphanumeric/underscore/
hyphen/dot left literal.
Missing dimensions/object expand to null components. No length-128 constraint
applies to this display. It is never parsed, stored as identity or hashed;
canonical structured subjects are the identity. Thus the supplied legal
138-character prefix witness has no effect on the name bound.

A finite, explicit input inventory and approved export plan are still required.
Drop round 1's 10,000-leaf × 10,000-key total-run ceiling and its physical-object,
reset-subject and all-subject suprema: they existed only to bound a lossless
failure list. Keep the **existing** ≤10,000 explicit keys per export pass/bundle,
≤262,144-byte manifests, ≤32 scoped dates, ≤4,096 writers/day, terminal/event
caps, ≤128 reviewed resolutions and all §7.2 rate/retention/budget limits, because
those bound actual evidence or retained tools. Each additional pass and every
older reset-support/dependency file is enumerated and charged; no recursive or
unplanned input discovery. The finite approved plan, not a new universal leaf
count, bounds the run. Do not restrict old reset history to the observation dates.

Exact totals must fit u; never saturate or wrap. Missing input, interrupted
traversal, overflow or resource exhaustion cannot produce a completed exact
summary: mark report.complete=false, artifact_gap and dependent rules
unevaluable, retain the partial work as provisional, never certify it. Finish
all independently evaluable rules. An incomplete run does not claim complete
failing names/counts; a completed result must. Failure to write a BLOCKED artifact
is an unsuccessful tool run, never PASS. Reserve a run-subject artifact_gap slot
before evaluation; it occupies one of that name's 16 slots by canonical order.

`DETAIL` ≤512 bytes is exactly `{root:sha,count:u,pages:u,tail_sha256:nullable(sha)}`.
Root is R over all entries **in this stream**, sorted by complete key; for failures
these are only the listed entries, never an implied root of omitted subjects.
Empty has R([]), 0, 0, null. Nonempty has C(last page), exact count and page count.
All pages live at deterministic `(stream_path,page_seq)` output keys. stream_path
is the full referring field path plus logical parent keys (including nested
reset/support keys), encoded by §7.5.4's length-prefixed tuple rule; it is not
merely a shared schema name. The validator's stream enum still chooses entry type. There is **no
index tree**, for failure or other A2 detail streams. A verifier already streams
every page, so an index provides no necessary correctness property.

`FAILURE_PAGE,WRITER_REPORT_PAGE,REGION_REPORT_PAGE` have common A2 tags plus
`{page_seq:seq,previous_sha256:nullable(sha),entries:array}`. First previous=null,
later previous=C(previous page); sequence starts 1 and is contiguous. The final
page must match tail_sha256. Entries strictly increase across pages; duplicate
keys reject. Failure key is canonical `[gap,subject]` byte order; writer key
`[day,role,boot]`, region key region. Pack all nonfinal pages to their entry limit;
the final page is nonempty. Limits: failure 255×1,024 bytes +≤1,024 wrapper
=≤262,144 bytes; writer 16×8,192 and region 8×16,384 +≤1,024 wrapper
=≤132,096 bytes. Failure streams have ≤58×16=928 entries and ≤4 pages.
Other detail streams retain their declared per-page entry/byte limits.

Verify complete sequence, tail, R-root, exact counts, keys and expected set
against independently enumerated input. For failures verify exact tallies and
prefix selection as well. Page dropping, swapping, duplication and wrong-subject
substitution all reject. A damaged emitted output is rejected with a new BLOCKED
artifact_gap/evidence_schema_gap result about its input bytes, never silently
repaired in place. No self-input or recursive storage acknowledgement is added.
Output pages belong to the report/output graph, not the proof manifest's input
attachments; this preserves the acyclic commitment.

#### 7.5.3 A2 ADD — Q4: one implementation per verifier, supplied port

Choose (a), a narrow supplied port. Tools supply the actual existing callables;
the pure gate neither imports the binding module nor copies its validation.
The signatures, including keyword-only parameters and return semantics, are:

```
verify_binding(token: str, keys: Sequence[TrustedKey], now: int) -> ShadowClaims
verify_certificate(token: str, keys: Sequence[TrustedKey], expected: Mapping[str, Any],
                   now: int, *, returned_key: tuple[str, str], page: bytes | None,
                   attempt: dict[str, Any], server_attempt: dict[str, Any],
                   booking: str) -> ReceiptVerification
```

These are the existing signatures (A2-S4). ShadowClaims is an opaque structural
return type at the pure boundary; no runtime or TYPE_CHECKING import of its
storage-importing module, no replacement claims/signature implementation.
The pure certificate module/TrustedKey may be imported within the existing
allowlist. Exceptions and C results remain failures; validate_claims alone is
not a verifier. Existing original-receipt/backing checks remain mandatory.

`VERIFIER_USE` ≤1,024 bytes is exactly `{entry_point:name,source_sha256:sha,
fixture_sha256:sha,keyset_sha256:nullable(sha),calls:u}`. Source SHA hashes the
exact defining source file; fixture SHA hashes the exact pinned fixture file
(or fixture manifest) for that implementation. Keyset SHA hashes the exact
named, canonical public trust-descriptor file supplied to that callable, with
all TrustedKey fields and ordered keys; no private material or signed tokens
are exported to reports. The retained-tools port has null keyset. Its entry_point
is the fully qualified function, not a user-chosen label. Fixture/source/keyset
files are registered named input artifacts; reviewed source revision and
transitive dependency files are bound by the external CI proof. A source hash
alone does not prove execution or key selection.

The trusted offline tool must establish callable identity against the imported
function, verify source bytes and fixture pin against the approved release,
provide the exact approved key sequence, and record invocation count. The pure
gate invokes it for every applicable object and independently checks contextual
identity/results. Tests substitute a same-signature always-true function, alter
keys, remove a port and suppress a call; all must block. A recorded self-asserted
hash or prior PASS transcript cannot substitute for these checks. No call is
needed on a genuinely unexercised branch, but both verifier bindings/pins must
still be present for a certification run. Pure imports/construction execute no
I/O; tools own source/file reads and dependency loading. Keep the AST and fresh-
interpreter storage-import guards honest (A2-S5); do not allowlist the binding
module. Moving the binding implementation into P1-A2 is rejected because it
would modify the frozen non-v2 ticket/claims dependency boundary and require
migration/differential proof beyond a port; two implementations are forbidden.

#### 7.5.4 A2 ADD — Q5: preimages, attachment shapes and delegation

There are only three digest constructions in A2:

- **F(file/row):** SHA-256 of the named file's or row's exact retained bytes,
  including final LF if present. Files resolve through the finite input inventory;
  rows use complete `(kind,key)`. No parse/re-serialize step before F.
- **R(entries,key):** SHA-256 of canonical JSON array of entries sorted by their
  complete logical key, unique keys required. Empty root is SHA-256 of the two
  bytes `[]`. Stream the canonical array (including brackets/commas); pagination
  must not change this root. New roots use this entry-array construction.
- **C(object):** SHA-256 of the canonical bytes of the named exact-schema object.
  No implicit projection, unspecified concatenation or opaque digest string.

**A1 exception retained, not repaired:** `coverage_root` validates the chain and
returns SHA-256(canonical([C(page1),…,C(pageN)])); its array entries are **page
digests in page_seq order**, not coverage entries. The obligation roots instead
hash sorted AUTHORIZATION_OBLIGATION entries (and their unresolved subset).
They are **not the same preimage construction** and repartitioning coverage
pages changes the former (A2-S2). Both use R's canonical-array hashing operation,
but the frozen coverage projection/key is explicitly page-digest/page_seq;
only frozen A1 coverage fields below use it. A2 does not call coverage_root on
entry arrays or change its behavior. This is the concrete reason for correcting
the candidate's assumption that A1 already has a single entry-root construction.

`ROOT_ENTRY` is A1 COVERAGE_ENTRY, unchanged: `{key,sha256,bytes,state}` and its
1,024-byte/exact-state rules. For a present source sha256=F(original bytes),
bytes=len(original bytes); a missing expected key has null hash/0 bytes, not an
empty-body digest. Conflict coverage retains every candidate in the input
inventory, qualified by its exact byte digest, and blocks. No latest-write-wins.
For **every new ROOT_ENTRY stream** in the table below (day counters, closures,
receipts, day/union ledgers and original slot-trace batches), group retained
candidates by the table's complete logical key **before** constructing entries.
Byte-identical repeated exports/references coalesce within that logical key,
never across different keys. With two or more distinct
exact bodies, emit exactly one entry at the unchanged logical key with
`state="conflict"`, `sha256=SHA-256(canonical(sorted([F(b) for b in bodies])))`,
and `bytes=sum(len(b) for b in bodies)`. The SHA preimage is the canonical array
of lowercase 64-character exact-byte digests, sorted in ASCII order, without LF;
it is C of that array, not F of a selected body or a concatenation. `bytes` is
all distinct candidates' original byte sizes, **not** this array's encoded size.
Distinct bodies with the same digest cannot be certified: retain both and block
artifact_gap; digest collision or int64 size overflow makes this entry/root
unevaluable, never choose a body or wrap a size.

The entry's complete sort key remains exactly the decoded typed tuple in the
digest table; there is no digest tiebreaker or extra root key. Exactly one entry
per independently expected logical key is required, missing/present/conflict
inclusive. ROOT_ENTRY stream counts count logical keys; input inventory object
counts count distinct retained candidates/files. Every candidate must appear
in the finite input inventory under a digest-qualified path and resolve to its
exact bytes; repeated references to it do not add a candidate. Unexpected keys
remain inventoried and fail the expected-set check; they do not expand the
expected root population. The conflict entry does not satisfy a present-object
predicate, and its candidates cannot supply a selected metric value. Missing
candidate bytes makes the root unverifiable. These rules also apply wherever
ROOT_ENTRY_PAGE carries one of these streams; other DETAIL entry schemas and
frozen A1 coverage streams keep their specified constructions.

This representation fits the **unchanged** reference
`A2R/src/trusted_router/async_settle_shadow_v2_a1_evidence.py:117–127`:
conflict permits a non-null digest and positive int64 byte count; no body-size
or digest-preimage constraint there selects one candidate. The 1,024-byte entry
bound still applies. Frozen COVERAGE_ENTRY and coverage_root (:140–151) are
untouched; this is an A2 interpretation of the new entry-array streams only.
The literal §11 conflict entry was also accepted by that frozen COVERAGE_ENTRY
validator in a read-only schema calculation (133 canonical bytes); this is not
an implementation/suite pass.
New ROOT_ENTRY.key encodings use length-prefixed ASCII tuple components:
join `decimal_byte_length:component` with `/`, no leading zero in a length.
Components use their existing safe field schemas; integer components use canonical
decimal and seq keeps its 20 digits. Require a unique parse, exact re-encoding
and ≤512 bytes; compare decoded tuples with the types/order specified below.
This avoids delimiter ambiguity and embedded JSON quoting in the frozen A1
COVERAGE_ENTRY string validator. A1 coverage keeps its frozen key spelling and order.

| Digest field | Construction and complete preimage/key |
|---|---|
| Day MANIFEST.counter_root | R(ROOT_ENTRY array for exactly all expected router counters that day, key=(day,boot)); each value hashes the exact counter row. |
| Day MANIFEST.closure_root | R(ROOT_ENTRY for every expected router closure, key=(day,boot)); exact closure row, not SEALED_CLOSURE. |
| Day MANIFEST.receipts_root | R(ROOT_ENTRY for all immutable receipt pages through each writer's committed HWM, key=(day,boot,first_seq)); exact page bytes. Validate page chains and event coverage too. |
| Day MANIFEST.prerequisite_ledger_root | R(ROOT_ENTRY for every ledger entry required by that day's roster, key=(role,boot,revision,region,boot_at_us)); exact A1 PREREQUISITE_LEDGER bytes including predecessor link. |
| Day MANIFEST.independent_inventory_sha256 | C(INDEPENDENT_INVENTORY for that day). |
| Day MANIFEST.close_result_root, publisher_trace_root, health_basis_root | Frozen A1 coverage_root over that day's respective COVERAGE_PAGE stream; each present entry hashes exact CLOSE_RESULT, publisher trace batch or health basis batch bytes. Validate all expected objects from lifecycle/slots/references. |
| PROOF_MANIFEST.evaluator_fixture_sha256 | F(`tests/fixtures/async_settlement/billing_v1.json`), exact frozen evaluator fixture bytes (A2-S8). |
| PROOF_MANIFEST.binding_fixture_sha256 | F(`tests/fixtures/async_settlement/shadow_v1.json`), exact frozen binding/comparison fixture bytes (A2-S8). |
| PROOF_MANIFEST.wire_fixture_sha256 | F(`tests/fixtures/async_settlement/shadow_v2/manifest.json`), baseline P1 wire fixture manifest. The combined A1/A2 release pin is separate; it does not replace the baseline wire identity. |
| PROOF_MANIFEST.ledger_root | R(ROOT_ENTRY for the union of all required ledger entries, same complete key as day ledger root; repeated references across days coalesce only when the original bytes agree). |
| PROOF_MANIFEST.shutdown_inventory_sha256 | C(SHUTDOWN_INVENTORY). |
| PROOF_MANIFEST.cohort_seed_policy_sha256 | C(COHORT_SEED_POLICY). |
| PROOF_MANIFEST.pre_budget_sha256, post_budget_sha256 | C(BUDGET_ENVELOPE with stage pre/post respectively), committing inputs, exact outputs and approvals, not a PASS string. |
| PROOF_MANIFEST.publisher_slot_trace_sha256 | R(ROOT_ENTRY for every original slot-trace batch in scope, key=(day,boot,first_slot)); exact batch bytes. It is not an alias for the page-sensitive A1 publisher_trace_root. |
| PROOF_MANIFEST.external_proofs_sha256 | C(EXTERNAL_PROOFS). |
| PROOF_MANIFEST.resolution_dag_sha256 | C(RESOLUTION_DAG), including retained reset-support references. |
| PROOF_MANIFEST.close_result_root, publisher_trace_root, health_basis_root, handoff_accounting_root, authorization_obligation_root | R of exactly `{day,root,count}` entries sorted by day, root=that day's frozen A1 coverage_root for the named stream; count=total underlying coverage entries. Stream is determined by field, not supplied by caller. The authorization stream covers every original A1 OBLIGATION_PROJECTION and AUTHORIZATION_OBLIGATION_PAGE; validates projection roots against complete receipt-derived entries. This cross-day envelope does not alter A1 page bytes. |
| GATE_OUTPUT.proof_manifest_sha256 | F(the exact supplied A1 PROOF_MANIFEST file bytes); report uses the same digest. |
| GATE_OUTPUT.prerequisite_ledger_root | Exactly PROOF_MANIFEST.ledger_root, computed from that union, not the hash of the hex string. |
| GATE_OUTPUT.coverage_sha256 | C(the one GATE_REPORT). |

The three repository release files named by PROOF_MANIFEST.evaluator_fixture_sha256, binding_fixture_sha256 and wire_fixture_sha256 are the sole exception to the finite input-inventory resolution rule for F files: they resolve at the fixed paths in the table in an immutable checkout independently verified to be the approved source_shas.router revision. The trusted offline tool must hash each file's exact original bytes, including any final LF, and require equality with both the corresponding proof-manifest field and that fixture's independently approved repository release pin; the source revision and pin approval must be established by the retained external CI/release provenance, not by self-asserted manifest values or agreement between two editable checkout files. These three files are release dependencies, not exported evidence or INPUT_LEAF members, require no FILE_REF, and are exempt from FILE_REF.bytes and attachment-size limits; their reads and verification work remain included in the finite approved verification plan. This same exception applies when one of these exact files supplies VERIFIER_USE.fixture_sha256, overriding only that file's input-inventory registration requirement in §7.5.3; all other verifier source, fixture, keyset, dependency and evidence/support files retain their existing inventory, provenance, completeness and bound requirements. Missing bytes or an unverified/mismatched revision, release pin or manifest digest blocks through G1 artifact_gap and the affected rule; no regeneration, parse/re-serialization, page-root substitution or fallback file may supply the preimage. The baseline wire fixture identity remains separate from the combined A1/A2 release pin.

A1's inner `open_obligations_root` remains R(unresolved AUTHORIZATION_OBLIGATION
entries,(original_receipt.day,boot,event_seq)); `reconciliation_root` remains R(all
entries,same key). A1 inner closure/counter/result digests keep canonical-object
semantics where its validators use sha256(object); do not change them to F just
because export ROOT_ENTRY hashes original row bytes. Both comparisons must pass.
Day `proof_manifest_sha256`, when present, is the same F as the output. Day
config/fixture and ledger policy/config/fixture/artifact SHA fields use F of
named exact config/policy/fixture/ARTIFACT files in the input inventory. A ledger
previous link is F of the previous exact ledger file. Completion times in
ARTIFACT must equal their decoded evidence, not only the descriptor. No ZERO
placeholder or digest with an undefined, absent or unverifiable preimage can
support PASS: undefined interpretation raises **evidence_schema_gap**; missing
or mismatched bytes raises **artifact_gap**; also retain the affected G2–G11
failure. The baseline generator's ZERO fields are schema fixtures, not a
passing end-to-end gate (A2-S3).

The following new payloads are minimal offline envelopes. Each has common A2
tags, is ≤262,144 bytes, and has exactly the fields below. `FILE_REF` ≤768 bytes
is `{path:ASCII[1..512],sha256:sha,bytes:u≤262144}`; path is a named **relative**
input-inventory key restricted to `[A-Za-z0-9_./:@+-]`, with no absolute path,
traversal, network URL or secret. Larger
source artifacts use separately bounded pages with a DETAIL; no giant embedded
JSON/base64 blob. `APPROVAL` ≤1,024 bytes is `{subject_sha256:sha,reviewer:name,
reviewed_at_us:u,decision:"approved",record:FILE_REF}`; subject is the exact
approved input/config file digest, and record's existing reviewed release
provenance must substantiate it. This is not a new signing protocol or a
self-certifying approval string. Unknown/missing review authority fails G7/G11.

| Schema | Exact additional fields, completeness and delegation |
|---|---|
| `INPUT_LEAF` | `{keys:array(FILE_REF,10000),complete:boolean}`; unique sorted path, conjunctive byte bound. File paths for rows encode kind/key and, on conflict, body digest. Receipt events are subobjects, not extra GETs. |
| `INPUT_INVENTORY_ENTRY` | `{bundle:FILE_REF,first_key:ASCII[1..512],last_key:ASCII[1..512],objects:u}` ≤2,048 bytes; one per explicitly planned leaf bundle, sorted by first_key, disjoint ranges. Report input_inventory is R of these entries; paginate 64/page using the common page envelope. All derived expected keys and dependency files must be covered. |
| `INDEPENDENT_INVENTORY` | `{day:day,serving:FILE_REF,deployment:FILE_REF,approval:APPROVAL,complete:boolean}`. serving contains the **retained v1 serving roster shape**, including retired/overlapping boots and exact intervals/pins. Tools delegate roster validation to pilot_enablement.roster_coverage and gate_roster_coverage; compare deployment source/approval, A1 day rosters, actual regions, lifetimes, ledger and four-flag intervals. New strict day/boot/revision/region bounds additionally apply. Never infer inventory from counters. |
| `SHUTDOWN_INVENTORY` | `{writers:DETAIL,measurements:FILE_REF,approval:APPROVAL,complete:boolean}`. `SHUTDOWN_ENTRY` ≤2,048 bytes is `{day,boot,termination_applies:boolean,reason:utc_boundary\|shutdown\|opt_out\|hard_kill,interval_end_us:u,grace_us:nullable(u),original_close:nullable(FILE_REF),close_result:nullable(FILE_REF),measurement:FILE_REF}`. Sorted (day,boot), ≤131,072, 64/page. Missing close has null original_close; its expected complete key is derived from day/boot and raises writer_closure_gap, never an invented digest. Termination requires known grace and measured full-pass durations from the referenced evidence, not just elapsed of the close RPC. A1 validate_close_result/sealed_closure/HANDOFF_ACCOUNTING own the actual close rules. Hard kill/missing result never becomes graceful by this envelope. |
| `COHORT_SEED_POLICY` | `{workspace_fingerprints:array(sha,32),regional_seed_policy:each_traffic_region\|fleet_only,required_regions:array(name,4096),primary_region:name,maximum_publishers:u,configuration:FILE_REF,approval:APPROVAL}`. Arrays sorted unique, workspaces nonempty; expected traffic regions derived from complete inventory must equal required_regions for the default each_traffic_region policy. Fleet_only is not the accepted §13 policy; changing to it requires a new owner decision, never an empty-array bypass. The first policy pins one dedicated eligible-only probe workspace, both routes, one supported candidate/request, fallback off and actual router seed regions through configuration; G1 checks this cohort restriction. Pre/post N equals workspace count; K, primary placement, config and readiness must match. |
| `DELEGATED_PAYLOAD` | `{entry_point:name,payload:FILE_REF,dependencies:DETAIL,source:FILE_REF}`. Entry point is one of the eight literal functions named below; exact original payload shape/finite binary64 values are delegated, never normalized into a second v1 schema. Dependencies enumerate every opened local file by FILE_REF, sorted path, all charged to the finite input/export plan (≤10,000 keys per pass). Tool paths are safely materialized to this finite sandbox; hidden files or network reads reject. |
| `BUDGET_ENVELOPE` | `{stage:pre\|post,retained:DELEGATED_PAYLOAD,output:FILE_REF,pre_envelope_sha256:nullable(sha),approval:APPROVAL,v2_load:LOAD_EVIDENCE}`. pre has null link, post points to C of the exact approved pre envelope. Delegate retained shape/checks to fleet_budgets_pre or fleet_budgets; preserve workspace/cap/allowance matching and approved measurement artifact bytes. Actual output is the delegated v1 tool output and must be recomputed, not trusted; it is not this run’s A2 GATE_OUTPUT, avoiding a self-reference. LOAD_EVIDENCE supplies the explicitly superseding v2 costs/sweep below. |
| `LOAD_EVIDENCE` | `{interval:INTERVAL,segments:DETAIL,operations:DETAIL,limits:FILE_REF,approval:APPROVAL}`. `LOAD_SEGMENT` ≤8,192 bytes has `{day,boot,region:name,start_us:u,end_us:u,role:router\|enclave,contender:boolean,workspace_count:u≤32,peaks:PEAKS}`; sorted (day,boot,start_us), no overlap for same boot; complete lifetime coverage. `EXPORT_OPERATION` ≤2,048 bytes is `{object:FILE_REF,pass:u,at_us:u,operation:get\|put\|delete,native_rpcs:u,bytes:u,elapsed_us:u}`; sorted (pass,at_us,operation,object.path), unique, bounded by the approved finite operation count (u) including reruns/deletes; operation entries themselves are enumerated and charged when exported. Derived report-output operations are charged by the acyclic plan, not embedded back into their own input. Rates/bursts must be computed from operations and reconcile producer traces; scalar asserted peaks alone never suffice. Limits file is exactly `{regions:array({region:name,approved:PEAKS},4096),fleet:PEAKS}` with known applicable limits and exact pre/post equality; peaks use their declared units. This augments, never raises, v1 approvals. |
| `EXTERNAL_PROOFS` | `{requirements:array(EXTERNAL_REQUIREMENT,64),pilot:nullable(DELEGATED_PAYLOAD)}`. Requirement ≤2,048 bytes is `{name:name,stage:gate_enum,router_revision:rev,enclave_revision:rev,evidence:FILE_REF,approval:APPROVAL}`. Unique (stage,name); required set is all eleven retained EXTERNAL_GATES plus §11 v2/A1/A2 proof rows, at their required stage and package scope fixed by §11 (P4b integration required before excluded-traffic opt-in), not a caller-chosen subset. Deferred P4b proof rows do not remove frozen verifier/schema/purpose or P4a fail-closed proofs from the first window. Evidence must be exact-revision CI/export/attestation with its named criterion and successful results, verified by tools and retained proof logic. pilot delegates pre_flip/post_flip at those stages, otherwise null. No digest-only PASS. |
| `RESET_ENTRY` | `{at_us:nullable(u),revision:rev,reason:hash\|identity\|normalization\|evaluator_disagreement\|counter_mismatch\|reviewed_resolution\|revision_rollback\|exclusion_conflict,support:DETAIL,resolved_at_us:nullable(u),fixed_revision:nullable(rev),invalidated_at_us:nullable(u)}` ≤2,048 bytes. Key (at_us,revision,reason,support.root); unknown time sorts before known and blocks. support enumerates exact source FILE_REFs (or derived rollback's serving/resolution references), not a sample. One per retained reset fact, duplicate facts combined only with complete support union. Complete finite history from the approved input plan; no round-1 synthetic reset-entry ceiling. The retained ≤128 reviewed resolutions still applies. |
| `RESOLUTION_DAG` | `{retained:DELEGATED_PAYLOAD,history:DETAIL,approvals:array(APPROVAL,128)}`. Retained payload supplies the unchanged `resolved_mismatches` shape (≤128 entries), serving tuples and manifests to collect_rollbacks. Retained report validation owns exact resolution fields, uniqueness and support requirement; use that validator via the tools, not collect_rollbacks alone. history points to RESET_ENTRY entries in common ATTACHMENT_PAGE envelopes (64 entries/page, ≤132,096 bytes/page; every nonfinal page has 64 entries). Compare all supplied history before window filtering; missing raw support is persistent missing_support. Resolve only with reviewed artifact and serving_since, exact fixed revision and retained DAG reachability, never SHA lexical order. |

`PROOF_RESULT` ≤262,144 bytes is the required wrapper when no retained tool
already owns a proof payload: common tags plus `{criterion:name,router_revision:rev,
enclave_revision:rev,completed_at_us:u,producer:name,checks:array({name:name,
result:pass|fail,evidence:FILE_REF},128),sources:DETAIL}`. Check names must exactly
match the selected §11 criterion's frozen P5 fixture inventory; all checks must
pass and reference verified exact evidence. Empty/self-asserted checklists fail.
Tool validation binds producer/source CI or attestation provenance to the approved
release; the wrapper does not manufacture trust. `REVIEW_RECORD` is common tags
plus `{subject_sha256:sha,reviewer:name,reviewed_at_us:u,decision:approved|rejected}`,
≤1,024 bytes; match APPROVAL and the approved reviewer/provenance source in the
release inputs. A2 does not introduce a new approval authority.
`SHUTDOWN_MEASUREMENT` is common tags plus `{day,boot,grace_us:u,
final_pass_us:u,close_us:u,result_us:u,export_wait_us:u,preparation_us:u,
outcome:graceful|hard_kill,source:FILE_REF}`, ≤2,048 bytes; measured total includes
all work and must fit both grace and §6.1's four-second pass ceiling as applicable.
Measurements and every FILE_REF have to be available, with independent source
provenance; a numeric assertion without its source is artifact_gap.

`ROOT_ENTRY_PAGE` is the common A2 page envelope with 128 ROOT_ENTRY entries;
`ATTACHMENT_PAGE` uses the same envelope with 64 entries of one declared schema
(the stream schema is determined by its referring field). Byte limit 132,096;
FILE_REF dependency lists use 128 entries/page (≤99,328 bytes). All DETAIL streams
use the same chain/tail/count/root validation and cannot silently change entry
type. These declarations supply the referenced payloads' entire schema; opaque
measurements/review/CI files have their retained validator/criterion named in the
referring envelope. If no approved parser can establish that criterion, the run
is unevaluable with evidence_schema_gap and the applicable rule's gap, not PASS.

**Delegation boundary and retained deltas.** The eight retained entry points are
`pilot_enablement.fleet_budgets_pre`, `fleet_budgets`, `shadow_serving`, `pre_flip`,
`post_flip`, `counter_coverage_report`, `roster_coverage`, and
`shadow_report.collect_rollbacks`. Each uses its existing signature (A2-S6/S7);
DELEGATED_PAYLOAD for collect_rollbacks additionally supplies exact legacy
`report` inputs so report's resolution/support validation runs. Tools call the
existing functions; no v1 parser or signature checker is forked into the pure
family. The existing functions are **not** whole-v2 predicates. P5's explicit
retained-delta adapter must compare their outputs and apply only already approved
§1.1/A1 deltas: per-writer known>0→E-based rule, historical sum→simultaneous sweep,
v1 health timing→A1 timing, global prerequisite/seed time→§5.3/§8, v1 row/policy
shape→A1 validators. Untouched checks, including approval/N/cap identity, roster,
CPU<45, post-flip rollback/handoff and reset support, remain delegated and required.
Do not demand v1 overall PASS on legitimate idle/sequential A1 inputs; do not
mask any additional failure. A2 adds no new predicate exception. The adapter's
source/fixtures are pinned as retained_tools VERIFIER_USE and differential
proofs must exercise each permitted delta and every untouched checklist bit.
Retained shape payloads remain v1 shape inside the envelope; no A1 rows are
passed to an unchanged v1 row validator. This adapter is P5 tool work, not a
runtime import or new validator implementation in P1-A2.

#### 7.5.5 A2 ADD — bounded output/export arithmetic

All figures are design arithmetic, not measured serialization/RSS/CPU or test
results. Exact evaluation still reads all relevant evidence. Stream assertions
in unique canonical key order (bounded external sort/spill if needed), maintain
58 exact totals, 58×D day counters and at most 928 listed subjects; never allocate
an all-failure dictionary. Sort/re-read/temp bytes require §7.2 approval.

| Item | Round-2 bound / arithmetic |
|---|---|
| Closed names / listing constant | 58 (<128 frozen names); K=16 subjects/name |
| Per-name tallies | 58 exact totals and ≤58×32=1,856 exact day counts; listed≤16, complete boolean |
| Failure count map / report | ≤49,152 bytes within one ≤65,536-byte GATE_REPORT; remaining fields/wrappers ≤16,384 |
| Listed failures | ≤58×16=928; ≤1,024 bytes/entry ⇒950,272 entry-body bytes |
| Failure pages | ceil(928/255)=4; each ≤262,144; exact conservative total ≤950,272+4×1,024=954,368 bytes |
| Failure/report/outer package | ≤954,368+65,536+262,144=1,282,048 bytes (1.283 MB decimal); ≤6 PUTs including report and outer |
| Failure indexes / all-subject bound | 0 index nodes/bytes/operations; no all-subject supremum needed |
| Source limits retained | ≤32 dates, ≤4,096 writers/day, ≤100,000 terminals/day, ≤400,000 events/day, ≤144 buckets/writer-day; these bound evidence, not diagnostic listing |
| Export input plan | Finite approved list of passes; ≤10,000 explicit keys/pass and ≤262,144 bytes/manifest; no new global leaf/object ceiling |
| Writer reports | ≤131,072 entries ×8,192=1,073,741,824 bytes; ≤8,192 pages; wrappers ≤8,388,608 ⇒1,082,130,432 bytes; 0 indexes |
| Region reports | Conservative inherited ≤131,072 entries ×16,384=2,147,483,648 bytes; ≤16,384 pages; wrappers ≤16,777,216 ⇒2,164,260,864 bytes; 0 indexes. First cohort's configured GCP router inventory fits one page; actual inventory governs |
| Other detail pages | Writer/region/root/attachment envelopes ≤132,096 bytes; dependency FILE_REF pages ≤99,328; manifests/source attachments ≤262,144, unchanged |
| Offline operations | Every pass L+W; coarse body bytes ≤262,144(L+W); native RPCs, spill/sort, verification/reruns, report and cleanup all charged |
| Producer lanes | Unchanged Σ(1+floor(active_seconds/5)) plus initial in-flight; ≤65,536 bytes/operation; does not pay for offline export |
| Cleanup | ≤1,000 sink keys/s, ≤30 s/pass; Spanner ≤200 keys/transaction, ≤5 transactions/s, ≤30 s/pass; additional passes charged |
| A2 runtime cost | 0 producer RPCs, request-path bytes, money operations, DDL/indexes or enclave runtime changes |
| Enclave fixture follow-up | Exactly 1 combined A1+A2 pin-only release |

The **failure diagnostic worst case is megabytes**, independent of the number
of failed subjects. The complete raw input, writer aggregates and reset history
are separate evidence costs, not bounded diagnostic samples; the conservative
writer/region maxima above are not first-cohort capacity claims or approvals.
Do not silently sample them to make the whole evidence export fit a megabyte.
Keep one ≤256 KiB decode buffer and bounded page/merge state; native overhead
and actual remaining-report fit need implementation proof.

PROOF_MANIFEST.attachments retains its conjunctive 10,000-count/256 KiB limits;
further input-leaf references live in authenticated input_inventory. Complete
input closure is independently derived from inventory, HWM, scheduled slots and
references, never from listed outputs. All referenced bytes must be available.
L counts all input/support/verifier GETs; W counts all flat detail/root/report/
output PUTs. Each rerun/verification pass and every cleanup key is charged.
P5 plans actual finite operations/frequency before execution; P6 approval and
G7 evidence_export_budget_gap reject unaffordable plans. No allowance increases.


## 8. Gate v2 — executable predicate specification

**D-V2-8 — one shared predicate implementation.** `shadow_report` and pilot
`fleet-budgets`, `shadow-serving`, `pre-flip` must call the same versioned
validators, not subtly different copies. **A2 BEFORE:** gaps were described as
stable suffixes prefixed by day/boot/phase/dimension or object ID.
**A2 AFTER:** gaps are the 58 closed GAP_NAME literals in this table, without
prefixes; every failure has a structured §7.5 FAILURE_SUBJECT. Evaluate all rules
and retain every failing name, exact counts and exact blocked days; list a
deterministic bounded prefix of subjects (K=16/name) under §7.5.2. Complete input
closure and full rule evaluation remain mandatory. GATE_OUTPUT.gaps equals the
sorted names with positive exact count; BLOCKED iff nonempty.
Schema damage/unevaluable required predicates produce failures, never skipped
rows or zero metrics. A2's report verification additionally validates all output
pages and verifier provenance before any PASS is accepted.

| Rule | Exact PASS predicate | Replaces/retains v1 rule | Gap/reset name |
|---|---|---|---|
| **A2 AFTER G1** Policy/schema | All certification rows, proof, wire provenance and gate artifacts exactly shadow-v2 with their frozen/A2 revision as specified in §7.5, bounded and hash-verified; accepted cohort/seed configuration matches (first cohort requires T=A, B=C=0 when closed cohort traffic is evaluated; pre-enable does not invent traffic totals); key/body identity agrees; no coercions; referenced objects present. artifact_gap includes missing/mismatched report/page/input/support/verifier bytes, incomplete/bound-exhausted export or absent verifier. evidence_schema_gap includes undefined preimage, invalid subject/name/order/count/chain, gaps↔exact-counts or listed-prefix mismatch, null-as-zero, substituted/unapproved verifier or wrong key set. Keep each affected rule’s own failure as well | shadow §§4/6 exact v1 schema (C7) replaced, strictness retained | `policy_version_gap`, `evidence_schema_gap`, `artifact_gap` |
| G2 Deployment | Complete independent boot/lifetime inventory equals all day manifests/counters/closures; full revisions and #1667 per-boot regions; all four flags off throughout; every traffic boot has §5.3 readiness before its first opted attempt; ledger hash chain complete | shadow §6 / outbox step 5 global gate timestamp and identity coverage (C8/C12) | `writer_roster_gap`, `regional_roster_unknown`, `legacy_region_label`, `legacy_sample_region`, `flag_interval_gap`, `boot_prerequisite_gap` |
| **A1 AFTER G3** Authorize obligations | Fresh+replay equation equals the complete durable decision receipts/facts. Closure commits all report obligations through §7.3; writer closure/HWM is not discharge. P5 enumerates open count/root and reconciles every authorization only to actual durable terminal evidence within its original horizon, regardless of router boot/enclave. No nonterminal disposition or same-enclave claim. Provisional through forward coverage; missing/invalid/conflicting terminal blocks issue day | Replaces unspecified owner/link permission with terminal-only report discharge; all original factual backing rules retained | `authorize_coverage_gap`, `authorization_link_gap`, `authorization_obligation_gap`, `authorization_terminal_conflict`, `authorization_horizon_gap` |
| G4 Terminal partition | T=settle+refund=A+B+C per writer/day/phase/dimension; C=0; A has compared proof, B has valid certificate or retained observed-exclusion proof; each receipt exactly once; phase sums equal aggregate; no envelope-present=T assumption | shadow §§3/4/6 and C7 envelope/observed/outcome equations | `observed_partition_gap`, `terminal_coverage_gap`, `phase_coverage_gap`, `exclusion_proof_gap` |
| G5 Eligible comparisons/predictions | Every eligible attempt represented in E independently of predictions; every A exact/explained with unchanged arithmetic/provenance and booking; Y+N+U=E, E>0⇒Y+N>0 and 100U≤2E; E=0⇒Y=N=U=0 with known closed partition; no unknown seed | shadow §5 per-counter known>0 and C7 known_exclusion admission requirement | `eligible_coverage_gap`, `prediction_demand_gap`, `prediction_unknown_gap`; correctness classes and `exclusion_conflict` cause resets |
| **A1 AFTER G6** Timer/health | Observer cadence/failure/streak/degraded gates and source isolation; exact Spanner O/C/R plus q/e predicate, slot/version fences and 250 ms health-read deadline (§3.1.1). Independently reconcile scheduled/attempted/published/failed/unknown/missed counters and traces, including queued old-day work. Usable late-landed rows never count as timely publications | Removes only missed-cycle⇒invalid-publication implication; strengthens shadow read deadline and server observation-slot bound; no host qualification | `observer_cadence_gap`, `observer_budget_gap`, `shadow_health_gap`, `health_source_gap`, `health_timestamp_gap`, `health_elapsed_gap`, `health_slot_gap`, `publisher_accounting_gap`, `publisher_trace_gap` |
| **A1 AFTER G7** Fleet load | Simultaneous/cap/startup/churn sweep, exact approvals, strict CPU<45%; include metadata, health attachments with full §7.2 operations/re-runs/cleanup, report joins and separate close-result transactions/bursts. Conservative 4.25+.25=4.50 chain and measured component deadlines; over-budget cycles remain failures | No load allowance increases; removes ownership/signature/clock-service budgets because those mechanisms are absent | `simultaneous_capacity_gap`, `pre_enable_budget`, `publisher_budget_gap`, `fleet_headroom_gap`, `freshness_budget_gap`, `evidence_export_budget_gap`, `close_handoff_budget_gap` |
| **A1 AFTER G8** Durability | Page/HWM/counter/no-loss rules and same-writer terminal-original completion context/order. Require original boot’s insert-only acknowledged CLOSE_RESULT, exact closure/day/boot/digest/checkpoint binding and independent inventory/coverage under §2 provenance. Failed original close never recovers. Unknown handoff reply can recover only landed fact; charge conservative bounds. Writer sealing never bypasses G3 | Defines durable later acknowledgement without a separate signing trust domain; ordinary checkpoint recovery unchanged | `checkpoint_reconciliation_gap`, `evidence_loss_gap`, `writer_closure_gap`, `retry_original_gap`, `close_result_identity_gap`, `close_result_conflict`, `handoff_coverage_gap` |
| **A2 AFTER G9** Resets | Zero unexplained mismatch including counters/unsampled/conflicting retry; preserve all earlier mismatch/fix/rollback history, exact supporting evidence and reviewed revision partial order; no admission-unknown hiding a mismatch | shadow §3.1/§6 reset and revision-rollback rules verbatim | Retained correctness reset names made explicit by A2: `hash`, `identity`, `normalization`, `evaluator_disagreement`, `counter_mismatch`, `reviewed_resolution`, `revision_rollback`; `missing_support`, `exclusion_conflict` |
| G10 Clock | First real durable eligible positive sample strictly after the `transport` PASS, post-opt-in `fleet-budgets` PASS (linked to `fleet-budgets-pre`) and its §5.3 boot prerequisites starts; exclude `shadow-serving` completion from that maximum; continuously covered ≥604,800 s; no gap/correctness reset within interval; regional seeds per proof policy | shadow §6 positive predicate/time, retained with policy and per-boot amendments | `positive_seed_gap`, `regional_seed_gap`, `continuous_window_gap` |
| G11 External proof | Exact-revision cross-repo CI/fixtures, frozen money oracle, all v1/v2 mutations, real-adapter faults, D2/D3/real drain/load/rollback and accepted exposure/side-effect policy supplied at the stage requiring them | outbox §8 and runbook steps 6–9 retained; shadow never proves async drain capacity | `external_proofs_missing`, `pilot_prerequisite_gap` |

For G6 cadence in an interval d seconds, retain pilot bounds:
`max(1,N*(floor(d/4)-1)-missed_ticks) ≤ workspace_reads ≤ N*(ceil(d/4)+1)` and
`max(1,floor(d)-1-missed_ticks) ≤ health_reads ≤ ceil(d)+1` (C8).
**A1 BEFORE:** “late/failed slots feed health unknown/failure evidence, never
false yes.” **A1 AFTER:** every scheduled slot and attempted claim reconciles
under §3.1.2; late/unknown replies are availability failures even when a landed
observation passes the independent prediction predicate. Invalid/stale observation
still yields unknown. `publisher.failures` is never erased by reader success. Zero
traffic does not excuse absent timers. G8 recovery never excuses G6 failures.

Gap intervals retain conservative whole affected UTC-day blocking; an incomplete
writer cannot declare its lost tail empty. Later restored coverage may begin a
new interval from a new positive seed, not backfill that day. Persistent missing
support/reset provenance stays persistent. **A2 BEFORE:** the output paragraph
required metrics without a contract-defined payload.
**A2 AFTER:** emit the unchanged A1 GATE_OUTPUT envelope and exactly one §7.5
GATE_REPORT whose canonical bytes hash to coverage_sha256. It carries both
comparator and admission coverage, A/B/C and independently derived E,
original/retry counts, checkpoint failure/recovery/loss, per-writer ratios
including not_exercised, publisher/fleet peaks, actual regions, null counts,
timing/deltas and first seed/reset/gap timestamps in the exact fixed schemas.
Writer, region, input and reset details are complete paginated streams; failure
subjects alone are a bounded prefix, with exact tallies/day counts in the report.
Each stream is committed by root, count and chain tail. Null means unevaluable/absent as specified, never
zero. Preserve every failing name/count/blocked day and every historical reset; list
only the deterministic subject prefix. Report/outer equality and
gaps↔positive-exact-count equality are mandatory. A required
aggregate that cannot be computed blocks through its named rule; no silent
partial total or report-free PASS.

Pilot tools, precisely: `fleet-budgets-pre` requires dormant pins, bounded ≤1-hour
measurements and approved **projected** G7; no demand/cadence evidence invented
before opt-in. `fleet-budgets` adds G1/G2/G5/G6/G7/G8 over actual closed observer
intervals. `shadow-serving` requires G1–G9 and a G10 seed, returns elapsed seconds
without claiming seven-day PASS early. `pre-flip` requires full G1–G11, ≥604,800 s,
selected real-drain trial artifacts and signer/attestation checks. `post-flip`
retains truthful-status, rollback and measured handoff/repair outcomes. No tool
turns flags on; old CLI outputs cannot be represented as v2 PASS.

Bootstrap is acyclic: close warm-up evidence → complete `transport` PASS and
post-opt-in `fleet-budgets` PASS linked to the matching `fleet-budgets-pre` →
collect the first qualifying durable eligible positive sample strictly after
`max(transport.completed_at_us, fleet-budgets.completed_at_us,
all of that sample boot's §5.3 prerequisite completion times)` → evaluate it with
`shadow-serving`. Neither warm-up closure nor either prerequisite PASS requires
a G10 seed or a prior `shadow-serving` PASS. With prerequisites satisfied but no
qualifying seed, `shadow-serving` is BLOCKED with `positive_seed_gap` and null
clock start. When the seed and its coverage/closure are durable and G1–G9 pass,
return its **original sample timestamp** as `clock_start_us`, not the later
verification, persistence or `shadow-serving` completion time. Explicitly exclude
`shadow-serving.completed_at_us` from the prerequisite maximum; no second seed
is needed. The same rule rejects a seed at/before any named prerequisite. Later
boots follow §5.3 without moving the initial fleet prerequisite timestamps.

## 9. Runbook v2 — replace the affected outbox enablement steps

**D-V2-9 — new measurements, new clock.** The current window **never started**:
gatekeeper reports no real health row and all 34 predictions unknown; all 141
opted authorizations were partner-adapter, so no eligible positive seed. Missing
health and ambiguous terminal coverage independently prevent the existing clock
(C3–C8). A sealed analysis manifest cannot repair them. October 11 was an earliest
possible date in v1, not a started or certifiable window. No historical v1 seconds
transfer to v2.

1. **Replace old steps 1–2:** retain/export current blocked v1 evidence; review a
   rollout removing shadow opt-in for migration. Close what can close; do not
   erase losses. Land baseline P1/P2, P1-A1/P1-A2 and exactly one combined
   enclave pin-only follow-up; revised P3/P4a/P5 dormant in §10 order.
   P4b certificate issuance is deferred beyond the first window. Deploy enclave
   `shadow-v2-read-echo` everywhere first, shadow and negotiation off; deploy
   router v2 readers/code with policy empty and shadow workspace list empty.
   Admission/protection/fast drain remain false. Record every serving build,
   true region, boot and attested image; partial rollout is not ready.
2. Provision/verify signer, positive authority epoch and public keyrings on all
   revisions as retained key-placement procedure requires; no new production
   key values in this document. Prove ticket/binding/exclusion separation and
   dormant money oracles. Baseline P2 capability and the single combined
   fixture pin must precede P4a compared emission. Certificate runtime issuance/
   worker drills wait for P4b; frozen three-purpose rejection and verifier ports
   remain required now. Prove terminal-only reconciliation and original-boot
   close-result production. Q1–Q6/Q-A1–Q-A4 defaults are accepted (§13); obtain
   the actual measurement-based approvals, not another choice of A1 mechanism.
3. **Replace old step 3:** provision the ONE dedicated probe workspace via the
   separate `shadow-probe-workspace` package (§10), in the Stage D provisioning
   script pattern. Pin its identity in scripts/deploy/rollout.sh, keep key handling
   outside this design, and verify that its only traffic is the deterministic
   eligible probe (§13). Exercise chat.completions/responses through every enclave
   regional hostname; verify actual router booking regions and seeds. Prove the
   one-supported-candidate/full-event fit and ordinary parameters. Keep actual-hop
   full/hash-only maximum header and fragmentation tests in every region; live
   certificate-variant traffic and mixed-cohort distribution studies are deferred
   to P4b because the first cohort has neither. Frozen certificate byte fixtures
   and malformed/unexpected-proof rejection still run. Run new `fleet-budgets-pre` with N=1,
   configured caps including surge, publisher contender K and all §3/§7 load.
   Measure bounded read/claim/publish cycles against the real adapter in an
   isolated controlled trial, reviewed before production opt-in; include this
   source in sizing. **A1 ADD:** measure server snapshot-slot alignment, 250 ms shadow health
   reads, full elapsed-age charge and ordinary attachment export operations/churn;
   size close-result transactions under simultaneous shutdown. No daemon is implicitly authorized by an old pre-enable PASS.
4. **Replace old step 4:** reviewed config-as-code activation P6 sets router
   policy shadow-v2, health region, and single shadow workspace; then enclave
   shadow on with negotiation off. Bootstrap/old-authorization traffic is recorded
   but noncertifying. Warm caches, issue eligible bindings and exercise both
   full/hash-only compared variants; prove no certificate issuance and no money
   worker calls. Require exact T=A, B=C=0 for the qualifying cohort; an ineligible
   authorization remains unresolved/C and blocks. Certificate emission/echo drills
   wait for P4b; no benign-classification shortcut is allowed. Measure one opted workspace on the actual single nam6 node:
   CPU/read/write/index row load, startup/overlap, publisher progress, checkpoint
   recovery, closure and all regional latency/freshness components. If budgets
   fail, stop opt-in and redesign/reapprove; never enlarge the old allowance silently.
5. **Replace old step 5:** finish and close the warm-up interval with bounded
   keys/exports, complete per-boot ledger, four off pins and full revision/region
   deployment. Then obtain `transport` PASS and post-opt-in
   `fleet-budgets --bundle ... --live` PASS linked to the matching new
   `fleet-budgets-pre` artifact and actual measurements. Choose fresh complete UTC
   days after rollout/label transition and reconciliation of pre-v2 authorizations
   (up to the 48-hour proof horizon; later ambiguous terminals still block).
   The first qualifying durable v2 eligible positive sample strictly after the
   `transport` and post-opt-in `fleet-budgets` completion times and its own §5.3
   boot prerequisites starts the fresh clock. `shadow-serving --bundle ... --live`
   then evaluates that seed and reports its original timestamp per §8; its own
   completion time is explicitly **not** a prerequisite for the seed. No seed means
   `positive_seed_gap`, not a requirement to complete `shadow-serving` first.
   New boots extend ledger, not global start time. **A1 ADD:** warm-up PASS also
   requires original-writer close results/inventory binding, complete slot traces
   and terminal reconciliation of committed report obligations. Missing results are blocked even if closure rows exist; lost
   handoff replies may recover only already-created valid facts.
6. **Retain old step 6 with v2 evidence:** require ≥604,800 continuous seconds,
   zero unexplained disagreement and G1–G11 at their specified stages. Keep
   admission/protection/negotiation/fast drain off for the entire interval.
   Dormant unrelated PR G code may land only with exact-revision proof and pins.
7. **DEFERRED until admission/pre-flip, not a first-window dependency — replace old step 7 selection inputs:** confirm admission-pilot workspace
   separately (§13), obtain its own traffic/concurrency/side-effect measurements,
   select all drain settings from real regional trial exports and run v2 pre-flip.
   Verify signer/epoch/keyring and same-price/status/rollback gates. Seven shadow
   days do not substitute for D2/D3, handoff or drain proof.
8. **DEFERRED until admission, retain old step 8 money order/source handover:** after shadow PASS,
   protection on → verify fencing → measured fast drain on → verify real fleet-v1
   worker health plus §3.2 re-verifications → install pilot allowlist/$5 override
   → admission on → enclave negotiation on and shadow off for that pilot.
   Stop shadow publisher/close evidence before claiming handover complete; if
   temporarily overlapping health sources, budget both and prove no cross-use.
   No manual env edit, skipped attestation or queued-rollout claim.
9. **DEFERRED money-pilot step 9/rollback, still mandatory before money opt-in:** admission/negotiation off first; retain
   protection/keys and drain until all accepted work is durably resolved; only
   then protection off. Re-enable solely through the gated sequence, export
   actual regional handoff/booking/repair percentiles and rollback evidence.

**First-window deferrals are stage-specific, not gate exceptions.** The
admission pilot's traffic/concurrency/side-effect study, D2/D3/real-drain setting
selection and fleet-v1 handover/money-flip drills in steps 7–9 belong to pre-flip
or later as §8 already specifies. They do not start the shadow clock. Keep all
applicable shadow transport, real-adapter faults, money isolation/oracles,
readiness, health, close/export, load and seven-day gates. Shadow opt-out/closure/
evidence retention rollback remains mandatory now; only money activation work
is deferred. Multi-workspace provisioning and N>1 sizing wait for a proposed
cohort addition, which must satisfy §5.2 before opt-in.

**A1 ADD:** daily export includes exact report obligations/terminal horizon joins,
close-result rows or explicit gaps, conservative handoff-accounting bounds,
complete publisher trace/counter conservation and replayable health inputs.
Failed original close stays blocked after read-back; a report cannot synthesize
a reply. No clock qualification, owner-page or per-boot signing export remains.

Every day export exact original rows plus pagination/key coverage, independent
inventory including killed/retired boots, manifest/closure/receipt roots,
instance_regions_by_day, prerequisite ledger append/hash, pins/attestations,
cohort N/seed policy, publisher slot/freshness/cost traces, counter ratios,
A/B/C/E and retry reconciliation, unresolved authorizations, positive samples,
reset/fix history and report/CLI/source/fixture hashes. Retain BLOCKED days and
raw content-free failure evidence. Export provisional days as provisional until
closure; logs and absence of a row are not a substitute.

## 10. Smallest reviewable ordered PR sequence

**A2 BEFORE:** A1 round 2 retained P1–P6 plus P1-A1 and an enclave fixture-pin
follow-up. **A2 AFTER — D-V2-10:** add P1-A2 and consolidate A1/A2 into exactly
one enclave pin-only follow-up (§7.1.1); split P4 into compared P4a and deferred
exclusion P4b. The dedicated probe package is a P6 prerequisite. P0-A1 and behavioral P2-A1 remain removed. Each
package carries its own tests; P1/P2 cross-repo fixture copies are an ordered
contract pair. Dormancy means no new task/RPC/parsing/serialization/counter on
empty v2 policy or nonmember paths, except the existing constant guard; no hidden
change to existing v1 behavior. Do not update a frozen oracle to absorb a surprise.

| Package | Repo, scope and dependencies | Dormancy, proof obligations and deployment/rollback |
|---|---|---|
| P1 `shadow-v2-contract` | Router schemas, pure decision/certificate validators, wire fixtures and evidence schema; no live imports/effects. Establish §7 fixture manifest. | Pure dormant contract. **WIRE**: exact schema/limits/three-purpose rejection/fallback matrix; existing billing literals unchanged. Deploy has no effect. |
| P2 `shadow-v2-enclave-echo` | Enclave copy P1 bytes; retention/variant/header and capability support at all §4 seams; async binder rejects certificate. Depends P1 contract. | Existing shadow switch plus v2 router marker gate new work. **ECHO/OFF**: real stream/refund/route and byte oracles, retry frozen bytes, no async bind. Land and attest/deploy everywhere **before P4a compared emission**, and also before later P4b certificate emission. Can revert while emission dormant; active rollback first disables router emission/ends certification. |
| **A1 ADD P1-A1** `shadow-v2-contract-a1` | Depends baseline P1. Pure exact O/C/R+q/e reader predicate, changed HEALTH timestamp/age-input fields and HEALTH_CLAIM server-slot timestamp, publisher conservation/basis/trace, terminal-only contextual completion, closure obligation marker/report join schema, CLOSE_RESULT kind/sealed projection, coverage/retention allowlists and new fixture release (§§3/7) | No live imports/RPC/lifecycle. Baseline bytes frozen; explicit new manifest/negative fixtures. Enclave follow-up copies/pins **all** files/variants with no behavior change. |
| **A2 ADD P1-A2** `shadow-v2-contract-a2` | Depends P1-A1. Pure additive report/failure/subject/preimage schemas, roots, output relationship checks and verifier port declarations; §7.1.1 exact surface and combined release. | Baseline/A1 behavior and bytes frozen. No live imports, I/O or G1–G11 implementation. All A2 fixtures and retained byte differentials required. One combined enclave pin, no behavior change. |
| P3 `shadow-v2-durable-observation` | Depends P1-A1; independent health publisher/reader, exact SDK metadata/sentinel, bounded receipt HWM/checkpoint/closure, committed report obligations, ordinary health attachment export and original-boot one-attempt CLOSE_RESULT. No certificate issuance yet | Policy empty means dormant. Native timing/races/faults/SIGTERM/SIGKILL and same-writer recovery proofs; preserve money/shared adapters unchanged. Terminal obligations survive local retirement via durable receipt chain. Measure export/close costs. |
| **A2 P4a** `shadow-v2-compared-path` | Depends P1-A1/revised P3 and deployed baseline P2 plus the single combined fixture pin for activation. Authorize decision/receipt through journal; existing v1 eligible binding/snapshot under v2 outer policy; terminal dispatch and compared verification; exactly one terminal receipt/attempt, links and independently derived demand E. No new wire/ownership data. | Opt-in/capability gated, off/nonmember pure. Noneligible gets NO certificate/binding, stays unresolved; terminal C blocks day. PARTITION/MONEY/stream/bytes and actual terminal reconciliation; no HTTP error or synchronous evidence lookup. Required for P6/first window. |
| **A2 P4b — DEFERRED** `shadow-v2-exclusion-issuance` | Depends P4a, P1-A1/P3 and existing P2. Adds issuance plus §4.1/§4.3 asynchronous exclusion verification worker, original receipt backing and certificate terminal projection. No A2 runtime dependency or new enclave behavior. | Not a P6/first-window prerequisite; required before opting in any cohort carrying excluded traffic. All certificate issuance/backing/locator integration and mixed-traffic proofs/load apply then. No auto-exclusion while absent. |
| **A2 separate prerequisite** `shadow-probe-workspace` | Being written separately; changes no shadow module. Script patterned on scripts/provision_stage_d_probe_workspace.py provisions one dedicated local typed Credits workspace/key and deterministic eligible probe; routes/provider pin/fallback off/ordinary parameters/volume and regional hostnames as §13. | Required before P6; provision/configure through reviewed release procedures, identity pin in scripts/deploy/rollout.sh. Verify eligibility, event fit, actual region seeds and load; no source or provisioning completion claimed here. |
| **A2 AFTER P5** `shadow-v2-gates` | Contract-only library/tools/planner depend P1-A1 + P1-A2; full live export integration additionally depends revised P3/P4a and baseline P2 with combined fixture-pin evidence. Add A2 report/pages/preimage resolution, exact verifier port binding and retained-tool delegation. Shared G1–G11, inventory/readiness, bounded exports, exact obligation enumeration and forward terminal joins, CLOSE_RESULT identity/coverage and handoff bounds, health replay/trace conservation | Offline tools explicit policy; no request storage. Full window/churn, missing-terminal/receipt/result and changed-version negatives. No clock qualification, owner collector or new boot-signature verifier; existing certificate/binding verifiers remain mandatory. |
| **A2 AFTER P6** `shadow-v2-measured-opt-in` | Depends P1/P2/P3/P4a/P5, P1-A1/P1-A2, shadow-probe-workspace and the single combined enclave pin follow-up; accepted §13 choices and actual exact-revision CI/resource gates. P4b is explicitly not required. Reviewed single-cohort router/enclave config/attestation releases; later money pilot is separate | Active release after dormant gates; actual N=1 measurements include 250 ms reads, server slots, export churn and close bursts. Preserve money off pins; opt-out/closure/export on rollback. |

**A2 ADD:** P3/P4a/P4b do not depend on P1-A2 schemas or implementation; their
A1 APIs and producer behavior stay frozen. Earlier table references to “A1
fixture pins” mean the consolidated release pin for activation, not a second
enclave follow-up or an A2 runtime dependency.

P3 combines health and persistence because publisher lifecycle/operation evidence
must itself use the new counters/closure; splitting either into an active partial
publisher is not a useful deploy. P4a/P4b are separate from P5 so wire/isolation review
is not buried in gate arithmetic. No money admission PR is bundled with fixes
to shadow certification. Reviewed CI, attestation, release workflows and rollout
gates remain intact in both repos.

First-window critical path: frozen P1/P2 + P1-A1 → revised P3 → P4a;
P1-A2 → P5 and the **one** combined enclave pin; the independent
`shadow-probe-workspace` prerequisite joins these at
measured P6 → warm-up/transport/fleet PASS → eligible seed → seven-day window.
P4b, N>1 expansion and money-admission packages are deferred to their actual
use. This removes unused issuance work from the path without changing G1–G11.

## 11. Test and proof matrix

**D-V2-11 — negative controls must kill compiled mutations.** Proposed tests,
not passing results. Use scratch mutants, never leave mutant code in either repo;
a syntax/import failure is not a kill. Exact named suites may be split by authors,
but each proof obligation below must appear in CI artifact inventory.

**A2 proof ownership / first-window boundary:** P1/P2 frozen certificate schemas,
verifier/purpose negatives and shared byte fixtures stay required. P4b owns the
live issuance/worker portions of PARTITION/WIRE (mixed/excluded and all qualifying
reasons), CANDIDATE BACKING (cross-boot certificate success/faults), ORIGINAL
RECEIPT LOCATOR (signed certificate lookup/integration), MONEY/ECHO's certificate
emission integration, and WINDOW's mixed eligible/certified-exclusion scenario;
those portions are DEFERRED before the first eligible-only window. General
journal reservation/HWM/size/closure/retry faults from those rows remain P3/P4a.
P4a must prove missing/invalid binding, ineligible facts, oversized/unsupported
candidate context, replay without original proof, header_absent and unexpected
certificate traffic all persist receipts and remain C/BLOCKED; never relabel as
B or drop from T/E. A B=0 full-window fixture must exercise all G1–G11 at their
stages. P5 certificate verifier contract tests remain now; a live worker is not
needed to run the frozen pure verifier. DEMAND's excluded-only arithmetic also
remains a pure gate proof. LIVE drain-handover and money activation proofs retain
their pre-flip stage, not the first shadow-window stage. Record deferred ownership
explicitly in EXTERNAL_PROOFS; it is not an arbitrary not_required escape.

| Change / obligation | Positive and fault tests | Scratch mutants that must fail |
|---|---|---|
| **A1 G-A single time domain** (§3) | DD1 .501 commit, .25 start/.90 commit, .40 commit/.80 or lost reply: separately assert prediction and availability. Native O/C/R exact metadata, empty unresolved result, nanosecond endpoints, q=.25 and A=5, delayed reply/cache, host ±250 ms/steps and far-future proposal rejected before claim mutation/scan, process pause, 4.25+.25 chain, server-slot misses. Invalid newer row destroys old cache. Compare wall-based p95 threshold to unchanged money calculation | Treat commit/read timestamp as acknowledgement; set O from host or receipt; omit q; refresh O on poll; accept host-labelled future winning slots or stale snapshot metadata, lossy conversion or four-second-old O; use CURRENT_TIMESTAMP as exact snapshot time; count late/lost reply as successful cycle |
| **A1 G-A accounting/dormancy** (§3) | Exhaustive scheduled→missed/attempted→lost/published/failed product, unknown/late overlaps; unknown claim is failure; lock contention before actual claim; executor pause before start across midnight; counter/trace equality, fleet missing winner, trace overflow. Native checkout/session/pool/restart/cleanup obey same deadline. Empty-policy startup and shared adapter cost/behavior differential | Unknown claim counted missed; skip pre-start day registration; late-start after lock; close before queued accounting; erase failure after reader yes; derive traces from totals; renew deadline at checkout; install wrappers/tasks/signing/export with empty policy |
| **A1 G-B report obligations** (§§6/7) | Retained/lost-response histories both close local evidence with explicit chain commitment and remain provisional without terminal; lost response/no terminal blocks issue day at horizon. Issuer exits, fresh router/enclave terminal independently reconciles; H1/E1 and H2/E2 intentionally same result, with no continuity claim. Missing receipt/root/count/forward day, bad/conflicting terminal, expiry/replay and end-of-window attempts remain blocked | Equate receipt/HWM with discharge; hide obligation after issuer exit; mark live call benign without terminal; expire to success; use arbitrary nonce or empty complete; require same-enclave identity the wire cannot prove; omit issue-day gap |
| **A1 completion context** (§6) | Every ordinary finalize/complete and specialized route, durable original missing from memory, absent page, wrong boot, same-day original seq 2/completion seq 1, equal seq across correctly ordered days, reversed days/times and original authorize. Same writer's cross-day terminal completion succeeds; fresh router's first terminal after another issuer remains a distinct path | Authorize-original completion; waive ordering whenever retry_original exists; cross-boot recovery; skip missing original validation; advance HWM on empty complete; overwrite original mismatch/prediction |
| **A1 G-C close handoff** (§§6/7) | Native final commit lands/reply lost stays vetoed after read-back. Timely original reply→insert-only result→lost result reply recovers landing with conservative bound. Kills before/after reply/result, late original, wrong live boot/day/digest/checkpoint, duplicate/conflict and missing inventory/export. One result attempt; original six rows plus separate one, shared 2+2≤4 s and remaining grace. Wall steps never replace monotonic timing | Mint result on read-back or in final transaction; supply another boot via caller/revision; retry failed final close clean; recursive marker; landed result implies zero failures; hide absent key or extend budget |
| **A1 provenance/export/load** (§§7/8) | Ordinary service-identity row/key/body/inventory agreement with explicit malicious-router limit; full basis/trace queue/input, token PUT-or-GET, unknown upload/restart, root/pass/cleanup operations and churn initial-token/in-flight bounds. Enclave new manifest pins with no wire change. Shared G3/G6/G7/G8 outputs | Assume inventory cryptographically proves process origin; allow P5 to originate reply facts; truncate roots; omit re-run/root/delete/churn work; use per-second producer bytes as total budget; pin A1 with old manifest; enlarge queues |
| HEALTH (§3), **A1 clarified**: 500 ms tests separate validity from availability as above | Real Observer with money flags off reaches yes/no via new health; missing/stale/future/incomplete/10,001/unknown-age rows unknown; dead/leased/legacy membership; 2s multi-contender slot race/delayed winner; all phase offsets and 250ms-read/500ms-cycle/4.50s/5s boundaries; empty opt-in stops work; spies on money claim/apply/finalize/reap remain zero | Read/write fleet-v1 for shadow; accept shadow in AdmissionCache; rejuvenate timestamp; omit dead rows/sentinel/fence; every contender scans; skip publisher load; allow stale yes |
| PARTITION/WIRE (§4) | Mixed partner/eligible settle and refund; requested versus actual fallback; excluded huge irrelevant pricing snapshot; eligible oversized snapshot blocks; missing signer/context/envelope; all allowed reasons with immutable witnesses; replay/header_absent; auth/terminal day/region changes; observed-tier proof; byte-identical fixtures all variants | Add snapshot_unavailable/missing_envelope/snapshot_size as exclusions; initial-other→eligible fallback excluded; variant bypasses signature/actual dimensions; issue exclusion on replay; wrong purpose accepted; no terminal backing; snapshot cap applied before certificate dispatch |
| CANDIDATE BACKING (§§4/7) | Cross-boot terminal after live catalog changes: original complete projection >2 KiB (including a 32-candidate/4,993-byte fixture) cannot fit; issuance records `candidate_receipt_size`, no certificate, terminal C/BLOCKED. Inject even a correctly signed candidate digest without the original full projection: still C/BLOCKED. Companion drives real authorize on A → reserved locator/signature → immutable page flush → graceful issuer exit with **A1 committed report obligation** → first terminal on fresh B after catalog changes. No verified certificate row, injected locator/cache or A-local state; use only echoed signed claims to locate/verify original facts in one complete-PK read (≤1 row/64 KiB/1s), then B. Exactly-2,048-byte complete event including locator coordinates fits, 2,049 does not; include metadata, no trimming | Accept digest-only backing; rebuild from live catalog; check projection size alone; truncate candidates; accept oversize certificate-backed B |
| ORIGINAL RECEIPT LOCATOR (§§4/7) | Correctly signed wrong boot, wrong page, wrong event, key/body/identity/sequence mismatch and event-digest mismatch; missing page; locator/certificate/row/receipt/wire overflow: named gaps and C, never B. Kill issuer after echo before page durability: terminal C and unclosed/BLOCKED. Append concurrent events, roll pages, checkpoint, cross midnight, lose commit reply then reconcile late commit without moving keys/HWM prematurely; four reserved/unacknowledged pages span all days. Ordinary signature-tampering rejects remain required | Accept a locator whose event digest mismatches; fall back to live catalog or scan pages when page is missing; derive boot from revision/current writer; require preseeded certificate row/cache; relocate/renumber a signed event; omit locator from fit checks; advance HWM on reservation; flush unfinished slot; allocate fifth reserved page; let later persistence forgive issuer kill |
| MONEY/ECHO (§4) | Frozen main authorizes, actual settle/refund and chat/Responses streams; off/nonmember/on malformed diagnostics; exact bytes/state/SQL/calls; swaps/expiry/forgery; true retry vs conflict; no double booking; old/new combinations and deployment marker | Attach certificate to async; edit settle body; shadow exception escapes; consume money executor/transaction; retry overwrites mismatch; echoed foreign auth accepted; old enclave treated capable |
| DEMAND (§5) | Closed idle/authorize-only/excluded-only writers with zero predictions; eligible E>0 with zero predictions fails; unknown eligibility fails; E from receipt partition; 1 unknown/49 fails, 1/50 fraction passes but cannot seed; all idle timer limits; real regional seed | E=prediction_count; known>0 unconditionally; drop idle roster; accept all-zero seed; count exclusions as positive; hide unknown or mismatch behind no demand |
| BUDGET/BOOT (§5) | Sequential boots fit one cap; overlapping rollout two; startup one-second peaks; uneven regional peaks; missing start/end/region/revision/prerequisite; midnight boundaries; exact old F5 N=1/N=2 arithmetic; publisher-inclusive pre/post match and strict CPU<45 | Historical boot sum; lifetime union used as count; average away peak; subtract overlap; reuse predecessor readiness; move whole clock on ledger append; omit publisher/receipt writes; accept 45% |
| DURABILITY (§6) | Failed idle/active checkpoint then exact complete cumulative recovery; failed sample recoverable only with exact retained data/readback; unknown commit late success reconciled; HWM holes/regression/conflict; failed close, overflow, lost sample, hard kill before first/last flush; midnight retained request and observer fold | Ignore all worker/store_unavailable; close before work drains; max-seen seq as HWM; acknowledge timeout as success; overwrite equal-seq conflict; discard failed page; clear sticky loss/reset; reinterpret v1 drops |
| SERIALIZATION (§7) | Native serialized transaction with four exactly-65,536-byte page bodies, exactly-65,536-byte counter and exactly-4,096-byte closure totals 331,776 body bytes/6 rows; separately measure maximum-key/encoding overhead and RPC/memory/flush/shutdown budgets. Full-sized events plus page metadata obey 64 KiB/128-event conjunctive limits; four-page cap spans all three day buckets; manifest byte/count boundaries and cap reservation arithmetic | Size at 258 KiB; omit counter/closure or encoding copies from budget; fit 128 full-size events on a page; apply page cap per day; truncate manifest |
| BOOTSTRAP (§§8/9) | Closed warm-up and matching transport/post-opt-in fleet PASS, boot readiness complete, no eligible seed: BLOCKED/`positive_seed_gap`, null start. Add one qualifying durable seed with closed coverage: valid start at its original timestamp without a second seed or timestamp shift; seed at/before transport, fleet or any boot prerequisite is not a start | Include shadow-serving completion in prerequisite maximum; start at verification/persistence time; require a seed for warm-up fleet PASS; accept a pre-prerequisite seed |
| WINDOW (§8) | Synthetic clock ≥604,800 seconds (include midnight and ≥8 dates when seed is after midnight), autoscaling churn across regions, graceful replacement/overlap, mixed eligible/certified exclusions, genuinely idle and authorize-only intervals, recovered checkpoints, late terminal/retries and required regional seeds | Remove one inventory writer/counter/closure/receipt; inject ±1 micro Go/Python/booked disagreement and usage/hash mismatch; bypass G9 when prediction unknown; use v1 seed or unverified fix/rollback |
| LIVE (§9) | Real-adapter fault/load evidence in all regions, exact versions, actual hop size/fragmentation, bounded one-workspace publisher-inclusive CPU/headroom and SIGTERM; no claim of async throughput from sync shadow | Treat fake-only or queued deploy as PASS; skip real drain-health handover; claim old pre-enable covers N=2 |

Real adapter means actual native Spanner client/transaction/result iteration,
not only `tests/fakes/spanner.py`: emulator for serialization/races and controlled
isolated database/transport fault harness for service deadlines/reset/lost replies.
Inject pre-commit failure, successful commit with lost response, delayed commit
landing after point-read miss, RST_STREAM, stream restart during iteration,
transaction abort, final close timeout and competing old/new checkpoint writes.
A point-read miss while commit might still land is not absence. Check durable
rows after settlement and every fault; prove retention fences and no shadow
writes in money transaction. Production load tests require separately reviewed
bounded load; no ad hoc body scans. Emulator alone cannot establish real regional
latency or single-node CPU capacity.

Each code PR runs full `uv run ruff check .`, `uv run mypy`, `uv run pytest -q`,
coverage ≥70%, relevant real-backend conformance, and for async/billing changes
`uv run pytest -q -n 4 -m proof_oracle tests/test_async_settle_proof_oracle.py`.
Enclave runs its full affected Go/oracle/stream suites and shared 759-case
contract. Patch backend classes, never STORE proxy. This design-only pass does
not claim those implementation tests passed.


**A2 ADD — proof rows (required future proofs, none executed here):**

| Boundary | Positive proof | Mutants that must fail |
|---|---|---|
| A2 report commitment | Exact canonical report/outer equality, complete fixed fields; independent known-zero versus null and E=0 not_exercised cases | Report hash differs from coverage_sha256; read null as zero; emit PASS with no report; change original seed time to verification time |
| A2 exact failure counts / bounded listing | >128 independent missing closures yield exact total/day counts and all blocked dates but exactly first 16 subjects; all 58 names, K−1/K/K+1 and four-page boundary, max subject, complete=true report with incomplete list; permuted input gives identical prefix; every rule evaluated after K | Drop page/name/nonlisted count/day, wrong subject/order/prefix or complete flag; infer counts/days from sample; omit failing rule after first/Kth failure; claim incomplete evaluation exact; positive count with PASS; zero count with listed entry |
| A2 roots/preimages | Independent literal byte/root vectors for every §7.5.4 field; LF-sensitive F versus canonical C; A1 page-root unchanged and obligation entry-root unchanged | Accept undefined-preimage digest; substitute ZERO; reorder/repartition A1 coverage silently; wrong field's root; missing object/page/tail or count; circular report/proof commitment |
| A2 verifier/purity | Both actual existing callables, exact keyset/source/fixture identity, per-applicable-object invocation; AST/fresh-process pure import tests | Verifier absent or same-signature replacement; claims-only acceptance; changed keyset; no call with fabricated transcript; import binding/storage into pure family |
| A2 packaging/delegation/cost | Byte-identical baseline/A1 release and one combined enclave pin; v1 no-policy stdout/exit differential and every retained predicate/delta; worst-size pages and full L+W/churn/rerun/cleanup plan | Second enclave pin prerequisite; altered A1 bytes; v1 PASS relabelled v2; unlisted predicate bypass; charge only steady producer bytes; omit page/report/cleanup or approve unbounded passes |

**A2-R1 literal counting/subject vectors.** Canonical JSON code lines below are
exact ASCII subject bytes, without LF. With days=["2026-10-12"], two unusable
health bases in router w1 that independently fail the required timestamp check
produce health_timestamp_gap count=listed=1, complete=true, day_counts=[1].
Their subject is exactly:

```json
{"at_us":null,"boot":"w1","day":"2026-10-12","dimensions":null,"object":null,"phase":null,"revision":null,"role":"router","scope":"writer"}
```

Three failing slots across two trace batches in that same writer-day produce
publisher_trace_gap count=listed=1, complete=true, day_counts=[1], with **the
same literal subject bytes above**. If those slots also violate accounting,
publisher_accounting_gap independently has count=listed=1 and day_counts=[1]
with those bytes. The same writer-day coalescing applies separately to the
other G6 names; these vectors do not suppress independently failing names.
Reordering the two bases, three slots or two batches changes none of these
counts/subjects. Their individual diagnostic coordinates are not extra failures.

For an otherwise valid expected page
async_settle_shadow_receipts_v2/2026-10-12/w1/00000000000000000001 whose first
event is schema-invalid, evidence_schema_gap count=listed=1, complete=true,
day_counts=[1] for that defect. Position is 0 even if the invalid event claims
seq=99. No extra page failure is counted merely for that invalid child. Exact
subject bytes (page-key component length is 34):

```json
{"at_us":null,"boot":"w1","day":"2026-10-12","dimensions":null,"object":{"event_seq":null,"key":"34:2026-10-12/w1/00000000000000000001/5:event/1:0","kind":"a2_invalid_receipt"},"phase":null,"revision":null,"role":"router","scope":"object"}
```

**A2-R2 literal conflict/input-permutation vectors.** At the ROOT_ENTRY byte
interpretation boundary use logical (day,boot)=("2026-10-12","w1") and exact
candidate bytes `{}` (hex 7b7d, size 2) and `{}\n` (hex 7b7d0a, size 3).
These minimal bodies exercise the root layer, not valid counter/closure payloads;
their source-schema rejection remains a separate G1 failure. Their respective F
values are `44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a` and
`ca3d163bab055381827226140568f3bef7eaac187cebd76878e0b63e9e442356`. The exact conflict SHA preimage is:

```json
["44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a","ca3d163bab055381827226140568f3bef7eaac187cebd76878e0b63e9e442356"]
```

It is 135 bytes without LF. The exact conflict entry is:

```json
{"bytes":5,"key":"10:2026-10-12/2:w1","sha256":"0b896b4ad8e80334b52d5f5de239a1bee9f008464e9c5f019974159aea183a0c","state":"conflict"}
```

The byte count is 5; the expected-set/root-entry count is 1, and the candidate
inventory count is 2. R of the one-entry canonical array is
`129591526645105fa353f07120da3ca4905ce6ec7ac74914a96604ec9fb1e2f1`.
Inputs [hex 7b7d, hex 7b7d0a], [hex 7b7d0a, hex 7b7d] and
[hex 7b7d0a, hex 7b7d, hex 7b7d0a] all yield exactly that entry, SHA preimage,
root and counts after byte-identical export deduplication. Neither candidate F
may replace the conflict SHA. The same construction applies at each complete
key in every new ROOT_ENTRY stream; it leaves A1 page-root vectors unchanged.

**A2-R3 observation vector.** One retained S sample with booked_micro=100,
booked_minus_frozen=-3 and eligibility.observed=null is referenced by an original
terminal, one retry and one completion, all within the window. The original
comparison is null, the retry comparison is explained-by-catalog-change, and both terminal prediction
objects are non-null (one may contain prediction="unknown"). Then |S|=1, |T|=2,
deltas.count=1, null_count=0, min_micro=max_micro=-3, bin `-2-10`=1 and every
other bin=0; null_counts.{eligibility,comparison,prediction,booking,delta} is
{1,1,0,0,0} in that field order. evidence_write_us has count=0, null_count=1
and null extrema/percentiles. The completion adds no observation. Separately,
two scheduled slots, one with rpc_count=3/rpc_elapsed_us=150 and one missed
with 0/0, yield publisher_slot_rpc_elapsed_us exactly
`{"count":2,"null_count":0,"min":0,"p50":0,"p95":150,"p99":150,"max":150}`.
Neither three 50-us RPC observations nor omission of the missed slot is valid.

## 12. Feasibility estimate and measurements before the clock

**D-V2-12 — estimate from residual failures, not traffic forced onto idle boots.**
Use the following explicitly conditional model; it is not a forecast. Let h be
fleet hard-kill/unclosed-writer events/day (or measured rate per boot-hour × actual
boot-hours/day), d unrecoverable evidence-loss events/day excluding h and request
failures, N eligible terminal attempts/day, and q probability per eligible attempt
of a blocking comparison/terminal failure (missing exact usage/refund proof,
booking unknown, lost evidence not already in d, etc.). Let r be daily probability
of failing another gate (timer/publisher/unknown-budget/roster/seed), excluding
events already counted. Independent stationary Poisson h,d and independent
Bernoulli request/gate events give

`P(7 consecutive days certify) = exp(-7*(h+d)) * (1-q)^(7*N) * (1-r)^7`.

For varying daily volumes/rates, replace exponents by sums over the seven days
and multiply daily gate-success probabilities. Actual disagreement is a defect
requiring a fix/reset, not an acceptable random-loss budget. This model applies
only after arithmetic correctness proofs. Seeds are measured scheduled successes;
if random eligible arrivals are assumed Poisson per region, model regional
no-positive risk explicitly rather than assuming seeds. Low request volume can
still fail the unchanged per-writer 2% unknown threshold; count that in r.

Measure h, d, q, N and r, with confidence intervals and dependencies, before
starting; none are supplied reliably by the input reports. Separate recovered
checkpoint failures from losses, graceful and forced scale-down, no-traffic boot
closure, provider usage omissions and transport faults; measure by region,
revision, lifetime and request/phase. Include primary publisher loss, restart
bursts, interval lengths, unknown predictions per eligible-demand writer,
eligible request distribution, candidate/cohort mix and positive-seed frequency.
Collect real-adapter shutdown/unknown-commit recovery and N=1 CPU/read/write
headroom. Use worst supported confidence bounds to compare with Joseph's target
blocked-day rate, not a point estimate after zero observed failures.

Correlated Spanner/platform outages violate independence. Also report empirical
whole-day/block-window failure rates and an outage scenario model; the product
above can be optimistic. V2 removes the unchanged gate's product over all writers
`∏(1-exp(-μ_j))` and its requirement that every failed checkpoint be absent.
The verification report's `(1-p)^W` argument explains why simply waiting under
millions of checkpoint attempts is fragile; its extrapolation from 339 reported
drops is **not** an estimate of v2's d. No invented hard-kill rate, eligible failure
rate or seven-day success percentage is authorized here.

**A2 first-window measurement scope:** mixed monitor/partner/candidate traffic
surveys, certificate issuance/worker load and excluded-traffic terminal-loss
studies are DEFERRED to P4b/cohort expansion. Multi-workspace N>1 sizing is
DEFERRED until an addition is proposed; each addition needs new §5.2 approval.
Money-pilot drain/side-effect measurements are DEFERRED to pre-flip. The dedicated
probe still needs actual eligibility/one-candidate/event-fit confirmation,
provider exact-usage/refund/failure behavior, configurable volume and observed
per-writer-day eligible-demand distribution well over 50 (target ≥200 for each
active bucket). Load balancing/churn cannot guarantee that by configuration
alone; measure and size before P6, retain unchanged 2% unknown rule on every
bucket including short-lived writers. Truly idle buckets stay E=0/not_exercised;
no synthetic demand or per-boot positive-seed rule is introduced. All h/d/q/r,
health, load, closure, authorization-horizon, export and observed regional-seed
measurements remain. Predictable scheduled probes remove the need to estimate
random monitor arrivals for the first cohort; actual seed success is still proven.
Here N in the probability model denotes daily eligible attempts, while §5.2 N
is opted workspace count (initially one). Neither is a measured value yet.

### 12.1 A1 round 2 — incremental costs and feasibility

No measurements or implementation test passes are supplied. G-A adds **zero**
health statements/RPCs/rows/mutations/indexes. Added commit timestamp column
costs ≤8I logical bytes/s and claim-current scalar ≤4K bytes/s; already-returned
read metadata adds another 8I bytes/s of local handling, not incremental wire.
Native encoding, exact conversion/predicate CPU, and the modest HEALTH field
changes inside 4 KiB. Clock-qualification production/distribution/verification
and all enclave/boot-origin capability costs are zero because removed. The real
availability cost is server claim/observation-slot alignment within .25 s and reducing
shadow-health read/install allowance to .25 s. Normal conservative age≤4.50 s,
actual accepted A<5; failure to meet those component gates blocks, not a new
CPU/latency allowance. Money read deadlines and ages remain unchanged.

Health evidence still costs ≤6,144Q basis payload bytes/s at Q eligible predictions/s
and ≤512K trace payload bytes/s plus wrappers. For steady R readers/K contenders,
§7.2 permits ≤(R+K)/5 producer PUT-or-GET operations/s and
≤13,107.2(R+K) transport body bytes/s. These are not totals: add L+W operations
per bounded export pass, every pass/restart, root/page/report bytes, row export
and cleanup. Finite intervals use one initial token per new boot-lane plus its
active cadence allowance and already-in-flight work; simultaneous counts alone
cannot bound churn. Queues ≤256 KiB/stream, ≤512 KiB for both, before native copies.
No ownership ingress, collector reads/generation writes, P3 owner polling,
provenance resolution or signing budget survives. Removing those round-1 lanes
resolves their R4 omissions by eliminating the operations; remaining health
transport has the full separate accounting in §7.2.

G-B adds the 44-byte closure interpretation member inside 4 KiB, plus the
25-byte contract_revision member where applicable. Receipt chain/count/HWM
already commit the enumerable obligations: **zero** authorize-event/response/
header/certificate bytes, per-auth rows, owner facts, enclave work or live RPCs.
P5 joins bounded receipt/terminal exports and writes obligation coverage pages;
charge its actual reads/CPU/bytes under §7.2. Report certification waits through
original horizons/complete forward coverage; same-enclave continuity is not
purchased. Event body cap stays 400,000×2,048=819,200,000 bytes/day before wrappers;
page/candidate/terminal/manifest limits are unchanged.

G-C adds at most one result/writer-day: ≤2 point-row reads, one transaction,
one mutation and ≤4,096 body bytes per attempt; one expected-result read per P5
sweep. At 4,096 writer-days/day: ≤8,192 write-path point reads, ≤4,096 rows,
≤16 MiB result bodies/day and ≤480 MiB over exactly 30 such days (before longer
retention, replicas/metadata/export). Handoff accounting ≤4 MiB/day before wrappers.
No per-boot signature/verification cost. Original final transaction remains six
rows/331,776 bytes; later result adds 4,096 for **335,872 bytes (328 KiB)** over two
transactions sharing ≤4 s. Preparation/encoding/cleanup and prior page/export
passes consume real shutdown grace; 2+2 is a ceiling sum, not a feasibility test.
Bursts use simultaneous closes; no daily-average concealment. One-shot result
nonlanding remains a new blocked-day surface, even on an otherwise healthy boot.

Keep h/d/q/N/r model without double-counting: h covers unclosed/hard-killed router
boots; d includes unrecoverable receipts/health exports and result nonlanding;
q includes required terminal failures. Add a separately measured authorization
no-terminal/horizon-loss rate a for all authorizations, including excluded ones,
only after removing overlap from h/d/q. Then the illustrative independent model
is `exp(-7*(h+d+a))*(1-q)^(7*N)*(1-r)^7`; r includes elapsed/slot/read-budget and
export/gate failures. No owner-loss or clock-qualification rate exists in A1.
Measure correlations among Spanner failures, shutdown and transport; independent
products can be optimistic. Losing a response with no terminal still blocks an
earlier issue day; ending the report window or waiting expiry never helps.
The practical difference from round 1 is no early benign-running classification;
final reports already waited for terminals, so no final correctness evidence is
lost by removing ownership. Q-A1–Q-A4 defaults were accepted on 2026-10-12;
actual cost measurements and activation approvals remain required.

### 12.2 A2 ADD — offline report cost only

§7.5.5 gives the incremental report/failure/flat-page/attachment arithmetic.
A2 adds **0** producer RPCs, **0** request-path bytes, **0** money operations,
**0** DDL/indexes and **0** enclave runtime behavior. It adds one pure contract
package and replaces two potential fixture pin steps with exactly **1** combined
pin-only follow-up. Failure pages total ≤954,368 bytes; including report and outer
envelope the bound is ≤1,282,048 bytes. Exact input evaluation and complete other
evidence still cost their actual planned operations; this grants no new budget. P5 must calculate actual costs before
execution; P6 approval and G7 remain mandatory. No latency, RSS, suite duration,
coverage percentage or practical capacity is claimed measured in this amendment.

## 13. Owner decisions and remaining delivery evidence

**D-V2-13 / A2 round 2 — owner decision, 2026-10-12:** “make this simple and
fast to check and verify and get fully to prod.” The owner accepted every
recommended default Q1–Q6 and Q-A1–Q-A4, then replaced Q1's conditional synthetic-
monitor choice with **ONE dedicated eligible probe workspace** after the source
check below. These are settled choices, not requests for another approval of
A1. Q4 still requires a measurement-based numeric target, and Q5 approval of
measurement is not fabricated approval of actual capacity. Concrete §5.2/P6
resource/release evidence remains mandatory. No workspace/key identity appears
here; its pin lives in `scripts/deploy/rollout.sh`.

| Question | Accepted answer / delivery consequence | Alternative only after a new owner decision |
|---|---|---|
| Q1 Shadow cohort | ONE dedicated probe workspace, neither operator nor synthetic monitor; separately delivered `shadow-probe-workspace` provisions it in the Stage D script pattern. Its only traffic is a deterministic probe: direct first-party OpenAI or Anthropic model/endpoint, provider pinned, fallbacks disallowed, exactly one supported candidate, local typed Credits/ordinary program, chat.completions and responses, only ordinary parameters, through the enclave to every regional hostname. Full event/snapshot fit and terminal evidence still verified. | Additional dedicated workspaces may use the same script; each addition changes §5.2 N and requires the corresponding N-specific budget approval and measurement before opt-in. No automatic N=1→N=2 expansion or inclusion of other traffic. |
| Q2 Must admission pilot of old §10 step 7 / detailed runbook step 7 match shadow cohort? | No. Keep the approved operator admission pilot/no-auto-refill/side-effect exclusions/$5 guard; use shadow cohort for eligible evidence and separately measure pilot traffic before admission. | Same workspace requires explicit revision of pilot identity/key/side-effect policy, trust-tier/concurrency/traffic and drain trials; dedicated shadow workspace is not automatically approved for async admission. |
| Q3 Per-region seed? | One durable eligible positive seed per **traffic-serving router region per window**, not per boot/day. Send the dedicated probe to every enclave regional hostname; attribute seeds to actual router booking regions as below. Unseeded required router regions still block G10; enclave hostname coverage is not a router seed. | Fleet-only seed retains every region's timer/transport/fixture/load coverage but labels price-comparison production coverage “not exercised” in unseeded regions. Per-day regional seeds increase probe demand and blocked-day probability; remeasure. |
| Q4 Acceptable residual blocked-day rate? | Choose an explicit target before P6 using §12 confidence bounds; do not choose a numeric target without measured h/d/q/N. Retain BLOCKED on every actual loss. | A target incompatible with observed hard kills calls for lifecycle reliability work or separate durable-handoff design; never loosen loss accounting. Requiring arbitrary-crash certification is outside v2. |
| Q5 Publisher cadence/budget on the single nam6 node? | Approve **measurement** of primary-region contenders, two-second cadence, ≤500ms whole publish cycle and explicit extra 5,000.5 worst-case indexed row visits/s; enable only after newly approved N=1 CPU/RPC/write/row headroom passes <45%. | If rejected or unmeetable, reduce contender/observation cost through a separately reviewed bounded ownership/index design or add capacity; slower freshness or raising the alarm requires a new policy decision, not an implementation shortcut. |
| Q6 Inventory/readiness/closure ownership? | Release tooling owns independent per-boot inventory and ledger, P5 enforces readiness; operators seal only proven closures. | If only revision inventory is available, missing boots remain unknowable and clock cannot start; procure boot-level evidence or separate durable registration/handoff design. |

**Why the synthetic monitor is not this cohort (A2-S10–S13).** In A2R,
`config.py:1036` defaults its model to trustedrouter/monitor; `synthetic/probes.py:1168–1181,1215–1230`
supplies no provider preference. The read-only default-catalog calculation
through `routing.chat_route_endpoint_candidates` gives **70 endpoints: 68 other,
1 OpenAI, 1 Anthropic**; the canonical §4 candidate projection with empty feature
lists is **6,341 bytes** (a lower bound if features must be added). This is local
source/catalog arithmetic, not a live catalog/traffic measurement or test pass.
`billing_snapshot.py:222,259,436–471` requires supported providers and at most
64 candidates. `async_settle_shadow_v2_contract.py:149–163` cannot prove an
all-other exclusion for that mixed set and cannot make a complete supported
snapshot; `:39,166–175` caps the whole event at 2,048 bytes. The v2 candidate
schema itself allows **128**, not 64; 64 is the **price snapshot** cap. Even a
hypothetical candidate-based certificate could not retain this projection.

`services/async_settle.py:290–310,323–329` is the existing async eligibility
mapper: it requires the async header, checks ordinary parameters and conservatively
marks a non-openai/anthropic model prefix partner. That heuristic is **not** an
immutable v2 commercial-program proof; §4.1/decide do not permit an alias prefix
to mint a certificate. Direct gateway probes (`synthetic/probes.py:1728–1746,1851–1867,1899–1916`)
POST authorize/settle to the control plane with only the internal auth header,
bypassing enclave capability/binding: header_absent is not an exclusion. Thus
these configured paths cannot provide certifying coverage as they stand; their
unresolved terminals block their days through G4. The reported **about 85%** of
workspace authorizations and “every day” frequency are **owner-supplied estimates,
not derivable or verified from source**. The conclusion needs no assumed fraction:
any such unresolved terminal blocks its day. No production export was queried.

**Dedicated cohort consequences.** Every cohort terminal must be A (compared);
B from certificates is expected zero and C is a probe/system defect that blocks
its day. Unexpected observed B also violates the eligible-only policy (§4).
Configure volume so active writer-day demand buckets have well over 50 eligible
predictions (target ≥200, subject to measured distribution, churn and budget).
Keep independent E, exact booking/usage, retries/refunds and all health/closure
rules; “eligible by construction” is an input restriction, never a proof bypass.
Do not force traffic onto idle boots or claim that a rate setting guarantees a
minimum on a boot that lives only briefly.

**Regional seed plan and its limit (A2-S14).** In the reference inventory the four
GCP hostnames are `api-us-central1.quillrouter.com`,
`api-us-east4.quillrouter.com`, `api-europe-west4.quillrouter.com` and
`api-us-west1.quillrouter.com` (`enclave_regions.py:7`, `config.py:1030`). Send
both probe routes to each; the complete reviewed enclave inventory, not a
hard-coded four-entry list, governs if more regional targets are in scope.
The gateway-specific routing configuration is newer
than the generic nearest-control-plane comment in enclave_regions.py: its
backend is **us-central1 plus southamerica-east1**, with us-central1 preferred
for all four enclave origins (`scripts/deploy/gateway_edge.sh:13–14`,
`gateway_edge_config.py:69–80,106–119`; `docs/runbooks/gateway-billing-edge.md:3–6,100–106`).
Accordingly **each of the four normally books in router us-central1**; failover
may book in southamerica-east1. This source configuration is not a serving
measurement: P6 must record actual per-terminal router regions and the approved
serving scope. Merely hitting the east/Europe hostname does not seed an east/
Europe router. us-west1 has no router deployment of its own.

All four enclave paths are exercised; only the observed booking router regions
are seeded. For a proven normal billing scope whose only traffic-serving router
region is us-central1, a durable eligible positive there after §8 prerequisites
satisfies Q3; no extra seed on idle router deployments is required. southamerica-east1 remains unseeded until a qualifying probe books
there; us-east4/europe-west4 control-plane deployments are also unseeded by this
normal billing path. If any of these is a traffic-serving router region in the
reviewed window scope, Q3/G10 still requires a real durable eligible seed there
through a reviewed enclave routing/failover trial before claiming coverage.
Do not use direct control-plane probes, infer seeds from hostnames, or silently
narrow inventory/disable the regional gate. If the live topology differs, use
its proven mapping. The accepted default covers actual traffic-serving router
regions, not a promise of comparison evidence on every control-plane deployment;
all deployed writers still owe their inventory/timer/closure/load evidence.


**A1 round-2 defaults accepted by the owner on 2026-10-12 (not reopened):**

| Question | Adopted minimal branch / consequence | Stronger or different branch / consequence |
|---|---|---|
| Q-A1 Freshness-only or publisher-cycle validity? | Freshness-only in Spanner’s domain, separate 500 ms availability accounting; accept the explicit old-wording weakening and measure .25 s server observation alignment/.25 s health reads. No host qualification or confirmation row. If measurements fail, activation stays blocked pending redesign/capacity. | Retain cycle-validity through a separately reviewed completion-evidence protocol. Immediate confirmation is not disproved: 4.50+H<5 or shared tightened .5 s publish allowance, ≤.5 extra transactions/mutations/s and I point rows/s plus native overhead. Requires reader fencing/missing-proof/cache and original-success semantics. |
| Q-A2 Ordinary evidence provenance or independent boot origin? | Use router service identity plus actual boot/day/digest and G2 inventory, as for pages/counters/closures. No P0-A1 or per-boot keys. Explicitly does not defend against a malicious router forging other boots’ evidence. | Fund a separately reviewed signed per-boot origin/authenticated-witness protocol with registration, key lifecycle, independent capture, replay/coverage and operation/churn costs. Apply the stronger threat boundary consistently to underlying evidence, not just one row; activation of that branch waits for its protocol. |
| Q-A3 One handoff attempt with bounds or exact last-reply diagnostics? | Keep accepted D-A1-C: original success required, one result attempt, landed result may survive lost reply; charge [0,1] handoff failure/unknown bound and measure nonlanding blocked-day rate. Failed original close never recovers. | Exact last-write reply survival requires a separately reviewed independent durable outcome witness and costs, or such days remain uncertifiable. Another recursive marker or a retry that clears original failure is not a solution. |
| Q-A4 Terminal-only discharge or early owner classification/continuity? | Close complete writer evidence with committed report obligations, keep report provisional and discharge only from any independently reconciled durable terminal in horizon. No same-enclave continuity or nonterminal disposition; missing terminal blocks issue day. Give up calling a still-running invocation benign before the horizon, with little effect on final reports already waiting for terminals. | If early classification or same-enclave continuity is required, separately review enclave-attested retention plus terminal-to-boot/invocation binding, loss/release/transfer coverage and all wire/fixture/export/operation costs. Round-1 acceptance alone fails H1/H2. Any nonterminal disposition needs its own reviewed evidence rule; expiry alone cannot qualify. |

## 14. Verified current-code claim ledger

This ledger is part of the amendment. Paths/lines refer to the pinned source,
not v1's older citations. It establishes mechanisms only, not production state
or successful execution of the proposed tests. All other implementation behavior
in §§2–13 is normative future work.

| ID | Verified current claim and file:line |
|---|---|
| C1 | Bounded workspace query, inner sentinel, trust read, 500ms LOW/no retry: `R/src/trusted_router/storage_gcp_async_admission.py:15–46`; health kind/key and unresolved sparse-index query: `:49–85`; cadence complete-key transaction and publisher summarizer/newest-observation guard: `:97–183`. Helper is callable directly; flags are not its arguments. |
| C2 | Fast-off returns legacy drain before publication; only fast branch publishes at claim: `R/src/trusted_router/services/settle_outbox_worker.py:89–93`, `:131–150`. This is the installed worker path, not a claim that direct helper invocation is impossible. |
| C3 | Missing/invalid/stale health → unknown, actual timestamp checks and cap/tier/age predicate: `R/src/trusted_router/services/async_settle_shadow_admission.py:183–215`; observer scheduler/stop: `:219–260`. Money health strict exact-key schema and decode: `R/src/trusted_router/services/async_settle.py:111–175`; runtime money reader only when admission enabled: `:217–228`. Shadow install only wires existing readers: `R/src/trusted_router/services/async_settle_shadow.py:569–590`. |
| C4 | Requested eligibility/signing prerequisites and admission-off snapshot projection: `R/src/trusted_router/services/async_settle.py:231–282`; header/replay/feature checks and requested-context construction: `:286–331`. Existing feature reasons: `R/src/trusted_router/billing_snapshot.py:149–205`. |
| C5 | Authorize replay/header_absent and snapshot/signer collapse/caps: `R/src/trusted_router/services/async_settle_shadow.py:139–171`; terminal attempt counters, local size checks before header collection, queue guard: `:202–277`; post-booking prediction and seed: `:384–424`; idle registration/cumulative flush failure: `:464–530`; lifecycle final flush: `:532–601`; observation budgets: `:36–41`; exception isolation: `:544–566`. |
| C6 | Missing-envelope error handling, signature/hash/authorization matching and refund route/stream exceptions: `R/src/trusted_router/async_settle_shadow_compare.py:127–200`. |
| C7 | Per-writer known>0 and unknown fraction: `R/scripts/async_settle/shadow_report.py:180–199`; positive comparator/admission seed and authenticated observed exclusion: `:202–238`; partition/envelope/failure and closed-counter checks: `:459–555`; seed/continuous clock and separate classification metrics: `:661–700`. |
| C8 | Boot/revision/region gate identity sets: `R/scripts/async_settle/pilot_enablement.py:146–152`; pre-enable dormant four pins and budget arithmetic/CPU<45: `:235–280`; shadow-serving prerequisite max: `:291–318`; pre/post N/caps match, shared observer budget/cadence, historical boot sum and cap comparison: `:321–394`. |
| C9 | Existing kinds/fields/reasons and safe enum: `R/src/trusted_router/async_settle_shadow_evidence.py:22–42`; retry backing described as existential: `:63–90`; flush failure produces worker/store_unavailable and sticky gap: `:433–449`. |
| C10 | Binding purpose/type/audience/lifetime, strict claim validation, ≤2048 bytes and separate signer descriptor: `R/src/trusted_router/async_settle_shadow_binding.py:15–76`. |
| C11 | Enclave header/local limits, shadow flag, bounded opaque binding parse and snapshot-required retention: `E/enclave-go/internal/trustedrouter/async_settle_shadow.go:23–28`, `:91–100`, `:117–244`; builder missing usage and typed diagnostic envelope: `:278–300`; canonical bounds/retry header behavior: `:336–424`. |
| C12 | #1667 proof region map equals per-day writer roster, invalid label coverage blocks: `R/scripts/async_settle/shadow_report.py:398–435`, `:454–458`. |
| C13 | Current source pins operator shadow workspace, admission/protection false and mounted signer/epoch: `R/scripts/deploy/rollout.sh:640–653`; full source revision env: `:419`; no-cpu-throttling and true target serving region: `:820–829`. These are source config, not verified serving values. |
| C14 | Entity complete primary key: `R/scripts/deploy/infra.sh:33`; sparse unresolved generated column/index: `R/scripts/deploy/migrate_async_settle_drain_health.sh:24–30`; workspace/status covering index: `R/scripts/deploy/migrate_async_settle_admission.sh:49`. Production DDL presence not verified. |
| C15 | Money ticket purpose/type/300s lifetime and lookup validation: `R/src/trusted_router/async_settle_ticket.py:20–23`, `:58–108`; Go binder verifies ticket type/issuer/audience: `E/enclave-go/internal/trustedrouter/async_settlement.go:98–114`. |
| C16 | Enclave authorize metadata/retention, settle/refund header and send seams: `E/enclave-go/internal/trustedrouter/client.go:482–483`, `:850–871`, `:1047–1081`, `:1215–1227`, `:1369–1380`. |
| C17 | Enclave route cohort and async local-binding suppression while shadow is active: `E/enclave-go/internal/trustedrouter/async_settlement.go:63–85`. |
| C18 | Evidence 2s write/1s read constants (older nearby comments still say one second): `R/src/trusted_router/storage_gcp_async_settle_shadow.py:37–56`; confirmed booking point read: `:252–261`; cap reservations, first sample/dedup, sequence-only flush, manifest and bounded pages: `:263–325`. |

### 14.1 A1 ADD — claims verified in the supplied P1/P2/P3 trees

A1 source roots (read-only, inspected 2026-10-12; no Git SHA verification or test
execution claimed): `P1/` = `/Users/jperla/josh/repos/tr/wt/v2-p1/`, `P2/` =
`/Users/jperla/josh/repos/tr/wt/v2-p2/`, `P3/` =
`/Users/jperla/josh/repos/tr/wt/v2-p3/`. The original C1–C18 block above is preserved
as the original design's historical ledger, not re-certified against these trees.
This block governs all A1 statements about supplied code; every A1 protocol,
capability, limit and test beyond these observations is **proposed work**.

| ID | Verified current-code claim and file:line |
|---|---|
| A1-C1 | P1 HEALTH checks observation/heartbeat order, shape, completeness/count and nominal <5 s age, without slot-time or reply proof: `P1/src/trusted_router/async_settle_shadow_v2_evidence.py:217–237`. HEALTH_CLAIM is only slot/claim wall time/publisher owner. P1 publisher totals are six counters and histograms, with no A1 outcome partition: `:263–264`. |
| A1-C2 | P3 same-slot owner plus strictly increasing slot/observation fences: `P3/src/trusted_router/async_settle_shadow_health_state.py:38–44`; prediction uses P1 `health_usable` only: `:83–105`. Its store reads both fences before mutation: `P3/src/trusted_router/storage_gcp_shadow_observation.py:154–163`. |
| A1-C3 | P3 health read builder returns the existing entity point statement and read returns only body; write supplies client wall datetime: `P3/src/trusted_router/storage_gcp_shadow_observation.py:76–77,96–110,135–141,173–177` and `P3/src/trusted_router/storage_gcp_io.py:82–86`. Snapshot acquisition in observation/read precedes query's scoped budget: `:88–94,165–176`. A1 requires a shadow-only metadata/sentinel/acquisition change, not a claim this exists. |
| A1-C4 | Money AdmissionCache uses cached observation age/p95: `P1/src/trusted_router/services/async_settle.py:98–105`; exact fleet schema and decode use complete/observed/heartbeat age and p95, no publisher elapsed/ack field: `:111–175`; installation uses the admission health reader only with admission enabled: `:217–226`. Money key/read is `settle_drain_control/fleet-v1`, body-only: `P1/src/trusted_router/storage_gcp_async_admission.py:101–112,132–145`. No A1 modification to these is authorized. |
| A1-C5 | P1 identity commits workspace/key/nonce/reservation; authorize contains facts/decision/router locator but no enclave boot/acceptance: `P1/src/trusted_router/async_settle_shadow_v2_evidence.py:66–82,121–144`. P3 marks an authorize reconciled after its page/backing loop without owner validation: `P3/src/trusted_router/async_settle_shadow_journal.py:291–306,351–356`. This is source reasoning, not a rerun of the R5 witness. |
| A1-C6 | P1 eight KINDS: `P1/src/trusted_router/async_settle_shadow_v2_evidence.py:59–60`; CLOSURE omits own ack and SEALED_CLOSURE has only version/policy plus closure digest/ack time: `:312–320`; row dispatch permits no close-result kind/key: `:380–399`; proof attachments have only hash/byte descriptors: `:366–372`. Therefore P1 has no schema-defined durable original-writer handoff; A1 supplies that row without an owner protocol. |
| A1-C7 | P1 authorize projections have policy+exclusion or policy+binding+snapshot, no separate receipt response: `P1/src/trusted_router/async_settle_shadow_v2_contract.py:263–268`; original receipt coordinate/hash is in certificate projection: `P1/src/trusted_router/async_settle_shadow_v2_evidence.py:205–214`. P2 exclusion claims explicitly parse that locator: `P2/enclave-go/internal/trustedrouter/async_settle_shadow_v2.go:28–60,104–107`. Binding inherits only ticket identity/snapshot claims, no receipt locator: `P1/src/trusted_router/async_settle_shadow_binding.py:22–27` and `P1/src/trusted_router/async_settle_ticket.py:27–46`. Round 2 adds no common response locator; only the baseline excluded certificate carries it. |
| A1-C8 | P2 explicitly treats local claims parsing as non-verification: `P2/enclave-go/internal/trustedrouter/async_settle_shadow_v2.go:63–66`; exclusion retention stores opaque certificate/authorization/request dimensions: `:150–193`; terminal emits certificate/attempt/revision: `:217–238`. Compared retention validates binding identity and snapshot then stores request-local shadow state: `P2/enclave-go/internal/trustedrouter/async_settle_shadow.go:187–255`. These inspected retention/emission paths do not produce durable owner acceptance; round 2 deliberately adds no such behavior. |
| A1-C9 | Router pins the P1 fixture manifest: `P1/src/trusted_router/async_settle_shadow_v2_contract.py:38`; P2 checks 23 files, 22 manifest members, byte SHA and variant lists: `P2/enclave-go/internal/trustedrouter/async_settle_shadow_v2_test.go:89–132`. This is verification of test source, not a test-pass claim. |
| A1-C10 | P3 constructs SEALED_CLOSURE in memory/log then retires day/counters: `P3/src/trusted_router/services/async_settle_shadow_health.py:292–305`; unknown transaction readback calls the same acknowledged function: `:397–405`; final failure/unknown accounting is after flush: `:427–445`. This supports the missing handoff/readback distinction without claiming an executed failure reproduction. |
| A1-C11 | Existing native entity DDL has `(kind,id)` primary key and `updated_at` allow_commit_timestamp: `P1/scripts/deploy/infra.sh:33`. This is source DDL, not production schema or emulator execution. The money control helper supplies a wall datetime, not an automatic commit timestamp: `P1/src/trusted_router/storage_gcp_async_admission.py:139–145`. |
| A1-C12 | P1 v1 constants WRITE=4/RPC=1: `P1/src/trusted_router/storage_gcp_async_settle_shadow.py:64–66`; computed flush=8 and observation=17: `P1/src/trusted_router/services/async_settle_shadow.py:33–41`. P3 separate v2 constants WRITE=2/READ=1/FLUSH=4: `P3/src/trusted_router/storage_gcp_shadow_observation.py:38–40`. Historical §6.1 “current” 2/1/4/9 wording is explicitly corrected. |
| A1-C13 | P1 completion original terminal-only, same-writer obligation in docstring, strict seq and event-time ordering: `P1/src/trusted_router/async_settle_shadow_v2_evidence.py:180–200`. P3 adds authorize originals and drops same-day seq comparison whenever reference supplied: `P3/src/trusted_router/async_settle_shadow_v2_evidence.py:180–212`. Journal only validates a completion original if found in memory and specialized path permits another boot: `P3/src/trusted_router/async_settle_shadow_journal.py:337–368`. A1 supplies context rules, not approval of these relaxations. |
| A1-C14 | P1 event/page/counter/closure transaction limits: `P1/src/trusted_router/async_settle_shadow_v2_evidence.py:44–58,203–204,402–414`; exact integer/identity/digest/revision primitives: `P1/src/trusted_router/async_settle_shadow_v2_schema.py:34–46,70–72,90–97`. Arithmetic in A1 is a design derivation, not native serialization/RSS measurement. |
| A1-C15 | P3 checks slot time before state-lock acquisition and counts attempted claim error as missed unless won: `P3/src/trusted_router/services/async_settle_shadow_health.py:211–245`. Closure's in-flight publisher check consults publishing set: `:421–422`. A1 requires pre-submission day ownership and conservation rather than assuming this draft meets R8/R9. |

### 14.2 Round-2 source/platform verification

`W/` is this worktree (`/Users/jperla/josh/repos/tr/wt/v2-design-a1/`);
`SDK/` is the inspected installed
`/Users/jperla/josh/repos/tr/wt/v2-p3/.venv/lib/python3.12/site-packages/`.
This is read-only source/API inspection, not a new implementation, emulator run,
Git identity check or production measurement. §14.1 retains round-1 source facts;
only its obsolete proposed owner-package conclusions have been removed.

| ID | Verified claim / exact file:line |
|---|---|
| A2-C1 | Existing entity DDL permits commit timestamp in updated_at: `W/scripts/deploy/infra.sh:33`, `W/tests/conformance/spanner_ddl.py:26`. General entity mutation helpers supply COMMIT_TIMESTAMP: `W/src/trusted_router/storage_gcp.py:7010–7022`. P3's dedicated writer instead supplies host datetime and returns only body: `P3/src/trusted_router/storage_gcp_shadow_observation.py:135–141,173–177`. Thus a shadow-only writer/reader amendment is necessary, no DDL. |
| A2-C2 | Lockfile and installed distribution are google-cloud-spanner 3.69.1: `W/uv.lock:1502–1503`, `SDK/google_cloud_spanner-3.69.1.dist-info/METADATA:3`. `SDK/google/cloud/spanner_v1/snapshot.py:970–990` requests return_read_timestamp=True, defaults to strong and consumes returned timestamp. Public streamed metadata access: `SDK/google/cloud/spanner_v1/streamed.py:66–74`; metadata retained on first response including empty values: `:150–167`. Commit timestamp is also available on Transaction.committed, `SDK/google/cloud/spanner_v1/transaction.py:325–328`, but the selected design uses the observation read timestamp, not a claim-commit timestamp. No extra RPC is inherent. |
| A2-C3 | Native CI emulator is pinned by digest and conformance invoked: `W/.github/workflows/ci.yml:34–76`. Emulator adapter validates snapshot options then removes only exact/max staleness: `W/tests/conformance/spanner_emulator.py:98–109`; native SQL acceptance seeds CURRENT_TIMESTAMP: `W/tests/conformance/test_spanner_sql_acceptance.py:49–57`. Combined with official supported-feature docs this establishes mechanism feasibility, not an exact-image A1 pass. |
| A2-C4 | Money age clock and percentile: `W/src/trusted_router/storage_gcp_async_admission.py:189–221` samples time.time, subtracts stored timestamp and uses ceiling-rank percentiles. `W/src/trusted_router/services/async_settle.py:98–105,166–175` checks cached age/p95≤5. P3 currently uses integer wall-microsecond age subtraction: `P3/src/trusted_router/async_settle_shadow_health_state.py:47–79`, with host observations supplied at `P3/src/trusted_router/storage_gcp_shadow_observation.py:165–171`. A1 must preserve money-threshold parity in shadow code only; nanosecond freshness is a different input. |
| A2-C5 | P1 terminal-only completion and original prediction/result preservation: `P1/src/trusted_router/async_settle_shadow_v2_evidence.py:180–200`; HEALTH and CLAIM current shapes/predicate: `:217–237`. The supplied P3 page reconciliation has no terminal obligation export commitment: `P3/src/trusted_router/async_settle_shadow_journal.py:291–306`; existing local sealed acknowledgement is memory/log then retirement: `P3/src/trusted_router/services/async_settle_shadow_health.py:292–305`. Round 2's explicit local/report boundary and result row remain proposed changes. |
| A2-C6 | Enclave fixture test checks directory inventory, every file hash, manifest and variant list: `P2/enclave-go/internal/trustedrouter/async_settle_shadow_v2_test.go:89–132`. Its terminal carries certificate/attempt/revision, no authenticated sending-boot binding: `P2/enclave-go/internal/trustedrouter/async_settle_shadow_v2.go:217–238`. Hence new manifest requires every-file pin follow-up, while terminal-only discharge requires no new enclave behavior. |

Official sources in §3.1.1 were consulted for timestamp/current-time/TrueTime and
emulator semantics. Exact-image A1 statements, empty-result metadata behavior,
wire/schema fixture bytes, all resource limits and deployment feasibility still
require the proposed implementation proofs. No new SQL was executed here.

A1 did not run code tests, measure CPU/latency/storage overhead, query production,
verify serving identity, write fixtures, mutate code or perform Git writes. The
review reports' tests/measurements remain attributed review results, not A1
validation. Round 2 requires no independent per-boot-origin/ownership export
capability. Ordinary health attachments still need P3/P5 implementation and
measured transport coverage; no existing execution or activation is inferred.

Unverified: raw production export and all input production counts, missing live
health, serving release/queued rollout, CPU extrapolation/sizing artifacts,
dedicated probe identity/actual traffic and regional booking evidence, future protocol/schema/load/closure
feasibility, independent boot roster availability, and all proposed PR/test
outcomes. The official Cloud Run contract establishes platform behavior, not
that this deployment finishes shutdown in its grace. These are explicit P6
measurement/proof gates and §13 choices, not excuses for dropping evidence.

### 14.3 A2 ADD — source verification for amendment A2

`A2R/` is the read-only reference tree
`/Users/jperla/josh/repos/tr/wt/v2-p1a1/`, inspected 2026-10-12. It is the
supplied P1-A1 review tree; no claim is made that it is deployed or merged.
A2-S identifiers are deliberately distinct from A1 round 2's old A2-C identifiers.
Every A2 implementation claim is limited to this ledger. Normative A2 schemas,
limits, adapter checks and fixture files above are future work, not current code.

| ID | Verified code claim; paths relative to A2R/ |
|---|---|
| A2-S1 | Baseline PAGE/EVENT/MANIFEST/WRITERS/GAPS/terminal/event/key bounds: `src/trusted_router/async_settle_shadow_v2_evidence.py:44–58`; DIMENSIONS and locator: `:74–82`; 144 bucket count: `:303–311`; MANIFEST/ARTIFACT/LEDGER/PROOF/GATE shapes: `:343–377`. A1 only extends proof/day roots and adds contract_revision to gate/ledger: `src/trusted_router/async_settle_shadow_v2_a1_evidence.py:39–46`; root-name sets: `src/trusted_router/async_settle_shadow_v2_a1_deltas.py:12–13`. GATE_OUTPUT has coverage_sha256, not a metrics payload, and only enforces PASS⇒empty gaps. |
| A2-S2 | COVERAGE_ENTRY exact fields/states, sorted-key/chain checks and page-digest-array root: `src/trusted_router/async_settle_shadow_v2_a1_evidence.py:117–151`. Obligation keys/pages and entry-array roots: `src/trusted_router/async_settle_shadow_v2_a1_reconciliation.py:83–115`. A1 close validation uses canonical object hashes: `src/trusted_router/async_settle_shadow_v2_a1_evidence.py:70–103`; report joins explicitly require independent backing reconciliation: `src/trusted_router/async_settle_shadow_v2_a1_reconciliation.py:151–173`. These functions do not establish a PASS from an arbitrary digest string. |
| A2-S3 | Generator fills day roots with ZERO at `tests/shadow_v2_fixture_generator.py:163–167`, ledger artifacts at `:168–171`, proof fields at `:172–175`; no end-to-end gate meaning follows. Exact primitives and byte/schema enforcement: `src/trusted_router/async_settle_shadow_v2_schema.py:45–50,63–109,218–258`; canonical bytes: `src/trusted_router/detached_jws.py:60–62`. Fixed retained dimensions are 4×4×3: `src/trusted_router/async_settle_shadow_v2_retained.py:96–102`; baseline requested adapter/route enums: `src/trusted_router/async_settle_shadow_v2_contract.py:57–58`. |
| A2-S4 | Binding imports ticket and storage_models at `src/trusted_router/async_settle_shadow_binding.py:11–13`; signature and one verification implementation at `:49–58`, separate claims validator at `:35–46`. Certificate full signature/backing/terminal/booking verification and exact signature: `src/trusted_router/async_settle_shadow_v2_certificate.py:214–243`; signature identity context: `:95–107`; ReceiptVerification fields: `:122–126`; original receipt checks: `:129–174`. TrustedKey fields: `src/trusted_router/detached_jws.py:27–33`. |
| A2-S5 | Pure AST import allowlist excludes binding/storage modules: `tests/test_async_settle_shadow_v2_evidence.py:402–416`; fresh-interpreter storage/services/main/routes guard: `:447–460`. A2 adopts a port to preserve these boundaries, not because claims validation is signature verification. |
| A2-S6 | V1 percentiles and signed bins: `scripts/async_settle/shadow_report.py:52–64`; eleven external gate labels: `:45–47`; DAG/earliest rollback/synthetic resolution provenance: `:241–292`; exact resolution shape/128 bound/uniqueness: `:315–328`; reset collection before day filtering and persistent missing support: `:335–380`; fix/clock logic: `:661–688`; separate comparator/admission metrics, nulls and timing: `:690–755`. Digest-shape external checks at `:395–400` are not independent artifact verification; A2 expressly requires actual preimages. |
| A2-S7 | Artifact hashes actual file bytes: `scripts/async_settle/pilot_enablement.py:41–44`; explicit-key 10,000/512-character and LOW/200 ms/no-retry reads: `:55–70`; serving roster/lifetime and gate subset validation: `:122–152`; regional/counter coverage: `:155–192`; dormant pre-enable measurement/approval/N/cap and strict CPU checks: `:226–288`; shadow-serving gate identity and linked pre-enable: `:291–318`; exact pre/post matching and superseded historical sum: `:321–394`; retained pre-flip signer/drain/CI and post-flip rollback/status/timing rules: `:397–463`. These functions perform file reads; they belong in the offline tool boundary, not the pure contract. |
| A2-S8 | Frozen evaluator file path/pin and byte check: `tests/test_billing_snapshot.py:20–22,224–228`; frozen binding/comparison file path and byte check: `tests/test_async_settle_shadow.py:38,76–77`; P1 fixture-manifest pin: `src/trusted_router/async_settle_shadow_v2_contract.py:38`; A1 independent manifest pin: `src/trusted_router/async_settle_shadow_v2_a1_evidence.py:31`. These are code pins, not invented A2 release hashes. |
| A2-S9 | Existing test source exercises fleet freshness/coverage and pre/post-flip artifacts/timings: `tests/test_async_settle_pilot_tools.py:202–266`; pre-enable offline CLI and measurement boundaries: `:445–501`; matching pre-enable and bounded roster lifetimes: `:504–538`. Reset tests exercise unknown/predecessor revisions and cycles: `tests/test_async_settle_shadow_r13_rollback.py:165–198,268–278`; missing-support/ambiguous-history witnesses: `tests/test_async_settle_shadow_r14_resolution.py:48–103`. Reading these assertions is not a test execution/pass claim. |

| A2-S10 | Monitor model default: `src/trusted_router/config.py:1036`; chat and Responses probe bodies without provider preference: `src/trusted_router/synthetic/probes.py:1168–1181,1215–1230`. Alias expansion: `src/trusted_router/routing_candidates.py:215–258,417–418`; endpoint expansion/filter/sort: `src/trusted_router/routing.py:248–284`. Read-only local evaluation with Settings defaults yielded 70 candidates (68 other, one OpenAI, one Anthropic) and 6,341 canonical bytes for `{endpoint_id,adapter,features:[]}` projections; no production state or traffic ratio measured. |
| A2-S11 | Price snapshot supports provider openai/anthropic only and ≤64 candidates: `src/trusted_router/billing_snapshot.py:222,259,436–471`. V2 candidate-set cap is separately 128: `src/trusted_router/async_settle_shadow_v2_contract.py:39–41,72–73`; fixed commercial proof/all-other check/supported-context requirement: `:128–163`; whole-event 2,048-byte fit: `:166–175`. Source reasoning: the default mixed monitor set has neither supported complete snapshot nor all-other exclusion proof; model prefix is not immutable commercial-program evidence. |
| A2-S12 | Direct control-plane probe URLs/headers/bodies: `src/trusted_router/synthetic/probes.py:1728–1746,1851–1867,1899–1916`, without enclave shadow/async headers. Existing async mapper header, ordinary parameters and conservative model-prefix condition: `src/trusted_router/services/async_settle.py:286–329`. NEVER_EXCLUSIONS includes header_absent: `src/trusted_router/async_settle_shadow_v2_contract.py:52–54`. About 85% unresolved and daily frequency are owner estimates, not verified by source or measured here. |
| A2-S13 | Dedicated provisioning pattern, dry-run/apply, named identity/key/funding inputs and local typed key result: `scripts/provision_stage_d_probe_workspace.py:1–7,39–84`; this is Stage D, not a claim the new shadow package exists. Provider.only filters endpoint provider: `src/trusted_router/routing.py:1065–1075`; fallbacks default true unless explicitly disabled: `:523–551`; no-fallback candidate truncation: `:281–284`. Probe must verify the resulting single supported candidate, not rely on model branding. Shadow workspace pin is in the ENV_VARS array: `scripts/deploy/rollout.sh:643`; no identity copied here. |
| A2-S14 | Four GCP enclave origins/no west router: `src/trusted_router/enclave_regions.py:7–19`; default regional hostname template: `src/trusted_router/config.py:1030`; control-plane list (including São Paulo) distinct from enclave list: `scripts/deploy/_lib.sh:18–45`. Gateway-specific primary/failover: `scripts/deploy/gateway_edge.sh:13–14`; exactly-two-region preflight and backend construction: `scripts/deploy/gateway_edge_config.py:69–80,106–119`. Authority/path and placement caveat: `docs/runbooks/gateway-billing-edge.md:3–6,100–106`. Intended normal booking for all four origins is us-central1, not four router regions; live routing and seeds remain P6 evidence. |
| A2-S15 | P2 certificate is optional: `P2/enclave-go/internal/trustedrouter/async_settle_shadow_v2.go:162–193`; no certificate with valid binding/snapshot returns to compared path. Excluded terminal builder needs retained exclusion state: `:217–238`; P2 root is defined in §14.1. Therefore deferring issuance requires no P2 behavior change. P2 merged status is owner-provided; not a Git or serving-state verification. |


Round-1 A2 recorded reading both `/tmp/v2-design-a2/p5-author-questions.md` and
`/tmp/v2-design-a2/test_contract_blockers.py` completely. The latter contains
four supplied executable witnesses, including 129 closures and the long prefix;
A2 inspected their code but did not execute them. Their hard-coded v2-p5 tree
is not the source root for A2's code claims. Round 2 performed read-only source
inspection and local default-catalog/count/byte arithmetic (A2-S10); it ran no
tests, production queries or performance/traffic measurements, created no fixtures,
edited no code and performed no Git writes. `git diff --check` is a document
whitespace check, not CI or implementation proof.
Only this design document and the requested scratch decision report are deliverables.
