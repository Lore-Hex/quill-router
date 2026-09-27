# Native GoogleSQL emulator conformance

This change adds a real GoogleSQL server gate alongside the unchanged Python fake and the PostgreSQL-dialect backend. It does not establish production equivalence. The first CI run established schema provisioning and rejection of the three invalid constructions; round 2 corrects the harness failures and strengthens offline guards. Round 3 extends the emulator accommodation to the native store through a shared SDK shim and restores SDK state on exit.

## Schema source and scope

The base GoogleSQL table comes from `scripts/deploy/infra.sh` (duplicated in `infra-stage0.sh` and `spanner_zero_downtime_cutover.sh`). The authoritative additions are `scripts/deploy/migrate_*.sh`. There is no standalone native GoogleSQL schema file in the existing checkout. `src/trusted_router/storage_postgres_schema.sql` is the other dialect and is not a source for this backend. `scripts/lightning/spanner_provenance.sql` and `scripts/deploy/backfill_credit_balance_trust.sql` are operational artifacts rather than this adapter's base schema. Context: [storage handoff](HANDOFF.md), [typed counters](../design/billing-typed-counters.md), [durable settle outbox](../design/durable-settle-outbox.md).

[spanner_ddl.py](../../tests/conformance/spanner_ddl.py) is checked-in schema as code. [The parser](../../tests/conformance/spanner_schema_source.py) extracts CREATE statements, resolves static index names, adds missing columns, and expands retention-policy calls without running shell scripts. Source digests additionally fail on changed helpers, new files, or shell syntax the narrow parser does not understand. Review changes before regenerating with `python -m tests.conformance.spanner_schema_source`. Unrecognized schema helpers fail extraction, so regeneration cannot silently bless an unsupported `ensure_*` call. Source digests intentionally make even migration-comment changes require review/regeneration.

The generated schema contains **63 DDL statements: 21 tables, 14 secondary indexes, and 9 row-deletion policies**, including the additive column operations. This is the fully migrated **fresh-install** schema, not a claim that every production database already has each optional migration. In particular:

- Table, column, and index creation is generally guarded by `INFORMATION_SCHEMA` existence checks. The typed migration also reapplies `allow_commit_timestamp=true` on the two `source_updated_at` columns.
- `migrate_request_retention.sh` is dry-run unless `--apply`; TTL policies require a zero-immediately-eligible-row preflight. `migrate_generation_records.sh` is also dry-run unless `--apply`.
- Entity TTL requires `--apply` plus `TR_HEAVY_DDL_ACK=tr_entities.ephemeral_expires_at`; its generated column applies only to numeric rate-limit expiry, not all entities.
- `migrate_gateway_request_index.sh` prepares the nonunique trace index; `--retire-unique` later drops the historical unique `tr_gateway_authorization_by_gateway_request_id`. Fresh installations include only `tr_gateway_authorization_by_trace_id`.
- `migrate_trust_reconciliation.sh` conditionally recreates the old three-column-key marker table only when it contains no real reconciliation state. This fixture uses the current five-column primary key without running that destructive upgrade.
- Existing installations intentionally add nullable key-window usage and reservation `credit_shard` columns; fresh CREATE definitions retain NOT NULL/defaults. This suite does not test historical rolling-upgrade schemas or backfills.

## CI provisioning and first-run evidence

The `spanner-emulator` job pins the exact image digests pulled in its first CI run:

- Spanner: `gcr.io/cloud-spanner-emulator/emulator@sha256:c6f3402f2599684f295a0fdefb6fbbbfb18a0e43e309ff5456ccb452a4570a79`.
- Bigtable SDK: `gcr.io/google.com/cloudsdktool/google-cloud-cli@sha256:7617d937e9360d769de4ef66266a8caae503c1dbbd050c93404d3f30045c5125` (the first run's `emulators` image).

Run 1 successfully started both service containers and submitted the complete 63-statement schema. Acceptance executed all 496 cases in approximately ten seconds: **449 passed, 47 failed**. All three rejection canaries were rejected. Failures were harness heartbeat types (32), TIMESTAMP strings (3), null-filtered-index eligibility checks (11), and the overlarge “below limit” control (1). The native conformance step passed its first test, then timed out in SDK teardown. Both steps spent about ten minutes joining the SDK multiplexed-session maintenance thread; this was teardown time, not test execution time.

The emulator path sets `DatabaseSessionsManager._MAINTENANCE_THREAD_POLLING_INTERVAL` to 100 ms before constructing any Spanner client. The offline mock-SDK test asserts this ordering and restoration of the original interval after database close, including body, setup and close failures. Production configuration is untouched. Native tests share one disposable instance/database/Bigtable table per session, construct a store per test, and retain session resource teardown. The per-test `unique` fixture supplies order-independent identifiers. Acceptance shares those resources if both suites run in one process; CI's two pytest processes each provision once.

The job exposes Spanner gRPC 9010 / REST 9020 and Bigtable 8086. The SDK service container stays alive with interactive/TTY options and starts Bigtable via `docker exec -d`. Readiness is bounded to 60 seconds. `TR_CONFORMANCE_EMULATOR_SCHEMA=1` requires both loopback emulator endpoints; missing or unreachable servers fail rather than skip. Anonymous credentials and synthetic resource IDs are used. DDL is submitted in ordered batches of at most 20 with bounded admin RPC waits.

Exactly ten removed legacy-money methods are strict xfails for **both native-store backends**, using the shared store-level registry. The fake-only Bigtable rollup ordering gap stays fake-only; emulator failures there remain failures. Collection-level checks enforce this distinction. Acceptance executes every registered native SQL case without xfails.

## Statement coverage and emulator limitations

283 native expressions (167 SELECT, 116 DML), 303 literal scenarios, 124 builder/capture cases, 427 primary acceptance cases, 116 additional batch cases, 3 rejection canaries and 3 positive controls; 15 builders and 202 dispatch scopes fingerprinted.

See [the inventory](spanner-sql-inventory.md) for every source expression and runtime case. Discovery covers all Python modules by default, excluding only the reviewed `storage_postgres.py` dialect adapter. Other non-GoogleSQL expressions in mixed or analytics modules remain explicitly classified and fingerprinted in the manifest, so new files and expressions cannot escape review.

The one SQL emulator accommodation is the statement hint `@{spanner_emulator.disable_query_null_filtered_index_check=true}`. A shared SDK-boundary shim derives index names from `CREATE [UNIQUE] NULL_FILTERED INDEX` in `spanner_ddl.DDL` and adds the hint only to statements naming one of those indexes. `emulator_resources()` installs it only after `require_emulators()` succeeds and restores the original SDK methods on exit, including exceptions. Both acceptance and the real `SpannerBigtableStore` use it; acceptance no longer rewrites individual calls. Other SQL passes through byte-identical, and already hinted SQL is never prefixed twice.

Source inspection confirms the store reaches `_SnapshotBase.execute_sql` (also inherited by `Transaction`), `Transaction.execute_update`, and `Transaction.batch_update` via `storage_gcp_batch_dml.execute_batch_dml`. The shim also covers `Database.execute_partitioned_dml` and `BatchSnapshot.execute_sql` defensively. In the installed SDK, `database.py:1618` belongs to **BatchSnapshot**, not Database; neither extra entry point currently has a caller under `src/trusted_router`. Positional and keyword `sql`/`dml`/`statements`, batch strings and tuples, bindings and other options are covered by always-on recorder tests. A forwarding test proves that nested SDK calls add the hint exactly once. An always-on guard still checks exact equality of the hinted and eligible registered statement sets. Production SQL is unchanged.

The affected native store methods are:

- `storage_gcp.list_receipt_keys`
- `storage_gcp_regional_quota.terminal_regional_hold_amount` / its nested `txn`
- `storage_gcp_request_records.read_gateway_authorization_by_gateway_request_id`
- `storage_gcp_settle_outbox.due`, `due_auto_refills`, and `auto_refill_pending_freshness`
- `storage_gcp_spend_lease._due_rows` and `arm_bound_retention`

The first CI run's emulator explained: “The emulator is not able to determine whether the null filtered index … can be used to answer this query as it may filter out nulls that may be required to answer the query.” It directed testing against Cloud Spanner and said “the emulator will accept the query and return a valid result when it is run with the check disabled.” These are live production queries; the hint bypasses the emulator's index eligibility check, not SQL parsing or execution. Eleven first-run failures had this message. This evidence comes from the supplied CI logs; no online documentation was fetched.

The DDL is unchanged, including generated columns and row-deletion policies. Acceptance establishes server analysis/execution with synthetic bindings, mostly on empty tables. Seeded heartbeat and done-returning cases additionally require affected rows. TTL background expiry, query plans, IAM/TLS, contention, staleness and production resource limits remain outside this proof.

The canaries now match their specific restriction: `create_if_missing` plus literal/parameter wording; JSON_REMOVE argument/path plus constant wording; and the exact 1000-function limit message. Each control uses the same table, column and expression as its canary, changing only the prohibited argument or repetition count. The matched IF expressions use 450 copies (~900 functions) for acceptance and 520 (~1040) for rejection. Unrelated InvalidArgument messages cannot satisfy the canaries. Exact emulator wording for the two JSON restrictions will be checked in CI.

Frozen SQL fragments remain explicit scenarios: `where` in `storage_gcp.list_credit_movements`, `storage_gcp._list_entities`, and `storage_gcp_google_ads._list_entities`; `suffix_sql` in `storage_gcp._list_entities` and `tail` in `storage_gcp_google_ads._list_entities`; `arms` in `SpannerOperationalAnalyticsOutbox.oldest_enqueued_at`; `sibling` in `done_retention_statements`; `phase_sql` in `_due_rows`; and `suffix` in `trust_eligibility.read_lease_trust` / `billing_paused_tx`. The always-on scope guard requires an assignment for each fragment inside its fingerprinted production scope, so changing its production construction invalidates completeness. Module column constants used by the newly registered reconciliation queries are evaluated from production. Builder batches use the matching key shard and settlement's `defer_retention=True`.

## Offline evidence and remaining verification

Round 2 uses `uv run --offline --frozen` throughout, without network, commits or pushes. The full repository suite was explicitly not run: this sandbox cannot bind localhost and CI owns that gate. The requested focused conformance/workflow tests, lint, mypy, isolated coverage proof and copy-based mutations are recorded in the round-2 result below. No local emulator execution is claimed.

Schema extraction now rejects every unconsumed DDL-bearing line with file:line. Exact reviewed exceptions cover helper dispatch/expansion, database options, printed rollback advice, destructive empty-marker recreation and `--retire-unique`. Heredoc DDL, single-quoted ALTER dispatches and variable-backed column definitions fail rather than silently regenerating. Unresolved shell dollars fail (the existing SQL JSON path `'$.expires_at'` is explicitly recognized). Regeneration prints a unified DDL diff before writing.

Coverage evaluates source expressions under `<spanner-sql-inventory:module>` pseudo-filenames, which coverage ignores; evaluating expressions does not claim production source-line coverage. Full-suite ≥70% coverage and the corrected real-emulator run remain CI responsibilities.

### Round-2 gate results

All commands used `uv run --offline --frozen`; the coverage proof used an isolated `COVERAGE_FILE` under `/private/tmp`.

| Gate | Result |
|---|---|
| `ruff check .` | All checks passed |
| `mypy` | Success: no issues found in 400 source files |
| `pytest -q -p no:cacheprovider tests/conformance tests/test_ci_workflow.py` | **294 passed, 990 skipped, 11 xfailed**, 18 warnings in 85.81s |
| `coverage run -m pytest -q tests/conformance/test_spanner_sql_acceptance.py` | **9 passed, 549 skipped**, 6 warnings in 74.56s |
| `coverage report > /dev/null` | Exit 0; no pseudo-source error |
| CI's `coverage report --show-missing --skip-covered --fail-under=70` | Exit 2: **22%**, below 70%, because this isolated proof ran only the focused acceptance module. Full-suite coverage was not run or claimed. |
| Copy-only mutations | **12/12 caught** at the intended failing tests below |
| Valid schema addition in a copy | Regeneration printed unified DDL diff before writing |

| Mutation | Failing test |
|---|---|
| Restore 10-minute polling | `test_provisioning_submits_all_ddl_and_cleans_up` |
| Drop one emulator legacy-money gap | `test_native_legacy_gaps_are_strict_at_collection` |
| Bind a `+00:00` TIMESTAMP | `test_timestamp_string_bindings_use_utc_z` |
| Omit hint for receipt-key index | `test_null_filtered_hint_set_matches_registered_statements` |
| Generic canary regex | `test_canary_rejection_is_specific[row-dependent-json-remove-path]` |
| Add INSERT OR IGNORE literal | `test_spanner_sql_inventory_is_complete` |
| Add SQL in a new module | `test_spanner_sql_inventory_is_complete` |
| Heredoc DDL | `test_regeneration_extraction` in the mutated copy |
| Single-quoted ALTER | `test_regeneration_extraction` in the mutated copy |
| Variable-backed column definition | `test_regeneration_extraction` in the mutated copy |
| Manifest STRING → INT64 | `test_every_registered_case_has_exact_typed_bindings` |
| Discard all heartbeat builder cases | `test_registered_builders_are_actually_called` |

The three schema mutations are also permanent parametrized negative controls in `test_schema_blind_spots_fail_closed_in_copies`. The new-module test additionally covers commented, parenthesized, split-prefix and dynamic-prefix SQL. Cleanup tests cover session creation, begin, body, rollback and deletion failures. No production code or schema source was edited; server acceptance, native rollup ordering, corrected teardown runtime and the whole-workflow ≥70% gate still need CI.

### Round-3 gate results

All commands used `uv run --offline --frozen`, with `UV_CACHE_DIR` under `/private/tmp` because the default cache is outside the writable sandbox. No network, commits, pushes, full-suite run or local emulator execution.

| Gate | Result |
|---|---|
| `ruff check .` | All checks passed |
| `mypy` | Success: no issues found in 400 source files |
| `pytest -q -p no:cacheprovider tests/conformance tests/test_ci_workflow.py` | **315 passed, 990 skipped, 11 xfailed**, 18 warnings in 62.33s |
| Remove `_SnapshotBase.execute_sql` shim installation in a temporary copy | **3 failed, 15 deselected** in 0.67s; all three fail on missing SQL hint |

The mutation failed `test_sdk_null_filtered_hint_and_passthrough[positional-_SnapshotBase.execute_sql]`, `test_sdk_null_filtered_hint_and_passthrough[keyword-_SnapshotBase.execute_sql]`, and `test_sdk_null_filtered_hint_and_passthrough[mixed-_SnapshotBase.execute_sql]` in `tests/conformance/test_spanner_emulator_sdk.py`. The copy reused the installed environment with `--no-sync` (building the copied project offline otherwise required uncached hatchling), and ran `python -m pytest -q -p no:cacheprovider tests/conformance/test_spanner_emulator_sdk.py -k 'test_sdk_null_filtered_hint_and_passthrough and _SnapshotBase'`. The working checkout kept the shim enabled.

## Exact table, column, index and policy inventory

The following is generated from the checked-in DDL. ALTER statements below each table are part of its final fresh schema. Types, lengths, nullability, defaults, generated expressions, commit timestamp options, primary keys, check constraints and TTL policies are retained verbatim apart from whitespace.


### tr_entities

```sql
CREATE TABLE tr_entities (kind STRING(64) NOT NULL,
  id STRING(512) NOT NULL,
  body STRING(MAX) NOT NULL,
  updated_at TIMESTAMP NOT NULL OPTIONS (allow_commit_timestamp=true)) PRIMARY KEY (kind,
  id)
ALTER TABLE tr_entities ADD COLUMN ephemeral_expires_at TIMESTAMP AS (CASE WHEN kind = 'rate_limit' THEN SAFE.TIMESTAMP_SECONDS(SAFE_CAST(JSON_QUERY(body, '$.expires_at') AS INT64)) END) STORED
ALTER TABLE tr_entities ADD COLUMN kid STRING(43)
ALTER TABLE tr_entities ADD COLUMN att_sha256 STRING(43)
ALTER TABLE tr_entities ADD ROW DELETION POLICY (OLDER_THAN(ephemeral_expires_at, INTERVAL 1 DAY))
```

### tr_analytics_outbox

```sql
CREATE TABLE tr_analytics_outbox ( shard INT64 NOT NULL,
  commit_ts TIMESTAMP NOT NULL OPTIONS (allow_commit_timestamp=true),
  event_id STRING(128) NOT NULL,
  payload STRING(MAX) NOT NULL,
  ) PRIMARY KEY (shard,
  commit_ts,
  event_id),
  ROW DELETION POLICY (OLDER_THAN(commit_ts,
  INTERVAL 7 DAY))
```

### tr_generation

```sql
CREATE TABLE tr_generation ( generation_id STRING(128) NOT NULL,
  workspace_id STRING(64) NOT NULL,
  key_hash STRING(128) NOT NULL,
  created_at TIMESTAMP NOT NULL,
  terminal_at TIMESTAMP NOT NULL,
  payload STRING(MAX) NOT NULL,
  ) PRIMARY KEY (generation_id),
  ROW DELETION POLICY (OLDER_THAN(terminal_at,
  INTERVAL 30 DAY))
```

### tr_earnings_balance

```sql
CREATE TABLE tr_earnings_balance ( user_id STRING(64) NOT NULL,
  shard INT64 NOT NULL DEFAULT (0),
  total_earned INT64 NOT NULL DEFAULT (0),
  total_transferred INT64 NOT NULL DEFAULT (0),
  updated_at TIMESTAMP OPTIONS (allow_commit_timestamp=true),
  ) PRIMARY KEY (user_id,
  shard)
```

### tr_credit_movement

```sql
CREATE TABLE tr_credit_movement ( account_id STRING(80) NOT NULL,
  movement_id STRING(160) NOT NULL,
  kind STRING(40) NOT NULL,
  amount_microdollars INT64 NOT NULL,
  counterparty_account_id STRING(80),
  custom_model_id STRING(96),
  authorization_id STRING(64),
  created_at TIMESTAMP NOT NULL,
  ) PRIMARY KEY (account_id,
  movement_id),
  ROW DELETION POLICY (OLDER_THAN(created_at,
  INTERVAL 400 DAY))
```

### tr_user_lifetime_topup

```sql
CREATE TABLE tr_user_lifetime_topup ( user_id STRING(64) NOT NULL,
  total_microdollars INT64 NOT NULL DEFAULT (0),
  updated_at TIMESTAMP OPTIONS (allow_commit_timestamp=true),
  ) PRIMARY KEY (user_id)
```

### tr_operational_analytics_outbox

```sql
CREATE TABLE tr_operational_analytics_outbox ( shard INT64 NOT NULL,
  commit_ts TIMESTAMP NOT NULL OPTIONS (allow_commit_timestamp=true),
  event_kind STRING(32) NOT NULL,
  event_id STRING(128) NOT NULL,
  payload STRING(MAX) NOT NULL,
  ) PRIMARY KEY (shard,
  commit_ts,
  event_kind,
  event_id),
  ROW DELETION POLICY (OLDER_THAN(commit_ts,
  INTERVAL 30 DAY))
```

### tr_gateway_authorization

```sql
CREATE TABLE tr_gateway_authorization ( authorization_id STRING(64) NOT NULL,
  workspace_id STRING(64) NOT NULL,
  key_hash STRING(64) NOT NULL,
  reservation_id STRING(64),
  model_id STRING(256) NOT NULL,
  provider STRING(64) NOT NULL,
  usage_type STRING(16) NOT NULL,
  estimated_microdollars INT64 NOT NULL,
  settled BOOL NOT NULL DEFAULT (false),
  created_at TIMESTAMP NOT NULL,
  terminal_at TIMESTAMP,
  payload STRING(MAX) ) PRIMARY KEY (authorization_id)
ALTER TABLE tr_gateway_authorization ADD COLUMN spend_lease_id STRING(64)
ALTER TABLE tr_gateway_authorization ADD COLUMN spend_lease_gen INT64
ALTER TABLE tr_gateway_authorization ADD COLUMN spend_lease_allocated_micro INT64
ALTER TABLE tr_gateway_authorization ADD COLUMN spend_lease_token STRING(MAX)
ALTER TABLE tr_gateway_authorization ADD COLUMN spend_lease_status STRING(16)
ALTER TABLE tr_gateway_authorization ADD COLUMN spend_lease_exp TIMESTAMP
ALTER TABLE tr_gateway_authorization ADD COLUMN idempotency_fingerprint STRING(64)
ALTER TABLE tr_gateway_authorization ADD COLUMN finalization_outcome STRING(32)
ALTER TABLE tr_gateway_authorization ADD COLUMN finalized_cost_microdollars INT64
ALTER TABLE tr_gateway_authorization ADD COLUMN spend_lease_admission_receipt STRING(MAX)
ALTER TABLE tr_gateway_authorization ADD COLUMN spend_lease_receipt_hash STRING(64)
ALTER TABLE tr_gateway_authorization ADD COLUMN started_at TIMESTAMP
ALTER TABLE tr_gateway_authorization ADD COLUMN heartbeat_seq INT64
ALTER TABLE tr_gateway_authorization ADD COLUMN heartbeat_at TIMESTAMP
ALTER TABLE tr_gateway_authorization ADD COLUMN heartbeat_hash STRING(64)
ALTER TABLE tr_gateway_authorization ADD COLUMN selected_endpoint_id STRING(128)
ALTER TABLE tr_gateway_authorization ADD COLUMN delivered_usage STRING(MAX)
ALTER TABLE tr_gateway_authorization ADD COLUMN pricing_snapshot STRING(MAX)
ALTER TABLE tr_gateway_authorization ADD COLUMN stage_d_boot_kid STRING(128)
ALTER TABLE tr_gateway_authorization ADD COLUMN invocation_nonce STRING(64)
ALTER TABLE tr_gateway_authorization ADD COLUMN gateway_request_id STRING(37)
ALTER TABLE tr_gateway_authorization ADD ROW DELETION POLICY (OLDER_THAN(terminal_at, INTERVAL 30 DAY))
```

### tr_stage_d_policy_watermark

```sql
CREATE TABLE tr_stage_d_policy_watermark ( plane STRING(16) NOT NULL,
  highest_sequence INT64 NOT NULL,
  updated_at TIMESTAMP ) PRIMARY KEY (plane)
```

### spend_lease_scope_arbitration

```sql
CREATE TABLE spend_lease_scope_arbitration ( scope_salt STRING(4) NOT NULL,
  idempotency_scope STRING(256) NOT NULL,
  registration_kind STRING(16) NOT NULL,
  authorization_id STRING(64),
  spend_lease_id STRING(64),
  spend_lease_gen INT64,
  spend_lease_allocated_micro INT64,
  provisional_id STRING(64),
  created_at TIMESTAMP NOT NULL,
  terminal_at TIMESTAMP,
  CONSTRAINT spend_lease_scope_arbitration_shape CHECK ((registration_kind = 'BOUND' AND authorization_id IS NOT NULL AND spend_lease_id IS NOT NULL AND spend_lease_gen IS NOT NULL AND spend_lease_allocated_micro IS NOT NULL AND provisional_id IS NULL) OR (registration_kind = 'CLAIM' AND provisional_id IS NOT NULL AND authorization_id IS NULL AND spend_lease_id IS NULL AND spend_lease_gen IS NULL AND spend_lease_allocated_micro IS NULL AND terminal_at IS NOT NULL)),
  ) PRIMARY KEY (scope_salt,
  idempotency_scope),
  ROW DELETION POLICY (OLDER_THAN(terminal_at,
  INTERVAL 30 DAY))
```

### spend_lease_open

```sql
CREATE TABLE spend_lease_open ( lease_id STRING(64) NOT NULL,
  phase STRING(16) NOT NULL,
  gen INT64 NOT NULL,
  key_hash STRING(64) NOT NULL,
  boot_kid STRING(64) NOT NULL,
  cap_micro INT64 NOT NULL,
  skew_seconds INT64 NOT NULL,
  workspace_id STRING(64) NOT NULL,
  region STRING(32) NOT NULL,
  creating_authorization_id STRING(64) NOT NULL,
  idempotency_scope STRING(256) NOT NULL,
  expires_at TIMESTAMP NOT NULL,
  next_attempt_at TIMESTAMP,
  attempts INT64 NOT NULL DEFAULT (0),
  last_error STRING(MAX),
  dead BOOL NOT NULL DEFAULT (false),
  close_eligible_since TIMESTAMP,
  global_closed_at TIMESTAMP,
  local_closed_at TIMESTAMP,
  recovering_at TIMESTAMP OPTIONS (allow_commit_timestamp = true),
  created_at TIMESTAMP NOT NULL,
  CONSTRAINT spend_lease_open_phase CHECK (phase IN ('candidate',
  'recovering',
  'open',
  'done')),
  ) PRIMARY KEY (lease_id)
```

### tr_trust_backfill

```sql
CREATE TABLE tr_trust_backfill ( provider STRING(16) NOT NULL,
  account_id STRING(255) NOT NULL,
  environment STRING(32) NOT NULL,
  source STRING(64) NOT NULL,
  source_version STRING(64) NOT NULL,
  history_start TIMESTAMP NOT NULL,
  closed_through TIMESTAMP NOT NULL,
  consistency_delay_seconds INT64 NOT NULL,
  unmatched_count INT64 NOT NULL,
  semantic_mismatch_count INT64 NOT NULL,
  completed_at TIMESTAMP,
  CONSTRAINT tr_trust_backfill_counts CHECK ( consistency_delay_seconds >= 0 AND unmatched_count >= 0 AND semantic_mismatch_count >= 0 ),
  CONSTRAINT tr_trust_backfill_completion CHECK ( completed_at IS NULL OR (unmatched_count = 0 AND semantic_mismatch_count = 0) ),
  ) PRIMARY KEY (provider,
  account_id,
  environment,
  source,
  source_version)
```

### tr_credit_balance

```sql
CREATE TABLE tr_credit_balance ( workspace_id STRING(64) NOT NULL,
  shard INT64 NOT NULL DEFAULT (0),
  total_credits INT64 NOT NULL DEFAULT (0),
  total_usage INT64 NOT NULL DEFAULT (0),
  reserved INT64 NOT NULL DEFAULT (0),
  trust_tier INT64 DEFAULT (0),
  trust_computed_at TIMESTAMP,
  trust_latched_at TIMESTAMP,
  trust_override_tier INT64,
  billing_pause_causes ARRAY<STRING(32)>,
  pause_epoch INT64 DEFAULT (0),
  trust_reconciled_through TIMESTAMP,
  source_updated_at TIMESTAMP OPTIONS (allow_commit_timestamp=true),
  updated_at TIMESTAMP OPTIONS (allow_commit_timestamp=true),
  ) PRIMARY KEY (workspace_id,
  shard)
```

### tr_key_limit

```sql
CREATE TABLE tr_key_limit ( key_hash STRING(64) NOT NULL,
  shard INT64 NOT NULL DEFAULT (0),
  limit_micro INT64,
  usage INT64 NOT NULL DEFAULT (0),
  byok_usage INT64 NOT NULL DEFAULT (0),
  reserved INT64 NOT NULL DEFAULT (0),
  include_byok BOOL NOT NULL DEFAULT (true),
  day_limit_micro INT64,
  week_limit_micro INT64,
  month_limit_micro INT64,
  day_usage INT64 NOT NULL DEFAULT (0),
  day_start TIMESTAMP,
  week_usage INT64 NOT NULL DEFAULT (0),
  week_start TIMESTAMP,
  month_usage INT64 NOT NULL DEFAULT (0),
  month_start TIMESTAMP,
  source_updated_at TIMESTAMP OPTIONS (allow_commit_timestamp=true),
  updated_at TIMESTAMP OPTIONS (allow_commit_timestamp=true),
  ) PRIMARY KEY (key_hash,
  shard)
```

### tr_reservation

```sql
CREATE TABLE tr_reservation ( reservation_id STRING(64) NOT NULL,
  workspace_id STRING(64),
  key_hash STRING(64),
  ws_shard INT64,
  credit_shard INT64 NOT NULL DEFAULT (0),
  key_shard INT64,
  credit_reserved_micro INT64,
  key_reserved_micro INT64,
  actual_micro INT64,
  hold_usage_type STRING(16),
  settled_usage_type STRING(16),
  authorization_id STRING(64),
  settled BOOL NOT NULL DEFAULT (false),
  idempotency_scope STRING(256),
  idempotency_fingerprint STRING(64),
  created_at TIMESTAMP OPTIONS (allow_commit_timestamp=true),
  expires_at TIMESTAMP,
  terminal_at TIMESTAMP,
  ) PRIMARY KEY (reservation_id)
ALTER TABLE tr_reservation ADD ROW DELETION POLICY (OLDER_THAN(terminal_at, INTERVAL 30 DAY))
```

### tr_trust_event

```sql
CREATE TABLE tr_trust_event ( workspace_id STRING(64) NOT NULL,
  event_id STRING(255) NOT NULL,
  kind STRING(16) NOT NULL,
  provider STRING(16) NOT NULL,
  amount_micro INT64,
  original_payment_ref STRING(255),
  adverse_ref STRING(255),
  occurred_at TIMESTAMP NOT NULL,
  recorded_at TIMESTAMP NOT NULL,
  payment_amount_micro INT64,
  currency STRING(8),
  credited_micro INT64,
  recovered_micro INT64,
  provider_subtype STRING(64),
  lifecycle_status STRING(32),
  cumulative_refunded INT64,
  recovery_target INT64,
  debit_status STRING(16),
  unrecovered_micro INT64,
  provider_ordering_watermark STRING(255),
  CONSTRAINT tr_trust_event_kind CHECK (kind IN ('payment','refund','dispute','abuse','grant')),
  CONSTRAINT tr_trust_event_provider CHECK (provider IN ('stripe','paypal','adyen','x402','lightning','operator','system')),
  CONSTRAINT tr_trust_event_lifecycle CHECK (lifecycle_status IS NULL OR lifecycle_status IN ('pending','succeeded','failed','reversed','won','lost','closed','terminal_by_horizon')),
  CONSTRAINT tr_trust_event_debit CHECK (debit_status IS NULL OR debit_status IN ('debited','partial','unrecovered')),
  ) PRIMARY KEY (workspace_id,
  event_id)
```

### tr_trust_inbox

```sql
CREATE TABLE tr_trust_inbox ( provider STRING(16) NOT NULL,
  adverse_ref STRING(255) NOT NULL,
  payload STRING(MAX) NOT NULL,
  received_at TIMESTAMP NOT NULL,
  ) PRIMARY KEY (provider,
  adverse_ref)
```

### tr_owner_workspace

```sql
CREATE TABLE tr_owner_workspace ( owner_user_id STRING(64) NOT NULL,
  workspace_id STRING(64) NOT NULL,
  ) PRIMARY KEY (owner_user_id,
  workspace_id)
```

### tr_trust_override

```sql
CREATE TABLE tr_trust_override ( workspace_id STRING(64) NOT NULL,
  tier INT64 NOT NULL,
  identity_bypass BOOL NOT NULL,
  operator_identity STRING(255) NOT NULL,
  reason STRING(500) NOT NULL,
  set_at TIMESTAMP NOT NULL,
  CONSTRAINT tr_trust_override_tier CHECK (tier >= 0 AND tier <= 3),
  ) PRIMARY KEY (workspace_id)
```

### tr_trust_demotion_remainder

```sql
CREATE TABLE tr_trust_demotion_remainder ( owner_user_id STRING(64) NOT NULL,
  workspace_id STRING(64) NOT NULL,
  target_identity_ceiling INT64 NOT NULL,
  created_at TIMESTAMP NOT NULL,
  attempts INT64 NOT NULL DEFAULT (0),
  last_error STRING(MAX),
  ) PRIMARY KEY (owner_user_id,
  workspace_id)
```

### tr_settle_outbox

```sql
CREATE TABLE tr_settle_outbox ( authorization_id STRING(64) NOT NULL,
  intent_kind STRING(16) NOT NULL,
  settle_origin STRING(16) NOT NULL,
  reservation_id STRING(64),
  actual_cost_micro INT64 NOT NULL,
  selected_endpoint_id STRING(128),
  model_id STRING(128),
  selected_usage_type STRING(16),
  settle_body STRING(MAX),
  status STRING(24) NOT NULL DEFAULT ('pending'),
  attempts INT64 NOT NULL DEFAULT (0),
  last_error STRING(MAX),
  next_attempt_at TIMESTAMP,
  lease_owner STRING(64),
  leased_until TIMESTAMP,
  created_at TIMESTAMP,
  updated_at TIMESTAMP,
  terminal_at TIMESTAMP,
  auto_refill_workspace_id STRING(64),
  auto_refill_status STRING(24),
  auto_refill_attempts INT64 NOT NULL DEFAULT (0),
  auto_refill_last_error STRING(MAX),
  auto_refill_next_attempt_at TIMESTAMP,
  auto_refill_lease_owner STRING(64),
  auto_refill_leased_until TIMESTAMP,
  auto_refill_enqueued_at TIMESTAMP,
  auto_refill_updated_at TIMESTAMP,
  auto_refill_terminal_at TIMESTAMP,
  queue_shard INT64 NOT NULL AS ( MOD( MOD(FARM_FINGERPRINT(CONCAT(authorization_id,
  '#',
  intent_kind)),
  16) + 16,
  16 ) ) STORED,
  ) PRIMARY KEY (authorization_id,
  intent_kind)
ALTER TABLE tr_settle_outbox ADD ROW DELETION POLICY (OLDER_THAN(terminal_at, INTERVAL 30 DAY))
```

### Secondary indexes

```sql
CREATE NULL_FILTERED INDEX tr_gateway_authorization_by_trace_id ON tr_gateway_authorization (gateway_request_id)
CREATE INDEX tr_generation_by_terminal_at ON tr_generation(terminal_at DESC) STORING (payload)
CREATE INDEX tr_credit_movement_by_time ON tr_credit_movement (account_id, created_at DESC)
CREATE NULL_FILTERED INDEX tr_receipt_key_versions ON tr_entities (kid, att_sha256)
CREATE NULL_FILTERED INDEX spend_lease_scope_arbitration_by_authorization ON spend_lease_scope_arbitration (authorization_id)
CREATE NULL_FILTERED INDEX spend_lease_open_due ON spend_lease_open (next_attempt_at)
CREATE UNIQUE NULL_FILTERED INDEX tr_reservation_by_idemp ON tr_reservation (idempotency_scope)
CREATE INDEX tr_reservation_by_expiry ON tr_reservation (settled, expires_at)
CREATE NULL_FILTERED INDEX tr_reservation_by_authorization ON tr_reservation (authorization_id)
CREATE NULL_FILTERED INDEX tr_reservation_by_terminal ON tr_reservation (settled, terminal_at) STORING (hold_usage_type, actual_micro, credit_reserved_micro)
CREATE UNIQUE NULL_FILTERED INDEX tr_trust_event_adverse_dedup ON tr_trust_event (provider, adverse_ref, kind)
CREATE UNIQUE NULL_FILTERED INDEX tr_trust_event_payment_dedup ON tr_trust_event (provider, original_payment_ref, kind)
CREATE NULL_FILTERED INDEX tr_settle_outbox_due_v2 ON tr_settle_outbox (queue_shard, next_attempt_at)
CREATE NULL_FILTERED INDEX tr_settle_outbox_auto_refill_due ON tr_settle_outbox (queue_shard, auto_refill_next_attempt_at)
```
