# PR3 round 4 validation

Base: `67556f11c9b44508f0d547567adb732b6c09a240`, branch
`speculation/shadow-observation`. Changes are uncommitted; no git writes,
migration, production enablement or deployment. The shadow flag stays false;
workspace/route/image/producer/slot lists stay empty.

## Finding closure

| Finding | Change and evidence |
|---|---|
| P2: interruptions skip finalization cleanup | `src/trusted_router/gateway_timing.py:126` includes setup and completion inside nested `try/finally` blocks: shadow restoration, timing restoration and loss recording each run even when a preceding step raises a process-control exception. `tests/test_speculation_shadow.py:1068` injects KeyboardInterrupt, GeneratorExit, SystemExit, CancelledError and BaseException during setup, completion and both restoration paths. It retains the traceback, checks exception identity, the committed 600 hold, restored scopes and coverage loss, then verifies the next synchronous authorization emits its own event. |
| P2: recorder swallows process-control exceptions | `src/trusted_router/services/speculation_shadow.py:108` suppresses ordinary Exception only. Non-Exception BaseException sets sticky uncertainty in a guarded handler and re-raises. `tests/test_speculation_shadow.py:1123` fails completion and interrupts the loss recorder with KeyboardInterrupt/GeneratorExit: the identical interrupt escapes, the hold remains, uncertainty is set and both scopes are restored. |
| P2: double restoration failure poisons the next request | `src/trusted_router/services/speculation_shadow.py:205` retires the observation before reset/fallback restoration; nesting (`:195`) and all four callbacks (`:160`) ignore retired observations. Restoration (`:128`) independently guards fallback failure and marks uncertainty. `tests/test_speculation_shadow.py:1143` makes both reset and fallback set fail on two sequential synchronous authorizations: both emit distinct events, preserve both 600 holds and leave callbacks unable to modify retired observations. |

Finalizer nesting (ordinary callback failures remain isolated):

```text
try:
    try: setup; request (remember and re-raise BaseException)
    finally:
        try: complete
        finally:
            try: close shadow scope (retire, then restore)
            finally: restore outcome-timing scope
        mark finalization finished
finally:
    try: record deferred losses
    finally: record interrupted-finalization / abnormal-request loss
```

A recorder interruption therefore cannot skip scope restoration; an earlier
interruption cannot bypass either restoration attempt or final loss marking.
If both ContextVar restoration operations fail, the retained observation is
inert and coverage is unknown. No new synchronous storage, lock or await is added.

## Preserved round 2 guarantees

- No added synchronous storage, RPC or await on the observation path; worker IO
  remains independent. Ordinary authorization stays pinned at **5 operations /
  6 with the transactional pause gate**, with/without boot authentication and
  with shadow off/on.
- All 36 prior mutations and all three round 4 mutations are red (39 total).
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
is unchanged. No tests are excluded from the full run.

```text
uv run ruff check .
All checks passed!

uv run mypy src/trusted_router
Success: no issues found in 382 source files

uv run mypy
Success: no issues found in 382 source files

Shadow, timing, exact RPC operations, Stage D, boot, outbox and conformance (-n 4)
3263 passed, 1068 skipped, 11 xfailed, 1912 warnings in 147.67s (0:02:27)

Billing path RPC budget
3 passed, 6 warnings in 0.89s

uv run python -m tests.speculation_shadow_mutations
39 red; all 39 baseline and restored-baseline runs pass; exit 0

First full run (no tests excluded)
3 failed, 17202 passed, 1118 skipped, 12 xfailed, 14426 warnings in 4766.03s (1:19:26)
Required test coverage of 70% reached. Total coverage: 85.76%

Isolated rerun of all three failures, unchanged source/tests
3 passed, 22 warnings in 4.03s

Final clean full run, no overlapping test jobs and no exclusions
uv run pytest -q -p no:cacheprovider -n 4 --basetemp /private/tmp/astra-r3d-$$ \
  --cov=trusted_router --cov-report=term --cov-fail-under=70
Required test coverage of 70% reached. Total coverage: 85.76%
17205 passed, 1118 skipped, 12 xfailed, 14424 warnings in 3629.11s (1:00:29)
Exit 0
```

The first full run returned HTTP 408 (`Request body timed out`) in
`test_patch_updates_every_live_key_and_rejects_bad_values`,
`test_authorize_settle_identity_and_measured_store_time[shadow-on-True-0.25-global]`
and `test_storage_error_handler_preserves_gateway_timing[shadow-off-True]`.
These failed before reaching the routes under test and all passed in isolation.
The run overlapped other test jobs initially and the machine had elevated load;
resource contention is the likely cause, not established as a code defect.
The first full log is `/private/tmp/astra-r3d-full.log`; isolated rerun log:
`/private/tmp/astra-r3d-timeout-rerun.log`. No code/test change was made in response.
The final clean full run passed without overlapping test jobs or exclusions;
its log is `/private/tmp/astra-r3d-full-clean.log`. No subsequent production or
test changes were made. Disk was checked before both full runs: 120 GiB and
110 GiB available respectively. The targeted basetemp and both full-run
basetemps (`/private/tmp/astra-r3d-87587` and `/private/tmp/astra-r3d-53544`)
were deleted after completion. Targeted logs are
`/private/tmp/astra-r3d-targeted.log` and `/private/tmp/astra-r3d-rpc.log`.

Direct `sys.settrace` probes at the actual finalizer completion call also passed
for KeyboardInterrupt and GeneratorExit: identical exception, committed 600 hold,
both scopes restored, loss marked, next synchronous authorization emits an event.
These probes ran in a separate process without modifying source or tests.

Native emulator execution remains unavailable locally: no Docker executable or
configured `SPANNER_EMULATOR_HOST`. Skips are not SQL acceptance. CI still runs
the native dedup/rollback test after its emulator readiness check.

## Mutation receipts

Each mutation executes behavior gates in a disposable copy, with passing
baseline and restored-baseline runs. Compile/import errors do not count as
kills. Exact replacements and gates are in `tests/speculation_shadow_mutations.py`;
full results are in `speculation-shadow-mutations.json` and the run log is
`/private/tmp/astra-r3d-mutations.log`.

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
| 28 | remove gateway_timing.py scope-cleanup boundary L119 | red | pass / pass |
| 29 | remove gateway_timing.py scope-setup boundary L128 | red | pass / pass |
| 30 | remove gateway_timing.py completion boundary L139 | red | pass / pass |
| 31 | remove gateway_timing.py timing boundary L189 | red | pass / pass |
| 32 | remove gateway_timing.py timing boundary L214 | red | pass / pass |
| 33 | remove speculation_shadow.py submit boundary L221 | red | pass / pass |
| 34 | unguard loss recorder | red | pass / pass |
| 35 | drop ContextVar restoration fallback | red | pass / pass |
| 36 | classify only Exception exits | red | pass / pass |
| 37 | flatten unconditional cleanup nesting | red | pass / pass |
| 38 | swallow recorder process-control exceptions | red | pass / pass |
| 39 | retire observation after restoration | red | pass / pass |
