# Native GoogleSQL statement inventory

227 native expressions (142 SELECT, 85 DML), 246 literal scenarios, 119 builder/capture cases, 365 primary acceptance cases, 85 additional batch cases, 3 rejection canaries and 3 positive controls; 15 builders and 157 dispatch scopes fingerprinted. (Before the 2026-09 removal of the regional-quota and spend-lease pilots: 283 expressions, 303 scenarios, 124 builder cases, 427 primary cases and 202 dispatch scopes; the run results below were recorded against that inventory.)

Primary transports: {'SELECT': 165, 'DML': 171, 'batch DML': 29}. Counts describe executable registered scenarios, not all parameter values. Each row below has acceptance cases; DML without THEN RETURN also runs in batch form.

Discovery scans every `src/trusted_router/**/*.py` by default. Only `storage_postgres.py` is excluded as the reviewed PostgreSQL adapter. The manifest separately fingerprints 46 discovered non-GoogleSQL expressions (PostgreSQL helpers/branches, ClickHouse, Google Ads, and one operator explanation), each with a reason. New modules and changed classifications fail closed. Leading comments, parenthesized queries, INSERT OR IGNORE, split prefixes and dynamic f-string prefixes are included.

First CI run: **449/496 passed**; all three rejection canaries were rejected. The remaining 47 failures were 32 heartbeat type errors, 3 timestamp errors, 11 null-filtered-index checks, and one oversized positive control.

Run 2 (round-2 commit `45a1d0f`, before the round-3 store-path shim) completed native conformance in **49.26 s: 96 passed, 10 xfailed, 16 skipped, 14 failed**; acceptance in **27.21 s: 554 passed, 5 failed**. The teardown hang is gone. The native failures were 12 stale movement reads, one receipt-key null-filtered-index check, and one synthetic-rollup ordering bug. Acceptance failed twice on the DML statement-level hint, twice on the CLAIM CHECK constraint, and once on JSON_REMOVE diagnostic wording. Evidence is the supplied `emu-ci/run2.txt`; round-4 server results are pending CI.

Pinned to the images pulled in run 1:

- Spanner: `gcr.io/cloud-spanner-emulator/emulator@sha256:c6f3402f2599684f295a0fdefb6fbbbfb18a0e43e309ff5456ccb452a4570a79`.
- Bigtable SDK: `gcr.io/google.com/cloudsdktool/google-cloud-cli@sha256:7617d937e9360d769de4ef66266a8caae503c1dbbd050c93404d3f30045c5125` (the first run's `emulators` image).

## Emulator limitations

The shim makes two emulator accommodations, without changing production SQL or settings:

1. It derives null-filtered index names from `CREATE [UNIQUE] NULL_FILTERED INDEX` in `spanner_ddl.DDL` and merges `spanner_emulator.disable_query_null_filtered_index_check=true` into each `@{FORCE_INDEX=...}` table-hint block naming one of those indexes. Matching is case-insensitive and whitespace-tolerant, preserves other keys, and is idempotent; other SQL is byte-identical. Run 2 showed that a statement-level prefix breaks UPDATE (including batch DML): the emulator accepts this hint on table scans and queries, while DML statements accept only `ignore_unknown_hints`. An always-on guard checks every registered index-name occurrence is a FORCE_INDEX value inside a hint and that every eligible block is rewritten.
2. It wraps `Database.snapshot` to drop only `exact_staleness` and `max_staleness`, making these reads strong. Explicit `read_timestamp` and `min_read_timestamp`, `multi_use`, and other options pass through unchanged. Fresh emulator databases otherwise read an older schema or miss just-written movements. Source inspection found only `exact_staleness`: conditional 5-second reads at `storage_gcp.py:4264`, `4376`, `5355`, movement history at 30 seconds (`4312`), and the earnings display aggregate at 60 seconds (`4347`). Production history/reporting intentionally lags writes by up to 30/60 seconds to use nearby replicas; these reads do not authorize transfers. Neither the Python fake (which ignores staleness) nor this emulator conformance backend asserts that production lag.

`emulator_resources()` installs the shared SDK shim only after emulator safety checks succeed and restores all patched methods on exit, including exceptions. Both acceptance and the real native store use it. Offline recorders verify SQL rewriting, snapshot option preservation, and restoration; server execution of the round-4 changes still requires CI.

The synthetic-rollup ordering failure reproduces on the real Bigtable emulator: `limit=2` returns the middle and oldest periods. This is native-store bug [#1370](https://github.com/Lore-Hex/quill-router/issues/1370): `synthetic_rollups` limits an ascending row-key scan before sorting newest-first. Its strict xfail now applies to both `spanner-fake` and `spanner-emulator`, alongside the ten legacy-money gaps. Collection tests enforce that classification. The store fix is outside this PR.

Run 2 also confirmed that the emulator enforces CHECK constraints the fake does not: the then-registered spend-lease `register_claim:1` scenario violated `spend_lease_scope_arbitration_shape` until it supplied a non-null provisional ID, scope and salt. The two spend-lease tables stay in the checked-in DDL, but no native expression has targeted them since the pilot's removal in 2026-09.

The first CI run's emulator explained: “The emulator is not able to determine whether the null filtered index … can be used to answer this query as it may filter out nulls that may be required to answer the query.” It directed testing against Cloud Spanner and said “the emulator will accept the query and return a valid result when it is run with the check disabled.” These are live production queries; the hint bypasses the emulator's index eligibility check, not SQL parsing or execution. Eleven first-run failures had this message. This evidence comes from the supplied CI logs; no online documentation was fetched.

The JSON_REMOVE canary requires `Argument 2 to JSON_REMOVE must be` followed by exactly `a constant expression` or `a literal or query parameter`. The other two canary patterns matched run 2. Offline fixtures check both reported phrases and unrelated-message rejection; they cannot prove live emulator wording.

## Fragment and binding fidelity

Frozen SQL fragments remain explicit scenarios: `where` in `storage_gcp.list_credit_movements`, `storage_gcp._list_entities`, and `storage_gcp_google_ads._list_entities`; `suffix_sql` in `storage_gcp._list_entities` and `tail` in `storage_gcp_google_ads._list_entities`; `arms` in `SpannerOperationalAnalyticsOutbox.oldest_enqueued_at`; `sibling` in `done_retention_statements`; and `suffix` in `trust_eligibility.billing_paused_tx`. The always-on scope guard requires an assignment for each fragment inside its fingerprinted production scope, so changing its production construction invalidates completeness. Module column constants used by the newly registered reconciliation queries are evaluated from production. Builder batches use the matching key shard and settlement's `defer_retention=True`.

Manifest types are compared to the production type maps wherever those maps cover a parameter; names, timestamp strings and heartbeat model annotations are checked separately. Profiling checks that every returned builder statement reaches an executed case (including seeds), not just that a builder was invoked.

Round-2 offline verification: 294 passed / 990 skipped / 11 xfailed for the focused conformance and workflow gate; 9 passed / 549 skipped for the isolated coverage proof; coverage reporting resolves all sources. The exact CI report command yields 22% for this focused proof (below its full-suite 70% threshold). All 12 requested mutations were caught in copies. See [gate results and failing test names](spanner-emulator-conformance.md#round-2-gate-results). Run-2 server results are recorded above; round-4 server execution remains pending CI.

Round-4 offline verification: ruff passed; mypy reported no issues in 400 source files; the focused conformance/workflow gate reported **325 passed, 990 skipped, 11 xfailed** in 59.73 s. All five copy-only mutations were caught, and canary specificity checks passed in both phrase mutations. See [round-4 results and failing test names](spanner-emulator-conformance.md#round-4-gate-results). No full-suite or local emulator run was performed.

## Source expressions

| ID / source | Kind | Typed parameters | Features |
|---|---|---|---|
| [byok_aad_backfill:scan:1](../../src/trusted_router/byok_aad_backfill.py#L237) | SELECT | ARRAY<STRING>: kinds; INT64: limit; STRING: after_id, after_kind | — |
| [byok_aad_backfill:census:1](../../src/trusted_router/byok_aad_backfill.py#L296) | SELECT | ARRAY<STRING>: kinds | — |
| [byok_aad_backfill:census:2](../../src/trusted_router/byok_aad_backfill.py#L303) | SELECT | INT64: limit | — |
| [byok_aad_backfill:census:3](../../src/trusted_router/byok_aad_backfill.py#L317) | SELECT | STRING: literal | — |
| [byok_aad_backfill:census:4](../../src/trusted_router/byok_aad_backfill.py#L326) | SELECT | STRING: literal | — |
| [storage_gcp:constants:1](../../src/trusted_router/storage_gcp.py#L274) | SELECT | STRING: lookup_hash | JSON_VALUE |
| [storage_gcp:constants:2](../../src/trusted_router/storage_gcp.py#L305) | SELECT | STRING: lookup_hash | JSON_VALUE |
| [storage_gcp:readiness_check:1](../../src/trusted_router/storage_gcp.py#L558) | SELECT | none | — |
| [storage_gcp:_owner_workspace_ids_tx:1](../../src/trusted_router/storage_gcp.py#L863) | SELECT | STRING: owner | — |
| [storage_gcp:txn:1](../../src/trusted_router/storage_gcp.py#L1096) | SELECT | INT64: shard_count; STRING: pk | — |
| [storage_gcp:txn:2](../../src/trusted_router/storage_gcp.py#L1268) | SELECT | INT64: shard_count; STRING: pk | — |
| [storage_gcp:_workspace_trust_events_tx:1](../../src/trusted_router/storage_gcp.py#L1232) | SELECT | STRING: pk | — |
| [storage_gcp:txn:3](../../src/trusted_router/storage_gcp.py#L1380) | SELECT | STRING: adverse_ref, provider | — |
| [storage_gcp:_existing_operator_abuse:1](../../src/trusted_router/storage_gcp.py#L1350) | SELECT | STRING: adverse_ref, provider | — |
| [storage_gcp:txn:4](../../src/trusted_router/storage_gcp.py#L1404) | SELECT | INT64: shard_count; STRING: pk | — |
| [storage_gcp:txn:5](../../src/trusted_router/storage_gcp.py#L1468) | DML | ARRAY<STRING>: causes; INT64: shard_count; STRING: pk; TIMESTAMP: now | — |
| [storage_gcp:txn:6](../../src/trusted_router/storage_gcp.py#L1543) | SELECT | INT64: shard_count; STRING: pk | — |
| [storage_gcp:txn:7](../../src/trusted_router/storage_gcp.py#L1581) | DML | ARRAY<STRING>: causes; INT64: shard_count; STRING: pk; TIMESTAMP: now | — |
| [storage_gcp:txn:8](../../src/trusted_router/storage_gcp.py#L1629) | SELECT | none | — |
| [storage_gcp:txn:9](../../src/trusted_router/storage_gcp.py#L1640) | SELECT | none | — |
| [storage_gcp:txn:10](../../src/trusted_router/storage_gcp.py#L1724) | SELECT | STRING: owner, workspace | — |
| [storage_gcp:process_trust_demotion_remainders:1](../../src/trusted_router/storage_gcp.py#L1703) | SELECT | INT64: limit | — |
| [storage_gcp:txn:11](../../src/trusted_router/storage_gcp.py#L1740) | SELECT | STRING: pk | — |
| [storage_gcp:txn:12](../../src/trusted_router/storage_gcp.py#L1755) | SELECT | INT64: shard_count; STRING: pk | — |
| [storage_gcp:txn:13](../../src/trusted_router/storage_gcp.py#L3454) | DML | INT64: amount; STRING: user_id; TIMESTAMP: now | — |
| [storage_gcp:_demote_owner_trust_tx:1](../../src/trusted_router/storage_gcp.py#L2071) | SELECT | STRING: pk | — |
| [storage_gcp:_demote_owner_trust_tx:2](../../src/trusted_router/storage_gcp.py#L2080) | SELECT | INT64: shard_count; STRING: pk | — |
| [storage_gcp:batch:1](../../src/trusted_router/storage_gcp.py#L2593) | SELECT | ARRAY<STRING>: ids; STRING: kind | — |
| [storage_gcp:get_byok_providers:1](../../src/trusted_router/storage_gcp.py#L2644) | SELECT | ARRAY<STRING>: ids; STRING: kind | — |
| [storage_gcp:list_stale_trust_inbox:1](../../src/trusted_router/storage_gcp.py#L3203) | SELECT | TIMESTAMP: older_than | — |
| [storage_gcp:_increment_lifetime_topup_tx:1](../../src/trusted_router/storage_gcp.py#L3309) | DML | INT64: amount; STRING: user_id; TIMESTAMP: now | — |
| [storage_gcp:_increment_lifetime_topup_tx:2](../../src/trusted_router/storage_gcp.py#L3321) | DML | INT64: amount; STRING: user_id; TIMESTAMP: now | — |
| [storage_gcp:_insert_credit_movement_tx:1](../../src/trusted_router/storage_gcp.py#L3347) | DML | INT64: amount; STRING: account_id, authorization_id, counterparty, custom_model_id, kind, movement_id; TIMESTAMP: created_at | — |
| [storage_gcp:txn:14](../../src/trusted_router/storage_gcp.py#L3466) | DML | INT64: amount; STRING: user_id; TIMESTAMP: now | — |
| [storage_gcp:txn:15](../../src/trusted_router/storage_gcp.py#L3529) | DML | INT64: amount; STRING: user_id; TIMESTAMP: now | — |
| [storage_gcp:txn:16](../../src/trusted_router/storage_gcp.py#L3641) | DML | INT64: amount; STRING: account_id, authorization_id, counterparty, custom_model_id, kind, movement_id; TIMESTAMP: created_at | INSERT OR IGNORE |
| [storage_gcp:txn:17](../../src/trusted_router/storage_gcp.py#L3670) | SELECT | STRING: account_id, movement_id | — |
| [storage_gcp:txn:18](../../src/trusted_router/storage_gcp.py#L3707) | SELECT | INT64: shard_count; STRING: workspace_id | — |
| [storage_gcp:txn:19](../../src/trusted_router/storage_gcp.py#L3915) | DML | INT64: amount; STRING: user_id; TIMESTAMP: now | — |
| [storage_gcp:_delete_credit_transfer_claim_tx:1](../../src/trusted_router/storage_gcp.py#L3784) | DML | STRING: account_id, movement_id | — |
| [storage_gcp:txn:20](../../src/trusted_router/storage_gcp.py#L4078) | DML | INT64: amount; STRING: user_id; TIMESTAMP: now | — |
| [storage_gcp:txn:21](../../src/trusted_router/storage_gcp.py#L4173) | DML | INT64: amount; STRING: user_id; TIMESTAMP: now | — |
| [storage_gcp:txn:22](../../src/trusted_router/storage_gcp.py#L4234) | SELECT | STRING: user_id | — |
| [storage_gcp:txn:23](../../src/trusted_router/storage_gcp.py#L4243) | DML | STRING: user_id; TIMESTAMP: now | — |
| [storage_gcp:txn:24](../../src/trusted_router/storage_gcp.py#L6388) | SELECT | INT64: limit; STRING: after, kind | — |
| [storage_gcp:earnings_summary:1](../../src/trusted_router/storage_gcp.py#L4268) | SELECT | STRING: user_id | — |
| [storage_gcp:list_credit_movements:1](../../src/trusted_router/storage_gcp.py#L4315) | SELECT | ARRAY<STRING>: kinds; INT64: limit; STRING: account_id; TIMESTAMP: before | FORCE_INDEX |
| [storage_gcp:custom_model_earnings_by_model:1](../../src/trusted_router/storage_gcp.py#L4350) | SELECT | STRING: account_id; TIMESTAMP: since | FORCE_INDEX |
| [storage_gcp:get_lifetime_topup_microdollars:1](../../src/trusted_router/storage_gcp.py#L4380) | SELECT | STRING: user_id | — |
| [storage_gcp:typed_key_usage:1](../../src/trusted_router/storage_gcp.py#L5363) | SELECT | INT64: shard_count; STRING: pk | — |
| [storage_gcp:typed_credit_snapshot:1](../../src/trusted_router/storage_gcp.py#L5416) | SELECT | INT64: shard_count; STRING: pk | — |
| [storage_gcp:typed_credit_trust_snapshot:1](../../src/trusted_router/storage_gcp.py#L5449) | SELECT | STRING: pk | — |
| [storage_gcp:list_trust_tier_workspace_ids:1](../../src/trusted_router/storage_gcp.py#L5467) | SELECT | none | — |
| [storage_gcp:_read_outbox_heartbeat_row:1](../../src/trusted_router/storage_gcp.py#L5802) | SELECT | STRING: id, kind | — |
| [storage_gcp:list_receipt_keys:1](../../src/trusted_router/storage_gcp.py#L6313) | SELECT | INT64: limit; STRING: after_att_sha256, after_kid, kind | FORCE_INDEX, null-filtered hint |
| [storage_gcp:list_receipt_keys:2](../../src/trusted_router/storage_gcp.py#L6324) | SELECT | INT64: limit; STRING: kind, legacy_after | — |
| [storage_gcp:list_receipt_keys:3](../../src/trusted_router/storage_gcp.py#L6334) | SELECT | INT64: limit; STRING: after_att_sha256, after_kid, kid, kind | FORCE_INDEX, null-filtered hint |
| [storage_gcp:list_receipt_keys:4](../../src/trusted_router/storage_gcp.py#L6349) | SELECT | INT64: limit; STRING: after_att_sha256, after_kid, kind | FORCE_INDEX, null-filtered hint |
| [storage_gcp:_read_entity_from:1](../../src/trusted_router/storage_gcp.py#L6542) | SELECT | STRING: id, kind | — |
| [storage_gcp:_list_entities:1](../../src/trusted_router/storage_gcp.py#L6594) | SELECT | INT64: limit; STRING: kind, prefix, suffix | — |
| [storage_gcp_analytics_outbox:enqueue_statement:1](../../src/trusted_router/storage_gcp_analytics_outbox.py#L81) | DML | INT64: shard; STRING: event_id, payload | PENDING_COMMIT_TIMESTAMP |
| [storage_gcp_authorize:key_window_limit_decision:1](../../src/trusted_router/storage_gcp_authorize.py#L315) | SELECT | INT64: shard_count; STRING: kh | — |
| [storage_gcp_authorize:constants:1](../../src/trusted_router/storage_gcp_authorize.py#L1078) | SELECT | INT64: limit; TIMESTAMP: now | — |
| [storage_gcp_authorize:constants:2](../../src/trusted_router/storage_gcp_authorize.py#L1083) | SELECT | INT64: limit; TIMESTAMP: now | — |
| [storage_gcp_authorize:_apply_user_model_payout_tx:1](../../src/trusted_router/storage_gcp_authorize.py#L1884) | DML | INT64: amount; STRING: account_id, authorization_id, counterparty, custom_model_id, kind, movement_id; TIMESTAMP: created_at | INSERT OR IGNORE |
| [storage_gcp_authorize:_apply_user_model_payout_tx:2](../../src/trusted_router/storage_gcp_authorize.py#L1913) | DML | INT64: amount; STRING: user_id | PENDING_COMMIT_TIMESTAMP |
| [storage_gcp_authorize:_apply_user_model_payout_tx:3](../../src/trusted_router/storage_gcp_authorize.py#L1922) | DML | INT64: amount; STRING: user_id | PENDING_COMMIT_TIMESTAMP |
| [storage_gcp_authorize:_apply_app_markup_payout_tx:1](../../src/trusted_router/storage_gcp_authorize.py#L1941) | DML | INT64: amount; STRING: account_id, app_id, authorization_id, counterparty, kind, movement_id; TIMESTAMP: created_at | INSERT OR IGNORE |
| [storage_gcp_authorize:_apply_app_markup_payout_tx:2](../../src/trusted_router/storage_gcp_authorize.py#L1970) | DML | INT64: amount; STRING: user_id | PENDING_COMMIT_TIMESTAMP |
| [storage_gcp_authorize:_apply_app_markup_payout_tx:3](../../src/trusted_router/storage_gcp_authorize.py#L1979) | DML | INT64: amount; STRING: user_id | PENDING_COMMIT_TIMESTAMP |
| [storage_gcp_authorize:_apply_custom_model_markup_payout_tx:1](../../src/trusted_router/storage_gcp_authorize.py#L1998) | DML | INT64: amount; STRING: account_id, authorization_id, counterparty, custom_model_id, kind, movement_id; TIMESTAMP: created_at | INSERT OR IGNORE |
| [storage_gcp_authorize:_apply_custom_model_markup_payout_tx:2](../../src/trusted_router/storage_gcp_authorize.py#L2027) | DML | INT64: amount; STRING: user_id | PENDING_COMMIT_TIMESTAMP |
| [storage_gcp_authorize:_apply_custom_model_markup_payout_tx:3](../../src/trusted_router/storage_gcp_authorize.py#L2036) | DML | INT64: amount; STRING: user_id | PENDING_COMMIT_TIMESTAMP |
| [storage_gcp_counter_dml:constants:1](../../src/trusted_router/storage_gcp_counter_dml.py#L36) | DML | INT64: actual; STRING: rid, sut; TIMESTAMP: terminal_at | — |
| [storage_gcp_counter_dml:constants:2](../../src/trusted_router/storage_gcp_counter_dml.py#L41) | DML | INT64: actual; STRING: rid, sut; TIMESTAMP: terminal_at | — |
| [storage_gcp_counter_dml:constants:3](../../src/trusted_router/storage_gcp_counter_dml.py#L49) | DML | STRING: rid; TIMESTAMP: terminal_at | — |
| [storage_gcp_counter_dml:constants:4](../../src/trusted_router/storage_gcp_counter_dml.py#L53) | DML | STRING: rid; TIMESTAMP: terminal_at | — |
| [storage_gcp_counter_dml:reserve_credit:1](../../src/trusted_router/storage_gcp_counter_dml.py#L76) | DML | INT64: est, shard; STRING: ws | — |
| [storage_gcp_counter_dml:debit_workspace_credit:1](../../src/trusted_router/storage_gcp_counter_dml.py#L108) | DML | INT64: amt; STRING: ws; TIMESTAMP: now | — |
| [storage_gcp_counter_dml:debit_credit_shard:1](../../src/trusted_router/storage_gcp_counter_dml.py#L152) | DML | INT64: donor, move; STRING: ws | — |
| [storage_gcp_counter_dml:credit_credit_shard:1](../../src/trusted_router/storage_gcp_counter_dml.py#L186) | DML | INT64: amount, shard; STRING: ws; TIMESTAMP: now | — |
| [storage_gcp_counter_dml:transfer_credit_budget:1](../../src/trusted_router/storage_gcp_counter_dml.py#L230) | DML | INT64: move, target; STRING: ws | — |
| [storage_gcp_counter_dml:release_credit:1](../../src/trusted_router/storage_gcp_counter_dml.py#L266) | DML | INT64: actual, hold, shard; STRING: ws | — |
| [storage_gcp_counter_dml:release_credit:2](../../src/trusted_router/storage_gcp_counter_dml.py#L303) | DML | INT64: amount, shard; STRING: ws | — |
| [storage_gcp_counter_dml:_credit_shard_count_from_rows:1](../../src/trusted_router/storage_gcp_counter_dml.py#L327) | SELECT | STRING: pk | — |
| [storage_gcp_counter_dml:reserve_key_statement:1](../../src/trusted_router/storage_gcp_counter_dml.py#L344) | DML | BOOL: is_byok; INT64: est, shard; STRING: kh | — |
| [storage_gcp_counter_dml:reserve_key:1](../../src/trusted_router/storage_gcp_counter_dml.py#L388) | SELECT | INT64: shard; STRING: kh | — |
| [storage_gcp_counter_dml:release_key:1](../../src/trusted_router/storage_gcp_counter_dml.py#L491) | DML | INT64: actual, hold, shard; STRING: kh; TIMESTAMP: day_floor, month_floor, week_floor | — |
| [storage_gcp_counter_dml:release_key:2](../../src/trusted_router/storage_gcp_counter_dml.py#L507) | DML | INT64: actual, hold, shard; STRING: kh; TIMESTAMP: day_floor, month_floor, week_floor | — |
| [storage_gcp_counter_dml:key_limit_exists:1](../../src/trusted_router/storage_gcp_counter_dml.py#L529) | SELECT | INT64: shard; STRING: kh | — |
| [storage_gcp_counter_dml:read_reservation_by_idempotency:1](../../src/trusted_router/storage_gcp_counter_dml.py#L562) | SELECT | STRING: scope | — |
| [storage_gcp_counter_dml:read_reservation:1](../../src/trusted_router/storage_gcp_counter_dml.py#L591) | SELECT | STRING: rid | — |
| [storage_gcp_counter_dml:reservation_insert_statement:1](../../src/trusted_router/storage_gcp_counter_dml.py#L639) | DML | INT64: credit_reserved_micro, credit_shard, key_reserved_micro, key_shard, ws_shard; STRING: authorization_id, hold_usage_type, idempotency_fingerprint, idempotency_scope, key_hash, reservation_id, workspace_id; TIMESTAMP: created_at, expires_at | — |
| [storage_gcp_counter_dml:reservation_retention_clear_statement:1](../../src/trusted_router/storage_gcp_counter_dml.py#L738) | DML | STRING: rid | — |
| [storage_gcp_counter_dml:entity_insert_statement:1](../../src/trusted_router/storage_gcp_counter_dml.py#L758) | DML | STRING: body, id, kind | PENDING_COMMIT_TIMESTAMP |
| [storage_gcp_counter_dml:insert_entity_dml_at:1](../../src/trusted_router/storage_gcp_counter_dml.py#L777) | DML | STRING: body, id, kind; TIMESTAMP: now | — |
| [storage_gcp_counter_dml:delete_entity_dml:1](../../src/trusted_router/storage_gcp_counter_dml.py#L804) | DML | STRING: id, kind | — |
| [storage_gcp_counter_dml:update_entity_body_dml:1](../../src/trusted_router/storage_gcp_counter_dml.py#L816) | DML | STRING: body, id, kind; TIMESTAMP: now | — |
| [storage_gcp_counter_reconcile:constants:1](../../src/trusted_router/storage_gcp_counter_reconcile.py#L28) | SELECT | none | — |
| [storage_gcp_counter_reconcile:constants:2](../../src/trusted_router/storage_gcp_counter_reconcile.py#L34) | SELECT | none | — |
| [storage_gcp_counter_reconcile:constants:3](../../src/trusted_router/storage_gcp_counter_reconcile.py#L65) | SELECT | STRING: ws | — |
| [storage_gcp_counter_reconcile:constants:4](../../src/trusted_router/storage_gcp_counter_reconcile.py#L70) | SELECT | STRING: ws | JSON_VALUE |
| [storage_gcp_counter_reconcile:constants:5](../../src/trusted_router/storage_gcp_counter_reconcile.py#L87) | SELECT | none | JSON_VALUE |
| [storage_gcp_counter_reconcile:constants:6](../../src/trusted_router/storage_gcp_counter_reconcile.py#L91) | SELECT | none | — |
| [storage_gcp_counter_reconcile:constants:7](../../src/trusted_router/storage_gcp_counter_reconcile.py#L99) | SELECT | none | JSON_VALUE |
| [storage_gcp_counter_reconcile:constants:8](../../src/trusted_router/storage_gcp_counter_reconcile.py#L117) | SELECT | none | — |
| [storage_gcp_counter_reconcile:constants:9](../../src/trusted_router/storage_gcp_counter_reconcile.py#L122) | SELECT | none | JSON_VALUE |
| [storage_gcp_counter_reconcile:audit_typed_invariants:1](../../src/trusted_router/storage_gcp_counter_reconcile.py#L223) | SELECT | none | — |
| [storage_gcp_counter_reconcile:audit_typed_invariants:2](../../src/trusted_router/storage_gcp_counter_reconcile.py#L228) | SELECT | none | — |
| [storage_gcp_counter_reconcile:repair_typed_reserved:1](../../src/trusted_router/storage_gcp_counter_reconcile.py#L498) | SELECT | STRING: ws | — |
| [storage_gcp_counter_reconcile:repair_typed_reserved:2](../../src/trusted_router/storage_gcp_counter_reconcile.py#L502) | SELECT | STRING: kh | — |
| [storage_gcp_counter_reconcile:repair_typed_reserved:3](../../src/trusted_router/storage_gcp_counter_reconcile.py#L505) | SELECT | STRING: pk | — |
| [storage_gcp_counter_reconcile:repair_typed_reserved:4](../../src/trusted_router/storage_gcp_counter_reconcile.py#L506) | SELECT | STRING: pk | — |
| [storage_gcp_counter_reconcile:repair_typed_reserved:5](../../src/trusted_router/storage_gcp_counter_reconcile.py#L508) | SELECT | STRING: kh | — |
| [storage_gcp_counter_reconcile:repair_typed_reserved:6](../../src/trusted_router/storage_gcp_counter_reconcile.py#L512) | SELECT | STRING: ws | — |
| [storage_gcp_counter_reconcile:repair_typed_usage:1](../../src/trusted_router/storage_gcp_counter_reconcile.py#L666) | SELECT | STRING: pk | — |
| [storage_gcp_counter_reconcile:repair_typed_usage:2](../../src/trusted_router/storage_gcp_counter_reconcile.py#L669) | SELECT | STRING: ws | — |
| [storage_gcp_counter_reconcile:repair_typed_usage:3](../../src/trusted_router/storage_gcp_counter_reconcile.py#L672) | SELECT | STRING: ws | JSON_VALUE |
| [storage_gcp_credit_json_cleanup:legacy_credit_workspace_ids:1](../../src/trusted_router/storage_gcp_credit_json_cleanup.py#L81) | SELECT | STRING: kind | — |
| [storage_gcp_credit_json_cleanup:_inspect_reader:1](../../src/trusted_router/storage_gcp_credit_json_cleanup.py#L117) | SELECT | INT64: shard_count; STRING: pk | — |
| [storage_gcp_credit_rebalance:credit_headroom_precheck:1](../../src/trusted_router/storage_gcp_credit_rebalance.py#L53) | SELECT | INT64: shard_count; STRING: pk | — |
| [storage_gcp_credit_rebalance:txn:1](../../src/trusted_router/storage_gcp_credit_rebalance.py#L136) | SELECT | INT64: shard_count; STRING: pk | — |
| [storage_gcp_credit_shard_admin:_typed_state:1](../../src/trusted_router/storage_gcp_credit_shard_admin.py#L80) | SELECT | INT64: shard_count; STRING: pk | — |
| [storage_gcp_credit_shard_admin:_typed_state:2](../../src/trusted_router/storage_gcp_credit_shard_admin.py#L91) | SELECT | STRING: ws | — |
| [storage_gcp_credit_shard_admin:txn:1](../../src/trusted_router/storage_gcp_credit_shard_admin.py#L291) | SELECT | INT64: shard_count; STRING: pk | — |
| [storage_gcp_credit_shard_admin:txn:2](../../src/trusted_router/storage_gcp_credit_shard_admin.py#L339) | SELECT | STRING: ws | — |
| [storage_gcp_credit_transfer:_read_shard_headroom:1](../../src/trusted_router/storage_gcp_credit_transfer.py#L165) | SELECT | INT64: shard_count; STRING: pk | — |
| [storage_gcp_credit_transfer:read:1](../../src/trusted_router/storage_gcp_credit_transfer.py#L400) | SELECT | INT64: limit; STRING: after, kind | — |
| [storage_gcp_custom_models:list_for_user:1](../../src/trusted_router/storage_gcp_custom_models.py#L96) | SELECT | STRING: owner_user_id, prefix | JSON_VALUE |
| [storage_gcp_federated_settlement:_read_entity_body:1](../../src/trusted_router/storage_gcp_federated_settlement.py#L55) | SELECT | STRING: id, kind | — |
| [storage_gcp_federated_settlement:_book_usage:1](../../src/trusted_router/storage_gcp_federated_settlement.py#L85) | DML | INT64: amt; STRING: ws | — |
| [storage_gcp_federated_settlement:_book_usage:2](../../src/trusted_router/storage_gcp_federated_settlement.py#L98) | DML | INT64: amt; STRING: ws; TIMESTAMP: now | — |
| [storage_gcp_generation_records:generation_insert_statement:1](../../src/trusted_router/storage_gcp_generation_records.py#L44) | DML | STRING: generation_id, key_hash, payload, workspace_id; TIMESTAMP: created_at, terminal_at | — |
| [storage_gcp_generation_records:upsert_generation_record:1](../../src/trusted_router/storage_gcp_generation_records.py#L77) | DML | STRING: generation_id, key_hash, payload, workspace_id; TIMESTAMP: created_at, terminal_at | — |
| [storage_gcp_generation_records:read_generation_record:1](../../src/trusted_router/storage_gcp_generation_records.py#L108) | SELECT | STRING: generation_id | — |
| [storage_gcp_generations:_reconcile_page:1](../../src/trusted_router/storage_gcp_generations.py#L291) | SELECT | INT64: limit; STRING: after_id, kind, prefix | — |
| [storage_gcp_google_ads:_read_entity_from:1](../../src/trusted_router/storage_gcp_google_ads.py#L139) | SELECT | STRING: id, kind | — |
| [storage_gcp_google_ads:_list_entities:1](../../src/trusted_router/storage_gcp_google_ads.py#L176) | SELECT | INT64: limit; STRING: kind, prefix, suffix | — |
| [storage_gcp_key_escrow:key_escrow_rows:1](../../src/trusted_router/storage_gcp_key_escrow.py#L40) | SELECT | INT64: shard_count; STRING: kh | — |
| [storage_gcp_key_shard_admin:_typed_key_state:1](../../src/trusted_router/storage_gcp_key_shard_admin.py#L91) | SELECT | INT64: shard_count; STRING: pk | — |
| [storage_gcp_key_shard_admin:_typed_key_state:2](../../src/trusted_router/storage_gcp_key_shard_admin.py#L102) | SELECT | STRING: kh | — |
| [storage_gcp_key_shard_admin:txn:1](../../src/trusted_router/storage_gcp_key_shard_admin.py#L245) | SELECT | INT64: shard_count; STRING: pk | — |
| [storage_gcp_key_shard_admin:txn:2](../../src/trusted_router/storage_gcp_key_shard_admin.py#L271) | SELECT | STRING: kh | — |
| [storage_gcp_keys:constants:1](../../src/trusted_router/storage_gcp_keys.py#L46) | SELECT | STRING: prefix, workspace_id | JSON_VALUE |
| [storage_gcp_keys:txn:1](../../src/trusted_router/storage_gcp_keys.py#L265) | SELECT | INT64: shard_count; STRING: kh | — |
| [storage_gcp_legacy_reservations:legacy_reservation_snapshot:1](../../src/trusted_router/storage_gcp_legacy_reservations.py#L44) | SELECT | TIMESTAMP: cutoff | JSON_VALUE |
| [storage_gcp_operational_analytics_outbox:oldest_enqueued_at:1](../../src/trusted_router/storage_gcp_operational_analytics_outbox.py#L179) | SELECT | TIMESTAMP: floor_0 | — |
| [storage_gcp_operational_analytics_outbox:oldest_enqueued_at:2](../../src/trusted_router/storage_gcp_operational_analytics_outbox.py#L188) | SELECT | TIMESTAMP: floor_0 | — |
| [storage_gcp_operational_analytics_outbox:_insert_statement:1](../../src/trusted_router/storage_gcp_operational_analytics_outbox.py#L245) | DML | INT64: shard; STRING: event_id, event_kind, payload | PENDING_COMMIT_TIMESTAMP |
| [storage_gcp_request_records:constants:1](../../src/trusted_router/storage_gcp_request_records.py#L135) | DML | STRING: authorization_id; TIMESTAMP: terminal_at | — |
| [storage_gcp_request_records:constants:2](../../src/trusted_router/storage_gcp_request_records.py#L140) | DML | STRING: authorization_id; TIMESTAMP: terminal_at | — |
| [storage_gcp_request_records:constants:3](../../src/trusted_router/storage_gcp_request_records.py#L149) | DML | INT64: estimated_microdollars, finalized_cost_microdollars, heartbeat_seq; STRING: authorization_id, delivered_usage, finalization_outcome, gateway_request_id, heartbeat_hash, idempotency_fingerprint, invocation_nonce, key_hash, model_id, payload, pricing_snapshot, provider, reservation_id, selected_endpoint_id, stage_d_boot_kid, usage_type, workspace_id; TIMESTAMP: created_at, heartbeat_at, started_at | — |
| [storage_gcp_request_records:read_gateway_authorization:1](../../src/trusted_router/storage_gcp_request_records.py#L219) | SELECT | STRING: authorization_id | — |
| [storage_gcp_request_records:gateway_authorization_settled_statement:1](../../src/trusted_router/storage_gcp_request_records.py#L368) | DML | INT64: finalized_cost_microdollars; STRING: authorization_id, finalization_outcome, gateway_request_id, payload | PARSE_JSON, TO_JSON |
| [storage_gcp_request_records:read_gateway_authorization_by_gateway_request_id:1](../../src/trusted_router/storage_gcp_request_records.py#L399) | SELECT | STRING: gateway_request_id | FORCE_INDEX, null-filtered hint |
| [storage_gcp_request_records:gateway_authorization_retention_clear_statement:1](../../src/trusted_router/storage_gcp_request_records.py#L454) | DML | STRING: authorization_id | — |
| [storage_gcp_settle_outbox:constants:1](../../src/trusted_router/storage_gcp_settle_outbox.py#L88) | SELECT | STRING: aid | — |
| [storage_gcp_settle_outbox:done_retention_statements:1](../../src/trusted_router/storage_gcp_settle_outbox.py#L149) | SELECT | STRING: aid, kind | — |
| [storage_gcp_settle_outbox:done_retention_statements:2](../../src/trusted_router/storage_gcp_settle_outbox.py#L158) | DML | STRING: aid, kind, record_id; TIMESTAMP: now | — |
| [storage_gcp_settle_outbox:constants:2](../../src/trusted_router/storage_gcp_settle_outbox.py#L177) | DML | STRING: aid, kind, lease_owner, status; TIMESTAMP: now | THEN RETURN |
| [storage_gcp_settle_outbox:constants:3](../../src/trusted_router/storage_gcp_settle_outbox.py#L186) | SELECT | STRING: aid, kind | — |
| [storage_gcp_settle_outbox:intent_insert_statements:1](../../src/trusted_router/storage_gcp_settle_outbox.py#L262) | DML | INT64: actual_cost_micro, attempts, auto_refill_attempts; STRING: authorization_id, auto_refill_last_error, auto_refill_lease_owner, auto_refill_status, auto_refill_workspace_id, intent_kind, last_error, lease_owner, model_id, reservation_id, selected_endpoint_id, selected_usage_type, settle_body, settle_origin, status; TIMESTAMP: auto_refill_enqueued_at, auto_refill_leased_until, auto_refill_next_attempt_at, auto_refill_terminal_at, auto_refill_updated_at, created_at, leased_until, next_attempt_at, terminal_at, updated_at | — |
| [storage_gcp_settle_outbox:refresh_txn:1](../../src/trusted_router/storage_gcp_settle_outbox.py#L463) | DML | INT64: actual_cost_micro; STRING: authorization_id, auto_refill_workspace_id, intent_kind, model_id, reservation_id, selected_endpoint_id, selected_usage_type, settle_body, settle_origin; TIMESTAMP: now | — |
| [storage_gcp_settle_outbox:due:1](../../src/trusted_router/storage_gcp_settle_outbox.py#L532) | SELECT | INT64: limit; TIMESTAMP: now | FORCE_INDEX, null-filtered hint |
| [storage_gcp_settle_outbox:txn:1](../../src/trusted_router/storage_gcp_settle_outbox.py#L563) | DML | STRING: aid, kind, owner; TIMESTAMP: lease, now | — |
| [storage_gcp_settle_outbox:txn:2](../../src/trusted_router/storage_gcp_settle_outbox.py#L617) | SELECT | STRING: aid, kind | — |
| [storage_gcp_settle_outbox:txn:3](../../src/trusted_router/storage_gcp_settle_outbox.py#L655) | DML | BOOL: done; INT64: attempts; STRING: aid, err, kind, lease_owner, status; TIMESTAMP: next_at, now, terminal_at | — |
| [storage_gcp_settle_outbox:txn:4](../../src/trusted_router/storage_gcp_settle_outbox.py#L730) | SELECT | STRING: aid, kind | — |
| [storage_gcp_settle_outbox:txn:5](../../src/trusted_router/storage_gcp_settle_outbox.py#L748) | DML | INT64: attempts; STRING: aid, err, kind, lease_owner; TIMESTAMP: next_at, now | — |
| [storage_gcp_settle_outbox:txn:6](../../src/trusted_router/storage_gcp_settle_outbox.py#L804) | DML | STRING: aid, workspace_id; TIMESTAMP: next_at, now | — |
| [storage_gcp_settle_outbox:due_auto_refills:1](../../src/trusted_router/storage_gcp_settle_outbox.py#L835) | SELECT | INT64: limit; TIMESTAMP: now | FORCE_INDEX, null-filtered hint |
| [storage_gcp_settle_outbox:txn:7](../../src/trusted_router/storage_gcp_settle_outbox.py#L882) | DML | STRING: aid, owner; TIMESTAMP: lease, now | — |
| [storage_gcp_settle_outbox:txn:8](../../src/trusted_router/storage_gcp_settle_outbox.py#L920) | SELECT | STRING: aid | — |
| [storage_gcp_settle_outbox:txn:9](../../src/trusted_router/storage_gcp_settle_outbox.py#L944) | DML | INT64: attempts; STRING: aid, error, lease_owner, status; TIMESTAMP: next_at, now, terminal_at | — |
| [storage_gcp_settle_outbox:get_auto_refill:1](../../src/trusted_router/storage_gcp_settle_outbox.py#L980) | SELECT | STRING: aid | — |
| [storage_gcp_settle_outbox:auto_refill_pending_freshness:1](../../src/trusted_router/storage_gcp_settle_outbox.py#L994) | SELECT | none | FORCE_INDEX, null-filtered hint |
| [storage_gcp_settle_outbox:get:1](../../src/trusted_router/storage_gcp_settle_outbox.py#L1026) | SELECT | STRING: aid, kind | — |
| [storage_gcp_stage_d:txn:1](../../src/trusted_router/storage_gcp_stage_d.py#L80) | SELECT | STRING: rid | — |
| [storage_gcp_stage_d:txn:2](../../src/trusted_router/storage_gcp_stage_d.py#L144) | DML | INT64: seq; STRING: authorization_id, delivered_usage, heartbeat_hash, selected_endpoint_id; TIMESTAMP: heartbeat_at, started_at | — |
| [storage_gcp_stage_d:txn:3](../../src/trusted_router/storage_gcp_stage_d.py#L174) | DML | STRING: rid; TIMESTAMP: renewed_expires_at | — |
| [storage_gcp_stage_d_policy:get_stage_d_policy_watermark:1](../../src/trusted_router/storage_gcp_stage_d_policy.py#L22) | SELECT | STRING: plane | — |
| [storage_gcp_stage_d_policy:txn:1](../../src/trusted_router/storage_gcp_stage_d_policy.py#L43) | SELECT | STRING: plane | — |
| [storage_gcp_stage_d_policy:txn:2](../../src/trusted_router/storage_gcp_stage_d_policy.py#L52) | DML | INT64: sequence; STRING: plane; TIMESTAMP: updated_at | — |
| [storage_gcp_stage_d_policy:txn:3](../../src/trusted_router/storage_gcp_stage_d_policy.py#L67) | DML | INT64: sequence; STRING: plane; TIMESTAMP: updated_at | — |
| [storage_gcp_strict_budget:reserve_strict_key:1](../../src/trusted_router/storage_gcp_strict_budget.py#L31) | DML | BOOL: is_byok; INT64: est; STRING: kh | — |
| [storage_gcp_strict_budget:reserve_strict_key:2](../../src/trusted_router/storage_gcp_strict_budget.py#L51) | SELECT | STRING: kh | — |
| [storage_gcp_trust:insert_credit_trust_event:1](../../src/trusted_router/storage_gcp_trust.py#L69) | DML | INT64: amount_micro, credited_micro, cumulative_refunded, payment_amount_micro, recovered_micro, recovery_target, unrecovered_micro; STRING: adverse_ref, currency, debit_status, event_id, kind, lifecycle_status, original_payment_ref, provider, provider_ordering_watermark, provider_subtype, workspace_id; TIMESTAMP: occurred_at, recorded_at | — |
| [storage_gcp_trust:_read_payment_tx:1](../../src/trusted_router/storage_gcp_trust.py#L151) | SELECT | STRING: original_payment_ref, provider | — |
| [storage_gcp_trust:_sync_principal_recovery_pause_tx:1](../../src/trusted_router/storage_gcp_trust.py#L182) | SELECT | INT64: shard_count; STRING: pk | — |
| [storage_gcp_trust:_sync_principal_recovery_pause_tx:2](../../src/trusted_router/storage_gcp_trust.py#L205) | DML | ARRAY<STRING>: causes; INT64: shard_count; STRING: pk; TIMESTAMP: now | — |
| [storage_gcp_trust:_sync_principal_recovery_pause_tx:3](../../src/trusted_router/storage_gcp_trust.py#L236) | DML | BOOL: paused; STRING: causes_json, reason, workspace_id; TIMESTAMP: now | JSON_SET, PARSE_JSON, TO_JSON |
| [storage_gcp_trust:absorb_unrecovered_recovery_tx:1](../../src/trusted_router/storage_gcp_trust.py#L279) | SELECT | STRING: pk | — |
| [storage_gcp_trust:absorb_unrecovered_recovery_tx:2](../../src/trusted_router/storage_gcp_trust.py#L299) | DML | INT64: amount; STRING: event_id, workspace_id | — |
| [storage_gcp_trust:absorb_unrecovered_recovery_tx:3](../../src/trusted_router/storage_gcp_trust.py#L323) | SELECT | STRING: pk | — |
| [storage_gcp_trust:_debit_available_principal_tx:1](../../src/trusted_router/storage_gcp_trust.py#L354) | SELECT | INT64: shard_count; STRING: pk | — |
| [storage_gcp_trust:_debit_available_principal_tx:2](../../src/trusted_router/storage_gcp_trust.py#L369) | DML | INT64: amount, shard; STRING: ws; TIMESTAMP: now | — |
| [storage_gcp_trust:apply_adverse_trust_event_tx:1](../../src/trusted_router/storage_gcp_trust.py#L417) | SELECT | STRING: adverse_ref, provider | — |
| [storage_gcp_trust:apply_adverse_trust_event_tx:2](../../src/trusted_router/storage_gcp_trust.py#L448) | DML | STRING: adverse_ref, event_id, provider, provider_ordering_watermark, provider_subtype, workspace_id; TIMESTAMP: occurred_at | — |
| [storage_gcp_trust:apply_adverse_trust_event_tx:3](../../src/trusted_router/storage_gcp_trust.py#L515) | DML | INT64: amount_micro; STRING: adverse_ref, event_id, lifecycle_status, provider, provider_ordering_watermark, provider_subtype, workspace_id; TIMESTAMP: occurred_at, recorded_at | — |
| [storage_gcp_trust:apply_adverse_trust_event_tx:4](../../src/trusted_router/storage_gcp_trust.py#L555) | DML | INT64: shard_count; STRING: pk; TIMESTAMP: now | — |
| [storage_gcp_trust:apply_adverse_trust_event_tx:5](../../src/trusted_router/storage_gcp_trust.py#L568) | SELECT | STRING: original_payment_ref, provider, workspace_id | — |
| [storage_gcp_trust:apply_adverse_trust_event_tx:6](../../src/trusted_router/storage_gcp_trust.py#L625) | DML | INT64: cumulative_refunded, recovered_micro, recovery_target, unrecovered_micro; STRING: debit_status, event_id, workspace_id | — |
| [storage_gcp_trust:apply_adverse_trust_event_tx:7](../../src/trusted_router/storage_gcp_trust.py#L651) | DML | INT64: cumulative_refunded, recovery_target, unrecovered_micro; STRING: debit_status, event_id, kind, workspace_id | — |
| [storage_gcp_trust:insert_trust_inbox_tx:1](../../src/trusted_router/storage_gcp_trust.py#L699) | DML | STRING: adverse_ref, payload, provider; TIMESTAMP: received_at | — |
| [storage_gcp_trust:drain_matching_trust_inbox_tx:1](../../src/trusted_router/storage_gcp_trust.py#L731) | SELECT | STRING: provider | — |
| [storage_gcp_trust:drain_matching_trust_inbox_tx:2](../../src/trusted_router/storage_gcp_trust.py#L756) | DML | STRING: adverse_ref, provider | — |
| [storage_gcp_trust:txn:1](../../src/trusted_router/storage_gcp_trust.py#L787) | SELECT | STRING: pk | — |
| [storage_gcp_trust:txn:2](../../src/trusted_router/storage_gcp_trust.py#L799) | SELECT | STRING: pk | — |
| [storage_gcp_trust:txn:3](../../src/trusted_router/storage_gcp_trust.py#L816) | SELECT | INT64: shard_count; STRING: pk | — |
| [storage_gcp_trust:txn:4](../../src/trusted_router/storage_gcp_trust.py#L846) | DML | INT64: shard_count, trust_tier; STRING: pk; TIMESTAMP: trust_computed_at | — |
| [storage_gcp_user_models:list_for_user:1](../../src/trusted_router/storage_gcp_user_models.py#L145) | SELECT | STRING: owner_user_id, prefix | JSON_VALUE |
| [storage_gcp_user_models:get_many:1](../../src/trusted_router/storage_gcp_user_models.py#L186) | SELECT | ARRAY<STRING>: model_ids | — |
| [storage_gcp_user_models:txn:1](../../src/trusted_router/storage_gcp_user_models.py#L360) | SELECT | STRING: kind, prefix | — |
| [storage_legacy_trust:spanner_pause_epoch:1](../../src/trusted_router/storage_legacy_trust.py#L179) | SELECT | STRING: ws | — |
| [storage_trust_inbox_resolution:retained_refund_row:1](../../src/trusted_router/storage_trust_inbox_resolution.py#L43) | SELECT | STRING: adverse_ref, provider | — |
| [storage_trust_inbox_resolution:spanner_tx:1](../../src/trusted_router/storage_trust_inbox_resolution.py#L107) | SELECT | STRING: adverse_ref, provider | — |
| [storage_trust_reconciliation:get_marker:1](../../src/trusted_router/storage_trust_reconciliation.py#L156) | SELECT | STRING: account_id, environment, provider, source, source_version | — |
| [storage_trust_reconciliation:txn:1](../../src/trusted_router/storage_trust_reconciliation.py#L197) | DML | INT64: consistency_delay_seconds, semantic_mismatch_count, unmatched_count; STRING: account_id, environment, provider, source, source_version; TIMESTAMP: closed_through, completed_at, history_start | — |
| [storage_trust_reconciliation:txn:2](../../src/trusted_router/storage_trust_reconciliation.py#L212) | DML | INT64: consistency_delay_seconds, semantic_mismatch_count, unmatched_count; STRING: account_id, environment, provider, source, source_version; TIMESTAMP: closed_through, completed_at, history_start | — |
| [storage_trust_reconciliation:list_provider_events:1](../../src/trusted_router/storage_trust_reconciliation.py#L306) | SELECT | STRING: provider | — |
| [storage_trust_reconciliation:txn:3](../../src/trusted_router/storage_trust_reconciliation.py#L332) | SELECT | STRING: workspace_id | — |
| [storage_trust_reconciliation:txn:4](../../src/trusted_router/storage_trust_reconciliation.py#L343) | SELECT | STRING: environment, provider, source, source_version | — |
| [storage_trust_reconciliation:txn:5](../../src/trusted_router/storage_trust_reconciliation.py#L369) | SELECT | STRING: workspace_id | — |
| [storage_trust_reconciliation:txn:6](../../src/trusted_router/storage_trust_reconciliation.py#L376) | DML | STRING: workspace_id; TIMESTAMP: watermark | — |
| [trust_eligibility:billing_paused_tx:1](../../src/trusted_router/trust_eligibility.py#L27) | SELECT | INT64: shard; STRING: ws | — |
| [trust_owner_budget:recompute_owner_budget:1](../../src/trusted_router/trust_owner_budget.py#L44) | SELECT | none | — |

## Runtime builder and batch cases

All 64 heartbeat null/non-null combinations seed an authorization and require one affected row. The guarded done-returning case requires one returned row. Explicit rollback and session deletion run on success and failure; cleanup failures do not mask the original error.

| Case | Transport | Statements |
|---|---|---:|
| authorization | DML | 1 |
| reservation | DML | 1 |
| entity | DML | 1 |
| reserve-key | DML | 1 |
| generation | DML | 1 |
| activity | DML | 1 |
| authorize-batch | batch DML | 3 |
| authorize-legacy-batch | batch DML | 3 |
| authorize-sequential-batch | batch DML | 2 |
| authorize-sequential-legacy-batch | batch DML | 2 |
| settle-metadata-batch | batch DML | 3 |
| settled-heartbeat-000000 | DML | 1 |
| settled-heartbeat-000001 | DML | 1 |
| settled-heartbeat-000010 | DML | 1 |
| settled-heartbeat-000011 | DML | 1 |
| settled-heartbeat-000100 | DML | 1 |
| settled-heartbeat-000101 | DML | 1 |
| settled-heartbeat-000110 | DML | 1 |
| settled-heartbeat-000111 | DML | 1 |
| settled-heartbeat-001000 | DML | 1 |
| settled-heartbeat-001001 | DML | 1 |
| settled-heartbeat-001010 | DML | 1 |
| settled-heartbeat-001011 | DML | 1 |
| settled-heartbeat-001100 | DML | 1 |
| settled-heartbeat-001101 | DML | 1 |
| settled-heartbeat-001110 | DML | 1 |
| settled-heartbeat-001111 | DML | 1 |
| settled-heartbeat-010000 | DML | 1 |
| settled-heartbeat-010001 | DML | 1 |
| settled-heartbeat-010010 | DML | 1 |
| settled-heartbeat-010011 | DML | 1 |
| settled-heartbeat-010100 | DML | 1 |
| settled-heartbeat-010101 | DML | 1 |
| settled-heartbeat-010110 | DML | 1 |
| settled-heartbeat-010111 | DML | 1 |
| settled-heartbeat-011000 | DML | 1 |
| settled-heartbeat-011001 | DML | 1 |
| settled-heartbeat-011010 | DML | 1 |
| settled-heartbeat-011011 | DML | 1 |
| settled-heartbeat-011100 | DML | 1 |
| settled-heartbeat-011101 | DML | 1 |
| settled-heartbeat-011110 | DML | 1 |
| settled-heartbeat-011111 | DML | 1 |
| settled-heartbeat-100000 | DML | 1 |
| settled-heartbeat-100001 | DML | 1 |
| settled-heartbeat-100010 | DML | 1 |
| settled-heartbeat-100011 | DML | 1 |
| settled-heartbeat-100100 | DML | 1 |
| settled-heartbeat-100101 | DML | 1 |
| settled-heartbeat-100110 | DML | 1 |
| settled-heartbeat-100111 | DML | 1 |
| settled-heartbeat-101000 | DML | 1 |
| settled-heartbeat-101001 | DML | 1 |
| settled-heartbeat-101010 | DML | 1 |
| settled-heartbeat-101011 | DML | 1 |
| settled-heartbeat-101100 | DML | 1 |
| settled-heartbeat-101101 | DML | 1 |
| settled-heartbeat-101110 | DML | 1 |
| settled-heartbeat-101111 | DML | 1 |
| settled-heartbeat-110000 | DML | 1 |
| settled-heartbeat-110001 | DML | 1 |
| settled-heartbeat-110010 | DML | 1 |
| settled-heartbeat-110011 | DML | 1 |
| settled-heartbeat-110100 | DML | 1 |
| settled-heartbeat-110101 | DML | 1 |
| settled-heartbeat-110110 | DML | 1 |
| settled-heartbeat-110111 | DML | 1 |
| settled-heartbeat-111000 | DML | 1 |
| settled-heartbeat-111001 | DML | 1 |
| settled-heartbeat-111010 | DML | 1 |
| settled-heartbeat-111011 | DML | 1 |
| settled-heartbeat-111100 | DML | 1 |
| settled-heartbeat-111101 | DML | 1 |
| settled-heartbeat-111110 | DML | 1 |
| settled-heartbeat-111111 | DML | 1 |
| claim-False-False-False | DML | 1 |
| claim-False-False-True | DML | 1 |
| claim-False-True-False | DML | 1 |
| claim-False-True-True | DML | 1 |
| claim-True-False-False | DML | 1 |
| claim-True-False-True | DML | 1 |
| claim-True-True-False | DML | 1 |
| claim-True-True-True | DML | 1 |
| enqueue-retention-clear-batch | batch DML | 2 |
| done-retention-None | batch DML | 1 |
| done-retention-acceptance-reservation | batch DML | 2 |
| speculative-done-batch | batch DML | 3 |
| release-key-False-0 | DML | 1 |
| release-key-False-1 | DML | 1 |
| release-key-True-0 | DML | 1 |
| release-key-True-1 | DML | 1 |
| strict-key-False-0 | DML | 1 |
| strict-key-False-1 | SELECT | 1 |
| strict-key-True-0 | DML | 1 |
| strict-key-True-1 | SELECT | 1 |
| settle-batch-False-False-False-False | batch DML | 1 |
| settle-batch-False-False-False-True | batch DML | 2 |
| settle-batch-False-False-True-False | batch DML | 2 |
| settle-batch-False-False-True-True | batch DML | 3 |
| settle-batch-False-True-False-False | batch DML | 4 |
| settle-batch-False-True-False-True | batch DML | 5 |
| settle-batch-False-True-True-False | batch DML | 5 |
| settle-batch-False-True-True-True | batch DML | 6 |
| settle-batch-True-False-False-False | batch DML | 2 |
| settle-batch-True-False-False-True | batch DML | 3 |
| settle-batch-True-False-True-False | batch DML | 3 |
| settle-batch-True-False-True-True | batch DML | 4 |
| settle-batch-True-True-False-False | batch DML | 5 |
| settle-batch-True-True-False-True | batch DML | 6 |
| settle-batch-True-True-True-False | batch DML | 6 |
| settle-batch-True-True-True-True | batch DML | 7 |
| outbox-enqueue-batch-False-False | batch DML | 2 |
| outbox-enqueue-batch-False-True | batch DML | 2 |
| outbox-enqueue-batch-True-False | batch DML | 3 |
| guarded-done-returning | DML | 1 |
| outbox-enqueue-batch-True-True | batch DML | 3 |
| oldest-full-shards-0 | SELECT | 1 |
| oldest-full-shards-1 | SELECT | 1 |
| oldest-full-shards-32 | SELECT | 1 |
