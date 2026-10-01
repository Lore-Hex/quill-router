# PR3 round 2 validation

Base: `a773c5b5`, branch `speculation/shadow-observation`. Changes remain
uncommitted. No git writes, migration, production enablement or deployment.
The shadow flag stays false; workspace/route/image/producer/slot lists stay empty.

## Finding closure

| Finding | Change and evidence |
|---|---|
| P1: observer failures alter ordinary results | All nine gateway callbacks and the timing/scope/submission sites use the shared `isolate` boundary. Argument evaluation is inside it. Exceptions set sticky, content-free coverage-loss reasons; process-control exceptions propagate. Fault injection executes every gateway source call site, checks original response/exception identity and holds, and exercises sync/async setup, cleanup, timing and error completion. Real HTTP off/on differentials compare committed holds and all response/state collections with callback, submission and queue faults. |
| P2: cached deadlines | Current key/trust/price deadlines and the two-second start window are checked before reuse. Shortening mints a fresh generation if eligible, otherwise a typed per-item miss. The exact 2000 → 2010 / trust-fresh-until 2011 reproduction is covered, including no-cache and authenticated batch variants. Tests also shorten claims beyond the cached token's expiry. |
| P2: native test absent from CI | Native test is parametrized as `spanner-emulator`, included in the explicit emulator list, and protected by an actual subprocess collection assertion with CI's `-k` selection. WIF is workflow-ref based (`infra/gcp_wif.tf`); the emulator job performs no WIF authentication and needs no allowlist change. |
| P2: incomplete differential | Compares every dict/list/set collection in the fake database: entities, typed counters, reservations, authorizations, generation records, both analytics outboxes, settlement outbox, Stage D watermarks and all versions/state. Boot-authenticated Stage D lifecycle runs authorize → replay → heartbeat → settle/refund. Assertions require nonempty watermarks/outboxes and generation records where applicable. Reviewer's estimate mutation is red. |
| P2: unbounded event/dedup retention | Idempotent seven-day TTL on event/success tables, regenerated schema, expiry/replay safety tests and shell migration test (first apply, rerun, conflicting-policy refusal). Exposure and scope/producer state receive no TTL. No new application SQL; existing three typed SQL registrations remain unchanged. New migration metadata query and DDL are covered by migration/schema tests. |
| PLAN evidence list | Runbook contains all three exact SQL statements and typed parameter bindings: credit-shard range, key-limit-shard range, trust-event ordered workspace range with LIMIT 1001. Production PLAN evidence remains a pre-enable requirement; none is claimed here. |

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

Python **3.12** is installed in `/private/tmp/astra-r3b-py312` using frozen
project dependencies. Commands set `UV_PROJECT_ENVIRONMENT` to that path and
`UV_CACHE_DIR=/private/tmp/astra-r3b-uv`. The repository's Python 3.11 environment
is unchanged. Both named local-only Python 3.11 failures pass under 3.12.

```text
uv run ruff check .
All checks passed!

uv run mypy src/trusted_router
Success: no issues found in 382 source files

uv run mypy
Success: no issues found in 382 source files

Shadow, timing, RPC, Stage D, boot, outbox and conformance selection (-n 4)
3002 passed, 1068 skipped, 11 xfailed, 1528 warnings in 93.34s

Final expanded shadow unit suite
122 passed, 14 warnings in 16.86s

Final complete lifecycle differential + migration idempotence
49 passed, 208 deselected, 774 warnings in 31.17s

Python 3.12 checks for the two named Python 3.11 failures
2 passed, 6 warnings in 0.92s
```

Warm ordinary-path RPC assertions remain **5 operations / 6 with the transactional
pause gate**, with and without boot authentication, in both shadow modes.
Native emulator execution is unavailable locally: no Docker executable and no
configured `SPANNER_EMULATOR_HOST`. Skips are not SQL acceptance. CI runs the
native dedup/rollback test after its emulator readiness check.

Final clean full run, exit code **0**, after all production and collected-test
changes (no tests excluded):

```text
uv run pytest -q -p no:cacheprovider -n 4 --basetemp /private/tmp/astra-r3b-$$ \
  --cov=trusted_router --cov-report=term --cov-fail-under=70
Required test coverage of 70% reached. Total coverage: 85.75%
16949 passed, 1118 skipped, 12 xfailed, 14044 warnings in 2901.35s (0:48:21)
```

Full log: `/private/tmp/astra-r3b-full.log`. The actual full-run basetemp
`/private/tmp/astra-r3b-92726` and focused-run basetemp
`/private/tmp/astra-r3b-targeted` were deleted after their processes completed.
Disk was checked before the run (120 GiB available). No post-run fixes to
production or collected tests were needed. All 33 mutation results are red,
with passing baseline and restored-baseline runs.

## Mutation receipts

Every mutation runs in a disposable copy with baseline and restored-baseline
checks. Shell/YAML mutations execute their behavioral test rather than being
compiled as Python. See `tests/speculation_shadow_mutations.py` and
`speculation-shadow-mutations.json` for exact replacements, gates and results.

| Round | Mutation | Result | Baseline / restored |
|---|---|---|---|
| 1 | duplicate callback counts | red | pass / pass |
| 1 | current request qualifies | red | pass / pass |
| 1 | submit waits on worker IO | red | pass / pass |
| 1 | extra synchronous boot read | red | pass / pass |
| 1 | success clears sticky loss | red | pass / pass |
| 1 | lifetime topup treated as paid | red | pass / pass |
| 1 | real grant type | red | pass / pass |
| 1 | real store namespace | red | pass / pass |
| 1 | first batch identity reused | red | pass / pass |
| 2 | cached current deadlines ignored | red | pass / pass |
| 2 | exhausted start window untyped | red | pass / pass |
| 2 | native backend deselected | red | pass / pass |
| 2 | native explicit CI list omitted | red | pass / pass |
| 2 | Stage D observer changes estimate | red | pass / pass |
| 2 | expired replay recreates dedup | red | pass / pass |
| 2 | old success recreates dedup | red | pass / pass |
| 2 | retention policy omitted | red | pass / pass |
| 2 | remove gateway.py resolved boundary L786 | red | pass / pass |
| 2 | remove gateway.py boot_verified boundary L812 | red | pass / pass |
| 2 | remove gateway.py authorized boundary L2405 | red | pass / pass |
| 2 | remove gateway.py reason boundary L790 | red | pass / pass |
| 2 | remove gateway.py reason boundary L820 | red | pass / pass |
| 2 | remove gateway.py reason boundary L1611 | red | pass / pass |
| 2 | remove gateway.py reason boundary L1378 | red | pass / pass |
| 2 | remove gateway.py reason boundary L1806 | red | pass / pass |
| 2 | remove gateway.py reason boundary L1718 | red | pass / pass |
| 2 | remove gateway_timing.py arguments boundary L105 | red | pass / pass |
| 2 | remove gateway_timing.py scope-setup boundary L113 | red | pass / pass |
| 2 | remove gateway_timing.py completion boundary L124 | red | pass / pass |
| 2 | remove gateway_timing.py scope-cleanup boundary L132 | red | pass / pass |
| 2 | remove gateway_timing.py timing boundary L163 | red | pass / pass |
| 2 | remove gateway_timing.py timing boundary L188 | red | pass / pass |
| 2 | remove speculation_shadow.py submit boundary L170 | red | pass / pass |
