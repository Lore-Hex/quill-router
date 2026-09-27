# Native GoogleSQL statement inventory

Generated from the registered current source expressions and explicit builder cases. Source line numbers are navigational; AST fingerprints guard completeness. Every row below is executed in CI; none is exempted for presumed emulator limitations. Parameter values and dynamic bindings are in [the manifest](../../tests/conformance/spanner_sql_manifest.json).

| Source expressions | SELECT | DML | Dispatch scopes |
|---|---:|---:|---:|
| 239 | 134 | 105 | 188 |

257 literal scenarios plus 124 runtime builder/capture cases = 381 primary acceptance cases: {'SELECT': 156, 'DML': 196, 'batch DML': 29}. In addition, 105 literal DML cases run through Batch DML and three rejection canaries must fail on the server, with three paired positive controls that must succeed. Seed DML is additional setup inside the same rolled-back transaction. Counts describe registered cases, not every possible parameter value.

| ID / source | Kind | Typed parameters | Emulator-sensitive features (support unverified offline) |
|---|---|---|---|
| [storage_gcp:readiness_check:1](../../src/trusted_router/storage_gcp.py#L726) | SELECT | none | — |
| [storage_gcp:_owner_workspace_ids_tx:1](../../src/trusted_router/storage_gcp.py#L1031) | SELECT | STRING: owner | — |
| [storage_gcp:txn:1](../../src/trusted_router/storage_gcp.py#L1264) | SELECT | INT64: shard_count; STRING: pk | — |
| [storage_gcp:txn:2](../../src/trusted_router/storage_gcp.py#L1283) | SELECT | STRING: pk | JSON_VALUE |
| [storage_gcp:_workspace_trust_events_tx:1](../../src/trusted_router/storage_gcp.py#L1429) | SELECT | STRING: pk | — |
| [storage_gcp:txn:3](../../src/trusted_router/storage_gcp.py#L1465) | SELECT | INT64: shard_count; STRING: pk | — |
| [storage_gcp:_existing_operator_abuse:1](../../src/trusted_router/storage_gcp.py#L1547) | SELECT | STRING: adverse_ref, provider | — |
| [storage_gcp:txn:4](../../src/trusted_router/storage_gcp.py#L1577) | SELECT | STRING: adverse_ref, provider | — |
| [storage_gcp:txn:5](../../src/trusted_router/storage_gcp.py#L1601) | SELECT | INT64: shard_count; STRING: pk | — |
| [storage_gcp:txn:6](../../src/trusted_router/storage_gcp.py#L1665) | DML | ARRAY<STRING>: causes; INT64: shard_count; STRING: pk; TIMESTAMP: now | — |
| [storage_gcp:txn:7](../../src/trusted_router/storage_gcp.py#L1740) | SELECT | INT64: shard_count; STRING: pk | — |
| [storage_gcp:txn:8](../../src/trusted_router/storage_gcp.py#L1778) | DML | ARRAY<STRING>: causes; INT64: shard_count; STRING: pk; TIMESTAMP: now | — |
| [storage_gcp:txn:9](../../src/trusted_router/storage_gcp.py#L1826) | SELECT | none | — |
| [storage_gcp:txn:10](../../src/trusted_router/storage_gcp.py#L1837) | SELECT | none | — |
| [storage_gcp:process_trust_demotion_remainders:1](../../src/trusted_router/storage_gcp.py#L1900) | SELECT | INT64: limit | — |
| [storage_gcp:txn:11](../../src/trusted_router/storage_gcp.py#L1921) | SELECT | STRING: owner, workspace | — |
| [storage_gcp:txn:12](../../src/trusted_router/storage_gcp.py#L1937) | SELECT | STRING: pk | — |
| [storage_gcp:txn:13](../../src/trusted_router/storage_gcp.py#L1952) | SELECT | INT64: shard_count; STRING: pk | — |
| [storage_gcp:_demote_owner_trust_tx:1](../../src/trusted_router/storage_gcp.py#L2268) | SELECT | STRING: pk | — |
| [storage_gcp:_demote_owner_trust_tx:2](../../src/trusted_router/storage_gcp.py#L2277) | SELECT | INT64: shard_count; STRING: pk | — |
| [storage_gcp:batch:1](../../src/trusted_router/storage_gcp.py#L2790) | SELECT | ARRAY<STRING>: ids; STRING: kind | — |
| [storage_gcp:get_byok_providers:1](../../src/trusted_router/storage_gcp.py#L2841) | SELECT | ARRAY<STRING>: ids; STRING: kind | — |
| [storage_gcp:list_stale_trust_inbox:1](../../src/trusted_router/storage_gcp.py#L3400) | SELECT | TIMESTAMP: older_than | — |
| [storage_gcp:_increment_lifetime_topup_tx:1](../../src/trusted_router/storage_gcp.py#L3506) | DML | INT64: amount; STRING: user_id; TIMESTAMP: now | — |
| [storage_gcp:_increment_lifetime_topup_tx:2](../../src/trusted_router/storage_gcp.py#L3518) | DML | INT64: amount; STRING: user_id; TIMESTAMP: now | — |
| [storage_gcp:_insert_credit_movement_tx:1](../../src/trusted_router/storage_gcp.py#L3544) | DML | INT64: amount; STRING: account_id, authorization_id, counterparty, custom_model_id, kind, movement_id; TIMESTAMP: created_at | — |
| [storage_gcp:txn:14](../../src/trusted_router/storage_gcp.py#L3651) | DML | INT64: amount; STRING: user_id; TIMESTAMP: now | — |
| [storage_gcp:txn:15](../../src/trusted_router/storage_gcp.py#L3663) | DML | INT64: amount; STRING: user_id; TIMESTAMP: now | — |
| [storage_gcp:txn:16](../../src/trusted_router/storage_gcp.py#L3726) | DML | INT64: amount; STRING: user_id; TIMESTAMP: now | — |
| [storage_gcp:txn:17](../../src/trusted_router/storage_gcp.py#L3867) | SELECT | STRING: account_id, movement_id | — |
| [storage_gcp:txn:18](../../src/trusted_router/storage_gcp.py#L3904) | SELECT | INT64: shard_count; STRING: workspace_id | — |
| [storage_gcp:_delete_credit_transfer_claim_tx:1](../../src/trusted_router/storage_gcp.py#L3981) | DML | STRING: account_id, movement_id | — |
| [storage_gcp:txn:19](../../src/trusted_router/storage_gcp.py#L4112) | DML | INT64: amount; STRING: user_id; TIMESTAMP: now | — |
| [storage_gcp:txn:20](../../src/trusted_router/storage_gcp.py#L4275) | DML | INT64: amount; STRING: user_id; TIMESTAMP: now | — |
| [storage_gcp:txn:21](../../src/trusted_router/storage_gcp.py#L4370) | DML | INT64: amount; STRING: user_id; TIMESTAMP: now | — |
| [storage_gcp:txn:22](../../src/trusted_router/storage_gcp.py#L4431) | SELECT | STRING: user_id | — |
| [storage_gcp:txn:23](../../src/trusted_router/storage_gcp.py#L4440) | DML | STRING: user_id; TIMESTAMP: now | — |
| [storage_gcp:earnings_summary:1](../../src/trusted_router/storage_gcp.py#L4465) | SELECT | STRING: user_id | — |
| [storage_gcp:list_credit_movements:1](../../src/trusted_router/storage_gcp.py#L4512) | SELECT | ARRAY<STRING>: kinds; INT64: limit; STRING: account_id; TIMESTAMP: before | FORCE_INDEX |
| [storage_gcp:custom_model_earnings_by_model:1](../../src/trusted_router/storage_gcp.py#L4547) | SELECT | STRING: account_id; TIMESTAMP: since | FORCE_INDEX |
| [storage_gcp:get_lifetime_topup_microdollars:1](../../src/trusted_router/storage_gcp.py#L4577) | SELECT | STRING: user_id | — |
| [storage_gcp:typed_key_usage:1](../../src/trusted_router/storage_gcp.py#L7073) | SELECT | INT64: shard_count; STRING: pk | — |
| [storage_gcp:typed_credit_snapshot:1](../../src/trusted_router/storage_gcp.py#L7126) | SELECT | INT64: shard_count; STRING: pk | — |
| [storage_gcp:typed_credit_trust_snapshot:1](../../src/trusted_router/storage_gcp.py#L7159) | SELECT | STRING: pk | — |
| [storage_gcp:list_trust_tier_workspace_ids:1](../../src/trusted_router/storage_gcp.py#L7177) | SELECT | none | — |
| [storage_gcp:_read_outbox_heartbeat_row:1](../../src/trusted_router/storage_gcp.py#L7528) | SELECT | STRING: id, kind | — |
| [storage_gcp:list_receipt_keys:1](../../src/trusted_router/storage_gcp.py#L8202) | SELECT | INT64: limit; STRING: after_att_sha256, after_kid, kind | FORCE_INDEX |
| [storage_gcp:list_receipt_keys:2](../../src/trusted_router/storage_gcp.py#L8213) | SELECT | INT64: limit; STRING: kind, legacy_after | — |
| [storage_gcp:list_receipt_keys:3](../../src/trusted_router/storage_gcp.py#L8223) | SELECT | INT64: limit; STRING: after_att_sha256, after_kid, kid, kind | FORCE_INDEX |
| [storage_gcp:list_receipt_keys:4](../../src/trusted_router/storage_gcp.py#L8238) | SELECT | INT64: limit; STRING: after_att_sha256, after_kid, kind | FORCE_INDEX |
| [storage_gcp:txn:24](../../src/trusted_router/storage_gcp.py#L8277) | SELECT | INT64: limit; STRING: after, kind | — |
| [storage_gcp:_read_entity_from:1](../../src/trusted_router/storage_gcp.py#L8495) | SELECT | STRING: id, kind | — |
| [storage_gcp:_list_entities:1](../../src/trusted_router/storage_gcp.py#L8547) | SELECT | INT64: limit; STRING: kind, prefix, suffix | — |
| [storage_gcp_analytics_outbox:enqueue_tx:1](../../src/trusted_router/storage_gcp_analytics_outbox.py#L66) | DML | INT64: shard; STRING: event_id, payload | PENDING_COMMIT_TIMESTAMP |
| [storage_gcp_authorize:key_lifetime_cap_precheck:1](../../src/trusted_router/storage_gcp_authorize.py#L255) | SELECT | INT64: shard_count; STRING: kh | — |
| [storage_gcp_authorize:key_window_limit_decision:1](../../src/trusted_router/storage_gcp_authorize.py#L329) | SELECT | INT64: shard_count; STRING: kh | — |
| [storage_gcp_authorize:constants:1](../../src/trusted_router/storage_gcp_authorize.py#L1167) | SELECT | INT64: limit; TIMESTAMP: now | — |
| [storage_gcp_authorize:constants:2](../../src/trusted_router/storage_gcp_authorize.py#L1172) | SELECT | INT64: limit; TIMESTAMP: now | — |
| [storage_gcp_authorize:_apply_user_model_payout_tx:1](../../src/trusted_router/storage_gcp_authorize.py#L2074) | DML | INT64: amount; STRING: user_id | PENDING_COMMIT_TIMESTAMP |
| [storage_gcp_authorize:_apply_user_model_payout_tx:2](../../src/trusted_router/storage_gcp_authorize.py#L2083) | DML | INT64: amount; STRING: user_id | PENDING_COMMIT_TIMESTAMP |
| [storage_gcp_authorize:_apply_app_markup_payout_tx:1](../../src/trusted_router/storage_gcp_authorize.py#L2131) | DML | INT64: amount; STRING: user_id | PENDING_COMMIT_TIMESTAMP |
| [storage_gcp_authorize:_apply_app_markup_payout_tx:2](../../src/trusted_router/storage_gcp_authorize.py#L2140) | DML | INT64: amount; STRING: user_id | PENDING_COMMIT_TIMESTAMP |
| [storage_gcp_authorize:_apply_custom_model_markup_payout_tx:1](../../src/trusted_router/storage_gcp_authorize.py#L2188) | DML | INT64: amount; STRING: user_id | PENDING_COMMIT_TIMESTAMP |
| [storage_gcp_authorize:_apply_custom_model_markup_payout_tx:2](../../src/trusted_router/storage_gcp_authorize.py#L2197) | DML | INT64: amount; STRING: user_id | PENDING_COMMIT_TIMESTAMP |
| [storage_gcp_counter_dml:constants:1](../../src/trusted_router/storage_gcp_counter_dml.py#L36) | DML | INT64: actual; STRING: rid, sut; TIMESTAMP: terminal_at | — |
| [storage_gcp_counter_dml:constants:2](../../src/trusted_router/storage_gcp_counter_dml.py#L41) | DML | INT64: actual; STRING: rid, sut; TIMESTAMP: terminal_at | — |
| [storage_gcp_counter_dml:constants:3](../../src/trusted_router/storage_gcp_counter_dml.py#L49) | DML | STRING: rid; TIMESTAMP: terminal_at | — |
| [storage_gcp_counter_dml:constants:4](../../src/trusted_router/storage_gcp_counter_dml.py#L53) | DML | STRING: rid; TIMESTAMP: terminal_at | — |
| [storage_gcp_counter_dml:reserve_credit:1](../../src/trusted_router/storage_gcp_counter_dml.py#L76) | DML | INT64: est, shard; STRING: ws | — |
| [storage_gcp_counter_dml:reserve_credit_for_spend_lease:1](../../src/trusted_router/storage_gcp_counter_dml.py#L114) | DML | INT64: est, expected_trust_tier, shard; STRING: ws; TIMESTAMP: trust_fresh_after, trust_now | — |
| [storage_gcp_counter_dml:debit_workspace_credit:1](../../src/trusted_router/storage_gcp_counter_dml.py#L162) | DML | INT64: amt; STRING: ws; TIMESTAMP: now | — |
| [storage_gcp_counter_dml:debit_credit_shard:1](../../src/trusted_router/storage_gcp_counter_dml.py#L206) | DML | INT64: donor, move; STRING: ws | — |
| [storage_gcp_counter_dml:credit_credit_shard:1](../../src/trusted_router/storage_gcp_counter_dml.py#L240) | DML | INT64: amount, shard; STRING: ws; TIMESTAMP: now | — |
| [storage_gcp_counter_dml:transfer_credit_budget:1](../../src/trusted_router/storage_gcp_counter_dml.py#L284) | DML | INT64: move, target; STRING: ws | — |
| [storage_gcp_counter_dml:release_credit:1](../../src/trusted_router/storage_gcp_counter_dml.py#L320) | DML | INT64: actual, hold, shard; STRING: ws | — |
| [storage_gcp_counter_dml:release_credit:2](../../src/trusted_router/storage_gcp_counter_dml.py#L357) | DML | INT64: amount, shard; STRING: ws | — |
| [storage_gcp_counter_dml:_credit_shard_count_from_rows:1](../../src/trusted_router/storage_gcp_counter_dml.py#L381) | SELECT | STRING: pk | — |
| [storage_gcp_counter_dml:reserve_key_statement:1](../../src/trusted_router/storage_gcp_counter_dml.py#L398) | DML | BOOL: is_byok; INT64: est, shard; STRING: kh | — |
| [storage_gcp_counter_dml:reserve_key:1](../../src/trusted_router/storage_gcp_counter_dml.py#L442) | SELECT | INT64: shard; STRING: kh | — |
| [storage_gcp_counter_dml:release_key:1](../../src/trusted_router/storage_gcp_counter_dml.py#L554) | DML | INT64: actual, hold, shard; STRING: kh; TIMESTAMP: day_floor, month_floor, week_floor | — |
| [storage_gcp_counter_dml:release_key:2](../../src/trusted_router/storage_gcp_counter_dml.py#L570) | DML | INT64: actual, hold, shard; STRING: kh; TIMESTAMP: day_floor, month_floor, week_floor | — |
| [storage_gcp_counter_dml:key_limit_exists:1](../../src/trusted_router/storage_gcp_counter_dml.py#L592) | SELECT | INT64: shard; STRING: kh | — |
| [storage_gcp_counter_dml:read_reservation_by_idempotency:1](../../src/trusted_router/storage_gcp_counter_dml.py#L625) | SELECT | STRING: scope | — |
| [storage_gcp_counter_dml:read_reservation:1](../../src/trusted_router/storage_gcp_counter_dml.py#L654) | SELECT | STRING: rid | — |
| [storage_gcp_counter_dml:reservation_insert_statement:1](../../src/trusted_router/storage_gcp_counter_dml.py#L702) | DML | INT64: credit_reserved_micro, credit_shard, key_reserved_micro, key_shard, ws_shard; STRING: authorization_id, hold_usage_type, idempotency_fingerprint, idempotency_scope, key_hash, reservation_id, workspace_id; TIMESTAMP: created_at, expires_at | — |
| [storage_gcp_counter_dml:reservation_retention_clear_statement:1](../../src/trusted_router/storage_gcp_counter_dml.py#L801) | DML | STRING: rid | — |
| [storage_gcp_counter_dml:entity_insert_statement:1](../../src/trusted_router/storage_gcp_counter_dml.py#L821) | DML | STRING: body, id, kind | PENDING_COMMIT_TIMESTAMP |
| [storage_gcp_counter_dml:insert_entity_dml_at:1](../../src/trusted_router/storage_gcp_counter_dml.py#L840) | DML | STRING: body, id, kind; TIMESTAMP: now | — |
| [storage_gcp_counter_dml:delete_entity_dml:1](../../src/trusted_router/storage_gcp_counter_dml.py#L867) | DML | STRING: id, kind | — |
| [storage_gcp_counter_dml:update_entity_body_dml:1](../../src/trusted_router/storage_gcp_counter_dml.py#L879) | DML | STRING: body, id, kind; TIMESTAMP: now | — |
| [storage_gcp_counter_reconcile:constants:1](../../src/trusted_router/storage_gcp_counter_reconcile.py#L28) | SELECT | none | — |
| [storage_gcp_counter_reconcile:constants:2](../../src/trusted_router/storage_gcp_counter_reconcile.py#L34) | SELECT | none | — |
| [storage_gcp_counter_reconcile:constants:3](../../src/trusted_router/storage_gcp_counter_reconcile.py#L75) | SELECT | STRING: ws | — |
| [storage_gcp_counter_reconcile:constants:4](../../src/trusted_router/storage_gcp_counter_reconcile.py#L101) | SELECT | none | — |
| [storage_gcp_counter_reconcile:constants:5](../../src/trusted_router/storage_gcp_counter_reconcile.py#L127) | SELECT | none | — |
| [storage_gcp_counter_reconcile:audit_typed_invariants:1](../../src/trusted_router/storage_gcp_counter_reconcile.py#L312) | SELECT | none | — |
| [storage_gcp_counter_reconcile:audit_typed_invariants:2](../../src/trusted_router/storage_gcp_counter_reconcile.py#L317) | SELECT | none | — |
| [storage_gcp_counter_reconcile:repair_typed_reserved:1](../../src/trusted_router/storage_gcp_counter_reconcile.py#L596) | SELECT | STRING: ws | — |
| [storage_gcp_counter_reconcile:repair_typed_reserved:2](../../src/trusted_router/storage_gcp_counter_reconcile.py#L600) | SELECT | STRING: kh | — |
| [storage_gcp_counter_reconcile:repair_typed_reserved:3](../../src/trusted_router/storage_gcp_counter_reconcile.py#L603) | SELECT | STRING: pk | — |
| [storage_gcp_counter_reconcile:repair_typed_reserved:4](../../src/trusted_router/storage_gcp_counter_reconcile.py#L604) | SELECT | STRING: pk | — |
| [storage_gcp_counter_reconcile:repair_typed_reserved:5](../../src/trusted_router/storage_gcp_counter_reconcile.py#L606) | SELECT | STRING: kh | — |
| [storage_gcp_counter_reconcile:repair_typed_reserved:6](../../src/trusted_router/storage_gcp_counter_reconcile.py#L610) | SELECT | STRING: ws | — |
| [storage_gcp_counter_reconcile:repair_typed_usage:1](../../src/trusted_router/storage_gcp_counter_reconcile.py#L780) | SELECT | STRING: pk | — |
| [storage_gcp_counter_reconcile:repair_typed_usage:2](../../src/trusted_router/storage_gcp_counter_reconcile.py#L783) | SELECT | STRING: ws | — |
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
| [storage_gcp_federated_settlement:_read_entity_body:1](../../src/trusted_router/storage_gcp_federated_settlement.py#L55) | SELECT | STRING: id, kind | — |
| [storage_gcp_federated_settlement:_book_usage:1](../../src/trusted_router/storage_gcp_federated_settlement.py#L85) | DML | INT64: amt; STRING: ws | — |
| [storage_gcp_federated_settlement:_book_usage:2](../../src/trusted_router/storage_gcp_federated_settlement.py#L98) | DML | INT64: amt; STRING: ws; TIMESTAMP: now | — |
| [storage_gcp_generation_records:generation_insert_statement:1](../../src/trusted_router/storage_gcp_generation_records.py#L44) | DML | STRING: generation_id, key_hash, payload, workspace_id; TIMESTAMP: created_at, terminal_at | — |
| [storage_gcp_generation_records:upsert_generation_record:1](../../src/trusted_router/storage_gcp_generation_records.py#L77) | DML | STRING: generation_id, key_hash, payload, workspace_id; TIMESTAMP: created_at, terminal_at | — |
| [storage_gcp_generation_records:read_generation_record:1](../../src/trusted_router/storage_gcp_generation_records.py#L108) | SELECT | STRING: generation_id | — |
| [storage_gcp_generations:_reconcile_page:1](../../src/trusted_router/storage_gcp_generations.py#L694) | SELECT | INT64: limit; STRING: after_id, kind, prefix | — |
| [storage_gcp_google_ads:_read_entity_from:1](../../src/trusted_router/storage_gcp_google_ads.py#L139) | SELECT | STRING: id, kind | — |
| [storage_gcp_google_ads:_list_entities:1](../../src/trusted_router/storage_gcp_google_ads.py#L176) | SELECT | INT64: limit; STRING: kind, prefix, suffix | — |
| [storage_gcp_key_escrow:txn:1](../../src/trusted_router/storage_gcp_key_escrow.py#L43) | SELECT | INT64: shard_count; STRING: kh | — |
| [storage_gcp_key_shard_admin:_typed_key_state:1](../../src/trusted_router/storage_gcp_key_shard_admin.py#L91) | SELECT | INT64: shard_count; STRING: pk | — |
| [storage_gcp_key_shard_admin:_typed_key_state:2](../../src/trusted_router/storage_gcp_key_shard_admin.py#L102) | SELECT | STRING: kh | — |
| [storage_gcp_key_shard_admin:txn:1](../../src/trusted_router/storage_gcp_key_shard_admin.py#L245) | SELECT | INT64: shard_count; STRING: pk | — |
| [storage_gcp_key_shard_admin:txn:2](../../src/trusted_router/storage_gcp_key_shard_admin.py#L271) | SELECT | STRING: kh | — |
| [storage_gcp_keys:txn:1](../../src/trusted_router/storage_gcp_keys.py#L266) | SELECT | INT64: shard_count; STRING: kh | — |
| [storage_gcp_operational_analytics_outbox:oldest_enqueued_at:1](../../src/trusted_router/storage_gcp_operational_analytics_outbox.py#L188) | SELECT | TIMESTAMP: floor_0 | — |
| [storage_gcp_operational_analytics_outbox:oldest_enqueued_at:2](../../src/trusted_router/storage_gcp_operational_analytics_outbox.py#L197) | SELECT | TIMESTAMP: floor_0 | — |
| [storage_gcp_operational_analytics_outbox:_insert_statement:1](../../src/trusted_router/storage_gcp_operational_analytics_outbox.py#L254) | DML | INT64: shard; STRING: event_id, event_kind, payload | PENDING_COMMIT_TIMESTAMP |
| [storage_gcp_regional_quota:txn:1](../../src/trusted_router/storage_gcp_regional_quota.py#L343) | SELECT | STRING: ws | — |
| [storage_gcp_regional_quota:regional_quota_fences:1](../../src/trusted_router/storage_gcp_regional_quota.py#L540) | SELECT | ARRAY<STRING>: ids; STRING: kind | — |
| [storage_gcp_regional_quota:_check_regional_key_windows:1](../../src/trusted_router/storage_gcp_regional_quota.py#L612) | SELECT | INT64: shard; STRING: kh | — |
| [storage_gcp_regional_quota:_credit_rows:1](../../src/trusted_router/storage_gcp_regional_quota.py#L1080) | SELECT | STRING: ws | — |
| [storage_gcp_regional_quota:_insert_entity_dml:1](../../src/trusted_router/storage_gcp_regional_quota.py#L1176) | DML | STRING: body, id, kind | PENDING_COMMIT_TIMESTAMP |
| [storage_gcp_regional_quota:_upsert_entity_dml:1](../../src/trusted_router/storage_gcp_regional_quota.py#L1201) | DML | STRING: body, id, kind | PENDING_COMMIT_TIMESTAMP |
| [storage_gcp_regional_quota:_upsert_entity_dml:2](../../src/trusted_router/storage_gcp_regional_quota.py#L1208) | DML | STRING: body, id, kind | PENDING_COMMIT_TIMESTAMP |
| [storage_gcp_regional_quota:_owned_regional_leases:1](../../src/trusted_router/storage_gcp_regional_quota.py#L1262) | SELECT | STRING: kind, prefix | — |
| [storage_gcp_regional_quota:_owned_regional_leases:2](../../src/trusted_router/storage_gcp_regional_quota.py#L1273) | SELECT | STRING: kind, prefix | — |
| [storage_gcp_regional_quota:_owned_regional_leases:3](../../src/trusted_router/storage_gcp_regional_quota.py#L1287) | SELECT | ARRAY<STRING>: ids; STRING: kind | — |
| [storage_gcp_regional_quota:txn:2](../../src/trusted_router/storage_gcp_regional_quota.py#L1399) | SELECT | STRING: aid | FORCE_INDEX |
| [storage_gcp_request_records:constants:1](../../src/trusted_router/storage_gcp_request_records.py#L46) | DML | STRING: authorization_id; TIMESTAMP: terminal_at | — |
| [storage_gcp_request_records:constants:2](../../src/trusted_router/storage_gcp_request_records.py#L51) | DML | STRING: authorization_id; TIMESTAMP: terminal_at | — |
| [storage_gcp_request_records:constants:3](../../src/trusted_router/storage_gcp_request_records.py#L60) | DML | INT64: estimated_microdollars, finalized_cost_microdollars, heartbeat_seq, spend_lease_allocated_micro, spend_lease_gen; STRING: authorization_id, delivered_usage, finalization_outcome, gateway_request_id, heartbeat_hash, idempotency_fingerprint, invocation_nonce, key_hash, model_id, payload, pricing_snapshot, provider, reservation_id, selected_endpoint_id, spend_lease_id, spend_lease_status, spend_lease_token, stage_d_boot_kid, usage_type, workspace_id; TIMESTAMP: created_at, heartbeat_at, spend_lease_exp, started_at | — |
| [storage_gcp_request_records:constants:4](../../src/trusted_router/storage_gcp_request_records.py#L82) | DML | INT64: estimated_microdollars, finalized_cost_microdollars, heartbeat_seq, spend_lease_allocated_micro, spend_lease_gen; STRING: authorization_id, delivered_usage, finalization_outcome, gateway_request_id, heartbeat_hash, idempotency_fingerprint, invocation_nonce, key_hash, model_id, payload, pricing_snapshot, provider, reservation_id, selected_endpoint_id, spend_lease_admission_receipt, spend_lease_id, spend_lease_receipt_hash, spend_lease_status, spend_lease_token, stage_d_boot_kid, usage_type, workspace_id; TIMESTAMP: created_at, heartbeat_at, spend_lease_exp, started_at | — |
| [storage_gcp_request_records:read_gateway_authorization_admission_columns:1](../../src/trusted_router/storage_gcp_request_records.py#L170) | SELECT | STRING: authorization_id | — |
| [storage_gcp_request_records:read_gateway_authorization:1](../../src/trusted_router/storage_gcp_request_records.py#L188) | SELECT | STRING: authorization_id | — |
| [storage_gcp_request_records:gateway_authorization_settled_statement:1](../../src/trusted_router/storage_gcp_request_records.py#L345) | DML | INT64: finalized_cost_microdollars; STRING: authorization_id, finalization_outcome, gateway_request_id, payload | PARSE_JSON, JSON_QUERY, JSON_TYPE, TO_JSON |
| [storage_gcp_request_records:read_gateway_authorization_by_gateway_request_id:1](../../src/trusted_router/storage_gcp_request_records.py#L376) | SELECT | STRING: gateway_request_id | FORCE_INDEX |
| [storage_gcp_request_records:gateway_authorization_retention_clear_statement:1](../../src/trusted_router/storage_gcp_request_records.py#L431) | DML | STRING: authorization_id | — |
| [storage_gcp_settle_outbox:constants:1](../../src/trusted_router/storage_gcp_settle_outbox.py#L88) | SELECT | STRING: aid | — |
| [storage_gcp_settle_outbox:done_retention_statements:1](../../src/trusted_router/storage_gcp_settle_outbox.py#L158) | DML | STRING: aid, kind, record_id; TIMESTAMP: now | — |
| [storage_gcp_settle_outbox:constants:2](../../src/trusted_router/storage_gcp_settle_outbox.py#L177) | DML | STRING: aid, kind, lease_owner, status; TIMESTAMP: now | THEN RETURN |
| [storage_gcp_settle_outbox:constants:3](../../src/trusted_router/storage_gcp_settle_outbox.py#L186) | SELECT | STRING: aid, kind | — |
| [storage_gcp_settle_outbox:rewrite_frozen_settlement_tx:1](../../src/trusted_router/storage_gcp_settle_outbox.py#L293) | DML | INT64: actual_cost_micro; STRING: aid, kind, lease_owner, settle_body; TIMESTAMP: now | — |
| [storage_gcp_settle_outbox:insert_txn:1](../../src/trusted_router/storage_gcp_settle_outbox.py#L472) | DML | INT64: actual_cost_micro, attempts, auto_refill_attempts; STRING: authorization_id, auto_refill_last_error, auto_refill_lease_owner, auto_refill_status, auto_refill_workspace_id, intent_kind, last_error, lease_owner, model_id, reservation_id, selected_endpoint_id, selected_usage_type, settle_body, settle_origin, status; TIMESTAMP: auto_refill_enqueued_at, auto_refill_leased_until, auto_refill_next_attempt_at, auto_refill_terminal_at, auto_refill_updated_at, created_at, leased_until, next_attempt_at, terminal_at, updated_at | — |
| [storage_gcp_settle_outbox:refresh_txn:1](../../src/trusted_router/storage_gcp_settle_outbox.py#L505) | DML | INT64: actual_cost_micro; STRING: authorization_id, auto_refill_workspace_id, intent_kind, model_id, reservation_id, selected_endpoint_id, selected_usage_type, settle_body, settle_origin; TIMESTAMP: now | — |
| [storage_gcp_settle_outbox:due:1](../../src/trusted_router/storage_gcp_settle_outbox.py#L574) | SELECT | INT64: limit; TIMESTAMP: now | FORCE_INDEX |
| [storage_gcp_settle_outbox:txn:1](../../src/trusted_router/storage_gcp_settle_outbox.py#L605) | DML | STRING: aid, kind, owner; TIMESTAMP: lease, now | — |
| [storage_gcp_settle_outbox:txn:2](../../src/trusted_router/storage_gcp_settle_outbox.py#L659) | SELECT | STRING: aid, kind | — |
| [storage_gcp_settle_outbox:txn:3](../../src/trusted_router/storage_gcp_settle_outbox.py#L697) | DML | BOOL: done; INT64: attempts; STRING: aid, err, kind, lease_owner, status; TIMESTAMP: next_at, now, terminal_at | — |
| [storage_gcp_settle_outbox:txn:4](../../src/trusted_router/storage_gcp_settle_outbox.py#L772) | SELECT | STRING: aid, kind | — |
| [storage_gcp_settle_outbox:txn:5](../../src/trusted_router/storage_gcp_settle_outbox.py#L790) | DML | INT64: attempts; STRING: aid, err, kind, lease_owner; TIMESTAMP: next_at, now | — |
| [storage_gcp_settle_outbox:txn:6](../../src/trusted_router/storage_gcp_settle_outbox.py#L846) | DML | STRING: aid, workspace_id; TIMESTAMP: next_at, now | — |
| [storage_gcp_settle_outbox:due_auto_refills:1](../../src/trusted_router/storage_gcp_settle_outbox.py#L877) | SELECT | INT64: limit; TIMESTAMP: now | FORCE_INDEX |
| [storage_gcp_settle_outbox:txn:7](../../src/trusted_router/storage_gcp_settle_outbox.py#L924) | DML | STRING: aid, owner; TIMESTAMP: lease, now | — |
| [storage_gcp_settle_outbox:txn:8](../../src/trusted_router/storage_gcp_settle_outbox.py#L962) | SELECT | STRING: aid | — |
| [storage_gcp_settle_outbox:txn:9](../../src/trusted_router/storage_gcp_settle_outbox.py#L986) | DML | INT64: attempts; STRING: aid, error, lease_owner, status; TIMESTAMP: next_at, now, terminal_at | — |
| [storage_gcp_settle_outbox:get_auto_refill:1](../../src/trusted_router/storage_gcp_settle_outbox.py#L1022) | SELECT | STRING: aid | — |
| [storage_gcp_settle_outbox:auto_refill_pending_freshness:1](../../src/trusted_router/storage_gcp_settle_outbox.py#L1036) | SELECT | none | FORCE_INDEX |
| [storage_gcp_settle_outbox:get:1](../../src/trusted_router/storage_gcp_settle_outbox.py#L1068) | SELECT | STRING: aid, kind | — |
| [storage_gcp_spend_lease:read_registration:1](../../src/trusted_router/storage_gcp_spend_lease.py#L270) | SELECT | STRING: scope, scope_salt | — |
| [storage_gcp_spend_lease:delete_bound:1](../../src/trusted_router/storage_gcp_spend_lease.py#L318) | DML | STRING: authorization_id, scope, scope_salt | — |
| [storage_gcp_spend_lease:arm_bound_retention:1](../../src/trusted_router/storage_gcp_spend_lease.py#L344) | DML | STRING: authorization_id; TIMESTAMP: terminal_at | FORCE_INDEX |
| [storage_gcp_spend_lease:upgrade_candidate_to_open:1](../../src/trusted_router/storage_gcp_spend_lease.py#L424) | DML | INT64: skew_seconds; STRING: creator, lease_id; TIMESTAMP: expires_at | — |
| [storage_gcp_spend_lease:take_recovery_ownership:1](../../src/trusted_router/storage_gcp_spend_lease.py#L458) | DML | STRING: lease_id | PENDING_COMMIT_TIMESTAMP |
| [storage_gcp_spend_lease:complete_candidate:1](../../src/trusted_router/storage_gcp_spend_lease.py#L476) | DML | STRING: lease_id | — |
| [storage_gcp_spend_lease:read_open_row:1](../../src/trusted_router/storage_gcp_spend_lease.py#L492) | SELECT | STRING: lease_id | — |
| [storage_gcp_spend_lease:defer_open_row:1](../../src/trusted_router/storage_gcp_spend_lease.py#L531) | DML | BOOL: dead; INT64: attempts, expected_attempts; STRING: error, lease_id, phase; TIMESTAMP: next_attempt_at | — |
| [storage_gcp_spend_lease:set_close_eligible_once:1](../../src/trusted_router/storage_gcp_spend_lease.py#L567) | DML | STRING: lease_id; TIMESTAMP: observed_at | — |
| [storage_gcp_spend_lease:mark_global_closed:1](../../src/trusted_router/storage_gcp_spend_lease.py#L587) | DML | STRING: lease_id; TIMESTAMP: closed_at | — |
| [storage_gcp_spend_lease:mark_local_closed:1](../../src/trusted_router/storage_gcp_spend_lease.py#L608) | DML | STRING: lease_id; TIMESTAMP: closed_at | — |
| [storage_gcp_spend_lease:delete_open_row:1](../../src/trusted_router/storage_gcp_spend_lease.py#L631) | DML | STRING: lease_id, phase | — |
| [storage_gcp_spend_lease:requeue_dead:1](../../src/trusted_router/storage_gcp_spend_lease.py#L647) | DML | STRING: lease_id; TIMESTAMP: next_attempt_at | — |
| [storage_gcp_spend_lease:dead_rows:1](../../src/trusted_router/storage_gcp_spend_lease.py#L662) | SELECT | INT64: limit | — |
| [storage_gcp_spend_lease:retained_done_candidates:1](../../src/trusted_router/storage_gcp_spend_lease.py#L679) | SELECT | INT64: limit; TIMESTAMP: cutoff | — |
| [storage_gcp_spend_lease:lag_inputs:1](../../src/trusted_router/storage_gcp_spend_lease.py#L695) | SELECT | TIMESTAMP: now | — |
| [storage_gcp_spend_lease:_due_rows:1](../../src/trusted_router/storage_gcp_spend_lease.py#L823) | SELECT | INT64: limit | FORCE_INDEX |
| [storage_gcp_spend_lease_authorize:_read_fence:1](../../src/trusted_router/storage_gcp_spend_lease_authorize.py#L512) | SELECT | STRING: id, kind | — |
| [storage_gcp_spend_lease_authorize:_read_fence:2](../../src/trusted_router/storage_gcp_spend_lease_authorize.py#L525) | SELECT | STRING: id, kind | — |
| [storage_gcp_spend_lease_authorize:_mark_incumbent:1](../../src/trusted_router/storage_gcp_spend_lease_authorize.py#L552) | DML | STRING: id, kind | JSON_SET, PARSE_JSON, JSON_VALUE, TO_JSON |
| [storage_gcp_spend_lease_authorize:_unmark_incumbent:1](../../src/trusted_router/storage_gcp_spend_lease_authorize.py#L572) | DML | STRING: id, kind | JSON_SET, PARSE_JSON, JSON_VALUE, TO_JSON |
| [storage_gcp_spend_lease_authorize:_unmark_incumbent:2](../../src/trusted_router/storage_gcp_spend_lease_authorize.py#L580) | DML | STRING: id, kind | JSON_SET, PARSE_JSON, JSON_VALUE, TO_JSON |
| [storage_gcp_spend_lease_authorize:_mark_closing:1](../../src/trusted_router/storage_gcp_spend_lease_authorize.py#L602) | DML | STRING: closing_at_text, id, kind | JSON_SET, PARSE_JSON, JSON_VALUE, TO_JSON |
| [storage_gcp_spend_lease_authorize:_decrement_fence:1](../../src/trusted_router/storage_gcp_spend_lease_authorize.py#L630) | DML | STRING: id, kind | JSON_SET, PARSE_JSON, JSON_VALUE, TO_JSON |
| [storage_gcp_spend_lease_authorize:_close_global_lease:1](../../src/trusted_router/storage_gcp_spend_lease_authorize.py#L651) | DML | STRING: body, id, kind | JSON_VALUE |
| [storage_gcp_spend_lease_authorize:_advance_fence:1](../../src/trusted_router/storage_gcp_spend_lease_authorize.py#L682) | DML | BOOL: authoritative_exhaustion, window_closed; INT64: limit, mark_count, observed_gen; STRING: body, id, kind; TIMESTAMP: candidate_expires_at | JSON_VALUE |
| [storage_gcp_spend_lease_reconcile:txn:1](../../src/trusted_router/storage_gcp_spend_lease_reconcile.py#L669) | SELECT | STRING: id, kind | — |
| [storage_gcp_spend_lease_reconcile:txn:2](../../src/trusted_router/storage_gcp_spend_lease_reconcile.py#L686) | DML | STRING: id, kind, state | JSON_SET, PARSE_JSON, JSON_VALUE, TO_JSON |
| [storage_gcp_spend_lease_reconcile:_read_global_body:1](../../src/trusted_router/storage_gcp_spend_lease_reconcile.py#L859) | SELECT | STRING: id, kind | — |
| [storage_gcp_spend_lease_reconcile:_upsert_lock:1](../../src/trusted_router/storage_gcp_spend_lease_reconcile.py#L925) | DML | STRING: body, id, kind | PENDING_COMMIT_TIMESTAMP |
| [storage_gcp_spend_lease_reconcile:_upsert_lock:2](../../src/trusted_router/storage_gcp_spend_lease_reconcile.py#L932) | DML | STRING: body, id, kind | PENDING_COMMIT_TIMESTAMP |
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
| [storage_gcp_user_models:txn:1](../../src/trusted_router/storage_gcp_user_models.py#L360) | SELECT | STRING: kind, prefix | — |

## Runtime builder and batch cases

Each calls production builders or captures emitted SQL without interpreting it. The completeness guard profiles builder calls so a registered builder cannot silently lack an exercised output. The 64 heartbeat cases seed real authorization rows and require an affected-row count of one. The guarded done-returning case also requires one returned row.

| Case | Transport | Statements | Parameter types |
|---|---|---:|---|
| authorization | DML | 1 | INT64, STRING, TIMESTAMP |
| admission | DML | 1 | INT64, STRING, TIMESTAMP |
| reservation | DML | 1 | INT64, STRING, TIMESTAMP |
| entity | DML | 1 | STRING |
| reserve-key | DML | 1 | BOOL, INT64, STRING |
| generation | DML | 1 | STRING, TIMESTAMP |
| activity | DML | 1 | INT64, STRING |
| authorize-batch | Batch DML | 3 | BOOL, INT64, STRING, TIMESTAMP |
| authorize-legacy-batch | Batch DML | 3 | BOOL, INT64, STRING, TIMESTAMP |
| authorize-sequential-batch | Batch DML | 2 | INT64, STRING, TIMESTAMP |
| authorize-sequential-legacy-batch | Batch DML | 2 | INT64, STRING, TIMESTAMP |
| settle-metadata-batch | Batch DML | 3 | INT64, STRING, TIMESTAMP |
| settled-heartbeat-000000 | DML | 1 | INT64, STRING |
| settled-heartbeat-000001 | DML | 1 | INT64, STRING |
| settled-heartbeat-000010 | DML | 1 | INT64, STRING |
| settled-heartbeat-000011 | DML | 1 | INT64, STRING |
| settled-heartbeat-000100 | DML | 1 | INT64, STRING |
| settled-heartbeat-000101 | DML | 1 | INT64, STRING |
| settled-heartbeat-000110 | DML | 1 | INT64, STRING |
| settled-heartbeat-000111 | DML | 1 | INT64, STRING |
| settled-heartbeat-001000 | DML | 1 | INT64, STRING |
| settled-heartbeat-001001 | DML | 1 | INT64, STRING |
| settled-heartbeat-001010 | DML | 1 | INT64, STRING |
| settled-heartbeat-001011 | DML | 1 | INT64, STRING |
| settled-heartbeat-001100 | DML | 1 | INT64, STRING |
| settled-heartbeat-001101 | DML | 1 | INT64, STRING |
| settled-heartbeat-001110 | DML | 1 | INT64, STRING |
| settled-heartbeat-001111 | DML | 1 | INT64, STRING |
| settled-heartbeat-010000 | DML | 1 | INT64, STRING |
| settled-heartbeat-010001 | DML | 1 | INT64, STRING |
| settled-heartbeat-010010 | DML | 1 | INT64, STRING |
| settled-heartbeat-010011 | DML | 1 | INT64, STRING |
| settled-heartbeat-010100 | DML | 1 | INT64, STRING |
| settled-heartbeat-010101 | DML | 1 | INT64, STRING |
| settled-heartbeat-010110 | DML | 1 | INT64, STRING |
| settled-heartbeat-010111 | DML | 1 | INT64, STRING |
| settled-heartbeat-011000 | DML | 1 | INT64, STRING |
| settled-heartbeat-011001 | DML | 1 | INT64, STRING |
| settled-heartbeat-011010 | DML | 1 | INT64, STRING |
| settled-heartbeat-011011 | DML | 1 | INT64, STRING |
| settled-heartbeat-011100 | DML | 1 | INT64, STRING |
| settled-heartbeat-011101 | DML | 1 | INT64, STRING |
| settled-heartbeat-011110 | DML | 1 | INT64, STRING |
| settled-heartbeat-011111 | DML | 1 | INT64, STRING |
| settled-heartbeat-100000 | DML | 1 | INT64, STRING |
| settled-heartbeat-100001 | DML | 1 | INT64, STRING |
| settled-heartbeat-100010 | DML | 1 | INT64, STRING |
| settled-heartbeat-100011 | DML | 1 | INT64, STRING |
| settled-heartbeat-100100 | DML | 1 | INT64, STRING |
| settled-heartbeat-100101 | DML | 1 | INT64, STRING |
| settled-heartbeat-100110 | DML | 1 | INT64, STRING |
| settled-heartbeat-100111 | DML | 1 | INT64, STRING |
| settled-heartbeat-101000 | DML | 1 | INT64, STRING |
| settled-heartbeat-101001 | DML | 1 | INT64, STRING |
| settled-heartbeat-101010 | DML | 1 | INT64, STRING |
| settled-heartbeat-101011 | DML | 1 | INT64, STRING |
| settled-heartbeat-101100 | DML | 1 | INT64, STRING |
| settled-heartbeat-101101 | DML | 1 | INT64, STRING |
| settled-heartbeat-101110 | DML | 1 | INT64, STRING |
| settled-heartbeat-101111 | DML | 1 | INT64, STRING |
| settled-heartbeat-110000 | DML | 1 | INT64, STRING |
| settled-heartbeat-110001 | DML | 1 | INT64, STRING |
| settled-heartbeat-110010 | DML | 1 | INT64, STRING |
| settled-heartbeat-110011 | DML | 1 | INT64, STRING |
| settled-heartbeat-110100 | DML | 1 | INT64, STRING |
| settled-heartbeat-110101 | DML | 1 | INT64, STRING |
| settled-heartbeat-110110 | DML | 1 | INT64, STRING |
| settled-heartbeat-110111 | DML | 1 | INT64, STRING |
| settled-heartbeat-111000 | DML | 1 | INT64, STRING |
| settled-heartbeat-111001 | DML | 1 | INT64, STRING |
| settled-heartbeat-111010 | DML | 1 | INT64, STRING |
| settled-heartbeat-111011 | DML | 1 | INT64, STRING |
| settled-heartbeat-111100 | DML | 1 | INT64, STRING |
| settled-heartbeat-111101 | DML | 1 | INT64, STRING |
| settled-heartbeat-111110 | DML | 1 | INT64, STRING |
| settled-heartbeat-111111 | DML | 1 | INT64, STRING |
| claim-False-False-False | DML | 1 | INT64, STRING, TIMESTAMP |
| claim-False-False-True | DML | 1 | INT64, STRING, TIMESTAMP |
| claim-False-True-False | DML | 1 | INT64, STRING, TIMESTAMP |
| claim-False-True-True | DML | 1 | INT64, STRING, TIMESTAMP |
| claim-True-False-False | DML | 1 | INT64, STRING, TIMESTAMP |
| claim-True-False-True | DML | 1 | INT64, STRING, TIMESTAMP |
| claim-True-True-False | DML | 1 | INT64, STRING, TIMESTAMP |
| claim-True-True-True | DML | 1 | INT64, STRING, TIMESTAMP |
| enqueue-retention-clear-batch | Batch DML | 2 | STRING |
| done-retention-None | Batch DML | 1 | STRING, TIMESTAMP |
| done-retention-acceptance-reservation | Batch DML | 2 | STRING, TIMESTAMP |
| speculative-done-batch | Batch DML | 3 | STRING, TIMESTAMP |
| release-key-False-False-0 | DML | 1 | INT64, STRING, TIMESTAMP |
| release-key-False-False-1 | DML | 1 | INT64, STRING, TIMESTAMP |
| release-key-False-True-0 | DML | 1 | INT64, STRING, TIMESTAMP |
| release-key-False-True-1 | DML | 1 | INT64, STRING, TIMESTAMP |
| release-key-True-False-0 | DML | 1 | INT64, STRING, TIMESTAMP |
| release-key-True-False-1 | DML | 1 | INT64, STRING, TIMESTAMP |
| release-key-True-True-0 | DML | 1 | INT64, STRING, TIMESTAMP |
| release-key-True-True-1 | DML | 1 | INT64, STRING, TIMESTAMP |
| strict-key-False-0 | DML | 1 | BOOL, INT64, STRING |
| strict-key-False-1 | SELECT | 1 | STRING |
| strict-key-True-0 | DML | 1 | BOOL, INT64, STRING, TIMESTAMP |
| strict-key-True-1 | SELECT | 1 | STRING |
| settle-batch-False-False-False-False | Batch DML | 1 | INT64, STRING |
| settle-batch-False-False-False-True | Batch DML | 2 | INT64, STRING |
| settle-batch-False-False-True-False | Batch DML | 2 | INT64, STRING, TIMESTAMP |
| settle-batch-False-False-True-True | Batch DML | 3 | INT64, STRING, TIMESTAMP |
| settle-batch-False-True-False-False | Batch DML | 4 | INT64, STRING, TIMESTAMP |
| settle-batch-False-True-False-True | Batch DML | 5 | INT64, STRING, TIMESTAMP |
| settle-batch-False-True-True-False | Batch DML | 5 | INT64, STRING, TIMESTAMP |
| settle-batch-False-True-True-True | Batch DML | 6 | INT64, STRING, TIMESTAMP |
| settle-batch-True-False-False-False | Batch DML | 2 | INT64, STRING, TIMESTAMP |
| settle-batch-True-False-False-True | Batch DML | 3 | INT64, STRING, TIMESTAMP |
| settle-batch-True-False-True-False | Batch DML | 3 | INT64, STRING, TIMESTAMP |
| settle-batch-True-False-True-True | Batch DML | 4 | INT64, STRING, TIMESTAMP |
| settle-batch-True-True-False-False | Batch DML | 5 | INT64, STRING, TIMESTAMP |
| settle-batch-True-True-False-True | Batch DML | 6 | INT64, STRING, TIMESTAMP |
| settle-batch-True-True-True-False | Batch DML | 6 | INT64, STRING, TIMESTAMP |
| settle-batch-True-True-True-True | Batch DML | 7 | INT64, STRING, TIMESTAMP |
| outbox-enqueue-batch-False-False | Batch DML | 2 | INT64, STRING, TIMESTAMP |
| outbox-enqueue-batch-False-True | Batch DML | 2 | INT64, STRING, TIMESTAMP |
| outbox-enqueue-batch-True-False | Batch DML | 3 | INT64, STRING, TIMESTAMP |
| guarded-done-returning | DML | 1 | STRING, TIMESTAMP |
| outbox-enqueue-batch-True-True | Batch DML | 3 | INT64, STRING, TIMESTAMP |
| oldest-full-shards-0 | SELECT | 1 |  |
| oldest-full-shards-1 | SELECT | 1 | TIMESTAMP |
| oldest-full-shards-32 | SELECT | 1 | TIMESTAMP |
