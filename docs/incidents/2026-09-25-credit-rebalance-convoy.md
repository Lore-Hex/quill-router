# 2026-09-25: starter-credit rebalance convoy

## Timeline and impact

The supplied incident investigation identified two **TR Gateway: billing path
5xx** alerts at **10:44Z** and **11:33Z** on September 25, 2026. Workspace
`61f32065` saw 24 authorize HTTP 503s over its two-hour life. It accounted for
125 of the fleet's 141 credit rebalance transactions in the observed 13-hour
window. One instance handled a burst of 12 requests in 12 seconds.

The starter-credit workspace had $25.30 of total credits spread over 16
`tr_credit_balance` shards (about $1.58 per shard), following
`DEFAULT_NEW_BILLING_SHARDS`. Authorize estimates were $0.84–$1.42; actual
spend was approximately eight times smaller. As usage and live reservations
reduced available credit, no individual shard could fund another estimate
even when aggregate headroom remained sufficient.

## Mechanism

The cold authorize path performs an all-shard read-only headroom precheck,
then a cross-shard read-write rebalance, then retries reserve. Each
rebalance still moves only `estimate - target_available` onto that request's random
target. Its own reserve consumed the new headroom, leaving followers with no
funded shard to reuse. The fragmented balance therefore needed one all-shard
transaction per request.

The process-local 0.5-second rebalance cooldown protects Spanner from that
stampede. Followers rechecked after two 0.25-second sleeps, but a peer's
one-request repair gave them no reusable capacity. On the third blocked
attempt they raised `StoreUnavailable("credit escrow rebalance is busy; retry")`,
which became HTTP 503 on `/internal/gateway/authorize`.

## Scope of this PR: correctness and telemetry, not convoy removal

**This PR does NOT remove the convoy class.** The root cause is
`DEFAULT_NEW_BILLING_SHARDS=16` for a roughly $25 starter workspace. The fix
requires a shard count that follows the balance: starter workspaces on one
shard, splitting on growth. That is a separate design for Joseph; this PR
changes neither the default nor existing shard counts.

This PR ships only signed aggregate-first checks and transfer telemetry.
For positive estimates, the complete-shard read-only precheck checks signed
aggregate affordability before its candidate loop; the locked rebalance checks
it before its target check. Negative headroom counts as debt. Only an already
funded TARGET returns `NOT_NEEDED` from rebalance. No other shard is offered.
Authorize reruns its own candidates after `MOVED` or `NOT_NEEDED`; guarded debit
repairs and retries shard zero. Precheck's existing funded-candidate routing and
initial bounded reserve/debit fast paths remain unchanged. This is not an
aggregate check on every admission.

Nonpositive estimates are an exception: rebalance returns `NOT_NEEDED` without
reading; precheck still compares signed headroom to the estimate. Thus
`[-1, 0, 0]` with estimate `0` is `INSUFFICIENT` in precheck but `NOT_NEEDED`
in rebalance.

Transfers retain the original needed-only algorithm exactly:
`needed = estimate - target_available`, filling negative target headroom first
and taking from the largest positive donors. Usage and reservations stay
untouched; guarded transfers preserve global `SUM(total_credits)` exactly.
Successful moves return `mode=topped_up`; authorize logs it, or `mode=none` for
no-transfer verdicts. Consolidation limits, eligibility guards, signed caps,
non-target funded-shard reuse, and returned-shard caller routing are withdrawn.
Cooldown, sleeps, attempt cap, and 402/503 mapping are unchanged.

## Why consolidation was withdrawn

Astra's round-3 review demonstrated a sequential regression, even when the
initial consolidation had no debt or outstanding reservations:

> Start with four shards holding **$0.50 each**, without reservations or debt.

The following is her later-grant sequence, quoted from the review (available
headroom in dollars):

| Operation | Round 3 | Original production / needed-only |
| --- | --- | --- |
| Authorize $0.60 on shard 0 | `[1.40,0,0,0]` | `[0,.50,.50,.40]` |
| Grant $2 through `credit_workspace_typed_direct` | `[1.90,.50,.50,.50]` | `[.50,1,1,.90]` |
| Authorize $0.50 on shard 1 | `[1.90,0,.50,.50]` | `[.50,.50,1,.90]` |
| Settle that donor reservation for $2 | `[1.90,-1.50,.50,.50]` | `[.50,-1,1,.90]` |
| Authorize $1.50, shard 0 first | **Accepted; net becomes −$0.10** | **Rejected; net remains $1.40** |

> The consolidation initially satisfies every new guard. The subsequent grant
> funds the emptied donors again, allowing a new donor reservation to settle
> into debt. The consolidated target retains $1.90, and its successful bounded
> reservation bypasses both aggregate checks.

An eligibility check at consolidation time cannot constrain later grants and
settlements. The conservative split retains needed-only transfers until funding,
debt creation, and admission have a coordinated design.

## Why non-target reuse and returned-shard routing were withdrawn

Astra's round-4 review closed the later-grant finding on needed-only code, then
identified new money regressions. Her first P1 sequence, quoted from the review:

> Start with three shards holding $1.50 each:

| Operation | Round 4 headroom | Exact origin/main headroom |
| --- | --- | --- |
| Authorize $1 on shard 0; settle for $3.50 | `[-2,1.50,1.50]` | Same |
| Guarded debit $1 | `[-2,.50,1.50]` | `[0,0,0]` |
| Authorize $1.50, shard 2 first | **Accepted; net −$1.50** | **Rejected; net $0** |

Her second sequential P1 disproves a negative-headroom eligibility guard:

> Another entirely sequential reproduction starts with credits `[40,150]`:
>
> 1. Authorize `50` on shard 1: headroom `[40,100]`, with no debt.
> 2. Debit `60`: round 4 leaves `[40,40]`; main leaves `[0,80]`.
> 3. Settle the donor reservation for `150`: round 4 leaves `[40,-60]`; main leaves `[0,-20]`.
> 4. Authorize `40`: **round 4 accepts; main rejects**.

She also reproduced the defect through authorization when a peer settlement
funded a donor between precheck and repair. Our additional authorization-only
regression starts `[40,150]`, holds 100 on shard 1, then settles that hold for zero
between the 60-unit request's precheck and repair. A later 50-unit donor hold
settles for 150. Final authorization of 40 must reject, leaving `[0,-20]`;
round-4 reuse instead accepts from its retained 40-unit target. Reverting only
guarded-debit routing would leave this regression open.

Her P2 race starts `[10,90,90]` and debits 80. Target repair moves 70 from shard 2
to shard 0; a peer reserves 80 on shard 1 between repair and retry. Debit succeeds
and leaves `[0,10,20]`. Round 4 returned shard 1 without moving funds; the peer
reservation made the exact-shard retry report `insufficient` with 110 remaining.

## Regression coverage and residual convoy

The tests below retain original accounting, replay, and recovery assertions.
The strict incident xfail must raise exactly
`StoreUnavailable("credit escrow rebalance is busy; retry")` at request index 1,
with one prior rebalance, two 0.25-second sleeps, and three blocked cooldown
attempts. Assertions check that evidence before reraising; any other failure is
red. The desired one-rebalance/no-wait convoy behavior remains unresolved.

| Test (abbreviated) | What it pins |
| --- | --- |
| `signed_affordability_precedes_any_funded_destination` | Aggregate debt precedes funded candidates and the target, without writes. |
| `authorize_precheck_rejects_funded_donor...` | Debt outside the bounded prefix rejects without repair. |
| `rebalance_negative_headroom_insufficient...` | Restored estimate-10 rejection despite a funded donor. |
| `guarded_debit_rejects_funded_donor...` | Aggregate debt rejects without money movement or event record. |
| `debt_sequence_matches_needed_only` | Changing estimates preserve net 150, then 60; routing rejects a funded 90-unit donor. |
| `donor_settlement_sequence_matches_needed_only` | Overrun leaves net 40; 60-unit admission and 50-unit precheck reject. |
| `later_grant_and_donor_overage...` | Round-3 grant/settlement sequence rejects final $1.50 and retains $1.40. |
| `guarded_debit_repairs_settlement_debt...` | Round-4 P1: fill negative target, debit once, reject subsequent $1.50. |
| `guarded_debit_repairs_target_before_future_donor_overage` | Round-4 P1: debt-free repair cannot expose later donor debt; final 40 rejects. |
| `guarded_debit_peer_reservation_after_repair...` | Round-4 P2: peer reserve does not falsely reject debit; net 30 and event replay. |
| `authorize_repairs_target_after_peer_settlement...` | Authorization-only reuse regression, final 40 rejects. |
| `second_process_reuses_funded_target...` | The same target can return `NOT_NEEDED` without transfer. |
| `authorize_repairs_own_target_after_peer_funds_another_shard` | Peer funds shard 7; caller repairs target 0 and retries its original candidates. |
| `guarded_debit_rebalances_to_shard_zero...` | Original target repair, one debit movement, event replay. |
| `needed_only_uses_largest_donor...`, `needed_only_preserves_money...`, `large_estimate_preserves_shards...` | Exact shortfall, donor order, conserved totals/usage/reservations, distributed capacity. |
| `needed_only_repeated_authorization_rejects_debt` | One 100-unit hold then two rejections, with net 60. |
| `fragmented_exhaustion_verdict_and_gateway_402...` | Transfer log mode and honest exhaustion/HTTP 402 without another repair. |
| `zero_estimate_precheck_respects_signed_affordability` | Nonpositive-estimate fast-path exception. |
| `small_balance_convoy_residual` | Strict xfail with exact busy message and third blocked attempt evidence. |
| `needed_only_residual_convoy_with_expired_cooldown` | Twelve needed-only repairs with holds or cheap settles; logged mode is topped_up. |
| Existing lock-order and recovery tests | Original bounded recovery and accounting survive the narrowed change. |

## Local validation

The five related files (the four requested files plus
`test_credit_row_sharding_stress.py`, the remaining importer) passed:
**159 passed, 0 failed, 1 strict xfail**. Repository-wide ruff passed.

Each red mutation and its restored green control ran in a fresh process.
The first four rows select the full contention and increment-3 files, including
the unchanged convoy xfail. Counts are **failed / passed**; the first four rows
also each have **1 xfailed** on both red and green runs.

| Production-only mutation | Red failed / passed | Restored green failed / passed |
| --- | --- | --- |
| Precheck aggregate after candidate loop | 6 / 53 | 0 / 59 |
| Locked rebalance aggregate removed | 7 / 52 | 0 / 59 |
| Returned `mode` removed | 11 / 48 | 0 / 59 |
| Logged `mode` removed | 4 / 55 | 0 / 59 |
| Re-add non-target reuse only (four new regressions) | 4 / 0 | 0 / 4 |
| Saved round-4 rebalance and both callers (four new regressions) | 4 / 0 | 0 / 4 |
| Authorize retries returned target instead of own candidates | 1 / 0 | 0 / 1 |

The reuse-only control fails both new P1s at the debit: without target repair,
the restored shard-zero retry cannot debit. The full round-4 control additionally
restores returned-shard routing, so both P1s reach the final authorization and
fail because it is **accepted**, reproducing the actual debt-spending bug.
P2 fails because the debit reports `insufficient`; the authorization-only case
fails because the final 40-unit authorization is accepted. The four-case
controls deselect 41 other contention cases; the caller-routing control
deselects 44.

The original needed-only production source from local `HEAD`
(`eec21c20d397d8673826054e0d16c5d86bff2223`) passes all four new regressions
plus the later-grant sequence: **5 passed, 0 failed, 40 deselected**. This is a
read-only baseline comparison, not a fetch or claim about a newly refreshed main.

A wrong-message control replaces only the convoy's busy exception message with
`unrelated outage`. It yields **1 failed, 0 passed, 44 deselected**, rather than
an xfail; restoring the message yields **0 failed, 0 passed, 1 xfailed,
44 deselected**. The marker therefore cannot mask an arbitrary `StoreUnavailable`.

Mypy passed all **393 production files** and all **six touched Python files**.
Repository-wide ruff and `git diff --check` passed.

The first full-suite attempt used four xdist workers with coverage. It was
interrupted during collection after 402 seconds under memory pressure:
**no tests executed**, so it supplies neither full-suite nor coverage evidence.
The replacement run used one process and `--assert=plain` (assertions remained
active), with coverage and the 70% floor enabled. It was also interrupted under
continued memory pressure after 572 seconds: **288 passed, 0 failed, 432 skipped,
11 xfailed**. These are partial counts, not a completed suite. **Full-suite
success and the 70% coverage gate remain unverified.** No timeout, production
setting, or test assertion was relaxed to obtain a pass.
All pytest, ruff, and mypy runs use `PYTHONPATH=src`; pytest uses the specified
main-clone virtualenv interpreter with `-p no:cacheprovider` so imports resolve
to this checkout. Mutation runs change production code only in memory, in
separate processes. No test diagnostics, virtualenv, or scratch directory is
added to the worktree. No commit, write-side git command, push, or deployment
is part of this task.
