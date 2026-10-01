# PR3 round 5 validation

Base: `b0fbe4f2053312f80b34827445b874edb972c00f`, branch
`speculation/shadow-observation`. Changes remain uncommitted; no git writes,
migration, production enablement or deployment. The shadow flag stays false;
workspace/route/image/producer/slot lists stay empty.

## Finding closure

| Finding | Change and evidence |
|---|---|
| P2: retirement interruption leaks active observation | `src/trusted_router/services/speculation_shadow.py:206` puts retirement in a `try` whose `finally` restores `_CURRENT`. Retirement still precedes restoration, preserving double-restoration-failure protection. `tests/test_speculation_shadow.py:1208` injects RuntimeError, KeyboardInterrupt and GeneratorExit at the actual retirement assignment; verifies response/exception identity, the committed 600 hold, restored scopes, lost coverage and a separate next synchronous event while retaining the traceback. |
| P2: reentrant authorization overwrites outer facts | `src/trusted_router/gateway_timing.py:101` creates a request correlation token; `src/trusted_router/services/speculation_shadow.py:194` shares only matching active identities. `tests/test_speculation_shadow.py:1254` covers body and completion callbacks, independent success and billing_paused denial, sync and async/thread continuations, and absent/distinct/reused log IDs. Both events retain their own workspace/key/nonce/authorization/reason/route, with the original outer response and holds preserved. |
| P3: mark-finished fault replaces response | `src/trusted_router/gateway_timing.py:136` performs `finalized = True` through the existing isolated cleanup helper at line 170. Ordinary assignment faults preserve the response and close coverage; process-control faults propagate identically. `tests/test_speculation_shadow.py:1208` traces this exact line with RuntimeError, KeyboardInterrupt and GeneratorExit and checks both scopes plus the next authorization. |

## Observation identity

The outer authorization wrapper puts an opaque `object()` correlation token in
`request.state._shadow_request_identity`; `Observation.request_identity` stores
that token. No request content or token is added to the queued Outcome. Log IDs
are deliberately not authoritative because middleware accepts caller-supplied
IDs that can be reused by separate requests.

The async entrypoint and synchronous worker receive the same Request/state and
therefore the same token. ContextVar propagation into the worker carries the
active observation; the matching token shares it and only the outer owner emits
an event. A different request gets its own token, observation and outcome-timing
scope. ContextVar tokens push/pop the active scope and restore the outer facts
after the inner authorization completes or fails. Callers without a Request/state
get fresh independent identities. Retired observations are always inert.

## Preserved guarantees

- Zero new synchronous storage, RPC, lock or await. Request identity is local
  bookkeeping; shadow storage IO remains in the independent worker.
- Ordinary authorization remains pinned at **5 operations / 6 with the
  transactional pause gate**, with/without boot authentication and shadow off/on.
- Cached grant current-deadline checks, full response/money/SQL Stage D
  differential gates and all previous mutation gates remain intact.
- Native conformance remains selected as `spanner-emulator` in CI's explicit
  file list with its collection guard and pinned emulator image.
- Production migration and the three exact PLAN queries remain pre-enable
  requirements; no production PLAN acceptance is claimed here.

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

Python **3.12.3**, frozen dependencies, existing environment:
`UV_PROJECT_ENVIRONMENT=/private/tmp/astra-r3b-py312` and
`UV_CACHE_DIR=/private/tmp/astra-r3b-uv`. The repository's Python 3.11
environment is unchanged. No tests are excluded.

```text
uv run --frozen ruff check .
All checks passed!

uv run --frozen mypy src/trusted_router
Success: no issues found in 382 source files

uv run --frozen mypy
Success: no issues found in 382 source files

Shadow suite
384 passed, 14 warnings in 33.93s

Shadow, timing, RPC (including billing path budget), Stage D, boot, outbox,
and conformance suites (-n 4)
3296 passed, 1068 skipped, 11 xfailed, 1908 warnings in 175.37s (0:02:55)
```

The targeted basetemp `/private/tmp/astra-r3e-targeted` was deleted after
completion. Logs: `/private/tmp/astra-r3e-shadow.log`,
`/private/tmp/astra-r3e-targeted.log`, `/private/tmp/astra-r3e-ruff.log`,
`/private/tmp/astra-r3e-mypy-src.log`, `/private/tmp/astra-r3e-mypy.log`.

Native emulator execution remains unavailable locally: no Docker executable or
configured emulator. Skips are not SQL acceptance. CI still selects the native
dedup/rollback test and retains its emulator readiness and collection guards.

Final clean full run, after all source/test fixes, with no exclusions and no
other test jobs launched concurrently for this task:

```text
uv run --frozen pytest -q -p no:cacheprovider -n 4 \
  --basetemp /private/tmp/astra-r3e-$$ \
  --cov=trusted_router --cov-report=term --cov-fail-under=70
Required test coverage of 70% reached. Total coverage: 85.77%
17235 passed, 1118 skipped, 12 xfailed, 14424 warnings in 3607.08s (1:00:07)
Exit 0
```

Coverage data was directed to `/private/tmp/astra-r3e.coverage`. The log is
`/private/tmp/astra-r3e-full-clean.log`; exit receipt:
`/private/tmp/astra-r3e-full-exit.txt`. `df -h /private/tmp` reported **116 GiB**
available immediately before the full run (receipt:
`/private/tmp/astra-r3e-disk-before.log`). Basetemp
`/private/tmp/astra-r3e-89619` was deleted after successful completion. Both
known Python 3.11-only failures were included and pass under Python 3.12.3.
No source or test edits were made after this full run.

## Mutation receipts

`uv run --frozen python -m tests.speculation_shadow_mutations` exited **0**:
**42 red**, all 42 baselines and all 42 restored baselines pass. Each mutation
runs behavior tests in a disposable copy; import/compile errors and timeouts
never count as detection. Exact replacements are in
`tests/speculation_shadow_mutations.py`; full receipts are in
`speculation-shadow-mutations.json`; log: `/private/tmp/astra-r3e-mutations.log`.

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
| 27 | remove gateway_timing.py arguments boundary L119 | red | pass / pass |
| 28 | remove gateway_timing.py scope-cleanup boundary L133 | red | pass / pass |
| 29 | remove gateway_timing.py scope-setup boundary L146 | red | pass / pass |
| 30 | remove gateway_timing.py completion boundary L157 | red | pass / pass |
| 31 | remove gateway_timing.py timing boundary L207 | red | pass / pass |
| 32 | remove gateway_timing.py timing boundary L232 | red | pass / pass |
| 33 | remove speculation_shadow.py submit boundary L224 | red | pass / pass |
| 34 | unguard loss recorder | red | pass / pass |
| 35 | drop ContextVar restoration fallback | red | pass / pass |
| 36 | classify only Exception exits | red | pass / pass |
| 37 | flatten unconditional cleanup nesting | red | pass / pass |
| 38 | swallow recorder process-control exceptions | red | pass / pass |
| 39 | retire observation after restoration | red | pass / pass |
| 40 | retirement outside restoration finally | red | pass / pass |
| 41 | ignore observation request identity | red | pass / pass |
| 42 | mark finished outside isolation | red | pass / pass |
