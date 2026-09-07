# Uncapped authorize contention regression proof

The gateway derives `skip_key_limit` from its already-authenticated ApiKey:
`limit_microdollars is None` and the request-applicable `window_limits` is empty.
BYOK-excluded requests omit window limits, including enforced windows on uncapped keys.
A BYOK-excluded request on a capped key still follows the original reserve path.
The typed store also refuses the skip when supplied blocking window limits.
Older/direct callers default to the original behavior.

`authorize_atomic` records the first pre-randomized key candidate and a zero
key hold without calling `reserve_key`. The existing reserve loop and its SQL
are unchanged. The shared settle/reaper/drain release helper now recovers positive zero-hold
usage on shard zero if the recorded shard disappeared. It emits the committed
`key_usage_shard_fallback_total` counter event (value 1) and a warning naming the
key, missing shard and amount. Both text and structured log fields carry the
metric. This is a log-based counter event, not a newly provisioned Cloud
Monitoring metric. If shard zero cannot book either, it raises `RuntimeError`
with reservation ID, key, shard, actual amount and usage type. Transactional
claims roll back and the outbox retains the frozen usage for retry/dead-letter.
No schema flag distinguishes skip reservations from legacy zero-hold records,
so this protection deliberately covers both. Held/capped deleted-key handling,
keyless pre-migration credit reservations, zero-usage refunds, usage display, and #1101's hot-row releases LAST in the same
transaction remain unchanged.

## Tests

`tests/test_uncapped_authorize_skip.py` records snapshot SQL, transaction SQL,
DML, and buffered mutations on the shared Spanner fake. Assertions cover the
entire gateway authorize call, not only the transaction helper.

| Spec invariant | Test |
| --- | --- |
| 1. Zero counter access for uncapped keys | `test_uncapped_authorize_zero_counter_statements_or_mutations` (both include-BYOK settings; no-window and alert-only-window configurations) |
| 2. Capped behavior unchanged | `test_capped_and_window_paths_keep_exact_reserve_sql_and_parameters` (literal pre-change SQL and parameter types/values) |
| 3. Daily/weekly/monthly windows retain enforcement | Same parameterized SQL test, plus `test_uncapped_window_limit_still_blocks` |
| 4. BYOK/include-BYOK behavior | Same SQL test for capped/window-only keys, plus `test_uncapped_byok_skips_authorize_and_settles_byok_usage` |
| 5. One booking shard and correct summed display | `test_uncapped_settle_books_one_selected_shard_and_display_sums` (nonzero usage already on another shard; all three windows; zero hold release) |
| 6. Legacy authorization settlement | `test_legacy_authorization_still_settles` (old caller omits opt-in and creates the original zero-hold reservation) |
| 7. Replay identity and exactly-once booking | `test_uncapped_replay_keeps_authorization_and_books_once` (candidate order changes before replay; repeats settlement) |

## Actual reversion and mutation runs

Rebased main baseline: `94c0cb4251849b270c2e7183123f7bc6872ab96f`
(`git merge-base HEAD origin/main`, also the WIP commit's parent).
Round-1 WIP: `b909e3fc`.

The following round-1 evidence is historical; the round-2 results below supersede
its gate counts. The original pre-rebase baseline was
`fe917a614e0228d33662f7577ac708e8919f81da`.

On 2026-09-05, the three implementation files were temporarily overwritten with
`git show HEAD:<path>` (gateway.py, storage_gcp.py, storage_gcp_authorize.py), the
new tests were run, and the fixed bytes were restored in a `finally` block.
No commit, checkout, or production operation was performed.

**Full implementation reversion: 8 failed, 16 passed.** The 16 preserved-behavior
cases correctly pass against the original code; making these fail on a faithful
reversion would contradict the requirement that those behaviors stay unchanged.
Their sensitivity was verified by individually breaking the behavior they guard:

| Temporary change | Failed / passed |
| --- | --- |
| Remove gateway opt-in | 8 / 16 |
| Remove store forwarding | 8 / 16 |
| Allow capped keys to skip | 3 / 21 |
| Remove both window skip guards | 9 / 15 |
| Pass `is_byok=False` to the existing reserve loop | 8 / 16 |
| Record shard zero instead of the first candidate | 6 / 18 |
| Treat skipped key as KEY_ACCEPTED (nonzero hold) | 8 / 16 |
| Release/book zero actual usage during settlement | 5 / 19 |
| Settle legacy records against shard zero | 2 / 22 |
| Display only the first usage shard | 3 / 21 |
| Return a fresh authorization ID on replay | 1 / 23 |
| Remove the settlement claim's replay early return | 1 / 23 |

Removing only the initial authorize idempotency lookup **survived**: the existing
unique-insert conflict fallback still returns the correct replay. This mutation
does not violate invariant 7. The last two mutations above directly break its
identity and settlement guards and both fail. All mutations were restored.

Targeted restored suite: **24 passed**.

## Historical round-1 gates

The existing virtualenv was used with `UV_CACHE_DIR=/tmp/qr-locks-uv-cache` and
`uv run --no-sync` because the default uv cache is outside the writable sandbox.

- `ruff check .`: passed.
- `mypy src/trusted_router`: passed, 355 source files.
- Full suite, partitioned into two disjoint batches: **8,848 passed, 368 skipped,
  10 xfailed, zero failures**.
- Measured coverage: **83.73%**, exceeding the required 70%. This is the
  conservative coverage from the main batch alone; the separately passing
  sitemap crawl is not needed to reach the floor.

The successful main batch passed all **481 other test file paths explicitly**
to pytest (no shell-expanded path variable), with `-q -n 4 --dist loadgroup
--cov=trusted_router --cov-report=term:skip-covered --cov-fail-under=70`.
It returned exit 0: **8,846 passed, 368 skipped, 10 xfailed** in 1,511.92 seconds.
The separate `pytest -q tests/test_sitemap_seo_hygiene.py` returned exit 0:
**2 passed** in 228.09 seconds. Together these batches cover every test file;
no test assertion was changed or disabled.

Two earlier attempts did not constitute passing gates:

1. The sandboxed run hit loopback `bind` PermissionErrors in the existing
   gateway/TLS probe tests. The reruns permitted test-owned local networking.
2. A worker in the next monolithic run terminated during
   `test_every_sitemap_page_has_clean_search_metadata`, the existing crawl of
   over 3,500 pages. That attempt reported 8,845 passed and one worker crash
   before interruption. Temporary-file cleanup delayed its summary. The
   unmodified sitemap file then passed alone, and **every other file was rerun**
   successfully with coverage as described above.

All results above are local validation; no production operation or commit was
performed. The two pre-existing `.codex-*-spec.md` files were left untouched.


## Round 2: bounded cap-change race

The skip intentionally uses the gateway's already-authenticated entity read.
A lifetime cap created after that read and before the authorize transaction can
miss requests already in flight at the cap commit. The admission bound is the
sum of those requests' estimates: each request carries its own estimate; this
is not an extra allowance for subsequent fresh requests. The next request that
reads the committed entity follows the capped reserve predicate. Cap removal
also takes effect on the next fresh entity read, without a cached-cap refusal.
An already-in-flight reader may still use its previous cap decision.
This describes the authorize/admission bound, not a new clamp on actual usage
at settlement. No counter or api_key read was added inside authorize to close
the window, since that would restore the hot-row read being removed.

`test_cap_commit_bounded_inflight_slip_and_fresh_removal` commits a real
`update_key` after the gateway entity read, before typed authorize. It observes
one admitted request with its exact credit estimate and no key hold, rejects
the next request with 402, then removes the cap through `update_key` and admits
the next request with zero key-counter statements.

## Round-2 regression and actual reversion proof

All runs use the existing virtualenv (`UV_CACHE_DIR=/tmp/qr-locks-uv-cache`,
`uv run --no-sync`). Implementation bytes were temporarily replaced and restored
in `finally` blocks; no checkout, commit, deletion, or production operation.
The full-suite run uses restored source, separate from mutation runs.

| Temporary reversion/mutation | Observed result |
| --- | --- |
| U1: restore HEAD's shared release helper / authorize module | **5 failed** (Credits/BYOK shard-9 shrink, zero rows with selected shard 0/3, frozen outbox rollback) |
| U3: restore HEAD's gateway skip condition | **3 failed, 9 passed** (all three BYOK-excluded windows fail; included/capped behavior passes) |
| U2: disable the atomic skip | **1 failed** (the in-flight slip/zero-statement contract) |
| U2: let capped keys skip | **1 failed** (the next request is no longer rejected) |
| U1: suppress the fallback metric field | **2 failed** (Credits and BYOK recovery telemetry) |
| U1: revert helper with the differential ledger tests | **2 failed** (healthy and recovered ledgers diverge) |
| U2: retain the old cap when removal commits | **1 failed** (the fresh request after removal is rejected) |
| Remove the keyless-legacy exemption | **1 failed** (existing pre-migration credit-shard settlement test) |

The shard-9 race removes the row after the entity read and before authorize;
settle books 700 on shard zero with correct lifetime/window attribution and no
replay double-booking. The zero-row cases deliberately pin the changed
KEY_MISSING behavior: authorize succeeds without counter access, settle raises
with the preserved amount, and a repaired row permits exactly-once retry.
The differential ledger check compares healthy settlement with missing-shard
recovery for Credits and BYOK: all credit balances, holds, lifetime and window
usage match. The outbox case proves the frozen payload, reservation claim,
authorization claim and credit hold survive the booking failure. Existing capped SQL and
#1101 ordering assertions remain in the required targeted gate.

## Round-2 gates on this tree

- `ruff check .`: passed.
- `mypy` and `mypy src/trusted_router`: passed, **370 source files**.
- Required explicit-path gate on the corrected/restored tree: **249 passed**
  in 48.31 seconds (the six paths below).
- Full suite on the corrected/restored tree: **9,429 passed, 368 skipped,
  10 xfailed, zero failures**, exit 0, in 651.36 seconds.
- Coverage: **84.11%**, above the required 70%.
- Final `git diff --check`: passed. No commits or file deletions.

```bash
uv run --no-sync pytest -q tests/test_uncapped_authorize_skip.py tests/test_billing_typed_enforcement.py tests/test_gateway_authorize_spanner_operations.py tests/test_settle_outbox_apply.py tests/test_settle_outbox_drain.py tests/test_settle_books_usage_always.py
```

The first completed round-2 full run found one real compatibility regression:
**1 failed, 9,426 passed, 368 skipped, 10 xfailed; 84.11% coverage**. A
pre-migration credit-only reservation has `key_hash=None` and must still settle
its credit usage. The new helper initially treated it as a missing key. The
fix excludes only those genuinely keyless records from key-usage recovery; the
existing migration test fails with that exemption reverted. No test was weakened.
The two additional differential cases were added after that run's collection.
The corrected full run collects them as well. An earlier exploratory full run
was interrupted before mutation testing; it is not counted as a passing gate.


Final full-suite command (test-owned loopback listeners permitted):

```bash
UV_CACHE_DIR=/tmp/qr-locks-uv-cache uv run --no-sync pytest -q -n 4 --dist loadgroup --cov=trusted_router --cov-report=term:skip-covered --cov-fail-under=70
```

Local logs: `/tmp/qr-round2-full-final.log` and
`/tmp/qr-round2-targeted-restored.log`. Reversion logs are
`/tmp/qr-round2-U1-revert.log`, `/tmp/qr-round2-U3-revert.log`,
`/tmp/qr-round2-U2-remove-skip.log`, `/tmp/qr-round2-U2-skip-capped.log`,
`/tmp/qr-round2-U1-remove-metric.log`,
`/tmp/qr-round2-U1-differential-revert.log`,
`/tmp/qr-round2-U2-stale-removal.log`, and
`/tmp/qr-round2-keyless-revert.log`. All implementation mutations were restored
before the final full and explicit-path gates.
