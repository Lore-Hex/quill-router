# RPC diet cut 2 — round 3 PR notes

A batch completing just before the speculative deadline could have its commit
rejected by that shorter deadline without releasing its locks, then enter
fallback and return 503. Speculative **statements and ABORTED reruns** now share
one absolute deadline; a successful callback commits with the **remaining
original 20-second budget**, never a renewed budget. SDK retry-loop timeouts
remain bounded by the statement deadline as well.

The speculation allowance is **min(16s, remaining − reserve)**. The reserve is
**2s cleanup floor + `TR_AUTHORIZE_SPECULATION_CLASSIFY_SECONDS`**, a positive,
finite Settings value defaulting to **4s**. Thus the default reserve is **6s**,
and an untouched 20s budget allows **14s** of speculation. If the allowance is
**below 3s**, skip speculation and enter sequential admission directly. Exactly
3s is eligible. The existing Settings object passed as `trust_settings` carries
this value into the native Spanner authorizer.

The reviewer's fallback RPC inventory (authentication has already completed):

| Fallback outcome | Sequential RPCs, including transaction completion |
|---|---:|
| Zero credit, one candidate | 3: reservation SELECT, credit UPDATE, rollback |
| Zero credit, four candidates | 6 |
| Funded, first capped key succeeds | 5 unarmed / 6 armed |
| Key UPDATE returns zero | Adds a classification SELECT; further candidates add UPDATE/SELECT pairs |

Six stages × the **105ms Europe planning RTT = 0.63s nominal**; the **4s**
classification allowance gives **6.35×** headroom for p99 queueing and SDK
retries. East4's 27ms planning RTT models 135–162ms for five to six stages;
Europe models 525–630ms. These are **planning bounds**, not measured regional
fallback p99 or guarantees under contention. Larger key-candidate scans may
exceed these shapes. Re-tune the allowance from **#1384 `data.timing.store_ms`
p99 by region after deployment**; the earlier 1.24s Europe baseline is not p99
evidence.

Three failure classes are explicit:

1. **Statement deadline/exception:** protected rollback, then a new sequential
   transaction classifies under the remaining original budget. Statement
   ABORTED retries retain identical IDs and the same absolute deadline.
2. **Definite commit non-commit:** ABORTED, or failure before the commit RPC is
   sent, gets protected rollback/cleanup and sequential fallback. The commit
   wrapper converts ABORTED before the SDK can retry it as fresh speculation.
3. **Uncertain commit:** once the commit RPC is sent, a lost response (deadline
   or transport error) triggers a **new strong snapshot** reading the reservation
   by idempotency scope. A stored reservation returns the ordinary replay result
   with stored IDs and fingerprint validation; absence permits fallback. Failed
   resolution propagates rather than admitting another hold. There is no blind
   retry or provisional-authorization success.

Installed-SDK tests cover regular and multiplexed sessions. The injected-clock
matrix pins **15.99s batch completion → commit crosses 16s → accepted, zero
rollbacks, one commit**, and **16.01s statement timeout → rollback → fallback →
accepted**, both within 20s. Those reproduction cases explicitly configure a 2s
classification allowance to reach the 16s cap. The same matrix separately pins
**13.99/14.01s** with the production default 4s allowance. Lost commit-response
tests use the real SDK strong-snapshot SQL path and verify exactly one held
amount, stored replay IDs, and no second write transaction when committed.

The pressure differential preserves main's exact 402 body/headers and unchanged
money state for zero credit. Funded persistent key contention still returns
main's 503 at the shared deadline. Failed statement cleanup spends at most its
protected floor at exhaustion, leaving 4s classification allowance by default;
it neither commits failed callback effects nor renews the original deadline.

The lock recorder proves credit-before-key table call order within each observed
transaction, with separate traces for speculation and fallback. It does not
prove live lock release, unique-index ordering or deadlock freedom. The round-2
one-shot barrier fix remains unchanged, including every debit/replay assertion.

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
14s then 12s after a 2s aborted attempt; fresh commit has 16s left after two such
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

All thirteen requested mutations were applied only to a temporary source copy.
The row-count mutant removes both prefix and final validation to eliminate the
guard under the new statement-exception fallback.

| Mutation | Result | Named failing test / assertion |
|---|---|---|
| Speculate with NULL scope | RED | `test_null_scope_never_speculates`: batch `[4] != [2]` |
| Treat ALREADY_EXISTS as success | RED | `test_in_status_already_exists_rolls_back_before_replay`: accepted != replay/mismatch |
| Commit after zero-row credit UPDATE | RED | `test_zero_credit_batch_never_commits`: accepted != insufficient_credits |
| Skip protected rollback | RED | `test_speculation_miss_configured_rollback_floor`: rollback RPC missing |
| Drop key reserve from batch | RED | `test_warm_lookup_authorize_exact_sequence_and_contents`: operation count exceeds 3/5 |
| Rerun ABORTED without speculation | RED | `test_abort_at_every_fresh_batch_index_retries_speculation`: batch/statement identity differs |
| Ignore row-count mismatch (prefix and final validation) | RED | `test_every_row_count_mismatch_rolls_back_before_fallback` |
| Remove speculation deadline | RED | `test_gateway_money_differential_against_main[delayed-key-zero-credit-False]`: 503 != 402 |
| Shrink reserved floor to zero | RED | `test_sdk_statement_deadline_leaves_commit_original_budget`: default 14s allowance violated |
| Treat failed cleanup as fatal | RED | `test_deadline_cleanup_failure_still_classifies_in_new_transaction`: cleanup ServiceUnavailable escapes |
| Commit uses speculation deadline | RED | `test_sdk_statement_deadline_leaves_commit_original_budget` (15.99s) |
| Uncertain commit blindly falls back | RED | `test_uncertain_commit_resolves_snapshot_without_second_hold` |
| Remove speculation floor | RED | `test_tiny_remaining_budget_skips_speculation` |

## Verification

- `uv run ruff check .`: all checks passed.
- `uv run mypy src/trusted_router`: success, 379 source files.
- `uv run mypy`: success, 379 source files.
- Fresh-batch, historical differential/replay, batch DML, operations, RPC-budget,
  IO, lock-order, Stage D and conformance suites: **2,788 passed, 1,073 skipped,
  11 xfailed** in 233.53s. Live database backends are not configured; skipped
  conformance cases do not establish live-backend correctness.
- Final temporary-copy mutation run: **13 RED, 0 survived, 0 build-broken**.
- The first selected run exposed two mock-introspection failures: commit is now
  wrapped per transaction. Those tests retain the original commit mocks before
  invocation; all original commit/no-commit assertions remain. The full selected
  rerun above passes after this fixture adaptation.
- Full instrumented suite: **85.79% coverage**, above the **70%** gate;
  `coverage report --precision=2 --skip-covered --fail-under=70` passed.
  The instrumented run had the two supplied Python 3.11 failures and two
  harness-only failures: setting coverage options in `PYTEST_ADDOPTS` caused
  nested pytest invocations to enforce 70% on their tiny subsets (18.25% and
  0.20%). The latter also triggered an outer lock-guard teardown error. This
  required an invocation correction, not a repository change.
- Final exact full command (`uv run pytest -q -p no:cacheprovider -n 4
  --basetemp /private/tmp/astra-batch3-$$`), without inherited coverage options:
  **2 failed, 16,858 passed, 1,123 skipped, 12 xfailed** in **809.07s**.
  Only the supplied local Python 3.11 incompatibilities remain:
  `tests/test_check_format_ordering.py::test_a_pep695_type_alias_annotation_is_not_enumerated`
  and `tests/test_store_protocol_conformance.py::test_typed_billing_store_helper_unwraps_the_module_proxy`.
  This full gate is not entirely green locally. Both harness-only failures and
  the teardown error are absent. Production and test sources are identical to
  the instrumented run.
- `df -h /private/tmp` checked before full testing; completed full-run,
  development, selected-suite and mutation-copy temporary directories removed.
  Mutation logs and coverage data are retained outside the repository.
- No git writes; changes remain uncommitted. The round-2 barrier fix is retained.
