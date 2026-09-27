# Native GoogleSQL emulator conformance

This change adds a real GoogleSQL server gate alongside the unchanged Python fake and the PostgreSQL-dialect backend. It does not establish production equivalence: CI must first demonstrate that the selected emulator accepts the current SQL and rejects the three known invalid constructions.

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

## CI provisioning

The new `spanner-emulator` job uses these explicit image tags:

- `gcr.io/cloud-spanner-emulator/emulator:latest`, exposing gRPC 9010 and REST 9020.
- `gcr.io/google.com/cloudsdktool/google-cloud-cli:emulators`, exposing 8086. GitHub service definitions have no `command` field, so the SDK image's shell is kept alive with interactive/TTY options and `docker exec -d` starts `gcloud beta emulators bigtable start --host-port=0.0.0.0:8086 --quiet`.

The image tags, image entrypoint behavior, and live startup were **not verified locally**: this machine has no Docker/emulator and network use was prohibited. The tags are moving tags; pin the successful CI images to reviewed digests when reproducibility is required. No image version has been invented or claimed tested.

The job sets `SPANNER_EMULATOR_HOST=127.0.0.1:9010`, `BIGTABLE_EMULATOR_HOST=127.0.0.1:8086`, and `TR_CONFORMANCE_EMULATOR_SCHEMA=1`. Readiness is bounded to 60 seconds. Missing/unreachable endpoints after explicit opt-in fail; they cannot silently skip the CI gate. Only loopback endpoints and a fixed synthetic project are accepted. No cloud credentials are required.

[Provisioning](../../tests/conformance/spanner_emulator.py) calls the installed SDK's `spanner.Client(..., credentials=AnonymousCredentials()).instance(..., configuration_name="projects/tr-conformance/instanceConfigs/emulator-config").create().result(timeout=60)` and `instance.database("conformance", ddl_statements=DDL[:20]).create().result(timeout=120)`. These invoke `InstanceAdminClient.create_instance` and `DatabaseAdminClient.create_database` with `extra_statements`; remaining DDL is submitted via `database.update_ddl(...).result(timeout=120)` in ordered batches of at most 20. GoogleSQL is the default dialect. Bigtable uses `bigtable.Client(..., admin=True, credentials=AnonymousCredentials()).instance(...).table("generations").create(column_families=...)`, invoking the table-admin `create_table` API. The emulator implicitly namespaces the Bigtable instance; no production cluster/profile is created. Families are `m`, `activity`, `benchmark`, `synthetic`, and `rollup`. Retention GC scheduling is not part of the Store behavioral contract.

Each conformance test gets disposable instance/database/table names; cleanup closes the store's Spanner session pool, deletes its Bigtable table and drops its Spanner instance. This avoids the production store's intentionally forbidden `reset()`. Acceptance cases share a separate module-scoped disposable database. SELECT results are consumed in snapshots. DML uses `database.session()`, `session.create()`, `session.transaction()`, `transaction.begin()`, and a `finally` rollback plus session deletion. There is no commit path, including on assertion or RPC failure. Batch DML checks status and one count per statement. No required-check configuration changes were made.

Ten legacy-money Store-protocol tests are already documented as adapter gaps in the fake registry because the native store removed the legacy methods. **No new xfail or skip is applied to the emulator.** These tests are expected to expose the same contract failures in the first CI run; this task does not reimplement removed money paths or weaken conformance assertions. The existing eleven fake xfails remain unchanged. The acceptance step runs even after a conformance failure, so contract gaps cannot prevent SQL diagnostics. All SQL acceptance cases fail normally on any rejection.

## Statement coverage and emulator limitations

See the [complete per-statement inventory](spanner-sql-inventory.md) for SELECT/DML grouping, named parameter types, sensitive features, source links, and actual batch shapes. The scanner covers all `storage_gcp*.py` SQL expressions, complete enclosing function fingerprints (normalized across Python AST versions), SQL dispatch scopes, and `*_statement`/`*_statements`/`*_sql` builders. New indirect calls, changed builders and literals fail the offline guard until registered. The current SQL is evaluated/imported at test time; the manifest does not freeze a second copy of it.

The runtime cases include both authorize batch alternatives (typed/legacy, speculative/sequential), claim/reaper/retention variants, strict windows, current/rolled key windows including BYOK and imported window amounts, both authorization INSERT variants, generation/operational outbox writes, full 32-shard freshness queries, enqueue batches, optional settlement metadata combinations, `_SETTLED_PAYLOAD_SQL`, and the guarded done UPDATE with THEN RETURN. `_API_KEY_AUTH_CONTEXT_SQL` and all other literal SELECT/DML expressions are included in the source inventory. Types used are STRING, INT64, BOOL, TIMESTAMP and ARRAY<STRING> (the inventory lists each binding).

**No current emulator limitation document was available locally, and none was fetched under the no-network instruction. Therefore this report does not label any current statement “documented unsupported” on the basis of an unverified recollection.** These are the primary references for the first CI review, not sources read during this implementation: [emulator overview and limitations](https://cloud.google.com/spanner/docs/emulator), [emulator README](https://github.com/GoogleCloudPlatform/cloud-spanner-emulator/blob/master/README.md), and [Bigtable emulator](https://cloud.google.com/bigtable/docs/emulator).

Per-statement support remains unverified for every JSON function, named argument, THEN RETURN, PENDING_COMMIT_TIMESTAMP, and FORCE_INDEX use listed in the inventory. The full production DDL, including all row-deletion policies and generated columns, is submitted unchanged; no unsupported DDL is silently removed. An unsupported statement is a named CI failure, not a skip or blanket xfail. TTL background expiry, query plans, IAM/TLS, production contention, optimizer choices, exact staleness, and production resource limits are not demonstrated by the offline tests or by simple SQL acceptance.

Three server canaries require InvalidArgument for row-dependent JSON_SET `create_if_missing`, row-dependent JSON_REMOVE paths, and a statement containing over 1000 IF calls. Three positive controls verify that literal JSON paths/options and a smaller function count actually work, so missing functions cannot masquerade as enforcement of the restrictions. If an emulator version accepts any of the invalid cases, CI fails and explicitly exposes the emulator's inability to guard that production rule. That failure needs investigation rather than an exception that makes the check green.

Most inventory cases test analysis/execution with synthetic bindings against empty tables, so they do not establish all value-dependent behavior. The settlement heartbeat cases and done-returning case do seed their target rows and require one affected/returned row. Conformance tests remain the separate behavioral contract.

## Offline evidence and remaining verification

Dependencies were installed with `uv sync --offline --frozen --link-mode copy` from a writable clone of the existing local uv cache. The final full-suite attempt used Python 3.14.6, pytest 9.1.1, google-cloud-spanner 3.69.1, google-cloud-bigtable 2.42.0, and coverage 7.13.5. Lint, mypy and the focused conformance run also passed in the frozen Python 3.11.15 workspace environment. Inventory fingerprints were verified across both Python AST versions.

| Local check | Result |
|---|---|
| `uv run --offline --frozen ruff check .` | Passed |
| `uv run --offline --frozen mypy` | Passed: 400 source files |
| Schema, SQL inventory, binding, builder, provisioning, rollback and mutation guards | 13 passed; 492 live-server cases explicitly skipped |
| Complete conformance suite plus eight timeout retries from an earlier full attempt | 278 passed, 933 skipped, 11 xfailed; includes 270 conformance/guard passes and eight passing retries |
| CI workflow guards after adding the focused native job | 4 passed |
| Final full-suite coverage attempt | **Blocked:** 1 failed, 5,581 passed, 935 skipped, 12 xfailed after 38m15s |
| Complete-suite coverage ≥70% | **Unverified**; the full run did not complete |

The final full command was `pytest -q --maxfail=1 --cov=trusted_router --cov-report=term --cov-fail-under=70`, run serially through the frozen Python 3.14 environment with `COVERAGE_CORE=ctrace` and `TR_CHECK_LIVE_OPENROUTER=0`. Its blocker is `tests/test_gateway_reuse_probe.py::test_attested_gateway_reuse_is_measured_on_the_route_it_keeps_warm`: the sandbox rejects its local test-server bind to `127.0.0.1:0` with `PermissionError: [Errno 1] operation not permitted`. This test was not modified or skipped to bypass the restriction. Partial coverage is not presented as a passing measurement.

Earlier parallel attempts encountered HTTP request-body and mocked release-subprocess timeouts; all eight initial timeout failures passed in a subsequent serial retry. Python 3.11 also cannot parse an existing unrelated PEP 695 test fixture, so the full run used Python 3.14. The serial run exposed and fixed one change-related failure: the existing workflow guard globally assumed exactly two pytest commands. It now protects the two full-suite jobs while separately verifying the new job's services, opt-in, focused commands, and acceptance step after a conformance failure.

Copy-based mutation guards append an unregistered SQL literal, add an unregistered builder, change `kind STRING(64)` to INT64 in a copied DDL module, and add an unknown migration idiom. Each triggers the corresponding assertion; originals are untouched. Missing-emulator skips explicitly say that no SQL was validated.

Pending an unrestricted CI run: complete repository-suite execution and the ≥70% coverage gate; both emulator image startup paths; every admin RPC and production DDL operation; real Spanner and Bigtable conformance; every SELECT/DML/batch execution and rollback against the server; all three production-rejection canaries and their positive controls; and current documented feature-support classification. No emulator or production execution is claimed locally. No network access, repository commit, push, deployment, fake modification, or required-check configuration change was performed.

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
