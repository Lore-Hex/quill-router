# PayPal and Adyen trust facts — PR 1b

This slice adds adverse facts to the existing verified webhooks. Eligibility stays
inert: `TR_TRUST_QUALIFYING_PROVIDERS` defaults to `stripe,x402`, and rollout keeps
`TR_SPEND_LEASE_TRUST_ELIGIBILITY_ENABLED=false`. PR 2 owns arming and admission.

## Integration contract

`provider_trust_history.provider_marker_qualifies` is the pure arm-gate seam.
Call it for each enabled provider's configured account and environment. Pass
`payment_occurred_at` when evaluating payment eligibility; PayPal captures older
than the marker's enumerable history must not qualify. The function requires a
completed, clean marker with these exact identities and consistency delays:

| Provider | Source | Source version | Environment | Delay |
|---|---|---|---|---|
| PayPal | `paypal-transaction-search` | `paypal-trust-v1` | `live` / `sandbox` | 10800 s |
| Adyen | `adyen-payment-accounting-report` | `adyen-trust-v1` | `live` / `test` | 0 s, relative to the report's closed coverage |

This predicate proves historical completion. PR 2 must also require ongoing
watermark freshness. Settings already validate maximum reconciliation age against
the greatest enabled-provider delay plus two cadences. At the default 900-second
cadence, PayPal requires **12600 seconds**; the literal 3600-second default is
rejected when PayPal is enabled. Adyen report publication lag is represented by
its conservative `closed_through`, never by stamping an old report with the
execution time.

Handlers retain the existing lifecycle graphs and transactional recovery writer.
Equal provider timestamps use the graph's topological order before the status
name, so a reversal cannot sort before the successful refund it reverses. Both
live and historical conversion use this same tie rule.
Refunds claim pro-rata credited principal, excluding processing fees. Disputes,
reversals, cancellations and capture failures claim at most one payment's
principal. Fraud notifications latch without claiming. Decision 76 explicitly
classifies Adyen `REFUNDED_REVERSED` as a full-principal claim; `REFUND_FAILED`
removes the refund claim. Every payment preserves
`recovery_target = recovered_micro + unrecovered_micro`.

Adverse IDs have a subtype namespace so a refund and a capture reversal cannot
collide. PayPal/Adyen inbox keys additionally hash each lifecycle observation;
the payload retains the canonical adverse identity. This prevents a pending
observation from swallowing completion before the payment arrives. PostgreSQL
inbox draining uses the payment transaction's connection, including historical
fact insertion; no separate commit can escape rollback. Stripe/x402 identities
and handling remain unchanged.

## Historical and rolling-deploy reconciliation

The packaged entry point is available in the existing production image:

```sh
python -m trusted_router.provider_trust_cli --help
```

`scripts/reconcile_provider_trust.py` is a local compatibility wrapper. Nothing
schedules or runs these jobs automatically in this slice.

For each account/environment, run `--mode backfill --provider paypal|adyen
--account-id ACCOUNT --history-start TIMESTAMP --drained-at TIMESTAMP` after every
old handler revision has drained. The initial invocation is a read-only plan;
`--apply` runs the same source through the 1d writers and semantic completion
proof. `drained_at` must fall inside the enumerated interval and before its closed
end. Re-running covers both the historical range and the rolling-deploy window.
Backfill requires existing local credit-idempotency evidence, and never mints
credits or pre-empts the crediting webhook. Uncredited payments, missing lineage,
unsupported records, failed source reads and semantic disagreement prevent a
completed marker.

PayPal uses the checkout OAuth credentials and API base URL. Transaction Search
windows are contiguous and at most 31 days, end at least three hours before the
pass, and start no earlier than the calendar three-year retention bound. Direct
capture/refund retrieval supplies canonical attribution. The dispute listing
also enumerates opened disputes with no balance transaction. Missing or changing
pagination fails the pass.

Adyen requires `--report-manifest FILE`, with complete Payment Accounting Report
exports and their provider coverage, for example:

```json
{
  "account_id": "YOUR_MERCHANT_ACCOUNT",
  "environment": "live",
  "covered_from": "2026-01-01T00:00:00Z",
  "covered_through": "2026-09-06T00:00:00Z",
  "files": ["payment-accounting.csv"]
}
```

The manifest is an operator assertion of complete canonical export coverage;
the CLI cannot infer missing exports from local credits. Preserve the original
authorisation rows needed to resolve old modifications. Required CSV columns:
`Merchant Account`, `Psp Reference`, `Merchant Reference`, `Record Type`,
`Booking Date`, `TimeZone`, `Main Currency`, `Main Amount`,
`Modification Psp Reference`. The signed checkout merchant reference establishes
workspace and credited principal; `Psp Reference` remains the authorisation,
while `Modification Psp Reference` identifies the adverse object. Incorrect
merchant, environment, currency, amount, signature or coverage fails closed.

Schedule `--mode recurring --apply` at the configured cadence only after a clean
backfill. Every pass re-covers the delayed created-time tail and re-fetches all
outstanding IDs, including objects created before the tail. Adyen re-fetch uses
the accumulated complete reports through the pass's closed coverage. Failure
leaves the last clean marker unchanged, so freshness eventually fails closed.
Without an authoritative provider mutation deadline, outstanding objects remain
tracked until terminal and alert after 30 days; no Stripe-specific deadline is
invented for these providers.

## Surface inventory and remaining operational work

- New settings: **none**. The existing maximum-age validation gets an explicit
  enabled-provider/minimum diagnostic.
- New tables or columns: **none**. Existing `tr_trust_event`, `tr_trust_inbox` and
  `tr_trust_backfill` store these providers' facts and markers.
- New routes: **none**. Existing `POST /v1/internal/paypal/webhook` and
  `POST /v1/internal/adyen/webhook` keep their verification and authentication.
- New CLI arguments: `--provider`, `--account-id`, `--mode`, `--history-start`,
  `--drained-at`, `--report-manifest`, `--apply`.

No provider calls, production backfills, scheduler changes or deployments were
performed. Real provider payload/export compatibility and completeness must be
verified with the configured merchant accounts before their markers are relied
on. Unknown records or unavailable retained history require canonical archive
coverage or a reviewed source-version extension, never a fabricated marker.
Real PostgreSQL/PGAdapter and Spanner-emulator conformance remains for a runtime
where sockets are permitted; in-process PostgreSQL and native Spanner harnesses
exercise the transaction logic here.

## Validation record

The focused run passed **432 tests**: 241 provider tests, 179 unchanged
slice-1a/1b′/1c/1d, P1, PayPal billing and Adyen billing tests, and 12 Store
protocol-conformance checks. This includes the
existing DDL shape/dedup-index checks and explicit `ON CONFLICT` target guards.

**73/73 unique mutations were detected.** Each mutation used a `cp` backup,
ran a proving test expecting a proving-test failure, and restored the file with
`cp`; SHA-256 equality confirmed byte-identical restoration. There were 138
mutation executions in total: three misses in the initial pass led to two
stronger tests and correction of a mutation that had targeted the Spanner guard
while running a PostgreSQL test. The final retained result for every mutation is
a proving-test failure. After
the full suite caught public Store signature drift, the transaction connection
argument moved to a private helper; both the atomicity mutation and a new public
signature mutation were then detected.

The mutation groups cover every provider event's literal claim contract,
reference resolution, currency/amount/time validation, inbox identity and atomic
draining, historical credit evidence, source/account/environment/version marker
matching, retention/delay/cadence bounds, pagination and canonical object IDs,
source semantic conflicts, drain coverage, recurring re-fetch/failure/identity,
no invented mutation horizon, both route writers, pro-rata recovery, one
principal across claims, debt cancellation before restoration, all-shard latch,
stale lifecycle rejection, inert defaults, unknown historical activity and
equal-timestamp lifecycle ordering.

Session evidence (outside the worktree): `/private/tmp/trust1bb-mutations.py`,
`/private/tmp/trust1bb-mutations/final-73.json`,
`/private/tmp/trust1bb-mutation-final.log`, and
`/private/tmp/trust1bb-mutation-additional.log`. The test policy plugin
`/private/tmp/trust1bb_offline.py` skips socket-dependent tests and blocks real
socket connect/bind/sendto/DNS calls. No live provider or emulator tests are
claimed by the in-process proofs.

The first completed full run recorded 9103 passed, 18 failed, 422 skipped,
10 xfailed, 2 setup errors and 83.797% branch-inclusive coverage. It exposed the
public Store signature mismatch fixed above. Most remaining failures were
HTTP 408 body-read timeouts; the harness also supplied an empty Sentry DSN where
one existing test requires `None`. After clearing that harness value, the failing
set passed in isolation: **19 passed, 1 skipped** on this tree, and **18 passed,
1 skipped** on the exported parent commit `9ad666c6` (which has no new PayPal test).
The skipped saved-card test makes an unmocked Stripe PaymentIntent search; it is
explicitly excluded under the no-network instruction.

The final full run uses the installed coverage package's supported C tracer,
with branch coverage and the 70% minimum retained. The prior run used the default
Python 3.14 monitoring core. This is a validation-environment change, not a
production timeout change. `UV_OFFLINE=1` and `UV_NO_SYNC=1` also cover child
processes; one earlier interrupted attempt exposed a shell test invoking its
own `uv` without inherited command-line flags. Another preliminary attempt was
interrupted to add the equal-timestamp ordering fix before final verification.

Final gates:

- `ruff check .`: passed.
- `mypy`: passed, 368 source files.
- Focused proving suite: **432 passed**, 284 warnings, 56.17 seconds.
- Full pytest: **9115 passed, 6 failed, 423 skipped, 10 xfailed, 1 setup error**,
  4860 warnings, 2287.53 seconds. Exit status 1; this gate is **not green**.
- Branch-inclusive coverage: **83.76950847767148%**, above the 70% gate;
  46840/53887 statements and 12793/17300 branches covered.
- Of the 423 skips, **55** explicitly enforce the no-network/no-sockets policy.
  Existing opt-in external-backend and other repository skips also remain.
- `git diff --check`: passed. Existing trust golden/test files were not edited.

The final full command was `uv run --offline --no-sync pytest -q -n 4 --dist
loadgroup -p trust1bb_offline --cov=trusted_router --cov-report=term:skip-covered
--cov-report=json:/private/tmp/trust1bb-coverage.json --cov-fail-under=70 -ra`,
using the shared Python 3.14.6 environment, `COVERAGE_CORE=ctrace`, the temporary
offline plugin described above, and inherited `UV_OFFLINE=1`/`UV_NO_SYNC=1`.

These final full-run cases remain unresolved:

```text
ERROR tests/test_core_api.py::test_streaming_chat_uses_provider_stream_without_materializing
FAILED tests/test_custom_models.py::test_custom_model_slug_create_duplicate_and_rename
FAILED tests/test_domain_aliases.py::test_wallet_challenge_uses_alias_siwe_domain
FAILED tests/test_federated_workspace.py::TestFederatedRequestReachesAuthorize::test_peer_local_app_collision_cannot_suspend_active_home_app
FAILED tests/test_federated_workspace.py::TestFederatedRequestReachesAuthorize::test_a_federated_key_no_longer_403s_on_a_missing_workspace
FAILED tests/test_gateway_fallback_billing.py::test_gateway_archimedes_rejects_byok_and_fallback_arrays
FAILED tests/test_identity_guidance.py::test_decline_stores_the_reason_but_never_shows_it[503-Suspected document tampering]
```

Six traces show HTTP 408 responses; the other asserts that a workspace should
exist after an authorization request, without exposing that response's status.
All seven passed together in isolation on both this final tree (**7 passed,
18 warnings, 3.18 seconds**) and exported base `9ad666c6` (**7 passed,
18 warnings, 3.30 seconds**). This does not prove that the full-run failures are
pre-existing. Full-suite stability remains an open validation item in a normal
CI/runtime environment; no unrelated request-timeout behavior was changed.
Every new-provider test passed in the final full run.

Final evidence: `/private/tmp/trust1bb-final-full.log`,
`/private/tmp/trust1bb-coverage.json`, `/private/tmp/trust1bb-gates.json`,
`/private/tmp/trust1bb-final-failure-check-current.log`, and
`/private/tmp/trust1bb-final-failure-check-base.log`.
