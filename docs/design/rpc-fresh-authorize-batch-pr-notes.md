# RPC diet cut 2 — round 2 PR notes

A zero-credit request could spend the entire shared deadline waiting for the
speculative key UPDATE, returning 503 where deployed main returns 402. Bound the
whole speculative transaction (statement RPCs, commit and ABORTED retries) with a
nested deadline of **remaining budget minus 4 seconds**, at most **16 seconds**
inside the existing **20-second** authorization budget. Skip speculation when
that reserve is all that remains. Statement timeout exits through protected cleanup and
fresh sequential classification; it never renews the overall deadline.

The **4-second reserve** allows the existing **2-second cleanup floor** plus
**2 seconds for classification**. The RPC-diet baseline measured authorize at
125/160 ms p50/p95 in us-central1, 455 ms in us-east4 and approximately 1,240 ms
in Europe. The classification allowance exceeds the whole slow-region baseline
by 760 ms; it is margin against measured cost, not a contention guarantee.

The pressure differential injects a key-write wait until the applicable deadline.
Zero credit returns main's exact 402, identical headers with no Retry-After, and
unchanged rows. Sequential classification never accesses the key. Funded requests
on both implementations wait for the key and return identical 503 responses at
the shared deadline, without committed holds. The main oracle receives production's
own `speculate_key_limit` selection, including the BYOK, uncapped and strict cases.

Protected cleanup **attempts** rollback for non-ABORTED callback API failures. If it fails,
the server transaction expires uncommitted; the SDK cannot commit the failed
callback's partial effects, and fallback uses a **new** transaction. Real SDK tests
cover regular and multiplexed sessions, in-status and transport deadlines, and
cleanup that fails after spending its full 2-second allowance. The new transaction
still has 2 seconds for authoritative classification and commits nothing.

The lock recorder proves credit-before-key table call order within each observed
transaction. A rolled-back speculative transaction and its sequential fallback
have separate traces. It does not prove lock release, unique-index lock order,
real contention behavior or deadlock freedom.

## Accepted replay cost

| Eligible capped request | Main | Candidate | Delta |
|---|---:|---:|---:|
| Fresh, unarmed | 5 | 3 | -2 |
| Replay, unarmed | 4 | 6 | +2 |
| Fresh, armed | 6 | 5 | -1 |
| Replay, armed | 4 | 8 | +4 |

The October 1 review measurement covered **72,730 enclave-authorized requests**;
**0.051%** had more than one authorize attempt. Enclave retries cover HTTP
502/503/504; multiple attempts may also include dial fallback, so this is a proxy
rather than a direct stored-replay rate or a measurement of client idempotency
reuse. The supplied expected extra cost is **~0.0015 operations/request** (a
3-operation average penalty), versus **2 saved per eligible fresh unarmed request**
(1 armed). The +2/+4 penalties bound that proxy calculation at ~0.0010–0.0020.
Break-even is **50% replays unarmed / 20% armed**. The cost and possible additional
replay contention are accepted; there is **no enclave hint or new field**.

Armed authorize remains **5 operations**. Cut 3 may move the selected-shard pause
read before the whole batch to reach 4. The current order is conservative, not a
data dependency: the first candidate is known, and `pause_epoch` supplies conflict
detection rather than a consumed value. That follow-up needs credit-before-pause
error precedence and pause/unpause race coverage.

## Pinned operation sequences

`RO` is the authentication snapshot (including the optional boot record).

| Request | Sequence | Operations |
|---|---|---:|
| Fresh, unarmed | RO → batch [credit, key, reservation, authorization] → commit | 3 |
| Fresh, armed | RO → credit UPDATE → pause SELECT → batch [key, reservation, authorization] → commit | 5 |
| Replay, unarmed | RO → batch → rollback → reservation SELECT in new transaction → commit → stored authorization RO | 6 |
| Replay, armed | RO → credit UPDATE → pause SELECT → batch → rollback → reservation SELECT in new transaction → commit → stored authorization RO | 8 |

ABORTED keeps the same speculative statements and IDs. The SDK deadline pins are
16s then 14s after a 2s aborted attempt; fresh commit has 12s left after two such
attempts. Replay fallback restores only the remaining original budget (16s here).

## Differential and negative controls

**56 comparisons against main 7fc31bd5 with production hint selection:**
24 route scenarios × armed/unarmed = 48, plus 4 credit-candidate configurations ×
armed/unarmed = 8. The separate historical frozen-sequential matrix retains its
**136 cases**. Route comparisons include status, body, headers and durable
money/request/outbox state, excluding additive timing data.

Missing stored authorization with a retained reservation returns main's 500,
unchanged holds and no new rows. The additional process-local mutation that uses
the provisional authorization when the stored row is missing fails
`test_gateway_money_differential_against_main[replay-missing-authorization-*]`
with **200 != 500** on both armed and unarmed variants.

All ten requested mutations were applied only to a temporary source copy.

| Mutation | Result | Named failing test / assertion |
|---|---|---|
| Speculate with NULL scope | RED | `test_null_scope_never_speculates`: batch `[4] != [2]` |
| Treat ALREADY_EXISTS as success | RED | `test_in_status_already_exists_rolls_back_before_replay`: accepted != replay/mismatch |
| Commit after zero-row credit UPDATE | RED | `test_zero_credit_batch_never_commits`: accepted != insufficient_credits |
| Skip protected rollback | RED | `test_speculation_miss_configured_rollback_floor`: rollback RPC missing |
| Drop key reserve from batch | RED | `test_warm_lookup_authorize_exact_sequence_and_contents`: operation count exceeds 3/5 |
| Rerun ABORTED without speculation | RED | `test_abort_at_every_fresh_batch_index_retries_speculation`: batch/statement identity differs |
| Ignore row-count mismatch | RED | `test_every_row_count_mismatch_rolls_back_before_fallback`: FailedPrecondition escapes instead of fallback |
| Remove speculation deadline | RED | `test_gateway_money_differential_against_main[delayed-key-zero-credit-False]`: 503 != 402 |
| Shrink reserved floor to zero | RED | Same deadline-pressure differential: 503 != 402 |
| Treat failed cleanup as fatal | RED | `test_deadline_cleanup_failure_still_classifies_in_new_transaction`: cleanup ServiceUnavailable escapes |

## Verification

- `uv run ruff check .`: all checks passed.
- `uv run mypy src/trusted_router`: success, 379 source files.
- `uv run mypy`: success, 379 source files.
- Selected fresh-batch, historical differential/replay, batch DML, operations,
  RPC-budget, lock-order, Stage D and conformance suites: **2,738 passed,
  1,073 skipped, 11 xfailed** in 199.69s. Live database backends are not configured;
  skipped conformance cases do not establish live-backend correctness.
- Ten temporary-copy mutations: **10 RED, 0 survived, 0 build-broken**.
- Additional missing-authorization process-local mutation: **2 RED**.
- Final full run (`uv run pytest -q -p no:cacheprovider -n 4`, isolated
  `/private/tmp/astra-batch2-final-$$` basetemp): **2 failed, 16,827 passed,
  1,123 skipped, 12 xfailed** in 1511.28s. Only the supplied Python 3.11
  incompatibilities fail:
  `tests/test_check_format_ordering.py::test_a_pep695_type_alias_annotation_is_not_enumerated`
  and `tests/test_store_protocol_conformance.py::test_typed_billing_store_helper_unwraps_the_module_proxy`.
  This gate is not entirely green locally.
- The first full run exposed the existing concurrency fixture's **worker hang**.
  It reproduced on an untouched archive of committed round 1 (`788a43c1`), while
  the candidate passed an isolated rerun (12.04s; baseline failure in 12.45s).
  The fake reused its three-party barrier for the loser's new sequential
  transaction, racing its 10-second barrier wait against the test's 10-second
  worker join. The test now disables the barrier through its completion action
  after both first attempts and the parent arrive. Simultaneous first attempts
  and every debit/replay assertion remain; fallback no longer waits for departed
  participants. The final full run passes this test.
- Post-fixture-fix billing enforcement plus fresh-batch suites: **179 passed**
  in 68.74s, with every original money assertion retained.
- Full instrumented run: **85.79% coverage**, above the **70%** gate.
  `coverage report --precision=2 --skip-covered --fail-under=70` passed.
  The instrumented run began before the one-shot test-fixture correction;
  production code is identical and the final full run verifies the fixed fixture.
- Temporary test directories and mutation/baseline copies removed after their
  processes exited. No git writes; all changes remain uncommitted.
