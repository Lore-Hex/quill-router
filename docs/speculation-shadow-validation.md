# PR3 round 6 validation

Base: `c2c10ee11076022435e99aca2a768e478b308ece`, branch
`speculation/shadow-observation`. No git writes, migration, enablement or deployment.
Changes remain uncommitted. The shadow flag and production prerequisites are unchanged.

## Continuation handoff

`gateway_timing._authorize_outcome` creates the async owner's observation and enters
`_handoff_scope`, which creates a `speculation_shadow.Continuation`. A ContextVar
makes that capability available to the async entrypoint without changing its public
signature. `gateway.authorize_gateway` explicitly passes it as `_shadow_continuation`
to `run_in_threadpool`, alongside the Request, body, settings and raw bytes.
`timed_gateway_sync` removes the private keyword before invoking the ordinary worker.

`outcome_scope` consumes the capability with `Continuation.claim`: a nonblocking lock
protects the check and one-time claim. Claimed, sealed, retired or contended capabilities
cannot attach. A successful claim pushes the outer observation into the worker scope;
only the async owner completes it. Without a successful explicit claim, every invocation
creates an independent observation and outcome-timing scope, including same-Request
calls, nested async calls, and completion callbacks. Scope exit restores its predecessor.
The old opaque Request token remains diagnostic correlation only; matching it grants
no authority. No Request or capability is placed in the event queue.

## Sealing and reviewer reproductions

In `_authorize_outcome`'s existing isolated completion boundary, after ordinary timing
has been captured and error timing extracted, `seal` copies allowlisted timing into an
immutable tuple and seals the observation **before invoking `shadow.complete`**.
Observation fact assignments after sealing are rejected and record
`observer-sealed-write-<field>` coverage loss. Fact callbacks retain that behavior after
retirement, and `_save_outcome_timing` rejects late timing writes. Completion receives a
detached timing dictionary; event construction uses the sealed tuple. Ordinary
`data.timing` continues through its existing timing implementation.

Regressions cover successful same-Request replay and caught billing-paused replay in
sync and async/threaded execution, including nested async reentry. Synthetic tests prove
separate authorization IDs and exact outer/inner timing. Real gateway tests prove a
successful replay retains its legitimate original authorization ID with `replay=True`,
while a denied invocation has its own empty authorization ID rather than inheriting the
outer successful authorization. Both emit two events, and the outer event stays
200/success with its original timing. Additional gates exercise every sealed fact,
late callback writes before/after retirement, repeated explicit claims and concurrent
claims. All round-5 fault-injection and different-Request reentrancy gates remain.

## Preserved guarantees

- No new synchronous storage or RPC. Capability claims use a process-local,
  nonblocking lock; shadow storage IO remains in the independent worker.
- Ordinary authorization stays pinned to **5 operations / 6 with the transactional
  pause gate**, with boot authentication and shadow off/on variants.
- Stage D response/money/SQL differentials, cached deadline checks, and native CI
  inclusion remain covered. Native emulator execution requires the configured CI
  emulator; local skips do not establish native SQL acceptance.
- Production migration and exact PLAN queries remain pre-enable requirements.

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

Python 3.12.3, existing frozen environment:
`UV_PROJECT_ENVIRONMENT=/private/tmp/astra-r3b-py312`,
`UV_CACHE_DIR=/private/tmp/astra-r3b-uv`. The repository's Python 3.11 environment
is unchanged. Both known Python 3.11-only failures are included without exclusions.

```text
uv run --frozen ruff check .
All checks passed!

uv run --frozen mypy src/trusted_router
Success: no issues found in 382 source files

uv run --frozen mypy
Success: no issues found in 382 source files

Shadow, protocol, timing, RPC/billing budget, Stage D, boot, outbox,
and conformance suites (-n 4), final clean targeted run:
3821 passed, 1068 skipped, 11 xfailed, 1908 warnings in 108.66s (0:01:48)
Exit 0

uv run --frozen pytest -q -p no:cacheprovider -n 4 \
  --basetemp /private/tmp/astra-r3f-$$ \
  --cov=trusted_router --cov-report=term --cov-fail-under=70
Required test coverage of 70% reached. Total coverage: 85.77%
17277 passed, 1118 skipped, 12 xfailed, 14414 warnings in 2395.80s (0:39:55)
Exit 0
```

The full run includes every test, without exclusions. No source or test fixes were
made after this full run started. `df -h /private/tmp` showed **115 GiB available**
before the run. Full basetemp `/private/tmp/astra-r3f-35289` and both targeted
basetemps were deleted after completion. Coverage data is outside the worktree at
`/private/tmp/astra-r3f.coverage`.

Gate logs: `/private/tmp/astra-r3f-ruff.log`,
`/private/tmp/astra-r3f-mypy-src.log`, `/private/tmp/astra-r3f-mypy.log`,
`/private/tmp/astra-r3f-targeted-clean.log`, and
`/private/tmp/astra-r3f-full-clean.log`. Full-run exit receipt:
`/private/tmp/astra-r3f-full-exit.txt`; disk receipt:
`/private/tmp/astra-r3f-disk-before.log`.

Native emulator execution remains unavailable locally (no Docker executable or
configured emulator). CI selection and its collection guards pass; local skips
are not native SQL acceptance.

## Mutation receipts

`uv run --frozen python -m tests.speculation_shadow_mutations` exited **0** on the
final source: **45 red**, with all 45 baselines and all 45 restored baselines
passing. Import/compile failures and timeouts never count as detection. Mutations
run in disposable copies. Receipts: `speculation-shadow-mutations.json`; log:
`/private/tmp/astra-r3f-mutations-clean.log`.

The 42 round-5 checks are retained. The former identity-guard mutation retains
its receipt name, but now bypasses the explicit handoff requirement by attaching
the ambient observation when no capability is supplied. Its existing independent
Request reentrancy gate kills that equivalent regression. Three new mutations
restore token matching, permit sealed writes, and allow a second claim.

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
| 18 | remove gateway.py resolved boundary L789 | red | pass / pass |
| 19 | remove gateway.py boot_verified boundary L815 | red | pass / pass |
| 20 | remove gateway.py authorized boundary L2408 | red | pass / pass |
| 21 | remove gateway.py reason boundary L793 | red | pass / pass |
| 22 | remove gateway.py reason boundary L823 | red | pass / pass |
| 23 | remove gateway.py reason boundary L1614 | red | pass / pass |
| 24 | remove gateway.py reason boundary L1381 | red | pass / pass |
| 25 | remove gateway.py reason boundary L1809 | red | pass / pass |
| 26 | remove gateway.py reason boundary L1721 | red | pass / pass |
| 27 | remove gateway_timing.py arguments boundary L141 | red | pass / pass |
| 28 | remove gateway_timing.py scope-cleanup boundary L155 | red | pass / pass |
| 29 | remove gateway_timing.py scope-setup boundary L168 | red | pass / pass |
| 30 | remove gateway_timing.py completion boundary L181 | red | pass / pass |
| 31 | remove gateway_timing.py timing boundary L238 | red | pass / pass |
| 32 | remove gateway_timing.py timing boundary L263 | red | pass / pass |
| 33 | remove speculation_shadow.py submit boundary L267 | red | pass / pass |
| 34 | unguard loss recorder | red | pass / pass |
| 35 | drop ContextVar restoration fallback | red | pass / pass |
| 36 | classify only Exception exits | red | pass / pass |
| 37 | flatten unconditional cleanup nesting | red | pass / pass |
| 38 | swallow recorder process-control exceptions | red | pass / pass |
| 39 | retire observation after restoration | red | pass / pass |
| 40 | retirement outside restoration finally | red | pass / pass |
| 41 | ignore observation request identity | red | pass / pass |
| 42 | mark finished outside isolation | red | pass / pass |
| 43 | match token instead of claimed continuation | red | pass / pass |
| 44 | callbacks can mutate sealed observation | red | pass / pass |
| 45 | continuation claimable twice | red | pass / pass |
