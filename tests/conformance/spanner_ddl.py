"""Fresh-install production GoogleSQL, generated from deployment scripts.

Regenerate: python -m tests.conformance.spanner_schema_source
Do not remove emulator-incompatible DDL; provisioning must report it.
"""

SOURCE_DIGESTS = {'scripts/deploy/infra.sh': '259931cd73d94d0f3fc535b5f8a6ef523ab7d21dd0ad4efbe679d2f5c5e92ef4',
 'scripts/deploy/migrate_analytics_outbox.sh': 'ae244118bf4d3266e286e663f99e4907289be8e24dea76d05f4bc0814fd4167a',
 'scripts/deploy/migrate_entity_ttl.sh': 'ad4f59b3608ff39a158244b71405ed427b5e47542665afb85370afd2cbebaa9f',
 'scripts/deploy/migrate_gateway_request_index.sh': '5b9a4b18007649f3108214ab2274d216b989909a5098cf38944cad7c7ad480f2',
 'scripts/deploy/migrate_generation_records.sh': 'de31377ce0ddc13926509564bf93426edb3f5fe897072ef9886db160864c0951',
 'scripts/deploy/migrate_money_primitives.sh': 'a35e4012706fa88f48be8f8d6b5abc7a3f828d36bceea59191784b58007d7e41',
 'scripts/deploy/migrate_operational_analytics_outbox.sh': 'ff4a7d8cdcfcf616065bec75a2da5927534ebde9d57e63c47c636227d78e4970',
 'scripts/deploy/migrate_receipt_key_versions.sh': '37c46050906d7f5fe50bc45e732ec1ba99059b916efc659aef1bb6882d437ef6',
 'scripts/deploy/migrate_request_retention.sh': '6817965c0ae1c836553aa4e94124d13e6d3cd98bf891fa11948ed5a0d156f510',
 'scripts/deploy/migrate_spend_lease.sh': '198790ab42b43f20306431e386db98188a0c0ad88f948afbc7232f072a7c2798',
 'scripts/deploy/migrate_trust_event_debt_index.sh': '22d7d8a24dae972dc9dd36662b5bec24483cf81578703b240f63248ff20fb864',
 'scripts/deploy/migrate_trust_reconciliation.sh': '7864237a2a0a187f5db14da4812e96481f105b28c4737bc2cfc83b481321140c',
 'scripts/deploy/migrate_typed_counters.sh': 'd1f3dc7eaa4fc383bcea7ce75846b92dd71532296d4773c3aa9cc818c24fc909',
 'scripts/deploy/retire_settle_outbox_hot_index.sh': 'ce6bac93d3c5442eccfe88aa74eeff151033905161b2d6c87489b4ec9c08cd45'}

DDL = ('CREATE TABLE tr_entities (kind STRING(64) NOT NULL, id STRING(512) NOT NULL, body '
 'STRING(MAX) NOT NULL, updated_at TIMESTAMP NOT NULL OPTIONS (allow_commit_timestamp=true)) '
 'PRIMARY KEY (kind, id)',
 'CREATE TABLE tr_analytics_outbox ( shard INT64 NOT NULL, commit_ts TIMESTAMP NOT NULL '
 'OPTIONS (allow_commit_timestamp=true), event_id STRING(128) NOT NULL, payload STRING(MAX) '
 'NOT NULL, ) PRIMARY KEY (shard, commit_ts, event_id), ROW DELETION POLICY '
 '(OLDER_THAN(commit_ts, INTERVAL 7 DAY))',
 'CREATE TABLE tr_generation ( generation_id STRING(128) NOT NULL, workspace_id STRING(64) NOT '
 'NULL, key_hash STRING(128) NOT NULL, created_at TIMESTAMP NOT NULL, terminal_at TIMESTAMP '
 'NOT NULL, payload STRING(MAX) NOT NULL, ) PRIMARY KEY (generation_id), ROW DELETION POLICY '
 '(OLDER_THAN(terminal_at, INTERVAL 30 DAY))',
 'CREATE TABLE tr_earnings_balance ( user_id STRING(64) NOT NULL, shard INT64 NOT NULL DEFAULT '
 '(0), total_earned INT64 NOT NULL DEFAULT (0), total_transferred INT64 NOT NULL DEFAULT (0), '
 'updated_at TIMESTAMP OPTIONS (allow_commit_timestamp=true), ) PRIMARY KEY (user_id, shard)',
 'CREATE TABLE tr_credit_movement ( account_id STRING(80) NOT NULL, movement_id STRING(160) '
 'NOT NULL, kind STRING(40) NOT NULL, amount_microdollars INT64 NOT NULL, '
 'counterparty_account_id STRING(80), custom_model_id STRING(96), authorization_id STRING(64), '
 'created_at TIMESTAMP NOT NULL, ) PRIMARY KEY (account_id, movement_id), ROW DELETION POLICY '
 '(OLDER_THAN(created_at, INTERVAL 400 DAY))',
 'CREATE TABLE tr_user_lifetime_topup ( user_id STRING(64) NOT NULL, total_microdollars INT64 '
 'NOT NULL DEFAULT (0), updated_at TIMESTAMP OPTIONS (allow_commit_timestamp=true), ) PRIMARY '
 'KEY (user_id)',
 'CREATE TABLE tr_operational_analytics_outbox ( shard INT64 NOT NULL, commit_ts TIMESTAMP NOT '
 'NULL OPTIONS (allow_commit_timestamp=true), event_kind STRING(32) NOT NULL, event_id '
 'STRING(128) NOT NULL, payload STRING(MAX) NOT NULL, ) PRIMARY KEY (shard, commit_ts, '
 'event_kind, event_id), ROW DELETION POLICY (OLDER_THAN(commit_ts, INTERVAL 30 DAY))',
 'CREATE TABLE tr_gateway_authorization ( authorization_id STRING(64) NOT NULL, workspace_id '
 'STRING(64) NOT NULL, key_hash STRING(64) NOT NULL, reservation_id STRING(64), model_id '
 'STRING(256) NOT NULL, provider STRING(64) NOT NULL, usage_type STRING(16) NOT NULL, '
 'estimated_microdollars INT64 NOT NULL, settled BOOL NOT NULL DEFAULT (false), created_at '
 'TIMESTAMP NOT NULL, terminal_at TIMESTAMP, payload STRING(MAX) ) PRIMARY KEY '
 '(authorization_id)',
 'CREATE TABLE tr_stage_d_policy_watermark ( plane STRING(16) NOT NULL, highest_sequence INT64 '
 'NOT NULL, updated_at TIMESTAMP ) PRIMARY KEY (plane)',
 'CREATE TABLE spend_lease_scope_arbitration ( scope_salt STRING(4) NOT NULL, '
 'idempotency_scope STRING(256) NOT NULL, registration_kind STRING(16) NOT NULL, '
 'authorization_id STRING(64), spend_lease_id STRING(64), spend_lease_gen INT64, '
 'spend_lease_allocated_micro INT64, provisional_id STRING(64), created_at TIMESTAMP NOT NULL, '
 'terminal_at TIMESTAMP, CONSTRAINT spend_lease_scope_arbitration_shape CHECK '
 "((registration_kind = 'BOUND' AND authorization_id IS NOT NULL AND spend_lease_id IS NOT "
 'NULL AND spend_lease_gen IS NOT NULL AND spend_lease_allocated_micro IS NOT NULL AND '
 "provisional_id IS NULL) OR (registration_kind = 'CLAIM' AND provisional_id IS NOT NULL AND "
 'authorization_id IS NULL AND spend_lease_id IS NULL AND spend_lease_gen IS NULL AND '
 'spend_lease_allocated_micro IS NULL AND terminal_at IS NOT NULL)), ) PRIMARY KEY '
 '(scope_salt, idempotency_scope), ROW DELETION POLICY (OLDER_THAN(terminal_at, INTERVAL 30 '
 'DAY))',
 'CREATE TABLE spend_lease_open ( lease_id STRING(64) NOT NULL, phase STRING(16) NOT NULL, gen '
 'INT64 NOT NULL, key_hash STRING(64) NOT NULL, boot_kid STRING(64) NOT NULL, cap_micro INT64 '
 'NOT NULL, skew_seconds INT64 NOT NULL, workspace_id STRING(64) NOT NULL, region STRING(32) '
 'NOT NULL, creating_authorization_id STRING(64) NOT NULL, idempotency_scope STRING(256) NOT '
 'NULL, expires_at TIMESTAMP NOT NULL, next_attempt_at TIMESTAMP, attempts INT64 NOT NULL '
 'DEFAULT (0), last_error STRING(MAX), dead BOOL NOT NULL DEFAULT (false), '
 'close_eligible_since TIMESTAMP, global_closed_at TIMESTAMP, local_closed_at TIMESTAMP, '
 'recovering_at TIMESTAMP OPTIONS (allow_commit_timestamp = true), created_at TIMESTAMP NOT '
 "NULL, CONSTRAINT spend_lease_open_phase CHECK (phase IN ('candidate', 'recovering', 'open', "
 "'done')), ) PRIMARY KEY (lease_id)",
 'CREATE TABLE tr_trust_backfill ( provider STRING(16) NOT NULL, account_id STRING(255) NOT '
 'NULL, environment STRING(32) NOT NULL, source STRING(64) NOT NULL, source_version STRING(64) '
 'NOT NULL, history_start TIMESTAMP NOT NULL, closed_through TIMESTAMP NOT NULL, '
 'consistency_delay_seconds INT64 NOT NULL, unmatched_count INT64 NOT NULL, '
 'semantic_mismatch_count INT64 NOT NULL, completed_at TIMESTAMP, CONSTRAINT '
 'tr_trust_backfill_counts CHECK ( consistency_delay_seconds >= 0 AND unmatched_count >= 0 AND '
 'semantic_mismatch_count >= 0 ), CONSTRAINT tr_trust_backfill_completion CHECK ( completed_at '
 'IS NULL OR (unmatched_count = 0 AND semantic_mismatch_count = 0) ), ) PRIMARY KEY (provider, '
 'account_id, environment, source, source_version)',
 'CREATE TABLE tr_credit_balance ( workspace_id STRING(64) NOT NULL, shard INT64 NOT NULL '
 'DEFAULT (0), total_credits INT64 NOT NULL DEFAULT (0), total_usage INT64 NOT NULL DEFAULT '
 '(0), reserved INT64 NOT NULL DEFAULT (0), trust_tier INT64 DEFAULT (0), trust_computed_at '
 'TIMESTAMP, trust_latched_at TIMESTAMP, trust_override_tier INT64, billing_pause_causes '
 'ARRAY<STRING(32)>, pause_epoch INT64 DEFAULT (0), trust_reconciled_through TIMESTAMP, '
 'source_updated_at TIMESTAMP OPTIONS (allow_commit_timestamp=true), updated_at TIMESTAMP '
 'OPTIONS (allow_commit_timestamp=true), ) PRIMARY KEY (workspace_id, shard)',
 'CREATE TABLE tr_key_limit ( key_hash STRING(64) NOT NULL, shard INT64 NOT NULL DEFAULT (0), '
 'limit_micro INT64, usage INT64 NOT NULL DEFAULT (0), byok_usage INT64 NOT NULL DEFAULT (0), '
 'reserved INT64 NOT NULL DEFAULT (0), include_byok BOOL NOT NULL DEFAULT (true), '
 'day_limit_micro INT64, week_limit_micro INT64, month_limit_micro INT64, day_usage INT64 NOT '
 'NULL DEFAULT (0), day_start TIMESTAMP, week_usage INT64 NOT NULL DEFAULT (0), week_start '
 'TIMESTAMP, month_usage INT64 NOT NULL DEFAULT (0), month_start TIMESTAMP, source_updated_at '
 'TIMESTAMP OPTIONS (allow_commit_timestamp=true), updated_at TIMESTAMP OPTIONS '
 '(allow_commit_timestamp=true), ) PRIMARY KEY (key_hash, shard)',
 'CREATE TABLE tr_reservation ( reservation_id STRING(64) NOT NULL, workspace_id STRING(64), '
 'key_hash STRING(64), ws_shard INT64, credit_shard INT64 NOT NULL DEFAULT (0), key_shard '
 'INT64, credit_reserved_micro INT64, key_reserved_micro INT64, actual_micro INT64, '
 'hold_usage_type STRING(16), settled_usage_type STRING(16), authorization_id STRING(64), '
 'settled BOOL NOT NULL DEFAULT (false), idempotency_scope STRING(256), '
 'idempotency_fingerprint STRING(64), created_at TIMESTAMP OPTIONS '
 '(allow_commit_timestamp=true), expires_at TIMESTAMP, terminal_at TIMESTAMP, ) PRIMARY KEY '
 '(reservation_id)',
 'CREATE TABLE tr_trust_event ( workspace_id STRING(64) NOT NULL, event_id STRING(255) NOT '
 'NULL, kind STRING(16) NOT NULL, provider STRING(16) NOT NULL, amount_micro INT64, '
 'original_payment_ref STRING(255), adverse_ref STRING(255), occurred_at TIMESTAMP NOT NULL, '
 'recorded_at TIMESTAMP NOT NULL, payment_amount_micro INT64, currency STRING(8), '
 'credited_micro INT64, recovered_micro INT64, provider_subtype STRING(64), lifecycle_status '
 'STRING(32), cumulative_refunded INT64, recovery_target INT64, debit_status STRING(16), '
 'unrecovered_micro INT64, provider_ordering_watermark STRING(255), CONSTRAINT '
 "tr_trust_event_kind CHECK (kind IN ('payment','refund','dispute','abuse','grant')), "
 'CONSTRAINT tr_trust_event_provider CHECK (provider IN '
 "('stripe','paypal','adyen','x402','lightning','operator','system')), CONSTRAINT "
 'tr_trust_event_lifecycle CHECK (lifecycle_status IS NULL OR lifecycle_status IN '
 "('pending','succeeded','failed','reversed','won','lost','closed','terminal_by_horizon')), "
 'CONSTRAINT tr_trust_event_debit CHECK (debit_status IS NULL OR debit_status IN '
 "('debited','partial','unrecovered')), ) PRIMARY KEY (workspace_id, event_id)",
 'CREATE TABLE tr_trust_inbox ( provider STRING(16) NOT NULL, adverse_ref STRING(255) NOT '
 'NULL, payload STRING(MAX) NOT NULL, received_at TIMESTAMP NOT NULL, ) PRIMARY KEY (provider, '
 'adverse_ref)',
 'CREATE TABLE tr_owner_workspace ( owner_user_id STRING(64) NOT NULL, workspace_id STRING(64) '
 'NOT NULL, ) PRIMARY KEY (owner_user_id, workspace_id)',
 'CREATE TABLE tr_trust_override ( workspace_id STRING(64) NOT NULL, tier INT64 NOT NULL, '
 'identity_bypass BOOL NOT NULL, operator_identity STRING(255) NOT NULL, reason STRING(500) '
 'NOT NULL, set_at TIMESTAMP NOT NULL, CONSTRAINT tr_trust_override_tier CHECK (tier >= 0 AND '
 'tier <= 3), ) PRIMARY KEY (workspace_id)',
 'CREATE TABLE tr_trust_demotion_remainder ( owner_user_id STRING(64) NOT NULL, workspace_id '
 'STRING(64) NOT NULL, target_identity_ceiling INT64 NOT NULL, created_at TIMESTAMP NOT NULL, '
 'attempts INT64 NOT NULL DEFAULT (0), last_error STRING(MAX), ) PRIMARY KEY (owner_user_id, '
 'workspace_id)',
 'CREATE TABLE tr_settle_outbox ( authorization_id STRING(64) NOT NULL, intent_kind STRING(16) '
 'NOT NULL, settle_origin STRING(16) NOT NULL, reservation_id STRING(64), actual_cost_micro '
 'INT64 NOT NULL, selected_endpoint_id STRING(128), model_id STRING(128), selected_usage_type '
 "STRING(16), settle_body STRING(MAX), status STRING(24) NOT NULL DEFAULT ('pending'), "
 'attempts INT64 NOT NULL DEFAULT (0), last_error STRING(MAX), next_attempt_at TIMESTAMP, '
 'lease_owner STRING(64), leased_until TIMESTAMP, created_at TIMESTAMP, updated_at TIMESTAMP, '
 'terminal_at TIMESTAMP, auto_refill_workspace_id STRING(64), auto_refill_status STRING(24), '
 'auto_refill_attempts INT64 NOT NULL DEFAULT (0), auto_refill_last_error STRING(MAX), '
 'auto_refill_next_attempt_at TIMESTAMP, auto_refill_lease_owner STRING(64), '
 'auto_refill_leased_until TIMESTAMP, auto_refill_enqueued_at TIMESTAMP, '
 'auto_refill_updated_at TIMESTAMP, auto_refill_terminal_at TIMESTAMP, queue_shard INT64 NOT '
 "NULL AS ( MOD( MOD(FARM_FINGERPRINT(CONCAT(authorization_id, '#', intent_kind)), 16) + 16, "
 '16 ) ) STORED, ) PRIMARY KEY (authorization_id, intent_kind)',
 'ALTER TABLE tr_entities ADD COLUMN ephemeral_expires_at TIMESTAMP AS (CASE WHEN kind = '
 "'rate_limit' THEN SAFE.TIMESTAMP_SECONDS(SAFE_CAST(JSON_QUERY(body, '$.expires_at') AS "
 'INT64)) END) STORED',
 'ALTER TABLE tr_entities ADD COLUMN kid STRING(43)',
 'ALTER TABLE tr_entities ADD COLUMN att_sha256 STRING(43)',
 'ALTER TABLE tr_gateway_authorization ADD COLUMN spend_lease_id STRING(64)',
 'ALTER TABLE tr_gateway_authorization ADD COLUMN spend_lease_gen INT64',
 'ALTER TABLE tr_gateway_authorization ADD COLUMN spend_lease_allocated_micro INT64',
 'ALTER TABLE tr_gateway_authorization ADD COLUMN spend_lease_token STRING(MAX)',
 'ALTER TABLE tr_gateway_authorization ADD COLUMN spend_lease_status STRING(16)',
 'ALTER TABLE tr_gateway_authorization ADD COLUMN spend_lease_exp TIMESTAMP',
 'ALTER TABLE tr_gateway_authorization ADD COLUMN idempotency_fingerprint STRING(64)',
 'ALTER TABLE tr_gateway_authorization ADD COLUMN finalization_outcome STRING(32)',
 'ALTER TABLE tr_gateway_authorization ADD COLUMN finalized_cost_microdollars INT64',
 'ALTER TABLE tr_gateway_authorization ADD COLUMN spend_lease_admission_receipt STRING(MAX)',
 'ALTER TABLE tr_gateway_authorization ADD COLUMN spend_lease_receipt_hash STRING(64)',
 'ALTER TABLE tr_gateway_authorization ADD COLUMN started_at TIMESTAMP',
 'ALTER TABLE tr_gateway_authorization ADD COLUMN heartbeat_seq INT64',
 'ALTER TABLE tr_gateway_authorization ADD COLUMN heartbeat_at TIMESTAMP',
 'ALTER TABLE tr_gateway_authorization ADD COLUMN heartbeat_hash STRING(64)',
 'ALTER TABLE tr_gateway_authorization ADD COLUMN selected_endpoint_id STRING(128)',
 'ALTER TABLE tr_gateway_authorization ADD COLUMN delivered_usage STRING(MAX)',
 'ALTER TABLE tr_gateway_authorization ADD COLUMN pricing_snapshot STRING(MAX)',
 'ALTER TABLE tr_gateway_authorization ADD COLUMN stage_d_boot_kid STRING(128)',
 'ALTER TABLE tr_gateway_authorization ADD COLUMN invocation_nonce STRING(64)',
 'ALTER TABLE tr_gateway_authorization ADD COLUMN gateway_request_id STRING(37)',
 'CREATE NULL_FILTERED INDEX tr_gateway_authorization_by_trace_id ON tr_gateway_authorization '
 '(gateway_request_id)',
 'CREATE INDEX tr_generation_by_terminal_at ON tr_generation(terminal_at DESC) STORING '
 '(payload)',
 'CREATE INDEX tr_credit_movement_by_time ON tr_credit_movement (account_id, created_at DESC)',
 'CREATE NULL_FILTERED INDEX tr_receipt_key_versions ON tr_entities (kid, att_sha256)',
 'CREATE NULL_FILTERED INDEX spend_lease_scope_arbitration_by_authorization ON '
 'spend_lease_scope_arbitration (authorization_id)',
 'CREATE NULL_FILTERED INDEX spend_lease_open_due ON spend_lease_open (next_attempt_at)',
 'CREATE INDEX tr_trust_event_by_debt ON tr_trust_event (workspace_id, kind, '
 'unrecovered_micro)',
 'CREATE UNIQUE NULL_FILTERED INDEX tr_reservation_by_idemp ON tr_reservation '
 '(idempotency_scope)',
 'CREATE INDEX tr_reservation_by_expiry ON tr_reservation (settled, expires_at)',
 'CREATE NULL_FILTERED INDEX tr_reservation_by_authorization ON tr_reservation '
 '(authorization_id)',
 'CREATE NULL_FILTERED INDEX tr_reservation_by_terminal ON tr_reservation (settled, '
 'terminal_at) STORING (hold_usage_type, actual_micro, credit_reserved_micro)',
 'CREATE UNIQUE NULL_FILTERED INDEX tr_trust_event_adverse_dedup ON tr_trust_event (provider, '
 'adverse_ref, kind)',
 'CREATE UNIQUE NULL_FILTERED INDEX tr_trust_event_payment_dedup ON tr_trust_event (provider, '
 'original_payment_ref, kind)',
 'CREATE NULL_FILTERED INDEX tr_settle_outbox_due_v2 ON tr_settle_outbox (queue_shard, '
 'next_attempt_at)',
 'CREATE NULL_FILTERED INDEX tr_settle_outbox_auto_refill_due ON tr_settle_outbox '
 '(queue_shard, auto_refill_next_attempt_at)',
 'ALTER TABLE tr_entities ADD ROW DELETION POLICY (OLDER_THAN(ephemeral_expires_at, INTERVAL 1 '
 'DAY))',
 'ALTER TABLE tr_gateway_authorization ADD ROW DELETION POLICY (OLDER_THAN(terminal_at, '
 'INTERVAL 30 DAY))',
 'ALTER TABLE tr_reservation ADD ROW DELETION POLICY (OLDER_THAN(terminal_at, INTERVAL 30 '
 'DAY))',
 'ALTER TABLE tr_settle_outbox ADD ROW DELETION POLICY (OLDER_THAN(terminal_at, INTERVAL 30 '
 'DAY))')
