# PR3 round 3 validation

Base: `573e02dfe823e946ac7e64786b6a2a61b0ffdd21`, branch
`speculation/shadow-observation`. Round 3 changes are uncommitted; no git writes,
migration, production enablement or deployment. The shadow flag stays false;
workspace/route/image/producer/slot lists stay empty.

## Finding closure

| Finding | Change and evidence |
|---|---|
| P1: recorder failures escape isolation | `src/trusted_router/services/speculation_shadow.py:107` guards all loss recording, including deferred reason bookkeeping, with `BaseException`. Failure sets a sticky process-local reference flag without calls, locks, counters, logging or IO. Status (`routes/internal/speculation.py:26`), worker projection (`services/speculation_shadow.py:261`) and minting (`:478`) honor it, including after dispatcher replacement. Every callback and timing boundary is tested with failed loss.set, reason-map lookup, injected logging and counter diagnostics. Production recording needs no logging or counters. Real HTTP lifecycle differentials include recorder failures and unchanged SQL, responses and all money-state collections. |
| P1: completion skips cleanup | `src/trusted_router/gateway_timing.py:129` defers completion/submission loss recording until both scope cleanups have independently executed. `tests/test_speculation_shadow.py:956` simultaneously fails completion, both resets and recording; it checks exact step order, response/exception identity and unchanged holds. |
| P2: failed ContextVar reset leaks state | `src/trusted_router/services/speculation_shadow.py:123` falls back to setting the saved previous value, then reports the reset failure through isolation. Both shadow and outcome-timing scopes use it independently. `tests/test_speculation_shadow.py:929` injects failed and already-used-token resets in either/both variables, restores a nonempty previous timing value, and proves the next direct synchronous request in the same context emits its own event. |
| P2: cancellation looks successful | `src/trusted_router/gateway_timing.py:122` tracks and re-raises the identical `BaseException`. `services/speculation_shadow.py:192` emits status 500 / reason aborted for non-Exception exits; finalization records sticky coverage loss. `tests/test_speculation_shadow.py:997` cancels an awaited future after observing authorization and also checks BaseException, KeyboardInterrupt and SystemExit. Holds and exception identity survive; projection creates no success rows/history, even with recorder faults. |

Finalization order: **complete → restore shadow scope → restore outcome-timing
scope → record deferred failures (then abnormal-exit loss)**. Each cleanup has
its own isolation invocation; a failed step does not early-return. Recorder
failures themselves only set the last-resort marker.

## Preserved round 2 guarantees

- No added synchronous storage, RPC or await on the observation path; worker IO
  remains independent. Ordinary authorization stays pinned at **5 operations /
  6 with the transactional pause gate**, with/without boot authentication and
  with shadow off/on.
- All 33 prior mutations remain red; three new mutations are also red.
- Cached grant reuse still checks current key/trust/price deadlines and the
  two-second start margin. Shortened deadlines fail closed or mint a shorter grant.
- Native conformance remains selected as `spanner-emulator` in CI's explicit
  file list, with digest-pinned emulator image and a collection guard. No WIF change.
- Full fake-store collection comparisons still cover boot-authenticated Stage D
  authorize → replay → heartbeat → settle/refund and both analytics outboxes.
- TTL/schema/migration tests remain intact. All three exact PLAN queries and
  typed bindings remain in the runbook; production PLAN evidence is still a
  pre-enable requirement, not claimed here.

## Retention

- Event and authorization/invocation success-dedup: **7 days** from server commit.
- Accepted delayed delivery: **1 day / 86,400 seconds**. Older/future events fail
  coverage closed without recreating event/dedup rows.
- Success history: **600 seconds**, last success within **30 seconds**, full clean
  interval **900 seconds**. Old successes cannot create dedup rows.
- At **10 distinct successes/second**, with invocation nonce: **2,592,000 rows/day**;
  seven-day logical retention **18,144,000 rows** (6,048,000 events + 12,096,000 dedup).
- Physical deletion is asynchronous: ten days, including typical 72-hour TTL lag,
  is **25,920,000 rows**, plus operational headroom. This is not a guaranteed hard
  physical cap; monitor TTL lag and undeletable rows as documented in the runbook.
- Retained exposure has **no TTL** and never refills as these rows expire.

## Environment and gates

Python **3.12**, frozen dependencies, existing environment:
`UV_PROJECT_ENVIRONMENT=/private/tmp/astra-r3b-py312` and
`UV_CACHE_DIR=/private/tmp/astra-r3b-uv`. The repository's Python 3.11 environment
is unchanged. The full run includes both named local-only Python 3.11 failures.

```text
uv run ruff check .
All checks passed!

uv run mypy src/trusted_router
Success: no issues found in 382 source files

uv run mypy
Success: no issues found in 382 source files

Expanded shadow + exact RPC/HTTP differential gates
493 passed, 1190 warnings in 25.04s

Shadow, timing, RPC, Stage D, boot, outbox and conformance selection (-n 4)
3243 passed, 1068 skipped, 11 xfailed, 1912 warnings in 102.26s (0:01:42)

uv run python -m tests.speculation_shadow_mutations
36 red; all 36 baseline and restored-baseline runs pass

uv run pytest -q -p no:cacheprovider -n 4 --basetemp /private/tmp/astra-r3c-$$ \
  --cov=trusted_router --cov-report=term --cov-fail-under=70
Required test coverage of 70% reached. Total coverage: 85.76%
17182 passed, 1118 skipped, 12 xfailed, 14430 warnings in 3814.20s (1:03:34)
```

Final clean full run exited **0**, with no tests excluded and no subsequent
production/test fixes. Full log: `/private/tmp/astra-r3c-full.log`. Mutation log:
`/private/tmp/astra-r3c-mutations.log`. Disk was checked before the full run
(120 GiB available). Full-run basetemp `/private/tmp/astra-r3c-89935` and targeted
basetemp `/private/tmp/astra-r3c-targeted` were deleted after completion.

Native emulator execution remains unavailable locally: no Docker executable or
configured `SPANNER_EMULATOR_HOST`. Skips are not SQL acceptance. CI still runs
the native dedup/rollback test after its emulator readiness check.

## Mutation receipts

Every mutation executes behavior gates in a disposable copy, with passing
baseline and restored-baseline runs. Compile/import errors do not count as
kills. Exact replacements and gates are in `tests/speculation_shadow_mutations.py`;
full results are in `speculation-shadow-mutations.json`.

| # | Mutation | Result | Baseline / restored |
|---|---|---|---|
| 1 | duplicate callback counts | red | pass / pass |
| 2 | current request qualifies | red | pass / pass |
| 3 | submit waits on worker IO | red | pass / pass |
| 4 | extra synchronous boot read | red | pass / pass |
| 5 | success clears sticky loss | red | pass / pass |
| 6 | lifetime topup treated as paid | red | pass / pass |
| 7 | real grant type | red | pass / pass |
| 8 | real store namespace | red | pass / pass |
| 9 | first batch identity reused | red | pass / pass |
| 10 | cached current deadlines ignored | red | pass / pass |
| 11 | exhausted start window untyped | red | pass / pass |
| 12 | native backend deselected | red | pass / pass |
| 13 | native explicit CI list omitted | red | pass / pass |
| 14 | Stage D observer changes estimate | red | pass / pass |
| 15 | expired replay recreates dedup | red | pass / pass |
| 16 | old success recreates dedup | red | pass / pass |
| 17 | retention policy omitted | red | pass / pass |
| 18 | remove gateway.py resolved boundary L786 | red | pass / pass |
| 19 | remove gateway.py boot_verified boundary L812 | red | pass / pass |
| 20 | remove gateway.py authorized boundary L2405 | red | pass / pass |
| 21 | remove gateway.py reason boundary L790 | red | pass / pass |
| 22 | remove gateway.py reason boundary L820 | red | pass / pass |
| 23 | remove gateway.py reason boundary L1611 | red | pass / pass |
| 24 | remove gateway.py reason boundary L1378 | red | pass / pass |
| 25 | remove gateway.py reason boundary L1806 | red | pass / pass |
| 26 | remove gateway.py reason boundary L1718 | red | pass / pass |
| 27 | remove gateway_timing.py arguments boundary L105 | red | pass / pass |
| 28 | remove gateway_timing.py scope-setup boundary L114 | red | pass / pass |
| 29 | remove gateway_timing.py completion boundary L129 | red | pass / pass |
| 30 | remove gateway_timing.py scope-cleanup boundary L141 | red | pass / pass |
| 31 | remove gateway_timing.py timing boundary L174 | red | pass / pass |
| 32 | remove gateway_timing.py timing boundary L199 | red | pass / pass |
| 33 | remove speculation_shadow.py submit boundary L204 | red | pass / pass |
| 34 | unguard loss recorder | red | pass / pass |
| 35 | drop ContextVar restoration fallback | red | pass / pass |
| 36 | classify only Exception exits | red | pass / pass |
