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
sentence, requiring Joseph's Q-A1 approval before activation. It does not relax
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
proof, cache invalidation and missing-proof rejection. Q-A1 chooses whether
preserving cycle-validity is worth that complexity and measurement burden. A1
recommends freshness-only validity for its narrower claim, not because immediate
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
| Old enclave, v2-active router | No verified v2 capability: do not emit v2-only certificate; preserve legacy result, count protocol coverage gap; cannot certify |
| New enclave, active new router | Full v2 protocol and per-boot marker/attested-image evidence required |

Proposed marker: `X-TR-Settlement-Shadow-Policy: shadow-v2` on authorize only;
optional response marker `billing_shadow_policy:"shadow-v2"`. It is a protocol
capability, not opt-in or authentication. Shadow workspace ownership remains
server-side. Deployment evidence pins build capability
`shadow-v2-read-echo` before router `shadow-v2-emit`; an arbitrary caller marker
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

## 8. Gate v2 — executable predicate specification

**D-V2-8 — one shared predicate implementation.** `shadow_report` and pilot
`fleet-budgets`, `shadow-serving`, `pre-flip` must call the same versioned
validators, not subtly different copies. In the table, gaps are stable suffixes
prefixed by day/boot/phase/dimension or object ID as applicable. Evaluate all rules
and retain all failures; schema damage yields BLOCKED, never skipped rows.

| Rule | Exact PASS predicate | Replaces/retains v1 rule | Gap/reset name |
|---|---|---|---|
| G1 Policy/schema | All certification rows, proof, wire provenance and gate artifacts exactly shadow-v2, bounded and hash-verified; key/body identity agrees; no coercions; referenced objects present | shadow §§4/6 exact v1 schema (C7) replaced, strictness retained | `policy_version_gap`, `evidence_schema_gap`, `artifact_gap` |
| G2 Deployment | Complete independent boot/lifetime inventory equals all day manifests/counters/closures; full revisions and #1667 per-boot regions; all four flags off throughout; every traffic boot has §5.3 readiness before its first opted attempt; ledger hash chain complete | shadow §6 / outbox step 5 global gate timestamp and identity coverage (C8/C12) | `writer_roster_gap`, `regional_roster_unknown`, `legacy_region_label`, `legacy_sample_region`, `flag_interval_gap`, `boot_prerequisite_gap` |
| **A1 AFTER G3** Authorize obligations | Fresh+replay equation equals the complete durable decision receipts/facts. Closure commits all report obligations through §7.3; writer closure/HWM is not discharge. P5 enumerates open count/root and reconciles every authorization only to actual durable terminal evidence within its original horizon, regardless of router boot/enclave. No nonterminal disposition or same-enclave claim. Provisional through forward coverage; missing/invalid/conflicting terminal blocks issue day | Replaces unspecified owner/link permission with terminal-only report discharge; all original factual backing rules retained | `authorize_coverage_gap`, `authorization_link_gap`, `authorization_obligation_gap`, `authorization_terminal_conflict`, `authorization_horizon_gap` |
| G4 Terminal partition | T=settle+refund=A+B+C per writer/day/phase/dimension; C=0; A has compared proof, B has valid certificate or retained observed-exclusion proof; each receipt exactly once; phase sums equal aggregate; no envelope-present=T assumption | shadow §§3/4/6 and C7 envelope/observed/outcome equations | `observed_partition_gap`, `terminal_coverage_gap`, `phase_coverage_gap`, `exclusion_proof_gap` |
| G5 Eligible comparisons/predictions | Every eligible attempt represented in E independently of predictions; every A exact/explained with unchanged arithmetic/provenance and booking; Y+N+U=E, E>0⇒Y+N>0 and 100U≤2E; E=0⇒Y=N=U=0 with known closed partition; no unknown seed | shadow §5 per-counter known>0 and C7 known_exclusion admission requirement | `eligible_coverage_gap`, `prediction_demand_gap`, `prediction_unknown_gap`; correctness classes and `exclusion_conflict` cause resets |
| **A1 AFTER G6** Timer/health | Observer cadence/failure/streak/degraded gates and source isolation; exact Spanner O/C/R plus q/e predicate, slot/version fences and 250 ms health-read deadline (§3.1.1). Independently reconcile scheduled/attempted/published/failed/unknown/missed counters and traces, including queued old-day work. Usable late-landed rows never count as timely publications | Removes only missed-cycle⇒invalid-publication implication; strengthens shadow read deadline and server observation-slot bound; no host qualification | `observer_cadence_gap`, `observer_budget_gap`, `shadow_health_gap`, `health_source_gap`, `health_timestamp_gap`, `health_elapsed_gap`, `health_slot_gap`, `publisher_accounting_gap`, `publisher_trace_gap` |
| **A1 AFTER G7** Fleet load | Simultaneous/cap/startup/churn sweep, exact approvals, strict CPU<45%; include metadata, health attachments with full §7.2 operations/re-runs/cleanup, report joins and separate close-result transactions/bursts. Conservative 4.25+.25=4.50 chain and measured component deadlines; over-budget cycles remain failures | No load allowance increases; removes ownership/signature/clock-service budgets because those mechanisms are absent | `simultaneous_capacity_gap`, `pre_enable_budget`, `publisher_budget_gap`, `fleet_headroom_gap`, `freshness_budget_gap`, `evidence_export_budget_gap`, `close_handoff_budget_gap` |
| **A1 AFTER G8** Durability | Page/HWM/counter/no-loss rules and same-writer terminal-original completion context/order. Require original boot’s insert-only acknowledged CLOSE_RESULT, exact closure/day/boot/digest/checkpoint binding and independent inventory/coverage under §2 provenance. Failed original close never recovers. Unknown handoff reply can recover only landed fact; charge conservative bounds. Writer sealing never bypasses G3 | Defines durable later acknowledgement without a separate signing trust domain; ordinary checkpoint recovery unchanged | `checkpoint_reconciliation_gap`, `evidence_loss_gap`, `writer_closure_gap`, `retry_original_gap`, `close_result_identity_gap`, `close_result_conflict`, `handoff_coverage_gap` |
| G9 Resets | Zero unexplained mismatch including counters/unsampled/conflicting retry; preserve all earlier mismatch/fix/rollback history, exact supporting evidence and reviewed revision partial order; no admission-unknown hiding a mismatch | shadow §3.1/§6 reset and revision-rollback rules verbatim | Existing correctness resets, `revision_rollback`, `missing_support`; new `exclusion_conflict` |
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
support/reset provenance stays persistent. Output both comparator and admission
coverage, A/B/C and E, original/retry counts, checkpoint failure/recovery/loss,
per-writer ratios including `not_exercised`, publisher and fleet peaks, actual
regions, null counts/timing/deltas and first seed/reset/gap timestamps.

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
   erase losses. **A1 BEFORE → AFTER:** land baseline P1/P2, then P1-A1 and the enclave fixture-pin follow-up,
   revised P3/P4/P5 dormant in §10 dependency order. Deploy enclave
   `shadow-v2-read-echo` everywhere first, shadow and negotiation off; deploy
   router v2 readers/code with policy empty and shadow workspace list empty.
   Admission/protection/fast drain remain false. Record every serving build,
   true region, boot and attested image; partial rollout is not ready.
2. Provision/verify signer, positive authority epoch and public keyrings on all
   revisions as retained key-placement procedure requires; no new production
   key values in this document. Prove ticket/binding/exclusion separation and
   dormant money oracles. **A1 ADD:** baseline P2 capability and the A1 fixture-pin follow-up must be
   present before P4 emission; no new enclave protocol or origin registry. Prove
   terminal-only report reconciliation and original-boot close-result production.
   Obtain Joseph’s Q-A1–Q-A4 choices before P6.
3. **Replace old step 3:** choose one shadow workspace (§13) and record its full
   operator-supplied ID/key separately; do not expand prefix d385c399… by guess.
   Collect immutable-fact/candidate/size distributions and actual-hop maximum
   auth+12,288-byte header tests, full/hash-only/certificate variants and
   fragmented delivery in every region. Run new `fleet-budgets-pre` with N=1,
   configured caps including surge, publisher contender K and all §3/§7 load.
   Measure bounded read/claim/publish cycles against the real adapter in an
   isolated controlled trial, reviewed before production opt-in; include this
   source in sizing. **A1 ADD:** measure server snapshot-slot alignment, 250 ms shadow health
   reads, full elapsed-age charge and ordinary attachment export operations/churn;
   size close-result transactions under simultaneous shutdown. No daemon is implicitly authorized by an old pre-enable PASS.
4. **Replace old step 4:** reviewed config-as-code activation P6 sets router
   policy shadow-v2, health region, and single shadow workspace; then enclave
   shadow on with negotiation off. Bootstrap/old-authorization traffic is recorded
   but noncertifying. Warm caches, issue and echo both variants, prove no money
   worker calls. Measure one opted workspace on the actual single nam6 node:
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
7. **Replace old step 7 selection inputs:** confirm admission-pilot workspace
   separately (§13), obtain its own traffic/concurrency/side-effect measurements,
   select all drain settings from real regional trial exports and run v2 pre-flip.
   Verify signer/epoch/keyring and same-price/status/rollback gates. Seven shadow
   days do not substitute for D2/D3, handoff or drain proof.
8. **Retain old step 8 money order, add source handover:** after shadow PASS,
   protection on → verify fencing → measured fast drain on → verify real fleet-v1
   worker health plus §3.2 re-verifications → install pilot allowlist/$5 override
   → admission on → enclave negotiation on and shadow off for that pilot.
   Stop shadow publisher/close evidence before claiming handover complete; if
   temporarily overlapping health sources, budget both and prove no cross-use.
   No manual env edit, skipped attestation or queued-rollout claim.
9. **Old step 9 and rollback retained:** admission/negotiation off first; retain
   protection/keys and drain until all accepted work is durably resolved; only
   then protection off. Re-enable solely through the gated sequence, export
   actual regional handoff/booking/repair percentiles and rollback evidence.

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

**A1 round 2 — D-V2-10:** retain P1–P6, add only contract package P1-A1
and an enclave fixture-pin follow-up. P0-A1 and behavioral P2-A1 are removed. Each
package carries its own tests; P1/P2 cross-repo fixture copies are an ordered
contract pair. Dormancy means no new task/RPC/parsing/serialization/counter on
empty v2 policy or nonmember paths, except the existing constant guard; no hidden
change to existing v1 behavior. Do not update a frozen oracle to absorb a surprise.

| Package | Repo, scope and dependencies | Dormancy, proof obligations and deployment/rollback |
|---|---|---|
| P1 `shadow-v2-contract` | Router schemas, pure decision/certificate validators, wire fixtures and evidence schema; no live imports/effects. Establish §7 fixture manifest. | Pure dormant contract. **WIRE**: exact schema/limits/three-purpose rejection/fallback matrix; existing billing literals unchanged. Deploy has no effect. |
| P2 `shadow-v2-enclave-echo` | Enclave copy P1 bytes; retention/variant/header and capability support at all §4 seams; async binder rejects certificate. Depends P1 contract. | Existing shadow switch plus v2 router marker gate new work. **ECHO/OFF**: real stream/refund/route and byte oracles, retry frozen bytes, no async bind. Land and attest/deploy everywhere **before P4 emits**. Can revert while emission dormant; active rollback first disables router emission/ends certification. |
| **A1 ADD P1-A1** `shadow-v2-contract-a1` | Depends baseline P1. Pure exact O/C/R+q/e reader predicate, changed HEALTH timestamp/age-input fields and HEALTH_CLAIM server-slot timestamp, publisher conservation/basis/trace, terminal-only contextual completion, closure obligation marker/report join schema, CLOSE_RESULT kind/sealed projection, coverage/retention allowlists and new fixture release (§§3/7) | No live imports/RPC/lifecycle. Baseline bytes frozen; explicit new manifest/negative fixtures. Enclave follow-up copies/pins **all** files/variants with no behavior change. |
| P3 `shadow-v2-durable-observation` | Depends P1-A1; independent health publisher/reader, exact SDK metadata/sentinel, bounded receipt HWM/checkpoint/closure, committed report obligations, ordinary health attachment export and original-boot one-attempt CLOSE_RESULT. No certificate issuance yet | Policy empty means dormant. Native timing/races/faults/SIGTERM/SIGKILL and same-writer recovery proofs; preserve money/shared adapters unchanged. Terminal obligations survive local retirement via durable receipt chain. Measure export/close costs. |
| P4 `shadow-v2-cohort-integration` | Depends P1-A1/revised P3 and deployed baseline P2 plus A1 fixture pins. Authorize decisions/certificate issuance, terminal dispatch/links/demand, no ownership data or new wire | Opt-in/capability gated, off/nonmember pure. Existing PARTITION/MONEY/stream/byte and actual terminal reconciliation proofs; no shadow HTTP error or synchronous evidence lookup. |
| P5 `shadow-v2-gates` | Depends P1-A1/revised P3/P4 and P2 wire/fixture evidence. Shared G1–G11, inventory/readiness, bounded exports, exact obligation enumeration and forward terminal joins, CLOSE_RESULT identity/coverage and handoff bounds, health replay/trace conservation | Offline tools explicit policy; no request storage. Full window/churn, missing-terminal/receipt/result and changed-version negatives. No clock qualification, owner collector or new boot-signature verifier; existing certificate/binding verifiers remain mandatory. |
| P6 `shadow-v2-measured-opt-in` | Depends P1–P5, P1-A1 and enclave pin follow-up, Joseph’s choices and exact-revision CI/resource gates. Reviewed single-cohort router/enclave config/attestation releases; later money pilot is separate | Active release after dormant gates; actual N=1 measurements include 250 ms reads, server slots, export churn and close bursts. Preserve money off pins; opt-out/closure/export on rollback. |

P3 combines health and persistence because publisher lifecycle/operation evidence
must itself use the new counters/closure; splitting either into an active partial
publisher is not a useful deploy. P4 is separate from P5 so wire/isolation review
is not buried in gate arithmetic. No money admission PR is bundled with fixes
to shadow certification. Reviewed CI, attestation, release workflows and rollout
gates remain intact in both repos.

## 11. Test and proof matrix

**D-V2-11 — negative controls must kill compiled mutations.** Proposed tests,
not passing results. Use scratch mutants, never leave mutant code in either repo;
a syntax/import failure is not a kill. Exact named suites may be split by authors,
but each proof obligation below must appear in CI artifact inventory.

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
lost by removing ownership. Q-A1–Q-A4 retain the real cost/policy choices.

## 13. Open questions for Joseph

**D-V2-13 — explicit operator choices, no guessed production identities.** These
choices precede P6; they do not reopen the decision to amend the gate or permit
weaker correctness. Recommendations below are proposed defaults, not asserted
approvals.

| Question | Recommended default | If Joseph chooses otherwise |
|---|---|---|
| Q1 Shadow cohort | Replace operator workspace `45819281…` with synthetic-monitor workspace `d385c399…`, **one opted workspace**. The abbreviated monitor ID `d385c399…` and roughly 25 OpenAI-provider requests/day from us-central1/europe-west4 are **gatekeeper-reported and unverified**, not established by the supplied inputs; confirm the full ID and measure traffic before P6, including immutable program/fallback eligibility, provider usage and actual seeds. Other requests require authenticated exclusions, not automatic partner exemption. | Keep operator workspace and add a small dedicated eligible probe **in that same workspace**, with owned key/routing and measured load; reported 141/141 partner authorizes supplied no eligible seed. A separate probe workspace makes N=2 and needs new approval/measurements; old budget fails. Neither proposal guarantees every monitor OpenAI request is actually eligible. |
| Q2 Must admission pilot of old §10 step 7 / detailed runbook step 7 match shadow cohort? | No. Keep the approved operator admission pilot/no-auto-refill/side-effect exclusions/$5 guard; use shadow cohort for eligible evidence and separately measure pilot traffic before admission. | Same workspace requires explicit revision of pilot identity/key/side-effect policy, trust-tier/concurrency/traffic and drain trials; monitor workspace is not automatically approved for async admission. |
| Q3 Per-region seed? | One durable eligible positive seed per **traffic-serving router region per window**, not per boot/day. If cohort reaches only two regions, route a bounded eligible probe to remaining serving regions or explicitly restrict reviewed serving scope before window. | Fleet-only seed retains every region's timer/transport/fixture/load coverage but labels price-comparison production coverage “not exercised” in unseeded regions. Per-day regional seeds increase probe demand and blocked-day probability; remeasure. |
| Q4 Acceptable residual blocked-day rate? | Choose an explicit target before P6 using §12 confidence bounds; do not choose a numeric target without measured h/d/q/N. Retain BLOCKED on every actual loss. | A target incompatible with observed hard kills calls for lifecycle reliability work or separate durable-handoff design; never loosen loss accounting. Requiring arbitrary-crash certification is outside v2. |
| Q5 Publisher cadence/budget on the single nam6 node? | Approve **measurement** of primary-region contenders, two-second cadence, ≤500ms whole publish cycle and explicit extra 5,000.5 worst-case indexed row visits/s; enable only after newly approved N=1 CPU/RPC/write/row headroom passes <45%. | If rejected or unmeetable, reduce contender/observation cost through a separately reviewed bounded ownership/index design or add capacity; slower freshness or raising the alarm requires a new policy decision, not an implementation shortcut. |
| Q6 Inventory/readiness/closure ownership? | Release tooling owns independent per-boot inventory and ledger, P5 enforces readiness; operators seal only proven closures. | If only revision inventory is available, missing boots remain unknowable and clock cannot start; procure boot-level evidence or separate durable registration/handoff design. |

**A1 round-2 choices (recommendations, not asserted approval):**

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
monitor full identity/traffic/eligibility, future protocol/schema/load/closure
feasibility, independent boot roster availability, and all proposed PR/test
outcomes. The official Cloud Run contract establishes platform behavior, not
that this deployment finishes shutdown in its grace. These are explicit P6
measurement/proof gates and §13 choices, not excuses for dropping evidence.
