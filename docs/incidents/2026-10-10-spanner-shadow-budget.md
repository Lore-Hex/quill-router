# Shadow evidence transaction budget

The October 10 alert emails identify policy `17359638140128527750`,
`TR Spanner: API failures`, with incidents starting at 04:42 and 05:38 UTC.
The latter reports 0.1367 unexpected API failures/second against a threshold
of 0.0167. These are Spanner API failures, not Cloud Run HTTP 5xx. The alert
excludes expected aborts and duplicate inserts.

Production logs independently show shadow counter flushes raising Spanner
`DeadlineExceeded` in Europe and South America. The subsequent shutdown
exception is repaired in #1652. The operations identity cannot read Cloud
Monitoring time series, so the aggregate alert's method/status composition
and post-release recovery still require verification. Do not claim every
API failure belongs to the shadow writer from these logs alone.

## Reproduction

A counter write reads its current sequence, reads the retention fence, then
commits. The implementation gave all three round trips a shared 200 ms
deadline, even though the shadow design specified a one-second worker budget
with individual RPCs then capped at 200 ms. With the 141 ms and 163 ms regional
latencies measured during #1651, the second read deterministically runs out
of budget. Tests reproduce both cases without sleeps or production writes.

## Initial repair (#1655)

- Share at most one second, or the caller's shorter remaining budget, across
  the entire evidence transaction. Never start a fresh worker budget.
- Add an opt-in context-local RPC cap to the existing deadline wrapper and
  set it to 200 ms for shadow evidence. Nested caps cannot widen an outer cap;
  normal billing callers do not opt in and retain their existing behavior.
- Preserve LOW priority and the one-callback fence against SDK transaction
  retries. The SDK retry horizon is not an RPC timeout; passing zero formerly
  became 500 ms in the wrapper. Pass the remaining total budget explicitly.
- Reserve the full one-second transaction horizon at the retention boundary,
  not just one RPC's duration. The durable retirement fence remains mandatory.
- Continue failing closed on exhaustion and uncertain commits. A storage
  timeout is never acknowledged as a successful evidence write.

Tests cover successful three-round-trip writes, budgets exhausted at either
read or commit, nested/context-isolated RPC limits, exception cleanup, SDK
abort re-entry, and retirement within the expanded transaction horizon.
Full regression and frozen-main billing-oracle gates remain required. No
alert policy, capacity, production ledger, or privacy setting is changed.

## Per-RPC follow-up

On 2026-10-10, after #1655 deployed at approximately 10:50Z, clean revision
`c07628b` still recorded approximately 1,670 `worker/store_unavailable` drops
per instance in the São Paulo-shaped pair over 2.5 hours (about 11/minute),
300–520 each in the Europe-sized set and 0–21 in US instances
(`tr-briefs/lease-shipped-state.md`, 13:5xZ per-instance table, sourced from
`tr-briefs/evidence/shadow-peek-20261010T135011Z.json`), so the shadow-store
RPC ceiling is now 500 ms within the unchanged one-second transaction
horizon; operator report reads retain their 200 ms limit.
