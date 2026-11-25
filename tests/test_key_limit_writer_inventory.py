"""Every writer of tr_key_limit, held to a reviewed list, and the limit
columns written only by the guarded functions.

A strict key's tr_key_limit limits (limit_micro, day/week/month_limit_micro)
never go from every one unset to any set: Python's strict reserve and the fast
path edge read them, and a limitless strict key is what the edge may decide
without a key hold (docs/design/fast-admission-e2-strict-keys.md). That rests
on a runtime chokepoint, one guarded function per backend that refuses the
change in the writing transaction:

- Spanner: storage_gcp_key_limit_mirror.write_key_limit_rows
- Postgres: PostgresStore._write_key_limit_caps_tx

tests/key_limit_writers.py finds every place that names tr_key_limit (in any
case, quoting, string construction, constant or variable) or writes a table it
cannot resolve, in Python, shell, SQL, Terraform, YAML and the fast path's Go.
Each is listed below with why it cannot break the rule; a new one fails until
it is reviewed and listed, and a listed one that is gone fails too. The scopes
that may write a limit column must be exactly the chokepoints and the
documented exceptions, each pinned by its own test.
"""

from __future__ import annotations

import functools
import textwrap
from pathlib import Path
from typing import Any

import pytest

from tests.fakes.spanner import _ParamTypes
from tests.key_limit_writers import (
    ROOT,
    ScanDetail,
    limit_column_writers,
    repository_paths,
    scan_paths,
    scan_paths_detail,
    scan_repository_detail,
    written_columns,
)


@functools.cache
def _detail() -> ScanDetail:
    return scan_repository_detail()


_COUNTER_DML = "assigns usage, reserved or window usage only; never a limit column"
_DDL = "the table's schema (DDL) only"

CHOKEPOINTS = {
    ("src/trusted_router/storage_gcp_key_limit_mirror.py", "write_key_limit_rows"),
    ("src/trusted_router/storage_postgres.py", "PostgresStore._write_key_limit_caps_tx"),
}

#: Scopes outside the chokepoints that write a limit column, each with the test
#: that pins why it cannot give a limitless strict key a limit.
LIMIT_COLUMN_EXCEPTIONS = {
    ("scripts/expand_billing_shards.py", "mutations"): (
        "an operator REST script (no Python Spanner client to route through the chokepoint) that inserts NEW "
        "shard rows, never overwriting a row, with the window caps the key's rows already hold, and returns them "
        "to execute, which commits them; make_plan refuses a strict key first "
        "(tests/test_strict_key_first_limit.py::test_shard_expansion_refuses_a_strict_key, "
        "test_shard_expansion_mutations_insert_new_shards_with_the_caps_they_already_hold)"
    ),
    ("scripts/backfill_leased_key_usage.py", "update_sql"): (
        "its SET list is built at run time from usage and window usage only; it returns the statement to the "
        "script, which runs it with gcloud "
        "(tests/test_strict_key_first_limit.py::test_the_leased_usage_backfill_assigns_no_limit_column)"
    ),
    ("src/trusted_router/storage_gcp_counter_dml.py", "release_key_statement"): (
        "its window SQL is built with str.replace in a loop, which the scanner cannot resolve, so the "
        "statement holds a part filled in at run time and fails closed; every statement it returns, in each "
        "mode, assigns reserved, usage or byok_usage and window usage only, and goes back to the release that "
        "runs it (tests/test_strict_key_first_limit.py::test_the_key_release_assigns_no_limit_column)"
    ),
    # Review round 8: scopes the backstop counts as limit writers, each read
    # and pinned by running it.
    ('src/trusted_router/storage_gcp_counter_dml.py', 'reserve_key_statement'): (
        'the capped-key hold: its UPDATE assigns reserved only and reads limit_micro in its WHERE (backstop); it returns the statement to reserve_key, which runs it (escape) (tests/test_key_limit_writer_exceptions.py::test_the_key_reserve_statement_assigns_reserved_only)'
    ),
    ('src/trusted_router/storage_gcp_counter_dml.py', 'reserve_key'): (
        'runs reserve_key_statement and reads limit_micro back to classify a refusal (backstop) (tests/test_key_limit_writer_exceptions.py::test_the_key_reserve_writes_no_limit)'
    ),
    ('src/trusted_router/storage_gcp_strict_budget.py', 'reserve_strict_key'): (
        'the strict hold: its UPDATE assigns reserved only, and its WHERE reads each limit column (backstop) (tests/test_key_limit_writer_exceptions.py::test_the_strict_reserve_writes_no_limit)'
    ),
    ('src/trusted_router/storage_postgres.py', 'PostgresStore.reserve_key_limit.reserve'): (
        "the Postgres hold: reads the key's limits and assigns reserved only (backstop) (tests/test_key_limit_writer_exceptions.py::test_the_postgres_reserve_writes_no_limit)"
    ),
    ('src/trusted_router/storage_gcp_key_shard_admin.py', 'reshard_key_usage.txn'): (
        'writes the partitioned limits only through write_key_limit_rows, the chokepoint, and refuses a strict key first (backstop) (tests/test_key_limit_writer_exceptions.py::test_the_key_reshard_writes_limits_only_through_the_chokepoint)'
    ),
    ('src/trusted_router/storage_gcp_speculation_shadow.py', 'SpannerSpeculationShadow.resolve'): (
        "reads the key's limit rows in a read-only snapshot, and the key's policy fields by computed name "
        "(getattr, which fails closed since review round 9); its writes are the shadow's own records and "
        "dict.update (tests/test_key_limit_writer_exceptions.py::test_the_speculation_shadow_resolve_only_reads_tr_key_limit)"
    ),
    ('scripts/backfill_leased_key_usage.py', 'read_key_counters'): (
        'a SELECT with the key hash interpolated after a [A-Za-z0-9_-] check (an unknown part at a call), given to _sql, which runs gcloud (escape) (tests/test_key_limit_writer_exceptions.py::test_the_leased_usage_backfill_reads_key_counters_only)'
    ),
    # Review round 9: judged with the functions nested in it (read_counters
    # reads limit_micro, txn writes through the chokepoint).
    ('src/trusted_router/storage_gcp_key_escrow.py', 'rebalance_key_limit_headroom.read_counters'): (
        "moves a capped key's escrow between its shards only through write_key_limit_rows, the chokepoint, "
        "keeping their sum, and writes nothing for a key with any shard's limit unset (backstop); its nested "
        "read_counters raises messages naming the table (escape; review round 10 moves the finding there) "
        "(tests/test_key_limit_writer_exceptions.py::test_the_key_escrow_moves_limits_only_through_the_chokepoint)"
    ),
    # Review round 10: scopes the escape rule counts as limit writers (the
    # table's name or a statement leaving the scope other than by an
    # executor, a mutation or a followed helper), each run by its pin.
    ('scripts/expand_billing_shards.py', 'Reader.plan'): (
        "gives the table's name to Reader.read, a Spanner read request; every request it makes is a read and its one "
        "statement a SELECT (escape) "
        "(tests/test_key_limit_writer_exceptions.py::test_the_shard_expansion_reader_only_reads_tr_key_limit)"
    ),
    ('scripts/fastpath/authorize_vectors.py', 'stored'): (
        "records the world the Go edge reads from the in-memory fake, the table's name a key of its rows; run on "
        "read-only mappings (escape) "
        "(tests/test_key_limit_writer_exceptions.py::test_the_authorize_vectors_record_only_reads_tr_key_limit)"
    ),
    ('scripts/deploy/replay_orphan_writes.py', 'key_traces'): (
        "returns the traces a target holds of a key, one labelled with the table's name; every statement it runs "
        "is a SELECT (escape) (tests/test_key_limit_writer_exceptions.py::test_the_orphan_replay_trace_check_only_reads)"
    ),
    ('src/trusted_router/storage_gcp_authorize.py', '_log_missing_key_releases'): (
        "logs a message naming the table and touches no database (escape) "
        "(tests/test_key_limit_writer_exceptions.py::test_the_missing_key_release_log_only_logs)"
    ),
    ('src/trusted_router/storage_gcp_counters.py', 'key_limit_mirror_rows'): (
        'builds the mirror rows the chokepoint writes, and raises a message naming the table on an incomplete shard set; it writes nothing (escape) (tests/test_key_limit_writer_exceptions.py::test_the_key_mirror_rows_raise_a_message_naming_the_table)'
    ),
    ('src/trusted_router/storage_key_usage.py', 'api_key_usage_snapshot'): (
        'sums the rows it is given for display, and raises a message naming the table on an incomplete set (escape) (tests/test_key_limit_writer_exceptions.py::test_the_key_usage_snapshot_raises_a_message_naming_the_table)'
    ),
    ('src/trusted_router/storage_gcp_authorize.py', 'key_window_limit_decision'): (
        "reads the key's window rows in a snapshot, and raises a message naming the table on an incomplete set (escape) (tests/test_key_limit_writer_exceptions.py::test_the_key_window_decision_only_reads_and_raises_a_message)"
    ),
    ('src/trusted_router/storage_gcp.py', 'SpannerStore.typed_key_usage'): (
        "reads the key's rows in a snapshot, and raises a message naming the table on an incomplete set (escape) (tests/test_key_limit_writer_exceptions.py::test_the_typed_key_usage_only_reads_and_raises_a_message)"
    ),
    # Review round 11: a value naming the table given to code it calls (a
    # container's __getitem__, a parameter's __eq__).
    ('scripts/fastpath/authorize_vectors.py', 'build_world'): (
        "builds a golden-vector world in the in-memory fake it makes, indexing the fake's rows by the table's name, and on purpose changes a strict key's mirrored limit behind the entity's back (a golden case); never a database (escape) (tests/test_key_limit_writer_exceptions.py::test_the_authorize_vectors_world_is_built_in_the_fake)"
    ),
    ('scripts/fastpath/key_window_vectors.py', '_Snapshot.execute_sql'): (
        'a read-only test double that compares the statement it is given with the SELECT it expects (escape) (tests/test_key_limit_writer_exceptions.py::test_the_key_window_vectors_snapshot_only_answers_its_select)'
    ),
    ('scripts/stress_credit_shards.py', 'run_stress'): (
        "reads the in-memory fake's rows by the table's name after a stress run on that fake (escape) (tests/test_key_limit_writer_exceptions.py::test_the_credit_stress_reads_only_its_fake)"
    ),
    ('fastpath/schema/fastpath.sql', '<file>'): (
        "the fast path's schema: its write words are other tables' ON DELETE CASCADE (backstop) (tests/test_key_limit_writer_exceptions.py::test_the_fast_path_schema_only_defines_tr_key_limit)"
    ),
    ('scripts/deploy/migrate_typed_counters.sh', '<file>'): (
        "the typed-counter DDL migration: its one write word is gcloud's ddl update (backstop) (tests/test_key_limit_writer_exceptions.py::test_the_typed_counter_migration_only_defines_tr_key_limit)"
    ),
}

#: The source each exception was reviewed at (tests/key_limit_writers.py,
#: ScanDetail.fingerprints). A change to an exception's scope fails the
#: chokepoint check until it is reviewed again: rerun its pin test, then
#: update the fingerprint here.
REVIEWED_FINGERPRINTS = {
    ('scripts/expand_billing_shards.py', 'mutations'): "02312030ba9571412d38f595c3369e0d822f6309f350d9329ca0b5fcaf94eb22",
    ('scripts/backfill_leased_key_usage.py', 'update_sql'): "af384bcd0d2a7878a389a13214ae58a0f7b1f4d463283a5456421d449645b2b7",
    ('src/trusted_router/storage_gcp_counter_dml.py', 'release_key_statement'): "f43b7848bb2eb8558ee5f65d110a97b671c3caec9116ab47991f0ee977481d1d",
    ('src/trusted_router/storage_gcp_counter_dml.py', 'reserve_key_statement'): "3855ad0e192a7e2738c872c579e22ad686fa9fbf720280e5140edc30ead8820c",
    ('src/trusted_router/storage_gcp_counter_dml.py', 'reserve_key'): "9d548d5a2d1fe323bd268a853b5b6b4782bdf13bfa3c604c6a090e840fccab42",
    ('src/trusted_router/storage_gcp_strict_budget.py', 'reserve_strict_key'): "3d24b509fb73e3daaf39a79af9cb47abe527eb4b6e005bda7c4c7452aecba872",
    ('src/trusted_router/storage_postgres.py', 'PostgresStore.reserve_key_limit.reserve'): "eb608a54c788aa5cc60c0a28b35671d8ad0566707bfe8c813c616053e296f584",
    ('src/trusted_router/storage_gcp_key_shard_admin.py', 'reshard_key_usage.txn'): "0ab5dd1203ecd1e7426f00f4216147cef7efb96d45889431f83a5fc687e7c9a7",
    ('src/trusted_router/storage_gcp_speculation_shadow.py', 'SpannerSpeculationShadow.resolve'): "28252a418604bdebd80aa1bcb42844f9373430126268aea2458d620a7fb0e3a3",
    ('scripts/backfill_leased_key_usage.py', 'read_key_counters'): "1442b6c8270aa5edb722f0eb6c045c1d403d9aadb0bed1b74301cdffc99919b5",
    ('src/trusted_router/storage_gcp_key_escrow.py', 'rebalance_key_limit_headroom.read_counters'): "81f77a1f338a4affda0c3e180de41e555d5a596b5cfb8c95630616ce0f74f55a",
    ('scripts/expand_billing_shards.py', 'Reader.plan'): "ec1bca6cb2f5a7765c27eef7755a48f0158758c955780c9c43fa117259d67574",
    ('scripts/fastpath/authorize_vectors.py', 'stored'): "23e565091797ad8103a639591981567d2861811882c34973a5253d49b84ef1e4",
    ('scripts/deploy/replay_orphan_writes.py', 'key_traces'): "27328581f49a9af1d57da7abb551dea3f11158f4987d09f72bfbb62865282c2e",
    ('src/trusted_router/storage_gcp_authorize.py', '_log_missing_key_releases'): "2c5e263b4ca1cdccc63a67355478346cfd84ff3a4dc6ee767c2beac30f1b40c1",
    ('src/trusted_router/storage_gcp_counters.py', 'key_limit_mirror_rows'): "6b6ce28598f6101740c081d57350b0253df69eb4e0963f56a93f95b6438c605c",
    ('src/trusted_router/storage_key_usage.py', 'api_key_usage_snapshot'): "4683ac05c272a0173e6130b0d375e609e238bfa8ed38495620fc3fef3011a542",
    ('src/trusted_router/storage_gcp_authorize.py', 'key_window_limit_decision'): "f53e6c86d4f78f815b7caf7d2f5fa87a2d11f3b1def5b846f5a6fc3e2c45cdeb",
    ('src/trusted_router/storage_gcp.py', 'SpannerStore.typed_key_usage'): "532ba62a4fdfcd1ce6b0b30adcdcb8701dcfe2865b90df6bf8b366ca3b01911a",
    ('scripts/fastpath/authorize_vectors.py', 'build_world'): "b7aed4af6bcc827a638706e1e127f8d7075206db8e02319a6099b4523711fe5f",
    ('scripts/fastpath/key_window_vectors.py', '_Snapshot.execute_sql'): "2147f0aeee09c61d52aa69bd1663ba11eabed6f548320f961aa40fd0b0f127bb",
    ('scripts/stress_credit_shards.py', 'run_stress'): "7bd0e15ab3a45a4e9601fac8b804d76cb52fc1ed50ef01187dac8f76a3c50017",
    ('fastpath/schema/fastpath.sql', '<file>'): "ea907766c7fcad450405fa2499164b874ab95a31f9c3b0dbf738ee6cadb7b5ff",
    ('scripts/deploy/migrate_typed_counters.sh', '<file>'): "662d7925229da597a7ddfa947f07f70bfdffef9b4bb0438ce05512f9b85ec465",
}

KEY_LIMIT_WRITERS: dict[tuple[str, str, str], str] = {
    # The chokepoints.
    ("key_limit.name", "src/trusted_router/storage_gcp_key_limit_mirror.py", "write_key_limit_rows"): (
        "the Spanner chokepoint"
    ),
    ("key_limit.sql", "src/trusted_router/storage_postgres.py", "PostgresStore._write_key_limit_caps_tx"): (
        "the Postgres chokepoint"
    ),
    ("key_limit.name", "src/trusted_router/storage_gcp_counters.py", "<module>.KEY_LIMIT_TABLE"): (
        "defines the table name; every scope that uses it is a finding of its own"
    ),
    # Counter DML: no limit column assigned.
    ("key_limit.sql", "src/trusted_router/storage_gcp_counter_dml.py", "reserve_key_statement"): (
        "a limit-column exception: " + LIMIT_COLUMN_EXCEPTIONS[('src/trusted_router/storage_gcp_counter_dml.py', 'reserve_key_statement')]
    ),
    ("key_limit.sql", "src/trusted_router/storage_gcp_counter_dml.py", "reserve_key"): (
        "a limit-column exception: " + LIMIT_COLUMN_EXCEPTIONS[('src/trusted_router/storage_gcp_counter_dml.py', 'reserve_key')]
    ),
    ("key_limit.sql", "src/trusted_router/storage_gcp_counter_dml.py", "release_key_statement"): (
        "a limit-column exception: " + LIMIT_COLUMN_EXCEPTIONS[("src/trusted_router/storage_gcp_counter_dml.py",
                                                                 "release_key_statement")]
    ),
    ("key_limit.sql", "src/trusted_router/storage_gcp_strict_budget.py", "reserve_strict_key"): (
        "a limit-column exception: " + LIMIT_COLUMN_EXCEPTIONS[('src/trusted_router/storage_gcp_strict_budget.py', 'reserve_strict_key')]
    ),
    ("key_limit.sql", "src/trusted_router/storage_postgres.py", "PostgresStore._release_key_hold_tx"): _COUNTER_DML,
    ("key_limit.sql", "src/trusted_router/storage_postgres.py",
     "PostgresStore.finalize_gateway_authorization.finalize"): _COUNTER_DML,
    ("key_limit.sql", "src/trusted_router/storage_postgres.py", "PostgresStore.reserve_key_limit.reserve"): (
        "a limit-column exception: " + LIMIT_COLUMN_EXCEPTIONS[('src/trusted_router/storage_postgres.py', 'PostgresStore.reserve_key_limit.reserve')]
    ),
    ("key_limit.indirect", "src/trusted_router/storage_gcp_speculation_shadow.py", "SpannerSpeculationShadow.resolve"): (
        "a limit-column exception: " + LIMIT_COLUMN_EXCEPTIONS[('src/trusted_router/storage_gcp_speculation_shadow.py', 'SpannerSpeculationShadow.resolve')]
    ),
    ("key_limit.sql", "scripts/backfill_leased_key_usage.py", "read_key_counters"): (
        "a limit-column exception: " + LIMIT_COLUMN_EXCEPTIONS[('scripts/backfill_leased_key_usage.py', 'read_key_counters')]
    ),
    ("key_limit.name", "src/trusted_router/storage_gcp_counter_reconcile.py", "repair_typed_reserved._txn"): (
        "repairs reserved only (its columns are key_hash, shard, reserved, updated_at)"
    ),
    ("key_limit.sql", "scripts/backfill_leased_key_usage.py", "update_sql"): (
        "a limit-column exception: " + LIMIT_COLUMN_EXCEPTIONS[("scripts/backfill_leased_key_usage.py",
                                                                 "update_sql")]
    ),
    # Row deletes: a deleted row has no limit to gain.
    ("key_limit.sql", "src/trusted_router/storage_postgres.py", "PostgresStore._delete_keys_batch.delete"): (
        "deletes a deleted key's row (Postgres)"
    ),
    ("key_limit.name", "src/trusted_router/storage_gcp_key_shard_admin.py", "reshard_key_usage.txn"): (
        "a limit-column exception: " + LIMIT_COLUMN_EXCEPTIONS[('src/trusted_router/storage_gcp_key_shard_admin.py', 'reshard_key_usage.txn')]
    ),
    ("key_limit.escape", "src/trusted_router/storage_gcp_key_escrow.py", "rebalance_key_limit_headroom.read_counters"): (
        "a limit-column exception: " + LIMIT_COLUMN_EXCEPTIONS[('src/trusted_router/storage_gcp_key_escrow.py',
                                                                 'rebalance_key_limit_headroom.read_counters')]
    ),
    ("key_limit.name", "scripts/cleanup_smoke_signups.py", "main.txn"): (
        "deletes synthetic smoke signups whole, key and rows together"
    ),
    # Operator scripts.
    ("key_limit.name", "scripts/expand_billing_shards.py", "mutations"): (
        "a limit-column exception: " + LIMIT_COLUMN_EXCEPTIONS[("scripts/expand_billing_shards.py", "mutations")]
    ),
    ("key_limit.name", "scripts/expand_billing_shards.py", "Reader.plan"): (
        "a limit-column exception: " + LIMIT_COLUMN_EXCEPTIONS[('scripts/expand_billing_shards.py', 'Reader.plan')]
    ),
    # Fixtures and load tests (the in-memory fake only).
    ("key_limit.name", "scripts/fastpath/authorize_vectors.py", "build_world"): (
        "a limit-column exception: " + LIMIT_COLUMN_EXCEPTIONS[('scripts/fastpath/authorize_vectors.py', 'build_world')]
    ),
    ("key_limit.name", "scripts/fastpath/authorize_vectors.py", "stored"): (
        "a limit-column exception: " + LIMIT_COLUMN_EXCEPTIONS[('scripts/fastpath/authorize_vectors.py', 'stored')]
    ),
    ("key_limit.name", "scripts/stress_credit_shards.py", "run_stress"): (
        "a limit-column exception: " + LIMIT_COLUMN_EXCEPTIONS[('scripts/stress_credit_shards.py', 'run_stress')]
    ),
    # Review round 10: a value naming the table leaving its scope.
    ("key_limit.escape", 'scripts/backfill_leased_key_usage.py', 'read_key_counters'): (
        "a limit-column exception: " + LIMIT_COLUMN_EXCEPTIONS[('scripts/backfill_leased_key_usage.py', 'read_key_counters')]
    ),
    ("key_limit.escape", 'scripts/backfill_leased_key_usage.py', 'update_sql'): (
        "a limit-column exception: " + LIMIT_COLUMN_EXCEPTIONS[('scripts/backfill_leased_key_usage.py', 'update_sql')]
    ),
    ("key_limit.escape", 'scripts/deploy/replay_orphan_writes.py', 'key_traces'): (
        "a limit-column exception: " + LIMIT_COLUMN_EXCEPTIONS[('scripts/deploy/replay_orphan_writes.py', 'key_traces')]
    ),
    ("key_limit.escape", 'scripts/expand_billing_shards.py', 'Reader.plan'): (
        "a limit-column exception: " + LIMIT_COLUMN_EXCEPTIONS[('scripts/expand_billing_shards.py', 'Reader.plan')]
    ),
    ("key_limit.escape", 'scripts/expand_billing_shards.py', 'mutations'): (
        "a limit-column exception: " + LIMIT_COLUMN_EXCEPTIONS[('scripts/expand_billing_shards.py', 'mutations')]
    ),
    ("key_limit.escape", 'scripts/fastpath/authorize_vectors.py', 'stored'): (
        "a limit-column exception: " + LIMIT_COLUMN_EXCEPTIONS[('scripts/fastpath/authorize_vectors.py', 'stored')]
    ),
    ("key_limit.escape", 'src/trusted_router/storage_gcp_authorize.py', '_log_missing_key_releases'): (
        "a limit-column exception: " + LIMIT_COLUMN_EXCEPTIONS[('src/trusted_router/storage_gcp_authorize.py', '_log_missing_key_releases')]
    ),
    ("key_limit.escape", 'src/trusted_router/storage_gcp_counter_dml.py', 'release_key_statement'): (
        "a limit-column exception: " + LIMIT_COLUMN_EXCEPTIONS[('src/trusted_router/storage_gcp_counter_dml.py', 'release_key_statement')]
    ),
    ("key_limit.escape", 'src/trusted_router/storage_gcp_counter_dml.py', 'reserve_key_statement'): (
        "a limit-column exception: " + LIMIT_COLUMN_EXCEPTIONS[('src/trusted_router/storage_gcp_counter_dml.py', 'reserve_key_statement')]
    ),
    ("key_limit.escape", 'src/trusted_router/storage_gcp_counters.py', 'key_limit_mirror_rows'): (
        "a limit-column exception: " + LIMIT_COLUMN_EXCEPTIONS[('src/trusted_router/storage_gcp_counters.py', 'key_limit_mirror_rows')]
    ),
    ("key_limit.escape", 'src/trusted_router/storage_key_usage.py', 'api_key_usage_snapshot'): (
        "a limit-column exception: " + LIMIT_COLUMN_EXCEPTIONS[('src/trusted_router/storage_key_usage.py', 'api_key_usage_snapshot')]
    ),
    ("key_limit.escape", 'src/trusted_router/storage_gcp_authorize.py', 'key_window_limit_decision'): (
        "a limit-column exception: " + LIMIT_COLUMN_EXCEPTIONS[('src/trusted_router/storage_gcp_authorize.py', 'key_window_limit_decision')]
    ),
    ("key_limit.escape", 'src/trusted_router/storage_gcp.py', 'SpannerStore.typed_key_usage'): (
        "a limit-column exception: " + LIMIT_COLUMN_EXCEPTIONS[('src/trusted_router/storage_gcp.py', 'SpannerStore.typed_key_usage')]
    ),
    ("key_limit.escape", "src/trusted_router/storage_gcp_key_limit_mirror.py", "write_key_limit_rows"): (
        "the Spanner chokepoint: its error messages name the table"
    ),
    ("key_limit.escape", 'scripts/fastpath/authorize_vectors.py', 'build_world'): (
        "a limit-column exception: " + LIMIT_COLUMN_EXCEPTIONS[('scripts/fastpath/authorize_vectors.py', 'build_world')]
    ),
    ("key_limit.escape", 'scripts/fastpath/key_window_vectors.py', '_Snapshot.execute_sql'): (
        "a limit-column exception: " + LIMIT_COLUMN_EXCEPTIONS[('scripts/fastpath/key_window_vectors.py', '_Snapshot.execute_sql')]
    ),
    ("key_limit.escape", 'scripts/stress_credit_shards.py', 'run_stress'): (
        "a limit-column exception: " + LIMIT_COLUMN_EXCEPTIONS[('scripts/stress_credit_shards.py', 'run_stress')]
    ),
    # Other files.
    ("key_limit.text", "fastpath/internal/store/gatewaykey.go", "<file>"): (
        "the edge's read of a strict key's shard 0 (ReadRowWithOptions)"
    ),
    ("key_limit.text", "fastpath/internal/store/keywindows.go", "<file>"): (
        "the edge's read of a key's window rows (ReadWithOptions)"
    ),
    ("key_limit.text", "fastpath/schema/fastpath.sql", "<file>"): (
        "a limit-column exception: " + LIMIT_COLUMN_EXCEPTIONS[('fastpath/schema/fastpath.sql', '<file>')]
    ),
    ("key_limit.text", "src/trusted_router/storage_postgres_schema.sql", "<file>"): _DDL,
    ("key_limit.text", "scripts/deploy/migrate_typed_counters.sh", "<file>"): (
        "a limit-column exception: " + LIMIT_COLUMN_EXCEPTIONS[('scripts/deploy/migrate_typed_counters.sh', '<file>')]
    ),
    ("key_limit.text", "infra/key_usage_alerts.tf", "<file>"): "an alert's documentation text",
    # Mutations and DML whose table is computed: none is tr_key_limit.
    ("mutation.unresolved", "src/trusted_router/fastpath_enabler.py", "_apply"): (
        "applies the enabler's own fastpath table mutations (tr_fastpath_workspace)"
    ),
    ("mutation.unresolved", "src/trusted_router/storage_gcp_speculation_shadow.py", "Transaction.flush"): (
        "the speculation shadow's own tables (table_name), never tr_key_limit"
    ),
    ("mutation.unresolved", "fastpath/internal/switchsql/switchsql.go", "<file>"): (
        "applies the fast path switch's own table mutations"
    ),
    ("sql.dynamic", "src/trusted_router/analytics_sink.py", "ClickHouseAnalyticsSink._post_rows"): (
        "ClickHouse analytics tables"
    ),
    ("sql.dynamic", "src/trusted_router/operational_analytics_direct.py", "DirectOperationalAnalyticsSink.flush"): (
        "ClickHouse analytics tables"
    ),
    ("sql.dynamic", "src/trusted_router/storage_gcp_settle_outbox.py", "done_retention_statements"): (
        "settle outbox retention"
    ),
    ("sql.dynamic", "clickhouse/ingest_operational_outbox.py", "ClickHouseOperationalWriter.insert"): (
        "ClickHouse tables"
    ),
    ("sql.dynamic", "clickhouse/ingest_outbox.py", "ClickHouseWriter.insert"): "ClickHouse tables",
    ("sql.dynamic", "clickhouse/refresh_workspace_directory.py", "ClickHouseDirectoryWriter._insert"): (
        "ClickHouse tables"
    ),
    ("sql.dynamic", "clickhouse/rollup_analytics.py", "recompute_partition"): "ClickHouse tables",
    ("sql.dynamic", "scripts/deploy/clickhouse_capacity_smoke.sh", "<file>"): "ClickHouse tables",
    ("sql.dynamic", "scripts/deploy/clickhouse_replicate_rollups.sh", "<file>"): "ClickHouse tables",
}


def test_every_writer_of_tr_key_limit_is_known() -> None:
    unknown = sorted(_detail().keys() - KEY_LIMIT_WRITERS.keys())
    assert not unknown, (
        "New code that names tr_key_limit, or writes a table the scanner cannot resolve. A limit column is "
        "written only through storage_gcp_key_limit_mirror.write_key_limit_rows (Spanner) or "
        "PostgresStore._write_key_limit_caps_tx (Postgres); list each finding here with why it cannot give a "
        f"limitless strict key a limit: {unknown}"
    )


def test_the_writer_inventory_has_no_dead_entries() -> None:
    dead = sorted(KEY_LIMIT_WRITERS.keys() - _detail().keys())
    assert not dead, f"Listed writers no longer found; remove them: {dead}"


def test_only_the_chokepoints_write_a_limit_column() -> None:
    writers = limit_column_writers(_detail())
    assert writers - CHOKEPOINTS - LIMIT_COLUMN_EXCEPTIONS.keys() == set(), (
        "A limit column of tr_key_limit is written outside the guarded functions; route it through "
        "write_key_limit_rows (Spanner) or _write_key_limit_caps_tx (Postgres)"
    )
    assert writers >= CHOKEPOINTS, "the scanner no longer sees a chokepoint write its limit columns"
    assert LIMIT_COLUMN_EXCEPTIONS.keys() <= writers, "a listed exception no longer writes a limit column"
    assert REVIEWED_FINGERPRINTS.keys() == LIMIT_COLUMN_EXCEPTIONS.keys()
    changed = sorted(scope for scope, fingerprint in REVIEWED_FINGERPRINTS.items()
                     if _detail().fingerprints.get(scope) != fingerprint)
    assert not changed, (
        "A reviewed exception's source changed; review it again, rerun its pin test, and update "
        f"REVIEWED_FINGERPRINTS: {changed}"
    )


def test_the_scan_covers_python_shell_sql_and_go() -> None:
    python, text = repository_paths()
    assert any(p.name == "storage_gcp_key_limit_mirror.py" for p in python)
    assert any(p.parts[-2:] == ("clickhouse", "ingest_outbox.py") for p in python)
    assert not any("tests" in p.relative_to(ROOT).parts for p in python + text)
    assert any(p.suffix == ".sh" for p in text) and any(p.suffix == ".sql" for p in text)
    assert any(p.suffix == ".go" for p in text) and not any(p.name.endswith("_test.go") for p in text)


def _scan_sample(tmp_path: Path, name: str, source: str) -> dict[tuple[str, str], bool]:
    path = tmp_path / name
    path.write_text(textwrap.dedent(source))
    detail = scan_paths_detail([path], []) if path.suffix == ".py" else scan_paths_detail([], [path])
    return {(style, where): limit for (style, _path, where), limit in detail.items()}


# The reviewer's forms the previous inventory missed (review round 2), as
# given. Those that name tr_key_limit or compute their table are found; those
# that name only tr_entities are out of this inventory's scope by design: no
# entity write reaches tr_key_limit's limits except through the chokepoints.
REVIEWER_FORMS = {
    "positional.py": (
        "def f(txn, table, rows):\n    txn.insert_or_update(table, ('kind', 'id', 'body'), rows)\n",
        {("mutation.unresolved", "f"): False},
    ),
    "bulk.py": (
        "def f(txn, rows):\n    args = {'table': 'tr_entities', 'columns': ('kind', 'id', 'body')}\n"
        "    args['values'] = rows\n    txn.insert_or_update(**args)\n",
        {},
    ),
    "helper.py": (
        "def f(store, tx, key):\n    emit = store._write_entity_tx\n    kind = 'api_key'\n"
        "    emit(tx, kind, key.hash, key)\n",
        {},
    ),
    "format.py": (
        "def f(txn):\n    table = 'tr_entities'\n"
        "    txn.execute_update('UPDATE %s SET body=@b WHERE kind=@k AND id=@i' % table)\n",
        {},
    ),
    "variable.sh": (
        "table=tr_key_limit\ngcloud spanner databases execute-sql db "
        "--sql=\"UPDATE $table SET limit_micro=1 WHERE key_hash='k' AND shard=0\"\n",
        # The table, a limit column and a write in one file: the backstop.
        {("key_limit.text", "<file>"): True, ("sql.dynamic", "<file>"): True},
    ),
    "quoted.sql": (
        "UPDATE `tr_key_limit` SET limit_micro=1 WHERE key_hash='k' AND shard=0;\n",
        {("key_limit.text", "<file>"): True},
    ),
    "variable.go": (
        'package example\nimport "cloud.google.com/go/spanner"\nfunc f() { table := "tr_key_limit"; '
        '_ = spanner.InsertOrUpdate(table, []string{"key_hash", "shard", "limit_micro"}, '
        '[]interface{}{"k", 0, 1}) }\n',
        {("key_limit.text", "<file>"): True, ("mutation.unresolved", "<file>"): True},
    ),
}


@pytest.mark.parametrize("name", REVIEWER_FORMS)
def test_the_reviewers_forms(tmp_path: Path, name: str) -> None:
    source, expected = REVIEWER_FORMS[name]
    assert _scan_sample(tmp_path, name, source) == expected


_R12_RUNNER = ("class Runner:\n    def __init__(self, c):\n        self.c = c\n"
               "    def run(self, q):\n        self.c.execute(q + ', day_limit_micro=1')\n        return False\n"
               "    def __getitem__(self, q):\n        return self.run(q)\n    def __radd__(self, q):\n"
               "        return self.run(q)\n    def __lt__(self, q):\n        return self.run(q)\n"
               "    def __eq__(self, q):\n        return self.run(q)\n")
_R12_RUN = "run = lambda c, q: c.execute(q + ', day_limit_micro=1')\n"


def _with(helpers: str, source: str) -> str:
    return helpers + source


# The same constructions aimed at tr_key_limit, and the other ways to name it.
KEY_LIMIT_FORMS = [
    ("positional computed table, limit columns",
     "def f(txn, table, rows):\n    txn.update(table, ('key_hash', 'shard', 'limit_micro'), rows)\n",
     {("mutation.unresolved", "f"): True}),
    ("bulk **kwargs",
     "def f(txn, rows):\n    args = {'table': 'tr_key_limit', 'columns': ('key_hash', 'shard', 'day_limit_micro')}\n"
     "    args['values'] = rows\n    txn.insert_or_update(**args)\n",
     {("key_limit.name", "f"): True}),
    ("bulk **kwargs, table added after",
     "def f(txn, rows):\n    args = {'columns': ('key_hash', 'shard', 'limit_micro'), 'values': rows}\n"
     "    args['table'] = 'TR_KEY_LIMIT'\n    txn.update(**args)\n",
     {("key_limit.name", "f"): True, ("key_limit.escape", "f"): True}),
    ("bulk **kwargs, not known",
     "def f(txn, args):\n    txn.insert_or_update(**args)\n", {("mutation.unresolved", "f"): True}),
    ("aliased mutation",
     "from trusted_router.storage_gcp_counters import KEY_LIMIT_TABLE\n"
     "def f(txn, rows):\n    emit = txn.insert_or_update\n    emit(table=KEY_LIMIT_TABLE, columns=('key_hash', 'shard',"
     " 'week_limit_micro'), values=rows)\nKEY_LIMIT_TABLE = 'tr_key_limit'\n",
     {("key_limit.name", "f"): True, ("key_limit.name", "<module>.KEY_LIMIT_TABLE"): False}),
    ("aliased positional mutation, table not known",
     "def f(txn, t, rows):\n    emit = txn.insert_or_update\n    emit(t, ('key_hash', 'shard', 'limit_micro'), rows)\n",
     {("mutation.unresolved", "f"): True}),
    # Review round 7: SQL the scanner cannot resolve to literals.
    ("augmented SQL", "def f(c):\n    sql = 'UPDATE tr_key_limit SET reserved=0'\n    sql += ', day_limit_micro=1'\n"
     "    c.execute(sql)\n", {("key_limit.sql", "f"): True}),
    ("augmented SQL, predicates only", "def f(c):\n    sql = 'UPDATE tr_key_limit SET reserved=0 WHERE key_hash=%s'\n"
     "    for p in ('day', 'week'):\n        sql += f' AND {p}_usage >= 0'\n    c.execute(sql)\n",
     {("key_limit.sql", "f"): False}),
    ("augmented SQL from an argument", "def f(c, sql):\n    sql += ' WHERE key_hash = %s'\n"
     "    c.execute('UPDATE tr_key_limit SET reserved = 0')\n    c.execute(sql)\n", {("key_limit.sql", "f"): True}),
    ("str.replace SQL", "def f(c):\n    sql = 'UPDATE tr_key_limit SET reserved=0'.replace('reserved=0', "
     "'day_limit_micro=1')\n    c.execute(sql)\n", {("key_limit.sql", "f"): True}),
    ("quoted f-string", "def f(c, fragment):\n    c.execute(f\"UPDATE tr_key_limit SET reserved=length('{fragment}')\")\n",
     {("key_limit.sql", "f"): True}),
    ("quoted f-string, built and returned", "def f(fragment):\n    return f\"UPDATE tr_key_limit SET "
     "reserved=length('{fragment}')\"\n", {("key_limit.sql", "f"): True, ("key_limit.escape", "f"): True}),
    ("% with an unknown argument", "def f(c, x):\n    c.execute('UPDATE tr_key_limit SET reserved=%s' % x)\n",
     {("key_limit.sql", "f"): True, ("key_limit.escape", "f"): True}),
    (".format with an unknown argument", "def f(c, x):\n    c.execute('UPDATE tr_key_limit SET reserved={}'.format(x))\n",
     {("key_limit.sql", "f"): True, ("key_limit.escape", "f"): True}),
    ("a join of non-literals", "def f(c, parts):\n    c.execute('UPDATE tr_key_limit SET ' + ', '.join(parts))\n",
     {("key_limit.sql", "f"): True}),
    ("a read with an interpolated value (an unknown part at a call, whatever is known)",
     "def f(c, k):\n    c.execute(f\"SELECT reserved FROM tr_key_limit WHERE key_hash = '{k}'\")\n",
     {("key_limit.sql", "f"): True}),
    ("an unknown verb before the table", "def f(c, verb):\n    c.execute(f'{verb} tr_key_limit SET limit_micro = 1')\n",
     {("key_limit.sql", "f"): True}),
    ("a statement from an argument, in a scope that writes the table", "def f(c, sql):\n"
     "    c.execute('UPDATE tr_key_limit SET reserved = 0')\n    c.execute(sql)\n", {("key_limit.sql", "f"): True}),
    ("a session setting with an unknown value, in a scope that mentions the table", "def f(c, ms):\n"
     "    c.execute(f'SET LOCAL statement_timeout = {ms}')\n    c.execute('UPDATE tr_key_limit SET reserved = 0')\n",
     {("key_limit.sql", "f"): True}),
    ("the statement timeout, provably an int", "def f(c, remaining):\n"
     "    c.execute(f'SET LOCAL statement_timeout = {max(1, int(remaining * 1000))}')\n"
     "    c.execute('UPDATE tr_key_limit SET reserved = 0')\n", {("key_limit.sql", "f"): False}),
    ("the statement timeout with more after it", "def f(c, remaining, more):\n"
     "    c.execute(f'SET LOCAL statement_timeout = {int(remaining)}; {more}')\n"
     "    c.execute('UPDATE tr_key_limit SET reserved = 0')\n", {("key_limit.sql", "f"): True}),
    # Review round 8: the round-7 reviewer's variants (items 1-6), and
    # unknown parts at any call in a scope that mentions the table.
    ("conditional +=", "def f(c, comment=False):\n    sql = 'UPDATE tr_key_limit SET reserved=0'\n"
     "    if comment:\n        sql += ' -- no cap'\n    else:\n        sql += ', day_limit_micro=1'\n"
     "    c.execute(sql)\n", {("key_limit.sql", "f"): True}),
    ("named format alternatives", "def f(c, counter=False):\n"
     "    col = 'reserved' if counter else 'day_limit_micro'\n"
     "    c.execute('UPDATE tr_key_limit SET {col}=1'.format(col=col))\n", {("key_limit.sql", "f"): True}),
    ("aliased executor", "def f(c):\n    sql = 'UPDATE tr_key_limit SET reserved=0'\n"
     "    sql += ', day_limit_micro=1'\n    run = c.execute\n    run(sql)\n", {("key_limit.sql", "f"): True}),
    ("aliased executor, an unknown piece", "def add_cap(sql):\n    return sql + ', usage = 0'\n"
     "def f(c):\n    sql = 'UPDATE tr_key_limit SET reserved=0'\n    sql += add_cap('')\n    run = c.execute\n"
     "    run(sql)\n", {("key_limit.sql", "f"): True, ("key_limit.escape", "f"): True}),
    ("batch DML", "def f(c):\n    sql = 'UPDATE tr_key_limit SET reserved=0'\n    sql += ', day_limit_micro=1'\n"
     "    c.batch_update([sql])\n", {("key_limit.sql", "f"): True}),
    ("a SELECT ahead of a helper's statement", "def add_cap(sql):\n    return sql + ', usage = 0'\n"
     "def f(c):\n    sql = 'UPDATE tr_key_limit SET reserved=0'\n    c.execute('SELECT 1; ' + add_cap(sql))\n",
     {("key_limit.sql", "f"): True, ("key_limit.escape", "f"): True}),
    ("SET LOCAL ahead of a helper's statement", "def add_cap(sql):\n    return sql + ', usage = 0'\n"
     "def f(c):\n    sql = 'UPDATE tr_key_limit SET reserved=0'\n"
     "    c.execute('SET LOCAL statement_timeout=1000; ' + add_cap(sql))\n", {("key_limit.sql", "f"): True, ("key_limit.escape", "f"): True}),
    ("mutable mutation payload", "def f(tx):\n    payload = {'update': {'table': 'tr_key_limit', "
     "'columns': ['key_hash', 'shard', 'reserved'], 'values': [('probe', 0, 0, 1)]}}\n"
     "    payload['update']['columns'].append('usage')\n    tx.update(**payload['update'])\n",
     {("key_limit.name", "f"): True}),
    ("a %(name)s mapping with an unknown value", "def f(c, col):\n"
     "    c.execute('UPDATE tr_key_limit SET %(col)s = 1' % {'col': col})\n", {("key_limit.sql", "f"): True, ("key_limit.escape", "f"): True}),
    ("a %(name)s mapping of a known column", "def f(c):\n"
     "    c.execute('UPDATE tr_key_limit SET %(col)s = 1' % {'col': 'reserved'})\n", {("key_limit.sql", "f"): False}),
    (".format(**kw)", "def f(c, kw):\n    c.execute('UPDATE tr_key_limit SET reserved={v}'.format(**kw))\n",
     {("key_limit.sql", "f"): True, ("key_limit.escape", "f"): True}),
    ("a returned statement with an unknown verb", "def f(verb):\n"
     "    return f'{verb} tr_key_limit SET limit_micro = 1'\n", {("key_limit.sql", "f"): True, ("key_limit.escape", "f"): True}),
    ("a returned statement edited by str.replace with an unknown part", "def f(column):\n"
     "    return 'UPDATE tr_key_limit SET reserved=0'.replace('reserved', column)\n", {("key_limit.sql", "f"): True, ("key_limit.escape", "f"): True}),
    ("the backstop sees a limit column split across literals", "def f(store, rows):\n"
     "    store.update('tr_key_limit', ('key_hash', 'day_limit_' + 'micro'), rows)\n",
     {("key_limit.name", "f"): True}),
    ("an aliased executor running a helper's statement", "def helper():\n    return 'x'\n"
     "def f(c):\n    c.execute('SELECT 1 FROM tr_key_limit')\n    run = c.execute\n    run(helper())\n",
     {("key_limit.sql", "f"): True}),
    ("a statement built with += and given to any call", "def f(store, more):\n"
     "    sql = 'UPDATE tr_key_limit SET reserved = 0'\n    sql += more\n    store.run_later(sql)\n",
     {("key_limit.sql", "f"): True, ("key_limit.escape", "f"): True}),
    ("SQL naming the table, given to any call", "def f(store, rhs):\n"
     "    store.run_later(sql='UPDATE tr_key_limit SET reserved=' + rhs)\n", {("key_limit.sql", "f"): True, ("key_limit.escape", "f"): True}),
    ("the backstop: table, limit column and a write in one scope", "def f(rows, store):\n"
     "    table = 'tr_key_limit'\n    column = 'day_limit_micro'\n    store.update(table, column, rows)\n",
     {("key_limit.name", "f"): True}),
    ("the backstop through the table constant", "KEY_LIMIT_TABLE = 'tr_key_limit'\n"
     "def f(store):\n    store.write(Mutation(table=KEY_LIMIT_TABLE, day_limit_micro=1))\n",
     {("key_limit.name", "f"): True, ("key_limit.name", "<module>.KEY_LIMIT_TABLE"): False, ("key_limit.escape", "f"): True}),
    # Spanner mutations: columns written out as literals, or every column.
    ("mutable mutation columns", "def f(tx):\n    cols = ['key_hash', 'shard', 'reserved']\n"
     "    cols.append('day_limit_micro')\n    tx.update('tr_key_limit', cols, [('k', 0, 0, 1)])\n",
     {("key_limit.name", "f"): True}),
    ("a column constant", "COLS = ('key_hash', 'shard', 'reserved')\ndef f(tx):\n"
     "    tx.update(table='tr_key_limit', columns=COLS, values=[])\n", {("key_limit.name", "f"): True}),
    ("a computed column list", "def f(tx, cols):\n    tx.update('tr_key_limit', tuple(cols), [])\n",
     {("key_limit.name", "f"): True}),
    ("a REPLACE mutation (nulls what it omits)", "def f(tx):\n    tx.replace('tr_key_limit', ('key_hash', 'shard', "
     "'reserved'), [('k', 0, 0)])\n", {("key_limit.name", "f"): True}),
    ("insert_or_update keeps the columns it omits", "def f(tx):\n    tx.insert_or_update('tr_key_limit', "
     "('key_hash', 'shard', 'reserved'), [('k', 0, 0)])\n", {("key_limit.name", "f"): False}),
    ("a REPLACE payload", "def f(rows):\n    return [{'replace': {'table': 'tr_key_limit', 'columns': ['key_hash'], "
     "'values': rows}}]\n", {("key_limit.name", "f"): True, ("key_limit.escape", "f"): True}),
    ("dynamic dispatch",
     "def f(txn, verb, rows):\n    getattr(txn, verb)(table='tr_key_limit', columns=COLS, values=rows)\n"
     "COLS = ('key_hash', 'shard', 'month_limit_micro')\n",
     # (and the value escapes: a callee reached by a computed name is no mutation the rule may stop at)
     {("key_limit.name", "f"): True, ("key_limit.indirect", "f"): True, ("key_limit.escape", "f"): True}),
    ("%-formatted SQL",
     "def f(txn):\n    table = 'tr_key_limit'\n"
     "    txn.execute_update('UPDATE %s SET limit_micro=1 WHERE key_hash=@k AND shard=0' % table)\n",
     {("key_limit.name", "f"): True, ("key_limit.sql", "f"): True}),
    ("%-formatted SQL, table not known",
     "def f(txn, table):\n    txn.execute_update('UPDATE %s SET limit_micro=1 WHERE key_hash=@k' % table)\n",
     {("sql.dynamic", "f"): True}),
    ("format() SQL",
     "T = 'tr_key_limit'\ndef f(txn):\n    txn.execute_update('UPDATE {} SET day_limit_micro=1'.format(T))\n",
     {("key_limit.name", "<module>.T"): False, ("key_limit.sql", "f"): True}),
    ("concatenated table name",
     "def f(txn):\n    txn.execute_update('UPDATE tr_' + 'key_limit SET limit_micro=1 WHERE key_hash=@k')\n",
     {("key_limit.sql", "f"): True}),
    ("concatenated SQL, table not known",
     "def f(txn, t):\n    txn.execute_update('UPDATE ' + t + ' SET reserved=0')\n", {("sql.dynamic", "f"): True}),
    ("SQL head, table appended later",
     "def f(txn, t):\n    sql = 'DELETE FROM '\n    sql += t\n    txn.execute_update(sql)\n",
     {("sql.dynamic", "f"): True}),
    ("backtick-quoted, lowercase, lock hint",
     "def f(t):\n    t.execute_update('@{LOCK_SCANNED_RANGES=EXCLUSIVE} update `tr_key_limit` set limit_micro = 1')\n",
     {("key_limit.sql", "f"): True}),
    ("f-string table from a constant",
     "def f(t):\n    t.execute_update(f'UPDATE {KL} SET reserved = 0 WHERE key_hash = @k')\nKL = 'tr_key_limit'\n",
     {("key_limit.sql", "f"): False, ("key_limit.name", "<module>.KL"): False}),
    ("f-string column filled in",
     "def f(t, column):\n    t.execute_update(f'UPDATE tr_key_limit SET {column} = 1')\n",
     {("key_limit.sql", "f"): True}),
    ("f-string column from a known choice",
     "def f(t, byok):\n    column = 'byok_usage' if byok else 'usage'\n"
     "    t.execute_update(f'UPDATE tr_key_limit SET {column} = {column} + @a')\n",
     {("key_limit.sql", "f"): False}),
    ("INSERT columns, then an upsert",
     "def f(c):\n    c.execute('INSERT INTO tr_key_limit (workspace_id, key_hash, shard, month_limit_micro) "
     "VALUES (%s, %s, 0, %s) ON CONFLICT (workspace_id, key_hash, shard) DO NOTHING')\n",
     {("key_limit.sql", "f"): True}),
    ("positional delete",
     "def f(txn, keyset):\n    txn.delete('tr_key_limit', keyset)\n", {("key_limit.name", "f"): False}),
    ("REST payload",
     "def f(rows):\n    return [{'insert': {'table': 'tr_key_limit', 'columns': ['key_hash', *CAPS], 'values': rows}}]\n"
     "CAPS = {'day_limit_micro': 'limit_daily_microdollars'}\n",
     {("key_limit.name", "f"): True, ("key_limit.escape", "f"): True}),
    ("the create batch before the chokepoint",
     "def create(batch, rows):\n    batch.insert_or_update(table=KEY_LIMIT_TABLE, columns=KEY_LIMIT_COLUMNS, values=rows)\n"
     "KEY_LIMIT_TABLE = 'tr_key_limit'\nKEY_LIMIT_COLUMNS = ('key_hash', 'shard', 'limit_micro', 'include_byok')\n",
     {("key_limit.name", "create"): True, ("key_limit.name", "<module>.KEY_LIMIT_TABLE"): False}),
    ("the escrow update before the chokepoint",
     "def txn(t, changed):\n    t.update(table=KEY_LIMIT_TABLE, columns=('key_hash', 'shard', 'limit_micro'), "
     "values=changed)\nKEY_LIMIT_TABLE = 'tr_key_limit'\n",
     {("key_limit.name", "txn"): True, ("key_limit.name", "<module>.KEY_LIMIT_TABLE"): False}),
    # Review round 9. getattr(obj, "name") is read as obj.name (through any
    # alias of getattr); getattr, attrgetter or methodcaller by a computed
    # name, setattr by one, exec and eval, and an executor or mutation API
    # passed on (functools.partial, a decorator applied to it) fail closed in
    # a scope that names the table (key_limit.indirect). A scope is an
    # outermost function with every function, lambda, comprehension and
    # class body nested in it; a helper of the module that runs a statement
    # it is given is followed, and one that may change it fails closed.
    ("getattr mutation (the reviewer's)", "def f(tx):\n    getattr(tx, 'insert_or_update')('tr_key_limit', "
     "('key_hash', 'shard', 'day_limit_micro'), [('k', 0, 1)])\n", {("key_limit.name", "f"): True}),
    ("getattr mutation, counter only", "def f(tx):\n    getattr(tx, 'insert_or_update')('tr_key_limit', "
     "('key_hash', 'shard', 'reserved'), [('k', 0, 1)])\n", {("key_limit.name", "f"): False}),
    ("getattr executor (the reviewer's)", "def add_cap(sql):\n    return sql + ', day_limit_micro=1'\n"
     "def f(c):\n    sql = 'UPDATE tr_key_limit SET reserved=0'\n"
     "    getattr(c, 'execute')(add_cap(sql))\n", {("key_limit.sql", "f"): True, ("key_limit.escape", "f"): True}),
    ("getattr executor, counter only", "def f(c):\n    getattr(c, 'execute')('UPDATE tr_key_limit SET reserved=0')\n",
     {("key_limit.sql", "f"): False}),
    ("an alias of getattr", "def add_cap(sql):\n    return sql + ', day_limit_micro=1'\n"
     "fetch = getattr\ndef f(c):\n    sql = 'UPDATE tr_key_limit SET reserved=0'\n"
     "    fetch(c, 'execute')(add_cap(sql))\n", {("key_limit.sql", "f"): True, ("key_limit.escape", "f"): True}),
    ("getattr imported under another name", "def add_cap(sql):\n    return sql + ', day_limit_micro=1'\n"
     "from builtins import getattr as fetch\ndef f(c):\n"
     "    sql = 'UPDATE tr_key_limit SET reserved=0'\n    fetch(c, 'execute')(add_cap(sql))\n",
     {("key_limit.sql", "f"): True, ("key_limit.escape", "f"): True}),
    ("builtins.getattr, bound to a name", "def add_cap(sql):\n    return sql + ', day_limit_micro=1'\n"
     "import builtins\ndef f(c):\n"
     "    sql = 'UPDATE tr_key_limit SET reserved=0'\n    run = builtins.getattr(c, 'execute')\n    run(add_cap(sql))\n",
     {("key_limit.sql", "f"): True, ("key_limit.escape", "f"): True}),
    ("__getattribute__", "def add_cap(sql):\n    return sql + ', day_limit_micro=1'\n"
     "def f(c):\n    sql = 'UPDATE tr_key_limit SET reserved=0'\n"
     "    c.__getattribute__('execute')(add_cap(sql))\n", {("key_limit.sql", "f"): True, ("key_limit.escape", "f"): True}),
    ("getattr with a default executor", "def add_cap(sql):\n    return sql + ', day_limit_micro=1'\n"
     "def f(c):\n    sql = 'UPDATE tr_key_limit SET reserved=0'\n"
     "    getattr(c, 'missing', c.execute)(add_cap(sql))\n",
     {("key_limit.sql", "f"): True, ("key_limit.indirect", "f"): True, ("key_limit.escape", "f"): True}),
    ("getattr by a computed name", "def f(c, name):\n    sql = 'UPDATE tr_key_limit SET reserved=0'\n"
     "    getattr(c, name)(sql)\n", {("key_limit.sql", "f"): False, ("key_limit.indirect", "f"): True, ("key_limit.escape", "f"): True}),
    ("getattr by a constant", "NAME = 'execute'\n"
     "def add_cap(sql):\n    return sql + ', day_limit_micro=1'\n"
     "def f(c):\n"
     "    sql = 'UPDATE tr_key_limit SET reserved=0'\n    getattr(c, NAME)(add_cap(sql))\n",
     {("key_limit.sql", "f"): False, ("key_limit.indirect", "f"): True, ("key_limit.escape", "f"): True}),
    ("methodcaller imported under another name", "def add_cap(sql):\n    return sql + ', day_limit_micro=1'\n"
     "from operator import methodcaller as call_by_name\ndef f(c):\n    sql = 'UPDATE tr_key_limit SET reserved=0'\n"
     "    call_by_name('execute', add_cap(sql))(c)\n", {("key_limit.sql", "f"): False, ("key_limit.indirect", "f"): True, ("key_limit.escape", "f"): True}),
    ("operator.methodcaller", "def add_cap(sql):\n    return sql + ', day_limit_micro=1'\n"
     "import operator\ndef f(c):\n    sql = 'UPDATE tr_key_limit SET reserved=0'\n"
     "    operator.methodcaller('execute', add_cap(sql))(c)\n",
     {("key_limit.sql", "f"): False, ("key_limit.indirect", "f"): True, ("key_limit.escape", "f"): True}),
    ("attrgetter", "def add_cap(sql):\n    return sql + ', day_limit_micro=1'\n"
     "from operator import attrgetter\ndef f(c):\n"
     "    sql = 'UPDATE tr_key_limit SET reserved=0'\n    attrgetter('execute')(c)(add_cap(sql))\n",
     {("key_limit.sql", "f"): False, ("key_limit.indirect", "f"): True, ("key_limit.escape", "f"): True}),
    ("functools.partial over an executor", "def add_cap(sql):\n    return sql + ', day_limit_micro=1'\n"
     "import functools\ndef f(c):\n"
     "    sql = 'UPDATE tr_key_limit SET reserved=0'\n    functools.partial(c.execute)(add_cap(sql))\n",
     {("key_limit.sql", "f"): False, ("key_limit.indirect", "f"): True, ("key_limit.escape", "f"): True}),
    ("functools.partial over a mutation", "import functools\ndef f(tx, cols):\n"
     "    p = functools.partial(tx.update, 'tr_key_limit', cols)\n    p([('k', 0, 1)])\n",
     {("key_limit.name", "f"): False, ("key_limit.indirect", "f"): True, ("key_limit.escape", "f"): True}),
    ("a setattr-built executor", "def add_cap(sql):\n    return sql + ', day_limit_micro=1'\n"
     "class O:\n    pass\ndef f(c):\n    o = O()\n"
     "    setattr(o, 'run', c.execute)\n    sql = 'UPDATE tr_key_limit SET reserved=0'\n    o.run(add_cap(sql))\n",
     {("key_limit.sql", "f"): True, ("key_limit.indirect", "f"): True, ("key_limit.escape", "f"): True}),
    ("an executor set on an attribute elsewhere", "def add_cap(sql):\n    return sql + ', day_limit_micro=1'\n"
     "class O:\n    def __init__(self, c):\n"
     "        setattr(self, 'run', c.execute)\n    def f(self):\n        sql = 'UPDATE tr_key_limit SET reserved=0'\n"
     "        self.run(add_cap(sql))\n", {("key_limit.sql", "O.f"): True, ("key_limit.escape", "O.f"): True}),
    ("setattr by a computed name", "def f(o, c, name):\n    setattr(o, name, c.execute)\n"
     "    o.go('UPDATE tr_key_limit SET reserved=0')\n",
     {("key_limit.sql", "f"): False, ("key_limit.indirect", "f"): True, ("key_limit.escape", "f"): True}),
    ("exec", "def f(c, code):\n    c.execute('SELECT 1 FROM tr_key_limit')\n    exec(code)\n",
     {("key_limit.indirect", "f"): True}),
    ("an alias of eval", "run = eval\ndef f(c, code):\n    c.execute('SELECT 1 FROM tr_key_limit')\n    run(code)\n",
     {("key_limit.indirect", "f"): True}),
    ("re.compile is not compile", "import re\ndef f(c, p):\n    c.execute('SELECT 1 FROM tr_key_limit')\n"
     "    return re.compile(p)\n", {}),
    ("a decorator wrapping an executor", "def with_cap(execute):\n    def run(sql):\n"
     "        return execute(sql + ', day_limit_micro=1')\n    return run\n"
     "def f(c):\n    with_cap(c.execute)('UPDATE tr_key_limit SET reserved=0')\n",
     {("key_limit.sql", "f"): False, ("key_limit.indirect", "f"): True, ("key_limit.escape", "f"): True}),
    ("a decorated helper of the module", "def with_cap(fn):\n    return lambda c, q: fn(c, q + ', day_limit_micro=1')\n"
     "@with_cap\ndef run(c, q):\n    c.execute(q)\ndef f(c):\n    run(c, 'UPDATE tr_key_limit SET reserved=0')\n",
     # (and the value escapes: the decorator's wrapper, not the reviewed def, receives it)
     {("key_limit.sql", "f"): True, ("key_limit.escape", "f"): True}),
    ("a helper of the module that changes the statement", "def run(c, q):\n    c.execute(q + ', day_limit_micro=1')\n"
     "def f(c):\n    run(c, 'UPDATE tr_key_limit SET reserved=0')\n", {("key_limit.sql", "f"): True}),
    ("a chain of helpers", "def run(c, q):\n    inner(c, q)\ndef inner(c, q):\n"
     "    c.execute(q + ', day_limit_micro=1')\ndef f(c):\n    run(c, 'UPDATE tr_key_limit SET reserved=0')\n",
     {("key_limit.sql", "f"): True}),
    ("a method helper given a statement not known", "def add_cap(sql):\n    return sql + ', day_limit_micro=1'\n"
     "class S:\n    def _run(self, q):\n"
     "        self.conn.execute(q)\n    def f(self):\n        sql = 'UPDATE tr_key_limit SET reserved=0'\n"
     "        self._run(add_cap(sql))\n", {("key_limit.sql", "S.f"): True, ("key_limit.escape", "S.f"): True}),
    ("a helper given the statement by keyword", "def run(c, *, query):\n"
     "    c.execute(query + ', day_limit_micro=1')\ndef f(c):\n    run(c, query='UPDATE tr_key_limit SET reserved=0')\n",
     {("key_limit.sql", "f"): True}),
    ("a helper holding exec", "def run(c, q):\n    exec('c.execute(q)')\n"
     "def f(c):\n    run(c, 'UPDATE tr_key_limit SET reserved=0')\n",
     {("key_limit.sql", "f"): True, ("key_limit.escape", "f"): True}),
    ("a pass-through helper, counter only", "def run(c, q):\n    c.execute(q)\n"
     "def f(c):\n    run(c, 'UPDATE tr_key_limit SET reserved=0')\n", {("key_limit.sql", "f"): False}),
    ("an int to a helper that builds its statement", "def reserve(c, n):\n"
     "    c.execute('UPDATE tr_key_limit SET reserved = reserved + ' + str(n))\n"
     "def f(c):\n    c.execute('SELECT 1 FROM tr_key_limit')\n    reserve(c, 5)\n",
     {("key_limit.sql", "reserve"): True, ("key_limit.escape", "reserve"): True}),
    ("a flag to a helper that builds its statement", "def reserve(c, byok):\n"
     "    c.execute('UPDATE tr_key_limit SET ' + ('byok_usage' if byok else 'usage') + ' = 0')\n"
     "def f(c):\n    c.execute('SELECT 1 FROM tr_key_limit')\n    reserve(c, True)\n",
     {("key_limit.sql", "reserve"): False}),
    ("a nested helper (the reviewer's)", "def f(c):\n    sql = 'UPDATE tr_key_limit SET reserved=0'\n"
     "    def run(q):\n        c.execute(q + ', day_limit_micro=1')\n    run(sql)\n",
     {("key_limit.sql", "f"): True, ("key_limit.sql", "f.run"): True}),
    ("a nested pass-through fails closed", "def f(c):\n    sql = 'UPDATE tr_key_limit SET reserved=0'\n"
     "    def run(q):\n        c.execute(q)\n    run(sql)\n",
     {("key_limit.sql", "f"): False, ("key_limit.sql", "f.run"): True}),
    ("a decorated nested helper", "def with_cap(fn):\n    return lambda q: fn(q + ', day_limit_micro=1')\n"
     "def f(c):\n    sql = 'UPDATE tr_key_limit SET reserved=0'\n    @with_cap\n    def run(q):\n"
     "        c.execute(q)\n    run(sql)\n",
     {("key_limit.sql", "f"): True, ("key_limit.sql", "f.run"): True, ("key_limit.escape", "f"): True}),
    ("a nested lambda", "def f(c):\n    sql = 'UPDATE tr_key_limit SET reserved=0'\n"
     "    run = lambda q: c.execute(q + ', day_limit_micro=1')\n    run(sql)\n", {("key_limit.sql", "f"): True, ("key_limit.escape", "f"): True}),
    ("a nested comprehension", "def add_cap(sql):\n    return sql + ', day_limit_micro=1'\n"
     "def f(c):\n    sql = 'UPDATE tr_key_limit SET reserved=0'\n"
     "    [c.execute(add_cap(s)) for s in [sql]]\n", {("key_limit.sql", "f"): True, ("key_limit.escape", "f"): True}),
    ("a nested class body", "def add_cap(sql):\n    return sql + ', day_limit_micro=1'\n"
     "def f(c):\n    sql = 'UPDATE tr_key_limit SET reserved=0'\n"
     "    class K:\n        def go(self):\n            c.execute(add_cap(sql))\n    K().go()\n",
     {("key_limit.sql", "f"): False, ("key_limit.sql", "f.K.go"): True, ("key_limit.escape", "f.K.go"): True}),
    ("the backstop across a nested function", "def f(tx):\n    def cols():\n"
     "        return ('key_hash', 'shard', 'day_limit_micro')\n    tx.update('tr_key_limit', cols(), [('k', 0, 1)])\n",
     {("key_limit.name", "f"): True}),
    ("a class's duplicate method", "class S:\n    def f(self, c):\n"
     "        c.execute('UPDATE tr_key_limit SET reserved=0')\n    def f(self, c):\n"
     "        c.execute('UPDATE tr_key_limit SET day_limit_micro=1')\n", {("key_limit.sql", "S.f"): True}),
    # Review round 10: the escape rule. A value naming the table may leave
    # its scope only by an executor or mutation call, or a followed helper
    # of the module through a parameter that only reaches executors; every
    # other route is key_limit.escape (every column). The reviewer's forms:
    ("a helper indexing the statement (the reviewer's)", "def run(c, batch):\n"
     "    c.execute(batch[0] + ', day_limit_micro=1')\ndef f(c):\n    run(c, ['UPDATE tr_key_limit SET reserved=0'])\n",
     {("key_limit.sql", "f"): False, ("key_limit.escape", "f"): True}),
    ("a helper unpacking the statement (the reviewer's)", "def run(c, batch):\n    q, = batch\n"
     "    c.execute(q + ', day_limit_micro=1')\ndef f(c):\n    run(c, ['UPDATE tr_key_limit SET reserved=0'])\n",
     {("key_limit.sql", "f"): False, ("key_limit.escape", "f"): True}),
    ("a helper taking the statement in **kwargs (the reviewer's)", "def run(c, **kw):\n"
     "    c.execute(kw['sql'] + ', day_limit_micro=1')\ndef f(c):\n    run(c, sql='UPDATE tr_key_limit SET reserved=0')\n",
     {("key_limit.sql", "f"): False, ("key_limit.escape", "f"): True}),
    ("a lambda helper (the reviewer's)", "run = lambda c, q: c.execute(q + ', day_limit_micro=1')\n"
     "def f(c):\n    run(c, 'UPDATE tr_key_limit SET reserved=0')\n",
     {("key_limit.sql", "f"): False, ("key_limit.escape", "f"): True}),
    ("a callable object (the reviewer's)", "class Runner:\n    def __call__(self, c, q):\n"
     "        c.execute(q + ', day_limit_micro=1')\nrun = Runner()\ndef f(c):\n"
     "    run(c, 'UPDATE tr_key_limit SET reserved=0')\n",
     {("key_limit.sql", "f"): False, ("key_limit.escape", "f"): True}),
    # The default path, by constructs the review did not use.
    ("a generator", "def f(c, run_all):\n    run_all(c, (s for s in ['UPDATE tr_key_limit SET reserved=0']))\n",
     {("key_limit.sql", "f"): False, ("key_limit.escape", "f"): True}),
    ("functools.reduce", "import functools\ndef f(c):\n    c.execute(functools.reduce(lambda a, b: a + b, "
     "['UPDATE tr_key_limit SET reserved=0', ', day_limit_micro=1']))\n",
     {("key_limit.sql", "f"): True, ("key_limit.escape", "f"): True}),
    ("a dataclass field", "import dataclasses\n@dataclasses.dataclass\nclass Stmt:\n    sql: str\n"
     "def f(c):\n    s = Stmt('UPDATE tr_key_limit SET reserved=0')\n    c.execute(s.sql + ', day_limit_micro=1')\n",
     {("key_limit.sql", "f"): True, ("key_limit.escape", "f"): True}),
    ("a walrus", "def f(c, helper):\n    if (q := 'UPDATE tr_key_limit SET reserved=0'):\n        helper(c, q)\n",
     {("key_limit.sql", "f"): False, ("key_limit.escape", "f"): True}),
    ("map", "def f(c):\n    list(map(lambda q: c.execute(q + ', day_limit_micro=1'), "
     "['UPDATE tr_key_limit SET reserved=0']))\n", {("key_limit.sql", "f"): True, ("key_limit.escape", "f"): True}),
    ("returned", "def build():\n    return 'UPDATE tr_key_limit SET reserved=0'\n",
     {("key_limit.sql", "build"): False, ("key_limit.escape", "build"): True}),
    ("yielded", "def build():\n    yield 'UPDATE tr_key_limit SET reserved=0'\n",
     {("key_limit.sql", "build"): False, ("key_limit.escape", "build"): True}),
    ("stored on an attribute", "class S:\n    def f(self):\n        self.sql = 'UPDATE tr_key_limit SET reserved=0'\n",
     {("key_limit.sql", "S.f"): False, ("key_limit.escape", "S.f"): True}),
    ("stored in a subscript", "CACHE = {}\ndef f():\n    CACHE['q'] = 'UPDATE tr_key_limit SET reserved=0'\n",
     {("key_limit.sql", "f"): False, ("key_limit.escape", "f"): True}),
    ("unpacked", "def f(c):\n    q, cap = 'UPDATE tr_key_limit SET reserved=0', ', day_limit_micro=1'\n"
     "    c.execute(q + cap)\n", {("key_limit.sql", "f"): True, ("key_limit.escape", "f"): True}),
    ("a default argument", "def f(c, q='UPDATE tr_key_limit SET reserved=0'):\n"
     "    c.execute(q + ', day_limit_micro=1')\n", {("key_limit.sql", "f"): True, ("key_limit.escape", "f"): True}),
    ("a global", "Q = ''\ndef f():\n    global Q\n    Q = 'UPDATE tr_key_limit SET reserved=0'\n",
     {("key_limit.sql", "f"): False, ("key_limit.escape", "f"): True}),
    ("an element bound and given to a helper", "def f(c, helper):\n    batch = ['UPDATE tr_key_limit SET reserved=0']\n"
     "    q = batch[0]\n    helper(q)\n", {("key_limit.sql", "f"): False, ("key_limit.escape", "f"): True}),
    ("appended to a list given to a helper", "def f(helper):\n    statements = []\n"
     "    statements.append('UPDATE tr_key_limit SET reserved=0')\n    helper(statements)\n",
     {("key_limit.sql", "f"): False, ("key_limit.escape", "f"): True}),
    ("a str method's result given to a helper", "def f(helper):\n"
     "    helper('UPDATE tr_key_limit SET reserved=0'.encode())\n",
     {("key_limit.sql", "f"): False, ("key_limit.escape", "f"): True}),
    ("str() of it given to a helper", "def f(helper):\n    helper(str('UPDATE tr_key_limit SET reserved=0'))\n",
     {("key_limit.sql", "f"): False, ("key_limit.escape", "f"): True}),
    ("a helper that runs and returns it", "def run(c, q):\n    c.execute(q)\n    return q\n"
     "def f(c):\n    run(c, 'UPDATE tr_key_limit SET reserved=0')\n",
     {("key_limit.sql", "f"): False, ("key_limit.escape", "f"): True}),
    ("logged", "import logging\nlog = logging.getLogger()\ndef f(k):\n    log.warning('no tr_key_limit row for %s', k)\n",
     {("key_limit.escape", "f"): True}),
    ("raised in a message (the handler reads it in another scope)",
     "def f():\n    raise RuntimeError('configured tr_key_limit usage shard set is incomplete')\n",
     {("key_limit.escape", "f"): True}),
    ("raised in a message with an unknown part", "def f(m):\n    raise ValueError(f'unknown tr_key_limit mutation: {m!r}')\n",
     {("key_limit.escape", "f"): True}),
    ("an assert's message", "def f(ok):\n    assert ok, 'tr_key_limit rows are missing'\n", {("key_limit.escape", "f"): True}),
    ("put into a container through an alias", "def f(helper):\n    a = []\n    b = a\n"
     "    b.append('UPDATE tr_key_limit SET reserved=0')\n    helper(a)\n", {("key_limit.sql", "f"): False, ("key_limit.escape", "f"): True}),
    ("appended to a list the caller holds", "def f(statements):\n"
     "    statements.append('UPDATE tr_key_limit SET reserved=0')\n",
     {("key_limit.sql", "f"): False, ("key_limit.escape", "f"): True}),
    ("appended to a module's list", "STATEMENTS = []\ndef f():\n"
     "    STATEMENTS.append('UPDATE tr_key_limit SET reserved=0')\n",
     {("key_limit.sql", "f"): False, ("key_limit.escape", "f"): True}),
    ("appended to a container from a loop", "def f(rows):\n    for row in rows:\n"
     "        row.append('UPDATE tr_key_limit SET reserved=0')\n",
     {("key_limit.sql", "f"): False, ("key_limit.escape", "f"): True}),
    ("a string built with +=, then run", "def f(c):\n    sql = 'SELECT 1'\n    sql += ' FROM tr_key_limit'\n"
     "    c.execute(sql)\n", {}),
    ("added with += through an alias", "def f(helper):\n    a = []\n    b = a\n"
     "    b += ['UPDATE tr_key_limit SET reserved=0']\n    helper(a)\n",
     {("key_limit.sql", "f"): False, ("key_limit.escape", "f"): True}),
    ("a dict constant of another scope's statements", "STATEMENTS = {'reserve': 'UPDATE tr_key_limit SET reserved=0'}\n"
     "def f(helper):\n    helper(STATEMENTS['reserve'])\n",
     {("key_limit.sql", "<module>.STATEMENTS"): False, ("key_limit.escape", "f"): True}),
    # Routes the rule allows.
    ("given to an executor with its parameters", "def f(c):\n    sql = 'UPDATE tr_key_limit SET reserved=0'\n"
     "    c.execute(sql + ' WHERE key_hash=%s', ('k',))\n", {("key_limit.sql", "f"): False}),
    ("given to a pass-through helper", "def run(c, q):\n    c.execute(q)\ndef f(c):\n"
     "    run(c, 'UPDATE tr_key_limit SET reserved=0')\n", {("key_limit.sql", "f"): False}),
    ("given to a followed helper that changes it", "def run_all(c, statements):\n    for q in statements:\n"
     "        c.execute(q + ', day_limit_micro=1')\ndef f(c):\n"
     "    run_all(c, (s for s in ['UPDATE tr_key_limit SET reserved=0']))\n", {("key_limit.sql", "f"): True}),
    ("a list literal batched", "def f(c):\n    statements = ['UPDATE tr_key_limit SET reserved=0']\n"
     "    c.batch_update(statements)\n", {("key_limit.sql", "f"): False}),
    ("statements looped over", "def f(c):\n    for q in ['UPDATE tr_key_limit SET reserved=0', "
     "'DELETE FROM tr_key_limit WHERE key_hash=%s']:\n        c.execute(q)\n", {("key_limit.sql", "f"): False}),
    ("compared with a parameter, printed, looked up in a parameter", "KEY_LIMIT_TABLE = 'tr_key_limit'\n"
     "def f(table, rows):\n    if table == KEY_LIMIT_TABLE and len(KEY_LIMIT_TABLE):\n"
     "        print(KEY_LIMIT_TABLE)\n    return rows[KEY_LIMIT_TABLE]\n",
     {("key_limit.name", "<module>.KEY_LIMIT_TABLE"): False, ("key_limit.name", "f"): False,
      ("key_limit.escape", "f"): True}),
    # Review round 11: an allowed step may never let code outside the
    # scanner's knowledge receive the value. The reviewer's four forms:
    ("a dict's setdefault result given on (the reviewer's)", "run = lambda c, q: c.execute(q + ', day_limit_micro=1')\n"
     "def f(c):\n    statements = {'sql': 'UPDATE tr_key_limit SET reserved=0'}\n"
     "    run(c, statements.setdefault('sql', ''))\n", {("key_limit.sql", "f"): False, ("key_limit.escape", "f"): True}),
    ("a helper returning a dict's setdefault result is not clean (the reviewer's)",
     "run = lambda c, q: c.execute(q + ', day_limit_micro=1')\ndef identity(c, q):\n    c.execute(q)\n"
     "    queries = {'sql': q}\n    return queries.setdefault('sql', '')\n"
     "def f(c):\n    run(c, identity(c, 'UPDATE tr_key_limit SET reserved=0'))\n",
     {("key_limit.sql", "f"): False, ("key_limit.escape", "f"): True}),
    ("sorted with a key callback (the reviewer's)", "from functools import partial\n"
     "run = lambda c, q: c.execute(q + ', day_limit_micro=1')\n"
     "def f(c):\n    sorted(['UPDATE tr_key_limit SET reserved=0'], key=partial(run, c))\n",
     {("key_limit.sql", "f"): False, ("key_limit.escape", "f"): True}),
    ("print to a writer (the reviewer's)", "class Writer:\n    def __init__(self, c):\n        self.c = c\n"
     "    def write(self, q):\n        self.c.execute(q + ', day_limit_micro=1')\n"
     "def f(c):\n    print('UPDATE tr_key_limit SET reserved=0', file=Writer(c))\n",
     {("key_limit.sql", "f"): False, ("key_limit.escape", "f"): True}),
    ("a key looked up in an object (the reviewer's)", "class Runner:\n    def __init__(self, c):\n        self.c = c\n"
     "    def __getitem__(self, q):\n        self.c.execute(q + ', day_limit_micro=1')\n"
     "def f(c):\n    Runner(c)['UPDATE tr_key_limit SET reserved=0']\n",
     {("key_limit.sql", "f"): False, ("key_limit.escape", "f"): True}),
    # More user-code routes the review did not use.
    ("in an object (__contains__)", "def f(runner):\n    return 'UPDATE tr_key_limit SET reserved=0' in runner\n",
     {("key_limit.sql", "f"): False, ("key_limit.escape", "f"): True}),
    ("added to an object (__radd__)", "def f(runner):\n    'UPDATE tr_key_limit SET reserved=0' + runner\n",
     {("key_limit.sql", "f"): True, ("key_limit.escape", "f"): True}),
    ("compared with an object (__eq__)", "def f(runner):\n    runner == 'UPDATE tr_key_limit SET reserved=0'\n",
     {("key_limit.sql", "f"): False, ("key_limit.escape", "f"): True}),
    ("max with a default", "def f(pick):\n    return max(['UPDATE tr_key_limit SET reserved=0'], default=pick)\n",
     {("key_limit.sql", "f"): False, ("key_limit.escape", "f"): True}),
    ("a mapping of a % template (__getitem__)", "def f(runner):\n"
     "    '%(UPDATE tr_key_limit SET reserved=0)s' % runner\n", {("key_limit.sql", "f"): True, ("key_limit.escape", "f"): True}),
    ("a format spec given to an object (__format__)", "def f(runner):\n"
     "    spec = 'UPDATE tr_key_limit SET reserved=0'\n    f'{runner:{spec}}'\n",
     {("key_limit.sql", "f"): False, ("key_limit.escape", "f"): True}),
    ("str.translate with an object", "def f(table):\n    'UPDATE tr_key_limit SET reserved=0'.translate(table)\n",
     {("key_limit.sql", "f"): True, ("key_limit.escape", "f"): True}),
    ("a codec not shipped with Python", "def f():\n    'UPDATE tr_key_limit SET reserved=0'.encode('probe-codec')\n",
     {("key_limit.sql", "f"): False, ("key_limit.escape", "f"): True}),
    ("a set with an element not builtin (__eq__, __hash__)", "def f(key):\n"
     "    {'UPDATE tr_key_limit SET reserved=0', key}\n", {("key_limit.sql", "f"): False, ("key_limit.escape", "f"): True}),
    ("isinstance of a class not builtin (__instancecheck__)", "def f(cls):\n"
     "    return isinstance('UPDATE tr_key_limit SET reserved=0', cls)\n",
     {("key_limit.sql", "f"): False, ("key_limit.escape", "f"): True}),
    ("a class body with a base (__init_subclass__)", "class Base:\n    pass\nclass Holder(Base):\n"
     "    q = 'UPDATE tr_key_limit SET reserved=0'\n", {("key_limit.sql", "Holder"): False, ("key_limit.escape", "Holder"): True}),
    ("a list sorted with a key", "def f(key):\n    q = ['UPDATE tr_key_limit SET reserved=0']\n    q.sort(key=key)\n",
     {("key_limit.sql", "f"): False, ("key_limit.escape", "f"): True}),
    ("a dict of statements indexed with an object (__eq__ meets its keys)", "def f(key):\n"
     "    statements = {'UPDATE tr_key_limit SET reserved=0': 1}\n    statements[key]\n",
     {("key_limit.sql", "f"): False, ("key_limit.escape", "f"): True}),
    ("sorted with an element not builtin (__lt__)", "def f(obj):\n"
     "    sorted(['UPDATE tr_key_limit SET reserved=0', obj])\n",
     {("key_limit.sql", "f"): False, ("key_limit.escape", "f"): True}),
    ("added to a set holding an object (__eq__)", "def f(key):\n    seen = {key}\n"
     "    seen.add('UPDATE tr_key_limit SET reserved=0')\n",
     {("key_limit.sql", "f"): False, ("key_limit.escape", "f"): True}),
    ("searched in a module's list (any code may change it)", "NAMES = []\ndef f():\n"
     "    return 'UPDATE tr_key_limit SET reserved=0' in NAMES\n",
     {("key_limit.sql", "f"): False, ("key_limit.escape", "f"): True}),
    ("concatenated with a lone f-string field (a str subclass perhaps)", "def f(obj):\n"
     "    'UPDATE tr_key_limit SET reserved=0' + f'{obj}'\n",
     {("key_limit.sql", "f"): True, ("key_limit.escape", "f"): True}),
    ("concatenated with a bare format of an object", "def f(obj):\n"
     "    'UPDATE tr_key_limit SET reserved=0' + '{}'.format(obj)\n",
     {("key_limit.sql", "f"): True, ("key_limit.escape", "f"): True}),
    ("dict.get with a key not builtin", "def f(k):\n    d = {'q': 'UPDATE tr_key_limit SET reserved=0'}\n"
     "    d.get(k)\n", {("key_limit.sql", "f"): False, ("key_limit.escape", "f"): True}),
    ("print (sys.stdout may be any writer)", "def f():\n    print('UPDATE tr_key_limit SET reserved=0')\n",
     {("key_limit.sql", "f"): False, ("key_limit.escape", "f"): True}),
    ("+= onto an object (__iadd__)", "def f(acc):\n    acc += 'UPDATE tr_key_limit SET reserved=0'\n",
     {("key_limit.sql", "f"): False, ("key_limit.escape", "f"): True}),
    ("a container passed on, then searched", "def f(fill):\n    names = []\n    fill(names)\n"
     "    return 'UPDATE tr_key_limit SET reserved=0' in names\n",
     {("key_limit.sql", "f"): False, ("key_limit.escape", "f"): True}),
    ("concatenated with str() of a parameter (a str subclass's __radd__)", "def f(c, n):\n"
     "    c.execute('UPDATE tr_key_limit SET reserved = reserved + ' + str(n))\n",
     {("key_limit.sql", "f"): True, ("key_limit.escape", "f"): True}),
    # Steps that stay allowed: every other operand provably builtin.
    ("compared with literals, searched in a literal dict", "KEY_LIMIT_TABLE = 'tr_key_limit'\ndef f():\n"
     "    tables = ('tr_credit_balance', 'tr_key_limit')\n    names = {'tr_key_limit': 1}\n    found = KEY_LIMIT_TABLE in names\n"
     "    if KEY_LIMIT_TABLE in tables and KEY_LIMIT_TABLE == 'tr_key_limit' and isinstance(KEY_LIMIT_TABLE, str):\n"
     "        return len(KEY_LIMIT_TABLE) + len(tables)\n    return 0\n",
     {("key_limit.name", "<module>.KEY_LIMIT_TABLE"): False, ("key_limit.name", "f"): False}),
    ("sorted literal statements, run", "def f(c):\n"
     "    for q in sorted(['UPDATE tr_key_limit SET reserved=0', 'DELETE FROM tr_key_limit']):\n        c.execute(q)\n",
     {("key_limit.sql", "f"): True}),
    ("a dict's setdefault result run", "def f(c):\n    d = {}\n"
     "    c.execute(d.setdefault('q', 'UPDATE tr_key_limit SET reserved=0'))\n", {("key_limit.sql", "f"): True}),
    ("an f-string with a parameter, run", "def f(c, k):\n    t = 'tr_key_limit'\n"
     "    c.execute(f'UPDATE {t} SET reserved=0 WHERE key_hash={k!r}')\n", {("key_limit.name", "f"): False, ("key_limit.sql", "f"): True}),
    # Review round 12: the inference of provably builtin fails closed by
    # construction (a binding that is not a value, a nested container, the
    # value before +=), and so do element-comparing methods, namespaces and
    # class bodies. Each item's forms, the reviewer's marked, then the
    # author's own pass (exception routes, spreads, scopes), and controls
    # that stay allowed.
    ("an unpacked operand (the reviewer's)",
     _with(_R12_RUNNER, "def f(c):\n    runner, = [Runner(c)]\n    runner['UPDATE tr_key_limit SET reserved=0']\n"),
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ('an unpacked operand compared',
     _with(_R12_RUNNER, "def f(c):\n    runner, = [Runner(c)]\n    runner == 'UPDATE tr_key_limit SET reserved=0'\n"),
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ('a starred target compared',
     _with(_R12_RUNNER, "def f(c):\n    first, *rest = [1, Runner(c)]\n    rest[0] == 'UPDATE tr_key_limit SET reserved=0'\n"),
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ('a with target compared',
     "def f(open_runner):\n    with open_runner() as runner:\n        runner == 'UPDATE tr_key_limit SET reserved=0'\n",
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ('an except name compared',
     "def f(c):\n    try:\n        c.go()\n    except Exception as error:\n        error == 'UPDATE tr_key_limit SET reserved=0'\n",
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ('an imported name compared',
     "suffix = ''\ndef f(c):\n    from elsewhere import suffix\n    suffix == 'UPDATE tr_key_limit SET reserved=0'\n",
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ('a module name bound in a loop compared',
     "suffix = ''\nfor suffix in [{}, 1]:\n    pass\ndef f(c):\n    suffix == 'UPDATE tr_key_limit SET reserved=0'\n",
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ('a module name beside import *',
     "from elsewhere import *\nsuffix = ''\ndef f(c):\n    suffix == 'UPDATE tr_key_limit SET reserved=0'\n",
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ("a builtin's name imported",
     "from elsewhere import len\ndef f(c):\n    len('UPDATE tr_key_limit SET reserved=0')\n",
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ('a comprehension over builtin text as an operand is allowed',
     "def f(c):\n    c.execute(' '.join(['UPDATE tr_key_limit SET reserved=0' + s for s in ('', ' WHERE a = 1')]))\n",
     {('key_limit.sql', 'f'): True}),
    ('a loop over builtin text as an operand is allowed',
     "def f(c):\n    for suffix in (' WHERE a = 1', ''):\n        c.execute('UPDATE tr_key_limit SET reserved=0' + suffix)\n",
     {('key_limit.sql', 'f'): False}),
    ("an element of a nested container changed (the reviewer's)",
     _with(_R12_RUNNER, "def f(c):\n    boxes = [[]]\n    boxes[0].append(Runner(c))\n    boxes[0][0]['UPDATE tr_key_limit SET reserved=0']\n"),
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ("an alias of a nested container changed (the reviewer's)",
     _with(_R12_RUNNER, "def f(c):\n    boxes = [[]]\n    inner = boxes[0]\n    inner.append(Runner(c))\n    boxes[0][0]['UPDATE tr_key_limit SET reserved=0']\n"),
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ('a nested container changed through a loop',
     _with(_R12_RUNNER, "def f(c):\n    boxes = [[]]\n    for inner in boxes:\n        inner.append(Runner(c))\n    boxes[0][0]['UPDATE tr_key_limit SET reserved=0']\n"),
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ('a nested container changed through setdefault',
     _with(_R12_RUNNER, "def f(c):\n    boxes = {}\n    boxes.setdefault('k', []).append(Runner(c))\n    boxes['k'][0]['UPDATE tr_key_limit SET reserved=0']\n"),
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ('searched in a nested container changed',
     _with(_R12_RUNNER, "def f(c):\n    boxes = [[]]\n    boxes[0].append(Runner(c))\n    'UPDATE tr_key_limit SET reserved=0' in boxes[0]\n"),
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ('searched in a nested container changed through an alias',
     _with(_R12_RUNNER, "def f(c):\n    boxes = [[]]\n    inner = boxes[0]\n    inner.append(Runner(c))\n    'UPDATE tr_key_limit SET reserved=0' in boxes[0]\n"),
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ('searched in a nested container changed through a loop',
     _with(_R12_RUNNER, "def f(c):\n    boxes = [[]]\n    for inner in boxes:\n        inner.append(Runner(c))\n    'UPDATE tr_key_limit SET reserved=0' in boxes[0]\n"),
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ('searched in a nested container changed through setdefault',
     _with(_R12_RUNNER, "def f(c):\n    boxes = {}\n    boxes.setdefault('k', []).append(Runner(c))\n    'UPDATE tr_key_limit SET reserved=0' in boxes['k']\n"),
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ('searched in a container given an object',
     _with(_R12_RUNNER, "def f(c):\n    boxes = [[], []]\n    boxes[0] = [Runner(c)]\n    'UPDATE tr_key_limit SET reserved=0' in boxes[0]\n"),
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ("searched in a container handed to an object's __radd__",
     "def f(runner):\n    boxes = [[]]\n    boxes + runner\n    'UPDATE tr_key_limit SET reserved=0' in boxes[0]\n",
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ('searched in a container compared with an object (its __eq__ receives it)',
     "def f(runner):\n    boxes = [[]]\n    boxes == runner\n    'UPDATE tr_key_limit SET reserved=0' in boxes[0]\n",
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ('searched in a nested container of builtins is allowed',
     "def f(c):\n    boxes = [['x']]\n    boxes[0].append('y')\n    if 'UPDATE tr_key_limit SET reserved=0' in boxes[0]:\n        c.execute('UPDATE tr_key_limit SET reserved=0')\n",
     {('key_limit.sql', 'f'): False}),
    ('a nested container of builtins is allowed',
     "def f(c):\n    boxes = [[' WHERE a = 1']]\n    boxes[0].append('')\n    c.execute('UPDATE tr_key_limit SET reserved=0' + boxes[0][0])\n",
     {('key_limit.sql', 'f'): True}),
    ('a container two names share (x = y = [])',
     _with(_R12_RUN, "def f(c):\n    x = y = []\n    y.append('UPDATE tr_key_limit SET reserved=0')\n    run(c, x[0])\n"),
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ("appended to a class body's list",
     "class Queries:\n    names = []\n    names.append('UPDATE tr_key_limit SET reserved=0')\n",
     {('key_limit.escape', 'Queries'): True, ('key_limit.sql', 'Queries'): False}),
    ("the value before += meets the other operand (the reviewer's)",
     _with(_R12_RUNNER, "def f(c):\n    q = 'UPDATE tr_key_limit SET reserved=0'\n    q += Runner(c)\n"),
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ('an element before += meets the other operand',
     _with(_R12_RUNNER, "def f(c):\n    queries = {'q': 'UPDATE tr_key_limit SET reserved=0'}\n    queries['q'] += Runner(c)\n"),
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ('+= with builtin text is allowed',
     "def f(c):\n    q = 'UPDATE tr_key_limit SET reserved=0'\n    q += ' WHERE a = 1'\n    c.execute(q)\n",
     {('key_limit.sql', 'f'): False}),
    ("sort beside an object (the reviewer's)",
     _with(_R12_RUNNER, "def f(c):\n    values = ['UPDATE tr_key_limit SET reserved=0', Runner(c)]\n    values.sort()\n"),
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ('index beside an object',
     _with(_R12_RUNNER, "def f(c):\n    values = ['UPDATE tr_key_limit SET reserved=0', Runner(c)]\n    values.index('x')\n"),
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ('count, remove beside an object',
     _with(_R12_RUNNER, "def f(c):\n    values = ['UPDATE tr_key_limit SET reserved=0', Runner(c)]\n    values.count('x')\n    values.remove('x')\n"),
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ('a method of a parameter holding it',
     "def f(c, values):\n    values.append('UPDATE tr_key_limit SET reserved=0')\n    values.sort()\n",
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ('sort of builtin text is allowed',
     "def f(c):\n    values = ['UPDATE tr_key_limit SET reserved=0', 'x']\n    values.sort()\n    c.execute(values[0])\n",
     {('key_limit.sql', 'f'): True}),
    ("locals() (the reviewer's)",
     _with(_R12_RUN, "def f(c):\n    sql = 'UPDATE tr_key_limit SET reserved=0'\n    run(c, locals()['sql'])\n"),
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ("globals() (the reviewer's)",
     _with(_R12_RUN, "sql = 'UPDATE tr_key_limit SET reserved=0'\ndef f(c):\n    run(c, globals()['sql'])\n"),
     {('key_limit.escape', 'f'): True, ('key_limit.sql', '<module>.sql'): False}),
    ('vars() and eval',
     _with(_R12_RUN, "def f(c):\n    sql = 'UPDATE tr_key_limit SET reserved=0'\n    run(c, vars()['sql'])\n    run(c, eval('sql'))\n"),
     {('key_limit.escape', 'f'): True, ('key_limit.indirect', 'f'): True, ('key_limit.sql', 'f'): False}),
    ("a frame's locals",
     _with('import sys\n' + _R12_RUN, "def f(c):\n    sql = 'UPDATE tr_key_limit SET reserved=0'\n    g(c)\ndef g(c):\n    run(c, sys._getframe(1).f_locals['sql'])\n"),
     {('key_limit.escape', 'g'): True, ('key_limit.sql', 'f'): False}),
    ("a module's namespace through sys.modules",
     _with('import sys\n' + _R12_RUN, "sql = 'UPDATE tr_key_limit SET reserved=0'\ndef f(c):\n    run(c, vars(sys.modules[__name__])['sql'])\n"),
     {('key_limit.escape', 'f'): True, ('key_limit.sql', '<module>.sql'): False}),
    ("a module constant rebound through globals() (the reviewer's)",
     _with(_R12_RUNNER, "suffix = ''\ndef f(c):\n    globals()['suffix'] = Runner(c)\n    'UPDATE tr_key_limit SET reserved=0' + suffix\n"),
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ("a traceback's frame",
     _with(_R12_RUN, "def f(c):\n    sql = 'UPDATE tr_key_limit SET reserved=0'\n    try:\n        c.go()\n    except Exception as error:\n        run(c, error.__traceback__.tb_frame.f_locals['sql'])\n"),
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ("inspect's current frame",
     _with('import inspect\n' + _R12_RUN, "def f(c):\n    sql = 'UPDATE tr_key_limit SET reserved=0'\n    run(c, inspect.getargvalues(inspect.currentframe()).locals['sql'])\n"),
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ('a local a frame may rebind',
     "import sys\ndef f(runner):\n    suffix = ''\n    sys._getframe(0).f_locals['suffix'] = runner\n    'UPDATE tr_key_limit SET reserved=0' + suffix\n",
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ('a name read in a class body a metaclass makes',
     "class Meta(type):\n    pass\nclass Queries(metaclass=Meta):\n    suffix = ''\n    'UPDATE tr_key_limit SET reserved=0' + suffix\n",
     {('key_limit.escape', 'Queries'): True, ('key_limit.sql', 'Queries'): False}),
    ("a name rebound by a nested function's nonlocal",
     "def f(runner):\n    suffix = ''\n    def g():\n        nonlocal suffix\n        suffix = runner\n    g()\n    'UPDATE tr_key_limit SET reserved=0' + suffix\n",
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): True}),
    ('a global read where a class body binds the name',
     "from elsewhere import suffix\ndef f(c):\n    class Queries:\n        suffix = ''\n    'UPDATE tr_key_limit SET reserved=0' + suffix\n",
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): True}),
    ('builtins.locals()',
     _with('import builtins\n' + _R12_RUN, "def f(c):\n    sql = 'UPDATE tr_key_limit SET reserved=0'\n    run(c, builtins.locals()['sql'])\n"),
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ('a namespace reader passed on through builtins',
     _with('import builtins\n' + _R12_RUN, "def f(c):\n    sql = 'UPDATE tr_key_limit SET reserved=0'\n    read = builtins.locals\n    run(c, read()['sql'])\n"),
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ('traceback summaries keeping locals',
     _with('import traceback\n' + _R12_RUN, "def f(c):\n    sql = 'UPDATE tr_key_limit SET reserved=0'\n    try:\n        c.go()\n    except Exception as error:\n        report = traceback.TracebackException.from_exception(error, capture_locals=True)\n        run(c, report.stack[0].locals['sql'])\n"),
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ("gc's objects",
     _with('import gc\n' + _R12_RUN, "def f(c):\n    sql = 'UPDATE tr_key_limit SET reserved=0'\n    g(c)\ndef g(c):\n    run(c, [o for o in gc.get_objects() if isinstance(o, str)][0])\n"),
     {('key_limit.escape', 'g'): True, ('key_limit.sql', 'f'): False}),
    ("a module's own object by import, read by a computed getattr",
     _with('import probe as me\n' + _R12_RUN, "SQL = 'UPDATE tr_key_limit SET reserved=0'\ndef f(c, name):\n    run(c, getattr(me, name))\n"),
     {('key_limit.escape', 'f'): True, ('key_limit.sql', '<module>.SQL'): False}),
    ('locals() where no value naming the table lives is allowed',
     "def f(c):\n    c.execute('UPDATE tr_key_limit SET reserved=0')\ndef g():\n    return locals()\n",
     {('key_limit.sql', 'f'): False}),
    ("a descriptor in a plain class (the reviewer's)",
     "class RunOnBind:\n    def __init__(self, c):\n        self.c = c\n    def __set_name__(self, owner, name):\n        self.c.execute(vars(owner)['sql'] + ', day_limit_micro=1')\ndef f(c):\n    class Queries:\n        sql = 'UPDATE tr_key_limit SET reserved=0'\n        on_bind = RunOnBind(c)\n",
     {('key_limit.escape', 'f.Queries'): True, ('key_limit.sql', 'f.Queries'): False}),
    ("a class's namespace passed on (the reviewer's)",
     _with(_R12_RUN, "def f(c):\n    class Queries:\n        sql = 'UPDATE tr_key_limit SET reserved=0'\n    run(c, vars(Queries)['sql'])\n"),
     {('key_limit.escape', 'f.Queries'): True, ('key_limit.sql', 'f.Queries'): False}),
    ('a plain class instantiated',
     _with(_R12_RUN, "class Queries:\n    sql = 'UPDATE tr_key_limit SET reserved=0'\n    def go(self, c):\n        run(c, vars(type(self))['sql'])\ndef f(c):\n    Queries().go(c)\n"),
     {('key_limit.escape', 'Queries'): True, ('key_limit.sql', 'Queries'): False}),
    ("a plain class's attribute given on",
     _with(_R12_RUN, "class Queries:\n    sql = 'UPDATE tr_key_limit SET reserved=0'\ndef f(c):\n    run(c, Queries.sql)\n"),
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'Queries'): False, ('key_limit.sql', 'f'): False}),
    ("a nested plain class's attribute given on",
     _with(_R12_RUN, "def f(c):\n    class Queries:\n        sql = 'UPDATE tr_key_limit SET reserved=0'\n    run(c, Queries.sql)\n"),
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f.Queries'): False}),
    ("a plain class's attribute executed is allowed",
     "class Queries:\n    sql = 'UPDATE tr_key_limit SET reserved=0'\ndef f(c):\n    c.execute(Queries.sql)\n",
     {('key_limit.sql', 'Queries'): False, ('key_limit.sql', 'f'): False}),
    ('a key looked up: its KeyError',
     _with(_R12_RUN, "def f(c):\n    try:\n        {}['UPDATE tr_key_limit SET reserved=0']\n    except KeyError as error:\n        run(c, error.args[0])\n"),
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ('encoded strictly: its UnicodeError',
     "def f(c):\n    c.execute('UPDATE tr_key_limit SET reserved=0'.encode('utf-8'))\n",
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): True}),
    ('encoded with a quiet handler is allowed',
     "def f(c):\n    c.execute('UPDATE tr_key_limit SET reserved=0'.encode('utf-8', 'replace'))\n",
     {('key_limit.sql', 'f'): True}),
    ('decoded by str()',
     "def f(c):\n    c.execute(str('UPDATE tr_key_limit SET reserved=0'.encode('utf-8', 'ignore'), 'utf-8'))\n",
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): True}),
    ("spread beside an object's key",
     _with(_R12_RUNNER, "def f(c):\n    {**{'UPDATE tr_key_limit SET reserved=0': 1}, Runner(c): 2}\n"),
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ("a nonlocal's uses in a nested function",
     _with(_R12_RUNNER, "def f(c):\n    q = 'UPDATE tr_key_limit SET reserved=0'\n    def h():\n        nonlocal q\n        q += ' WHERE a = 1'\n        Runner(c) + q\n    h()\n"),
     {('key_limit.escape', 'f.h'): True, ('key_limit.sql', 'f'): False, ('key_limit.sql', 'f.h'): True}),
    ('a global read where a nested function binds the name',
     _with(_R12_RUN, "sql = 'UPDATE tr_key_limit SET reserved=0'\ndef f(c):\n    def g():\n        sql = 1\n    run(c, sql)\n"),
     {('key_limit.escape', 'f'): True, ('key_limit.sql', '<module>.sql'): False, ('key_limit.sql', 'f'): False}),
    ('a type indexed with it (list[sql])',
     _with(_R12_RUN, "def f(c):\n    alias = list['UPDATE tr_key_limit SET reserved=0']\n    run(c, alias.__args__[0])\n"),
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    # Review round 13: the round-12 reviewer's examples (each a SQLite limit
    # write round 12 classified as no writer), and forms that stay allowed.
    ('round 12 reviewer: builtin_namespace',
     "run=lambda c,q: c.execute(q + ', day_limit_micro=1')\ndef f(c):\n    globals()['len'] = lambda q: run(c,q)\n    len('UPDATE tr_key_limit SET reserved=0')\n",
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ('round 12 reviewer: class_method_nonlocal',
     "class Runner:\n    def __init__(self,c): self.c=c\n    def __eq__(self,q):\n        self.c.execute(q + ', day_limit_micro=1')\n        return False\ndef f(c):\n    runner=0\n    class Change:\n        def change(self):\n            nonlocal runner\n            runner=Runner(c)\n    Change().change()\n    'UPDATE tr_key_limit SET reserved=0' == runner\n",
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ('round 12 reviewer: class_nonlocal',
     "class Runner:\n    def __init__(self,c): self.c=c\n    def __eq__(self,q):\n        self.c.execute(q + ', day_limit_micro=1')\n        return False\ndef f(c):\n    runner=0\n    class Change:\n        nonlocal runner\n        runner=Runner(c)\n    'UPDATE tr_key_limit SET reserved=0' == runner\n",
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ('round 12 reviewer: codec_inference',
     "import codecs\nclass Encoded(bytes):\n    def __eq__(self,q):\n        self.c.execute(q + ', day_limit_micro=1')\n        return True\ndef f(c):\n    def encoder(text,errors='strict'):\n        value=Encoded(b'x')\n        value.c=c\n        return value, len(text)\n    codecs.register(lambda name: codecs.CodecInfo(name='r12_test',encode=encoder,decode=None) if name=='r12_test' else None)\n    value='x'.encode('r12_test')\n    value == 'UPDATE tr_key_limit SET reserved=0'\n",
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ('round 12 reviewer: comprehension_target',
     "class Runner:\n    def __init__(self,c): self.c=c\n    def __eq__(self,q):\n        self.c.execute(q + ', day_limit_micro=1')\n        return True\ndef f(c):\n    values=[[] for i in [0]]\n    alias=values + []\n    alias[0].append(Runner(c))\n    values[0][0] == 'UPDATE tr_key_limit SET reserved=0'\n",
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ('round 12 reviewer: concat_alias',
     "class Runner:\n    def __init__(self,c): self.c=c\n    def __eq__(self,q):\n        self.c.execute(q + ', day_limit_micro=1')\n        return True\ndef f(c):\n    boxes=[[]]\n    copy=boxes + []\n    copy[0].append(Runner(c))\n    boxes[0][0] == 'UPDATE tr_key_limit SET reserved=0'\n",
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ('round 12 reviewer: decorator_shadow',
     "from functools import wraps\ndef cache(func):\n    @wraps(func)\n    def wrapper(c,q):\n        return func(c,q + ', day_limit_micro=1')\n    return wrapper\n@cache\ndef run(c,q):\n    c.execute(q)\ndef f(c):\n    run(c,'UPDATE tr_key_limit SET reserved=0')\n",
     {('key_limit.sql', 'f'): True, ('key_limit.escape', 'f'): True}),
    ('round 12 reviewer: default_locals',
     "run=lambda c,q: c.execute(q + ', day_limit_micro=1')\nsql='UPDATE tr_key_limit SET reserved=0'\ndef f(c,q=locals()['sql']):\n    run(c,q)\n",
     {('key_limit.escape', 'f'): True, ('key_limit.sql', '<module>.sql'): False}),
    ('round 12 reviewer: getattr_shadow',
     "def make_getattr(c):\n    return lambda q,name: lambda: c.execute(q + ', day_limit_micro=1')\ndef f(c):\n    getattr=make_getattr(c)\n    getattr('UPDATE tr_key_limit SET reserved=0','upper')()\n",
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ('round 12 reviewer: maketrans_inference',
     "class Runner:\n    def __init__(self,c): self.c=c\n    def __eq__(self,q):\n        self.c.execute(q + ', day_limit_micro=1')\n        return True\ndef f(c):\n    value=''.maketrans({1:Runner(c)})[1]\n    value == 'UPDATE tr_key_limit SET reserved=0'\n",
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ('round 12 reviewer: maketrans_mutation',
     "class Runner:\n    def __init__(self,c): self.c=c\n    def __eq__(self,q):\n        self.c.execute(q+', day_limit_micro=1')\n        return False\ndef f(c):\n    a=''.maketrans({})\n    a[65]=Runner(c)\n    'UPDATE tr_key_limit SET reserved=0' == a[65]\n",
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ('round 12 reviewer: maketrans_value',
     "class Runner:\n    def __init__(self,c): self.c=c\n    def __eq__(self,q):\n        self.c.execute(q + ', day_limit_micro=1')\n        return False\ndef f(c):\n    runner=''.maketrans({65:Runner(c)})[65]\n    'UPDATE tr_key_limit SET reserved=0' == runner\n",
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ('round 12 reviewer: nonlocal_class_body',
     "class Runner:\n    def __init__(self,c): self.c=c\n    def __eq__(self,q):\n        self.c.execute(q + ', day_limit_micro=1')\n        return True\ndef f(c):\n    value=''\n    class Rebind:\n        nonlocal value\n        value=Runner(c)\n    value == 'UPDATE tr_key_limit SET reserved=0'\n",
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ('round 12 reviewer: nonlocal_class_method',
     "class Runner:\n    def __init__(self,c): self.c=c\n    def __eq__(self,q):\n        self.c.execute(q + ', day_limit_micro=1')\n        return True\ndef f(c):\n    value=''\n    class Rebind:\n        def change(self):\n            nonlocal value\n            value=Runner(c)\n    Rebind().change()\n    value == 'UPDATE tr_key_limit SET reserved=0'\n",
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ('round 12 reviewer: partition_inference',
     "class Separator(str):\n    def __eq__(self,q):\n        self.c.execute(q + ', day_limit_micro=1')\n        return True\ndef f(c):\n    separator=Separator(':')\n    separator.c=c\n    value='a:b'.partition(separator)[1]\n    value == 'UPDATE tr_key_limit SET reserved=0'\n",
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ('round 12 reviewer: partition_subclass',
     "class Runner(str):\n    def __new__(cls,c):\n        obj=super().__new__(cls,'x')\n        obj.c=c\n        return obj\n    def __eq__(self,q):\n        self.c.execute(q + ', day_limit_micro=1')\n        return False\ndef f(c):\n    runner='x'.partition(Runner(c))[1]\n    'UPDATE tr_key_limit SET reserved=0' == runner\n",
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ('round 12 reviewer: repeat_alias',
     "class Runner:\n    def __init__(self,c): self.c=c\n    def __eq__(self,q):\n        self.c.execute(q + ', day_limit_micro=1')\n        return True\ndef f(c):\n    boxes=[[]]\n    copy=boxes * 1\n    copy[0].append(Runner(c))\n    boxes[0][0] == 'UPDATE tr_key_limit SET reserved=0'\n",
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ('round 12 reviewer: shallow_add',
     "class Runner:\n    def __init__(self,c): self.c=c\n    def __eq__(self,q):\n        self.c.execute(q + ', day_limit_micro=1')\n        return False\ndef f(c):\n    boxes=[[]]\n    (boxes + [])[0].append(Runner(c))\n    'UPDATE tr_key_limit SET reserved=0' in boxes[0]\n",
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ('round 12 reviewer: shallow_multiply',
     "class Runner:\n    def __init__(self,c): self.c=c\n    def __eq__(self,q):\n        self.c.execute(q + ', day_limit_micro=1')\n        return False\ndef f(c):\n    boxes=[[]]\n    (boxes * 1)[0].append(Runner(c))\n    'UPDATE tr_key_limit SET reserved=0' in boxes[0]\n",
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ('round 12 reviewer: shared_assignment',
     "class Runner:\n    def __init__(self,c): self.c=c\n    def __eq__(self,q):\n        self.c.execute(q + ', day_limit_micro=1')\n        return True\ndef f(c):\n    values=alias=[]\n    alias.append(Runner(c))\n    'UPDATE tr_key_limit SET reserved=0' in values\n",
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ('round 12 reviewer: shared_literal',
     "class Runner:\n    def __init__(self,c): self.c=c\n    def __eq__(self,q):\n        self.c.execute(q+', day_limit_micro=1')\n        return False\ndef f(c):\n    a=b=[]\n    a.append(Runner(c))\n    'UPDATE tr_key_limit SET reserved=0' in b\n",
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ('round 12 reviewer: shared_walrus',
     "class Runner:\n    def __init__(self,c): self.c=c\n    def __eq__(self,q):\n        self.c.execute(q+', day_limit_micro=1')\n        return False\ndef f(c):\n    a=(b:=[])\n    a.append(Runner(c))\n    'UPDATE tr_key_limit SET reserved=0' in b\n",
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ('a flat list copied with + is allowed',
     "def f(c):\n    names = ['a']\n    more = names + ['b']\n    if 'UPDATE tr_key_limit SET reserved=0' in names:\n        c.execute('UPDATE tr_key_limit SET reserved=0')\n",
     {('key_limit.sql', 'f'): False}),
    ('text added to an element is allowed',
     "def f(c):\n    head = 'a:b'.partition(':')[0]\n    c.execute('UPDATE tr_key_limit SET reserved=0' + head)\n",
     {('key_limit.sql', 'f'): True}),
    ('the builtin getattr with a literal name is read as the attribute',
     "def f(c):\n    c.execute(getattr('UPDATE tr_key_limit SET reserved=0', 'strip')())\n",
     {('key_limit.sql', 'f'): False}),
    ('a stdlib decorator imported by name is plain',
     "from functools import cache\n@cache\ndef run(c, q):\n    c.execute(q)\ndef f(c):\n    run(c, 'UPDATE tr_key_limit SET reserved=0')\n",
     {('key_limit.sql', 'f'): False}),
    ('a stdlib decorator through its module is plain',
     "import functools\n@functools.lru_cache(maxsize=None)\ndef run(c, q):\n    c.execute(q)\ndef f(c):\n    run(c, 'UPDATE tr_key_limit SET reserved=0')\n",
     {('key_limit.sql', 'f'): False}),
    ('a decorator named like a stdlib one but bound otherwise',
     "cache = lambda func: lambda c, q: func(c, q + ', day_limit_micro=1')\n@cache\ndef run(c, q):\n    c.execute(q)\ndef f(c):\n    run(c, 'UPDATE tr_key_limit SET reserved=0')\n",
     {('key_limit.sql', 'f'): True, ('key_limit.escape', 'f'): True}),
    ('a method named getattr is not the builtin',
     "def f(c, obj):\n    obj.getattr('UPDATE tr_key_limit SET reserved=0', 'upper')()\n",
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ('a decorator named cache imported from elsewhere',
     "from mylib import cache\n@cache\ndef run(c, q):\n    c.execute(q)\ndef f(c):\n    run(c, 'UPDATE tr_key_limit SET reserved=0')\n",
     {('key_limit.sql', 'f'): True, ('key_limit.escape', 'f'): True}),
    ('a stdlib decorator imported, then rebound',
     "from functools import cache\ncache = lambda func: lambda c, q: func(c, q + ', day_limit_micro=1')\n@cache\ndef run(c, q):\n    c.execute(q)\ndef f(c):\n    run(c, 'UPDATE tr_key_limit SET reserved=0')\n",
     {('key_limit.sql', 'f'): True, ('key_limit.escape', 'f'): True}),
    ('getattr bound by a parameter',
     "def f(c, getattr):\n    getattr('UPDATE tr_key_limit SET reserved=0', 'upper')()\n",
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    ('a lambda default reading locals()',
     "run = lambda c, q: c.execute(q + ', day_limit_micro=1')\ndef f(c):\n    sql = 'UPDATE tr_key_limit SET reserved=0'\n    g = lambda q=locals()['sql']: run(c, q)\n    g()\n",
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    # Review round 14: a walrus in an annotation binds in the scope around the
    # function, so the module's constant of that name is not what f reads.
    ('a walrus in an annotation rebinding a module constant locally',
     "SEP = ', '\ndef f(c, make):\n    def g(x: (SEP := make())):\n        pass\n"
     "    SEP.join(['UPDATE tr_key_limit SET reserved=0'])\n",
     {('key_limit.escape', 'f'): True, ('key_limit.sql', 'f'): False}),
    # ... and eval reached as an attribute of builtins imported under another
    # name is eval (key_limit.indirect) and a namespace read (the escape).
    ('eval through builtins imported under another name',
     "import builtins as b\ndef f(c):\n    sql = 'UPDATE tr_key_limit SET reserved=0'\n    b.eval('c.execute(sql)')\n",
     {('key_limit.sql', 'f'): False, ('key_limit.indirect', 'f'): True, ('key_limit.escape', 'f'): True}),
    # ... and getattr imported under another name inside a function, then
    # passed on, is getattr passed on (key_limit.indirect).
    ('getattr imported under another name in a function, passed on',
     "def f(c):\n    from builtins import getattr as fetch\n    sql = 'UPDATE tr_key_limit SET reserved=0'\n"
     "    keep = [fetch]\n",
     {('key_limit.sql', 'f'): False, ('key_limit.indirect', 'f'): True}),
    # ... a helper rebound by another module's wrapper may change any statement
    # it is given, so in a scope that mentions the table it fails closed even
    # for text that does not name it.
    ('a helper rebound by another module\'s wrapper',
     "from wrappers import with_limit\ndef run(c, q):\n    c.execute(q)\nrun = with_limit(run)\n"
     "def f(c):\n    c.execute('SELECT reserved FROM tr_key_limit')\n    run(c, 'SELECT 1')\n",
     {('key_limit.sql', 'f'): True}),
    # ... and a nested function's dict of the same spelling is another dict:
    # its table is not the spread payload's.
    ('a nested function\'s dict of the same name',
     "def f(txn, rows):\n    payload = {'values': rows}\n    payload['table'] = 'tr_key_limit'\n"
     "    def other():\n        payload = {}\n        payload['table'] = 'tr_other'\n        return payload\n"
     "    txn.update(**payload)\n",
     {('key_limit.name', 'f'): True, ('key_limit.escape', 'f'): True}),
]


@pytest.mark.parametrize(("name", "source", "expected"), KEY_LIMIT_FORMS, ids=[c[0] for c in KEY_LIMIT_FORMS])
def test_each_way_to_write_tr_key_limit_is_found(tmp_path: Path, name: str, source: str, expected: dict) -> None:
    assert _scan_sample(tmp_path, "sample.py", source) == expected, name


NOT_WRITES = [
    ("a SELECT", "def f(t):\n    return t.execute_sql('SELECT limit_micro FROM tr_key_limit WHERE key_hash=@k')\n"),
    ("a SELECT ... FOR UPDATE",
     "def f(c):\n    c.execute('SELECT limit_micro FROM tr_key_limit WHERE key_hash = %s FOR UPDATE')\n"),
    ("a SELECT built from a constant",
     "def f(t):\n    return t.execute_sql('SELECT 1 FROM ' + T + ' WHERE key_hash=@k')\nT = 'tr_credit_balance'\n"),
    ("a docstring", "def f():\n    '''Updates nothing in tr_key_limit.'''\n"),
    ("an index name", "def f(t):\n    return t.execute_sql('SELECT 1 FROM x@{FORCE_INDEX=tr_key_limit_by_key_hash}')\n"),
    ("str.replace", "def f(s, a, b):\n    return s.replace(a, b, 1)\n"),
    ("dict.update with **", "def f(d, **fields):\n    d.update(scope=1, **fields)\n"),
    ("a read request", "def f(c, t):\n    return c.read(request={'table': t, 'columns': ['x'], 'key_set': {}})\n"),
    ("another table's mutation",
     "def f(txn, rows):\n    txn.insert_or_update(table='tr_credit_balance', columns=('workspace_id',), values=rows)\n"),
    ("a log line", "def f(log, n):\n    log.info('could not update %s keys', n)\n"),
]


@pytest.mark.parametrize(("name", "source"), NOT_WRITES, ids=[c[0] for c in NOT_WRITES])
def test_reads_and_mentions_are_not_writes(tmp_path: Path, name: str, source: str) -> None:
    assert _scan_sample(tmp_path, "sample.py", source) == {}, name


@pytest.mark.parametrize(("name", "suffix", "source", "expected"), [
    # Only a .sql file is read as SQL: DML near the table in any other text
    # (a shell argument or heredoc, YAML, Go) counts as writing every column.
    ("shell DML in an argument", ".sh",
     "gcloud spanner databases execute-sql db --sql=\"UPDATE TR_KEY_LIMIT SET reserved=0\"\n",
     {("key_limit.text", "<file>"): True}),
    ("shell heredoc DML", ".sh", "psql <<'SQL'\nUPDATE tr_key_limit SET reserved = 0 WHERE key_hash = 'k;';\nSQL\n",
     {("key_limit.text", "<file>"): True}),
    ("shell heredoc limit", ".sh", "psql <<'SQL'\nUPDATE tr_key_limit SET reserved = 0, limit_micro = 1;\nSQL\n",
     {("key_limit.text", "<file>"): True}),
    ("padded shell heredoc", ".sh", "psql <<'SQL'\nUPDATE\n\n\n\ntr_key_limit SET day_limit_micro=1;\nSQL\n",
     {("key_limit.text", "<file>"): True}),
    ("indented multiline SQL in a shell string", ".sh",
     "gcloud spanner databases execute-sql db --sql=\"\n    UPDATE\n\n        tr_key_limit\n    SET reserved = 0\"\n",
     {("key_limit.text", "<file>"): True}),
    ("a semicolon in a comment, heredoc", ".sh", "psql <<'SQL'\nUPDATE /* clear stale cap; use small test value "
     "*/ tr_key_limit SET reserved=1;\nSQL\n", {("key_limit.text", "<file>"): True}),
    ("a semicolon in a comment, YAML", ".yaml", "args: ['--sql=UPDATE /* stale cap; small value */ tr_key_limit "
     "SET reserved=1;']\n", {("key_limit.text", "<file>"): True}),
    ("a shell line continuation", ".sh", "psql <<SQL\nUP\\\nDATE tr_key_limit SET reserved=1;\nSQL\n",
     {("key_limit.text", "<file>"): True}),
    ("the backstop in a heredoc", ".sh", "psql <<'SQL'\nWITH c AS (SELECT 1) SELECT 1; -- tr_key_limit\n"
     "-- update\nSQL\necho day_limit_micro\n", {("key_limit.text", "<file>"): True}),
    ("a DDL command beside the table", ".sh",
     "gcloud spanner databases ddl update db --ddl='ALTER TABLE x ADD COLUMN y INT64'\n\n" + "echo ok\n" * 70
     + "apply_ddl 'CREATE TABLE tr_key_limit (key_hash STRING(64)) PRIMARY KEY (key_hash)'\n",
     {("key_limit.text", "<file>"): False}),
    ("SQL file delete", ".sql", "delete from tr_key_limit where key_hash = 'k';\n",
     {("key_limit.text", "<file>"): False}),
    ("SQL upsert of a limit", ".sql", "INSERT OR UPDATE tr_key_limit (key_hash, shard, day_limit_micro) "
     "VALUES ('k', 0, 1);\n", {("key_limit.text", "<file>"): True}),
    ("Go literal mutation", ".go", 'm := spanner.InsertOrUpdate("tr_key_limit", []string{"limit_micro"}, vals)\n',
     {("key_limit.text", "<file>"): True}),
    ("Go format", ".go", 'q := fmt.Sprintf("UPDATE %s SET reserved = 0", table)\n', {("sql.dynamic", "<file>"): True}),
    ("YAML job", ".yaml", "args: [\"--sql=UPDATE tr_key_limit SET week_limit_micro = 1\"]\n",
     {("key_limit.text", "<file>"): True}),
    ("a comment only", ".sh", "# tr_key_limit is created by migrate_typed_counters.sh\n", {}),
    # Review round 9: shell line continuations are joined before every check
    # (and the text is read as written too: a quoted heredoc passes them on);
    # a line in a heredoc or an open string, or after a continuation, or
    # holding another line break, is never taken for a comment.
    ("a table split by a continuation (the reviewer's)", ".sh",
     "psql <<SQL\nUPDATE tr_key_\\\nlimit SET day_limit_micro=1;\nSQL\n", {("key_limit.text", "<file>"): True}),
    ("a table split by a continuation, YAML run (the reviewer's)", ".yaml",
     "run: |\n  psql <<SQL\n  UPDATE tr_key_\\\n  limit SET day_limit_micro=1;\n  SQL\n",
     {("key_limit.text", "<file>"): True}),
    ("a continuation a quoted heredoc passes on", ".sh", "psql <<'SQL'\nUPDATE tr_key_limit\\\nSET day_limit_micro=1;\nSQL\n",
     {("key_limit.text", "<file>"): True}),
    ("a table split by a continuation, quoted heredoc", ".sh",
     "psql <<'SQL'\nUPDATE tr_key_\\\nlimit SET day_limit_micro=1;\nSQL\n", {("key_limit.text", "<file>"): True}),
    ("a table split by a continuation, command line", ".sh", "psql -c \"UPDATE tr_key_\\\nlimit SET reserved=0\"\n",
     {("key_limit.text", "<file>"): True}),
    ("a hash line in a heredoc (psql reads an operator, then a statement)", ".sh",
     "psql <<SQL\nSELECT 1\n# 0; UPDATE tr_key_limit SET day_limit_micro=1;\nSQL\n", {("key_limit.text", "<file>"): True}),
    ("a hash line in a quoted heredoc", ".sh",
     "psql <<'SQL'\nSELECT 1\n# 0; UPDATE tr_key_limit SET day_limit_micro=1;\nSQL\n",
     {("key_limit.text", "<file>"): True}),
    ("a hash line in an open string", ".sh",
     "psql -c \"SELECT 1\n# 0; UPDATE tr_key_limit SET day_limit_micro=1\"\n", {("key_limit.text", "<file>"): True}),
    ("a hash line after a continuation", ".sh",
     "psql -c SELECT\\ 1\\\n#\\ 0\\;\\ UPDATE\\ tr_key_limit\\ SET\\ day_limit_micro=1\n",
     {("key_limit.text", "<file>"): True}),
    # (A lone \r is a line feed once the file is read; YAML 1.1 readers end a
    # line at NEL, LS and PS too.)
    ("a comment holding another line break", ".yaml",
     "# note args: ['--sql=UPDATE tr_key_limit SET day_limit_micro=1']\n", {("key_limit.text", "<file>"): True}),
    ("a table from a command substitution", ".sh",
     "psql <<SQL\nUPDATE $(printf 'tr_key_%s' limit) SET day_limit_micro=1;\nSQL\n", {("sql.dynamic", "<file>"): True}),
    ("a table from a psql variable", ".sh", "psql -v t=tr_key_limit <<'SQL'\nUPDATE :t SET day_limit_micro=1;\nSQL\n",
     {("key_limit.text", "<file>"): True, ("sql.dynamic", "<file>"): True}),
    ("a continuation at the end of a comment", ".sh", "# tr_key_limit \\\necho ok\n", {}),
])
def test_each_text_form_is_found(tmp_path: Path, name: str, suffix: str, source: str, expected: dict) -> None:
    assert _scan_sample(tmp_path, "sample" + suffix, source) == expected, name


def test_the_scanner_finds_every_writer_the_chokepoints_replaced(tmp_path: Path) -> None:
    """Before the chokepoints, key creation, the limit edit, the reshard and
    the escrow each wrote the limit columns themselves; scanned as they were,
    each is a limit-column writer outside the chokepoints."""
    before = '''
    from trusted_router.storage_gcp_counters import KEY_LIMIT_COLUMNS, KEY_LIMIT_TABLE

    class SpannerApiKeys:
        def create(self, batch, key, rows):
            batch.insert_or_update(table=KEY_LIMIT_TABLE, columns=KEY_LIMIT_COLUMNS, values=rows)

        def update(self, transaction, config_rows, counters):
            transaction.insert_or_update(
                table=KEY_LIMIT_TABLE,
                columns=KEY_LIMIT_COLUMNS + KEY_LIMIT_COUNTER_COLUMNS,
                values=[config + counters[int(config[1])] for config in config_rows],
            )

    def rebalance(transaction, changed):
        transaction.update(table=KEY_LIMIT_TABLE, columns=("key_hash", "shard", "limit_micro"), values=changed)
    '''
    sample = tmp_path / "before.py"
    sample.write_text(textwrap.dedent(before))
    counters = ROOT / "src" / "trusted_router" / "storage_gcp_counters.py"
    keys = ROOT / "src" / "trusted_router" / "storage_gcp_keys.py"
    detail = scan_paths_detail([sample, counters, keys], [])
    assert {where for path, where in limit_column_writers(detail) if path == "before.py"} == {
        "SpannerApiKeys.create", "SpannerApiKeys.update", "rebalance"}


def test_scan_paths_matches_the_detail(tmp_path: Path) -> None:
    sample = tmp_path / "s.py"
    sample.write_text("def f(t):\n    t.delete('tr_key_limit', ks)\n")
    assert scan_paths([sample], []) == set(scan_paths_detail([sample], []))


def test_the_root_is_the_repository() -> None:
    assert (ROOT / "src" / "trusted_router" / "storage_key_patch.py").is_file()


# Review round 3: writes of a limit column the previous column check missed,
# each a positive control, with the forms that write none beside them.
COLUMN_FORMS = [
    ("tuple assignment", "UPDATE tr_key_limit SET (reserved, day_limit_micro) = (GREATEST(reserved - %s, 0), 5000000)"
     " WHERE key_hash = %s", True),
    ("tuple assignment, no limit", "UPDATE tr_key_limit SET (reserved, usage) = (0, 0) WHERE key_hash = %s", False),
    ("schema-qualified insert", "INSERT INTO public.tr_key_limit (workspace_id, key_hash, shard, limit_micro) "
     "VALUES (%s, %s, 0, 10)", True),
    ("quoted schema-qualified update", 'UPDATE "public"."tr_key_limit" SET "week_limit_micro" = 1', True),
    ("schema-qualified counter update", "UPDATE public.tr_key_limit SET reserved = 0 WHERE key_hash = %s", False),
    ("insert without a column list, from a query", "INSERT INTO tr_key_limit SELECT * FROM restored_key_limits", True),
    ("insert without a column list, values", "INSERT INTO tr_key_limit VALUES (%s, %s, 0, 10)", True),
    ("qualified target through an alias", "UPDATE tr_key_limit AS k SET k.day_limit_micro = 1 WHERE k.key_hash = %s",
     True),
    ("a limit after a subquery", "UPDATE tr_key_limit SET reserved = (SELECT 0 FROM x), limit_micro = 5", True),
    ("a block comment before the table", "UPDATE /* repair */ tr_key_limit SET month_limit_micro = 1", True),
    ("upsert's update", "INSERT INTO tr_key_limit (key_hash, shard) VALUES (%s, 0) ON CONFLICT (workspace_id, "
     "key_hash, shard) DO UPDATE SET limit_micro = 1", True),
    ("upsert that does nothing", "INSERT INTO tr_key_limit (key_hash, shard) VALUES (%s, 0) ON CONFLICT "
     "(workspace_id, key_hash, shard) DO NOTHING", False),
    ("a target filled in", "UPDATE tr_key_limit SET {} WHERE key_hash = %s", True),
    ("an expression as a target", "UPDATE tr_key_limit SET coalesce(x) = 1", True),
    ("DML on another table, a read of this one", "UPDATE tr_key_limit_shadow SET a = 1; SELECT 1 FROM tr_key_limit",
     False),
    ("a delete", "DELETE FROM tr_key_limit WHERE key_hash = %s", False),
    ("a merge", "MERGE INTO tr_key_limit USING x ON y WHEN MATCHED THEN UPDATE SET reserved = 0", True),
    # Review round 4: a boundary inside quotes or a comment is none. The
    # reviewer's statement, then each quoting form.
    ("a semicolon in a string (the reviewer's)", "UPDATE tr_key_limit SET reserved = GREATEST(reserved - %s, "
     "length(';') - 1), day_limit_micro = 5000000 WHERE key_hash = %s", True),
    ("a semicolon in a string, no limit", "UPDATE tr_key_limit SET reserved = length(';') WHERE key_hash = 'a;b'",
     False),
    ("a doubled quote", "UPDATE tr_key_limit SET reserved = length('it''s; where'), day_limit_micro = 1", True),
    ("a doubled quote, no limit", "UPDATE tr_key_limit SET reserved = length('it''s; where') WHERE key_hash = %s",
     False),
    ("double quotes", 'UPDATE tr_key_limit SET reserved = length("a; from b"), week_limit_micro = 1', True),
    ("backticks", "UPDATE tr_key_limit SET reserved = length(`x;`), month_limit_micro = 1", True),
    ("a dollar quote", "UPDATE tr_key_limit SET reserved = length($$; where $$), limit_micro = 1", True),
    ("a tagged dollar quote", "UPDATE tr_key_limit SET reserved = length($q$; where $q$), limit_micro = 1", True),
    # The same with the quoted boundary at the top of the SET list, where
    # only the tokenizer keeps it from ending the list.
    ("a top-level semicolon in a string", "UPDATE tr_key_limit SET reserved = CASE WHEN key_hash = ';' THEN 0 "
     "ELSE reserved END, day_limit_micro = 1", True),
    ("a top-level boundary in double quotes", 'UPDATE tr_key_limit SET reserved = CASE WHEN key_hash = "; where" '
     'THEN 0 ELSE reserved END, week_limit_micro = 1', True),
    ("a top-level boundary in a dollar quote", "UPDATE tr_key_limit SET reserved = CASE WHEN key_hash = $$; where$$ "
     "THEN 0 ELSE reserved END, limit_micro = 1", True),
    ("a top-level boundary in a tagged dollar quote", "UPDATE tr_key_limit SET reserved = CASE WHEN key_hash = "
     "$k$; where$k$ THEN 0 ELSE reserved END, month_limit_micro = 1", True),
    ("a line comment", "UPDATE tr_key_limit SET reserved = 0 -- ; where\n, day_limit_micro = 1", True),
    ("a block comment", "UPDATE tr_key_limit SET reserved = 0 /* ; where */, day_limit_micro = 1", True),
    ("an unterminated quote", "UPDATE tr_key_limit SET reserved = 'open, day_limit_micro = 1", True),
    ("UPDATE ONLY, schema-qualified", "UPDATE ONLY public.tr_key_limit SET day_limit_micro = 5 WHERE key_hash = %s",
     True),
    ("a CTE before the update", "WITH x AS (SELECT 5 AS cap) UPDATE public.tr_key_limit SET (reserved, "
     "day_limit_micro) = (0, (SELECT cap FROM x)) WHERE key_hash = %s", True),
    ("an update inside a CTE", "WITH u AS (UPDATE tr_key_limit SET reserved = 0 RETURNING key_hash) "
     "SELECT * FROM u", True),
    # Review round 6: the reviewer's probes, then what the grammar fails
    # closed on by construction.
    ("a nested block comment (the reviewer's)", "UPDATE tr_key_limit SET reserved = 0 /* outer /* inner */ ; */ , "
     "day_limit_micro = 1 WHERE key_hash = %s", True),
    ("a write beside one the grammar does not clear (the reviewer's)", "WITH u AS (UPDATE tr_key_limit SET reserved = 0 "
     "WHERE FALSE RETURNING key_hash) UPDATE ONLY (tr_key_limit) SET day_limit_micro = 1 WHERE key_hash = %s", True),
    ("a cleared statement beside one that is not", "UPDATE tr_key_limit SET reserved = 0; "
     "UPDATE ONLY (tr_key_limit) SET day_limit_micro = 1", True),
    ("a cleared statement beside a COPY", "UPDATE tr_key_limit SET reserved = 0; COPY tr_key_limit FROM STDIN", True),
    ("a block comment that hides nothing", "UPDATE tr_key_limit SET reserved = 0 /* note */ WHERE key_hash = %s", True),
    ("a hash comment", "UPDATE tr_key_limit SET reserved = 0 # ;\n , day_limit_micro = 1", True),
    ("a backslash in a string", "UPDATE tr_key_limit SET reserved = length('a\\'), day_limit_micro = 1 WHERE x = 'b'",
     True),
    ("a triple-quoted string", "UPDATE tr_key_limit SET reserved = length(\'\'\'a\'b\'\'\'), day_limit_micro = 1",
     True),
    ("a prefixed literal", "UPDATE tr_key_limit SET reserved = length(E'x') WHERE key_hash = %s", True),
    ("a parenthesized target", "UPDATE (tr_key_limit) SET reserved = 0", True),
    ("ONLY", "UPDATE ONLY tr_key_limit SET reserved = 0", True),
    ("a quoted target", 'UPDATE "tr_key_limit" SET reserved = 0', True),
    ("a three-part target", "UPDATE db.public.tr_key_limit SET reserved = 0", True),
    ("a write word in an expression", "UPDATE tr_key_limit SET reserved = (SELECT 0 FROM x WHERE y IN "
     "(DELETE FROM z RETURNING 1))", True),
    ("FROM after the SET list", "UPDATE tr_key_limit SET reserved = 0 FROM other WHERE other.k = key_hash", True),
    ("a placeholder filled in at run time", "UPDATE tr_key_limit SET reserved = {} WHERE key_hash = %s", True),
    ("a subquery in the SET list, no limit", "UPDATE tr_key_limit SET reserved = (SELECT 0 FROM x) WHERE key_hash = %s",
     False),
    ("an upsert updating a counter", "INSERT INTO tr_key_limit (key_hash, shard) VALUES (%s, 0) ON CONFLICT "
     "(workspace_id, key_hash, shard) DO UPDATE SET reserved = 0", False),
    ("a dollar quote, no limit", "UPDATE tr_key_limit SET reserved = length($$; where$$) WHERE key_hash = %s", False),
    ("a cleared counter update", "@{LOCK_SCANNED_RANGES=EXCLUSIVE} UPDATE tr_key_limit SET reserved = reserved + @est "
     "WHERE key_hash = @kh AND shard = @shard THEN RETURN reserved", False),
    # Review round 7.
    ("a line comment ended by a carriage return", "UPDATE tr_key_limit SET reserved=0 -- note\r, day_limit_micro=1",
     True),
    ("a line comment ended by CR LF, no limit", "UPDATE tr_key_limit SET reserved=0 -- note\r\n WHERE key_hash = %s",
     False),
    ("another line end inside a line comment", "UPDATE tr_key_limit SET reserved = 0 -- a\x0b, day_limit_micro = 1"
     "\n WHERE key_hash = %s", True),
    ("a doubled quote inside a doubled quote, no limit", "UPDATE tr_key_limit SET reserved=length('a''''b')", False),
    ("padded multiline SQL", "UPDATE\n\n\n\ntr_key_limit SET day_limit_micro=1", True),
    ("Spanner's INSERT OR UPDATE keeps the columns it omits", "INSERT OR UPDATE INTO tr_key_limit (key_hash, shard, "
     "reserved) VALUES (@kh, 0, 0)", False),
]


@pytest.mark.parametrize(("name", "sql", "writes_limit"), COLUMN_FORMS, ids=[c[0] for c in COLUMN_FORMS])
def test_each_way_to_write_a_limit_column_is_found(tmp_path: Path, name: str, sql: str, writes_limit: bool) -> None:
    from tests.key_limit_writers import assigns_limit_column

    assert assigns_limit_column(sql) is writes_limit, name
    source = f"def f(conn):\n    conn.execute({sql!r})\n"
    assert _scan_sample(tmp_path, "sample.py", source) == {("key_limit.sql", "f"): writes_limit}, name
    assert _scan_sample(tmp_path, "sample.sql", sql + ";\n") == {("key_limit.text", "<file>"): writes_limit}, name


def test_a_cross_module_helpers_statement_fails_closed(tmp_path: Path) -> None:
    """Review round 7's "Cross-module helper" probe: a scope that writes the
    table runs a statement an imported helper returns. The helper's text is
    not followed across modules, so the call counts as writing every
    column (tests/key_limit_writers.py, threat model); since review round
    10 the statement given to the helper is an escape too."""
    helper = tmp_path / "probe_helper.py"
    helper.write_text("def append_limit(sql):\n    return sql + ', day_limit_micro=1'\n")
    caller = tmp_path / "caller.py"
    caller.write_text("from probe_helper import append_limit\ndef f(c):\n"
                      "    c.execute(append_limit('UPDATE tr_key_limit SET reserved=0'))\n")
    detail = scan_paths_detail([helper, caller], [])
    assert detail == {("key_limit.sql", "caller.py", "f"): True, ("key_limit.escape", "caller.py", "f"): True}


def test_the_escape_rule_follows_a_constant_from_another_module(tmp_path: Path) -> None:
    """Review round 10: a module constant naming the table, imported and
    given to a callee that is neither an executor nor a mutation, escapes
    where it is given; given to a mutation, it does not."""
    constants = tmp_path / "probe_tables.py"
    constants.write_text("KEY_TABLE = 'tr_key_limit'\nSTATEMENTS = {'reserve': 'UPDATE tr_key_limit SET reserved=0'}\n")
    caller = tmp_path / "probe_caller.py"
    caller.write_text("from probe_tables import KEY_TABLE, STATEMENTS\ndef f(store):\n    return store.fetch(KEY_TABLE, ['kh'])\n"
                      "def g(tx):\n    tx.update(KEY_TABLE, ('key_hash', 'shard', 'reserved'), [])\n"
                      "def h(store):\n    store.run_later(STATEMENTS['reserve'])\n")
    detail = scan_paths_detail([constants, caller], [])
    assert detail.get(("key_limit.escape", "probe_caller.py", "f")) is True
    assert detail.get(("key_limit.escape", "probe_caller.py", "h")) is True
    assert ("key_limit.escape", "probe_caller.py", "g") not in detail


def test_the_escape_rule_follows_an_attribute_any_binding_of_which_names_the_table(tmp_path: Path) -> None:
    """Review round 12: an attribute read in another module is followed when
    any binding of it names the table, not only when every binding resolves
    (here another module binds the same attribute name to a parameter)."""
    constants = tmp_path / "probe_queries.py"
    constants.write_text("class ProbeQueries:\n    sql = 'UPDATE tr_key_limit SET reserved=0'\n")
    caller = tmp_path / "probe_reader.py"
    caller.write_text("from probe_queries import ProbeQueries\ndef set_sql(obj, text):\n    obj.sql = text\n"
                      "def f(store):\n    store.run_later(ProbeQueries.sql)\n")
    detail = scan_paths_detail([constants, caller], [])
    assert detail.get(("key_limit.escape", "probe_reader.py", "f")) is True


_PG = "src/trusted_router/storage_postgres.py"
_RELEASE_END = "\n    @staticmethod\n    def _recorded_key_hold"


def _injected(tmp_path: Path, relative: str, change: str, helpers: str = "") -> set[tuple[str, str]]:
    source = (ROOT / relative).read_text()
    if relative == _PG:
        assert source.count(_RELEASE_END) == 1
        where = source.index(_RELEASE_END)
        source = source[:where] + "\n" + textwrap.indent(textwrap.dedent(change), "        ") + source[where:]
        source += "\n\ndef add_cap(sql):\n    return sql + ', day_limit_micro=1'\n" + helpers
    elif relative.endswith(".py"):
        needle = '        return {"keys": len(plan)}'
        assert source.count(needle) == 1
        source = source.replace(needle, textwrap.indent(textwrap.dedent(change), "        ") + needle)
    else:
        source += "\n" + change
    injected = tmp_path / Path(relative).name
    injected.write_text(source)
    detail = scan_paths_detail([injected], []) if injected.suffix == ".py" else scan_paths_detail([], [injected])
    return limit_column_writers(detail)


@pytest.mark.parametrize(("relative", "scope", "change"), [
    (_PG, "PostgresStore._release_key_hold_tx", """
        sql = 'UPDATE tr_key_limit SET reserved=0'
        sql += ', day_limit_micro=1'
        conn.execute(sql)
    """),
    (_PG, "PostgresStore._release_key_hold_tx", """
        sql = 'UPDATE tr_key_limit SET reserved=0'.replace('reserved=0', 'day_limit_micro=1')
        conn.execute(sql)
    """),
    (_PG, "PostgresStore._release_key_hold_tx", """
        fragment = str(key_hash)
        conn.execute(f"UPDATE tr_key_limit SET reserved=length('{fragment}')")
    """),
    ("src/trusted_router/storage_gcp_counter_reconcile.py", "repair_typed_reserved._txn", """
        columns = ['key_hash', 'shard', 'reserved']
        columns.append('day_limit_micro')
        transaction.update('tr_key_limit', columns, [('probe', 0, 0, 1)])
    """),
    ("src/trusted_router/storage_gcp_counter_reconcile.py", "repair_typed_reserved._txn", """
        transaction.replace('tr_key_limit', ('key_hash', 'shard', 'usage', 'reserved'), [('probe', 0, 0, 0)])
    """),
], ids=["augmented SQL", "str.replace SQL", "quoted f-string", "mutable mutation columns", "REPLACE mutation"])
def test_the_round_7_probes_injected_into_listed_scopes_are_caught(tmp_path: Path, relative: str, scope: str,
                                                                   change: str) -> None:
    """Review round 7's injections, each into a scope the inventory lists:
    the chokepoint-only check names the scope a limit-column writer."""
    assert (Path(relative).name, scope) in _injected(tmp_path, relative, change)
    clean = tmp_path / "clean"
    clean.mkdir()
    assert (Path(relative).name, scope) not in _injected(clean, relative, "")


_ROUND_7 = {
    "conditional +=": (_PG, "PostgresStore._release_key_hold_tx", """
        comment = False
        probe_sql = 'UPDATE tr_key_limit SET reserved=0'
        if comment:
            probe_sql += ' -- no cap'
        else:
            probe_sql += ', day_limit_micro=1'
        conn.execute(probe_sql)
    """),
    "named format alternatives": (_PG, "PostgresStore._release_key_hold_tx", """
        counter = False
        probe_col = 'reserved' if counter else 'day_limit_micro'
        conn.execute('UPDATE tr_key_limit SET {col}=1'.format(col=probe_col))
    """),
    "aliased executor": (_PG, "PostgresStore._release_key_hold_tx", """
        probe_sql = 'UPDATE tr_key_limit SET reserved=0'
        probe_sql += ', day_limit_micro=1'
        probe_run = conn.execute
        probe_run(probe_sql)
    """),
    "aliased executor unknown piece": (_PG, "PostgresStore._release_key_hold_tx", """
        probe_sql = 'UPDATE tr_key_limit SET reserved=0'
        probe_sql += add_cap('')
        probe_run = conn.execute
        probe_run(probe_sql)
    """),
    "SELECT prefix": (_PG, "PostgresStore._release_key_hold_tx", """
        probe_sql = 'UPDATE tr_key_limit SET reserved=0'
        conn.execute('SELECT 1; ' + add_cap(probe_sql))
    """),
    "SET LOCAL prefix": (_PG, "PostgresStore._release_key_hold_tx", """
        probe_sql = 'UPDATE tr_key_limit SET reserved=0'
        conn.execute('SET LOCAL statement_timeout=1000; ' + add_cap(probe_sql))
    """),
    "mutable mutation payload": ("src/trusted_router/storage_gcp_counter_reconcile.py", "repair_typed_reserved._txn", """
        payload = {'update': {
            'table': 'tr_key_limit', 'columns': ['key_hash', 'shard', 'reserved'],
            'values': [('probe', 0, 0, 1)]}}
        payload['update']['columns'].append('day_limit_micro')
        transaction.update(**payload['update'])
    """),
}


@pytest.mark.parametrize("label", list(_ROUND_7))
def test_the_round_7_reviewers_injections_into_listed_scopes_are_caught(tmp_path: Path, label: str) -> None:
    """Review round 7's variants, injected where the reviewer put them (a
    helper add_cap beside the module): the chokepoint-only check names the
    scope a limit-column writer."""
    relative, scope, change = _ROUND_7[label]
    assert (Path(relative).name, scope) in _injected(tmp_path, relative, change)


def _injected_detail(tmp_path: Path, relative: str, source: str) -> Any:
    injected = tmp_path / Path(relative).name
    injected.write_text(source)
    return scan_paths_detail([injected], []) if injected.suffix == ".py" else scan_paths_detail([], [injected])


def test_a_write_injected_into_the_strict_reserve_fails_its_review_and_its_pin(tmp_path: Path) -> None:
    """Review round 7's "batch DML", injected into reserve_strict_key, a
    reviewed exception: the scope's fingerprint no longer matches the one it
    was reviewed at, and its pin, running the changed function, sees the
    limit written."""
    import inspect

    from tests.test_key_limit_writer_exceptions import _Capture
    from trusted_router import storage_gcp_strict_budget as strict

    relative = "src/trusted_router/storage_gcp_strict_budget.py"
    scope = (relative, "reserve_strict_key")
    body = ("    probe_sql = 'UPDATE tr_key_limit SET reserved=0'\n    probe_sql += ', day_limit_micro=1'\n"
            "    transaction.batch_update([probe_sql])\n")
    source = (ROOT / relative).read_text().replace("    now = utcnow()\n", body + "    now = utcnow()\n", 1)
    detail = _injected_detail(tmp_path, relative, source)
    assert detail.fingerprints[(Path(relative).name, "reserve_strict_key")] != REVIEWED_FINGERPRINTS[scope]
    function = inspect.getsource(strict.reserve_strict_key).replace("    now = utcnow()\n", body + "    now = utcnow()\n", 1)
    namespace = dict(vars(strict))
    exec(compile(function, strict.__file__, "exec"), namespace)  # noqa: S102 - the injected function, run by its pin
    transaction = _Capture(count=1, rows=[(True, 0, 10, 0, None, 20, 0, None, 30, 0, None)])
    namespace["reserve_strict_key"](transaction, _ParamTypes(), "kh", 5, is_byok=False, enforce_windows=True)
    assert any("day_limit_micro" in (written_columns(sql) or {"day_limit_micro"}) for sql in transaction.statements)


@pytest.mark.parametrize("heredoc", [
    "psql <<'SQL'\nUPDATE\n\n\n\ntr_key_limit SET day_limit_micro=1;\nSQL\n",
    "psql <<'SQL'\nUPDATE /* clear stale cap; use small test value */ tr_key_limit SET day_limit_micro=1;\nSQL\n",
    "psql <<SQL\nUP\\\nDATE tr_key_limit SET day_limit_micro=1;\nSQL\n",
    "psql <<SQL\nUPDATE tr_key_\\\nlimit SET day_limit_micro=1;\nSQL\n",
], ids=["padded heredoc", "comment semicolon", "shell line continuation", "split table (round 9)"])
def test_a_write_appended_to_the_migration_fails_its_review_and_its_pin(tmp_path: Path, heredoc: str) -> None:
    """The round-6 and round-7 shell injections, appended to
    migrate_typed_counters.sh, a reviewed exception: its fingerprint no
    longer matches, and its pin, running the script with only a recording
    gcloud on PATH, fails on the psql it does not expect."""
    import subprocess

    from tests.test_key_limit_writer_exceptions import MIGRATION, run_migration

    relative = "scripts/deploy/migrate_typed_counters.sh"
    source = MIGRATION.read_text() + "\n" + heredoc
    detail = _injected_detail(tmp_path, relative, source)
    assert detail.fingerprints[(Path(relative).name, "<file>")] != REVIEWED_FINGERPRINTS[(relative, "<file>")]
    with pytest.raises(subprocess.CalledProcessError):
        run_migration(tmp_path / "run", source)


def test_the_reviewers_unreadable_write_beside_a_cleared_one_is_caught(tmp_path: Path) -> None:
    """Review round 5's second probe: _release_key_hold_tx's UPDATE turned
    into a cleared CTE write followed by UPDATE ONLY (tr_key_limit) ... SET
    day_limit_micro. The unreadable statement fails closed although the
    other parses, and the chokepoint-only check names the scope."""
    source = (ROOT / "src/trusted_router/storage_postgres.py").read_text()
    begin = source.index("    def _release_key_hold_tx(")
    end = source.index("\n    def ", begin + 1)
    method = source[begin:end]
    needle = '"UPDATE tr_key_limit SET"'
    assert method.count(needle) == 1
    method = method.replace(needle, '"WITH unused AS (UPDATE tr_key_limit SET reserved = reserved WHERE FALSE '
                                    'RETURNING key_hash) UPDATE ONLY (tr_key_limit) SET"').replace(
        '"   reserved = GREATEST(reserved - %s, 0)"', '"   reserved = GREATEST(reserved - %s, 0), day_limit_micro = 5000000"')
    injected = tmp_path / "storage_postgres.py"
    injected.write_text(source[:begin] + method + source[end:])
    assert ("storage_postgres.py", "PostgresStore._release_key_hold_tx") in limit_column_writers(
        scan_paths_detail([injected], []))


@pytest.mark.parametrize("assignment", [
    '"   (reserved, day_limit_micro) = (GREATEST(reserved - %s, 0), 5000000)"',  # review round 3
    '"   reserved = GREATEST(reserved - %s, length(\';\') - 1), day_limit_micro = 5000000"',  # review round 4
    '"   reserved = GREATEST(reserved - %s, 0) /* outer /* inner */ ; */ , day_limit_micro = 5000000"',  # round 5
    '"   reserved = GREATEST(reserved - %s, 0) -- note\\r, day_limit_micro = 5000000"',  # round 7
], ids=["tuple assignment", "semicolon in a string", "nested block comment", "carriage return"])
def test_the_reviewers_injected_cap_write_is_caught(tmp_path: Path, assignment: str) -> None:
    """The reviewers' probes: a limit column written from a listed counter
    writer (PostgresStore._release_key_hold_tx). The column check names that
    scope a limit-column writer outside the chokepoints."""
    source = (ROOT / "src/trusted_router/storage_postgres.py").read_text()
    old = '"   reserved = GREATEST(reserved - %s, 0)"'
    assert source.count(old) == 1
    injected = tmp_path / "storage_postgres.py"
    injected.write_text(source.replace(old, assignment))
    writers = limit_column_writers(scan_paths_detail([injected], []))
    assert ("storage_postgres.py", "PostgresStore._release_key_hold_tx") in writers
    clean = tmp_path / "clean" / "storage_postgres.py"
    clean.parent.mkdir()
    clean.write_text(source)
    assert ("storage_postgres.py", "PostgresStore._release_key_hold_tx") not in limit_column_writers(
        scan_paths_detail([clean], []))


# Review round 9: the reviewer's injections and the sweep's, each into
# PostgresStore._release_key_hold_tx, a listed counter writer (add_cap and
# any helpers beside the module): the chokepoint-only check names the scope,
# or a function nested in it, a limit-column writer.
_ROUND_9 = {
    "getattr executor (the reviewer's)": ("""
        probe_sql = 'UPDATE tr_key_limit SET reserved=0'
        getattr(conn, 'execute')(add_cap(probe_sql))
    """, ""),
    "getattr mutation (the reviewer's)": ("""
        getattr(conn, 'insert_or_update')('tr_key_limit', ('key_hash', 'shard', 'day_limit_micro'), [('k', 0, 1)])
    """, ""),
    "an alias of getattr": ("""
        probe_fetch(conn, 'execute')(add_cap('UPDATE tr_key_limit SET reserved=0'))
    """, "probe_fetch = getattr\n"),
    "getattr by a computed name": ("""
        probe_name = 'exe' + 'cute'
        getattr(conn, probe_name)('UPDATE tr_key_limit SET reserved=0')
    """, ""),
    "operator.methodcaller": ("""
        operator.methodcaller('execute', add_cap('UPDATE tr_key_limit SET reserved=0'))(conn)
    """, "import operator\n"),
    "functools.partial": ("""
        functools.partial(conn.execute)(add_cap('UPDATE tr_key_limit SET reserved=0'))
    """, "import functools\n"),
    "a setattr-built executor": ("""
        probe_holder = types.SimpleNamespace()
        setattr(probe_holder, 'run', conn.execute)
        probe_holder.run(add_cap('UPDATE tr_key_limit SET reserved=0'))
    """, "import types\n"),
    "exec": ("""
        exec(probe_code)
    """, "probe_code = 'pass'\n"),
    "a decorator wrapping an executor": ("""
        probe_with_cap(conn.execute)('UPDATE tr_key_limit SET reserved=0')
    """, "def probe_with_cap(execute):\n    return lambda sql: execute(sql + ', day_limit_micro=1')\n"),
    "a nested helper (the reviewer's)": ("""
        probe_sql = 'UPDATE tr_key_limit SET reserved=0'
        def probe_run(q):
            conn.execute(q + ', day_limit_micro=1')
        probe_run(probe_sql)
    """, ""),
    "a nested lambda": ("""
        probe_run = lambda q: conn.execute(add_cap(q))
        probe_run('UPDATE tr_key_limit SET reserved=0')
    """, ""),
    "a helper of the module that changes the statement": ("""
        probe_run(conn, 'UPDATE tr_key_limit SET reserved=0')
    """, "def probe_run(c, q):\n    c.execute(q + ', day_limit_micro=1')\n"),
    "a decorated helper of the module": ("""
        probe_run(conn, 'UPDATE tr_key_limit SET reserved=0')
    """, "def probe_with_cap(fn):\n    return lambda c, q: fn(c, q + ', day_limit_micro=1')\n"
         "@probe_with_cap\ndef probe_run(c, q):\n    c.execute(q)\n"),
    "a pass-through helper given a statement not known": ("""
        probe_run(conn, add_cap('UPDATE tr_key_limit SET reserved=0'))
    """, "def probe_run(c, q):\n    c.execute(q)\n"),
}


@pytest.mark.parametrize("label", list(_ROUND_9))
def test_the_round_9_injections_into_a_listed_scope_are_caught(tmp_path: Path, label: str) -> None:
    change, helpers = _ROUND_9[label]
    scope = "PostgresStore._release_key_hold_tx"
    writers = _injected(tmp_path, _PG, change, helpers)
    assert any(path == "storage_postgres.py" and (where == scope or where.startswith(scope + "."))
               for path, where in writers), label


_SPLIT_TABLE = "psql <<SQL\nUPDATE tr_key_\\\nlimit SET day_limit_micro=1;\nSQL\n"


@pytest.mark.parametrize(("relative", "change"), [
    ("scripts/deploy/clickhouse_capacity_smoke.sh", _SPLIT_TABLE),
    ("scripts/deploy/clickhouse_capacity_smoke.sh", _SPLIT_TABLE.replace("<<SQL", "<<'SQL'")),
    (".github/workflows/cloud-staleness.yml", "      - run: |\n" + textwrap.indent(_SPLIT_TABLE, " " * 10)),
], ids=["shell", "shell, quoted heredoc", "YAML run"])
def test_the_split_table_appended_to_a_file_is_a_limit_writer(tmp_path: Path, relative: str, change: str) -> None:
    """Review round 9's shell and YAML probe, appended to a file that does
    not name the table: the file becomes a limit-column writer."""
    name = Path(relative).name
    assert (name, "<file>") in _injected(tmp_path, relative, change)
    clean = tmp_path / "clean"
    clean.mkdir()
    assert (name, "<file>") not in _injected(clean, relative, "")


_COUNTER_DML_PATH = "src/trusted_router/storage_gcp_counter_dml.py"


def _redefined(how: str) -> str:
    """storage_gcp_counter_dml.py with reserve_key_statement defined again:
    a version that assigns a limit when amount == 6 (the reviewer's)."""
    import ast
    import inspect

    from trusted_router import storage_gcp_counter_dml as counter

    source = (ROOT / _COUNTER_DML_PATH).read_text()
    function = inspect.getsource(counter.reserve_key_statement)
    assert function.count("    return (") == 1
    mutant = function.replace("    return (", "    if amount == 6:\n"
                              "        return ('UPDATE tr_key_limit SET day_limit_micro=1', {}, {})\n    return (")
    if how == "conditional definitions (the reviewer's)":
        tree = ast.parse(source)
        original = next(node for node in tree.body
                        if isinstance(node, ast.FunctionDef) and node.name == "reserve_key_statement")
        tree.body[tree.body.index(original)] = ast.If(test=ast.Constant(value=False), body=[original],
                                                      orelse=[ast.parse(mutant).body[0]])
        return ast.unparse(ast.fix_missing_locations(tree))
    if how == "a later definition":
        return source + "\n\n" + mutant
    if how == "a rebinding":
        return source + "\n\nreserve_key_statement = release_key_statement\n"
    if how == "a walrus in another function's default":
        # A default runs in the scope around the function: this binds the module's name.
        return source + "\n\ndef probe_signature(x=(reserve_key_statement := release_key_statement)):\n    return x\n"
    if how == "unparsed, unchanged":
        return ast.unparse(ast.parse(source))
    assert how == "an edit elsewhere in the module"
    return source + "\n\ndef probe_unrelated():\n    return 1\n"


@pytest.mark.parametrize(("how", "changes"), [
    ("conditional definitions (the reviewer's)", True),
    ("a later definition", True),
    ("a rebinding", True),
    ("a walrus in another function's default", True),
    ("unparsed, unchanged", False),
    ("an edit elsewhere in the module", False),
])
def test_an_exceptions_fingerprint_covers_every_definition_of_its_name(tmp_path: Path, how: str,
                                                                       changes: bool) -> None:
    """Review round 9: an exception's fingerprint covers every definition
    sharing its qualified name in the module (a conditional or later def)
    and every rebinding of the name; positions, formatting and other
    scopes aside."""
    scope = (_COUNTER_DML_PATH, "reserve_key_statement")
    detail = _injected_detail(tmp_path, _COUNTER_DML_PATH, _redefined(how))
    fingerprint = detail.fingerprints[(Path(_COUNTER_DML_PATH).name, "reserve_key_statement")]
    assert (fingerprint != REVIEWED_FINGERPRINTS[scope]) is changes, how


def test_a_class_bodys_duplicate_method_changes_the_exceptions_fingerprint(tmp_path: Path) -> None:
    """A second def of PostgresStore.reserve_key_limit (the later one is the
    method) changes the fingerprint of the exception nested in it."""
    source = (ROOT / _PG).read_text()
    where = source.index(_RELEASE_END)
    duplicate = "\n    def reserve_key_limit(self, *args, **kwargs):\n        return None\n"
    detail = _injected_detail(tmp_path, _PG, source[:where] + duplicate + source[where:])
    scope = (_PG, "PostgresStore.reserve_key_limit.reserve")
    assert detail.fingerprints[("storage_postgres.py", scope[1])] != REVIEWED_FINGERPRINTS[scope]


@pytest.mark.parametrize("rebinding", [
    "PostgresStore.reserve_key_limit = lambda self, *args, **kwargs: None\n",
    "setattr(PostgresStore, 'reserve_key_limit', lambda self, *args, **kwargs: None)\n",
])
def test_a_method_rebound_through_its_class_changes_the_exceptions_fingerprint(tmp_path: Path,
                                                                                rebinding: str) -> None:
    """Review round 14: Cls.method = ... (or setattr) rebinds the method an
    exception is nested in, so its fingerprint holds that statement too."""
    source = (ROOT / _PG).read_text() + "\n\n" + rebinding
    detail = _injected_detail(tmp_path, _PG, source)
    scope = (_PG, "PostgresStore.reserve_key_limit.reserve")
    assert detail.fingerprints[("storage_postgres.py", scope[1])] != REVIEWED_FINGERPRINTS[scope]


# Review round 10: the reviewer's five forms, and the escape rule's default
# path by constructs the review did not use, each injected into
# PostgresStore._release_key_hold_tx (helpers beside the module): the escape
# rule names the scope a limit-column writer, under a finding of its own.
_ROUND_10 = {
    "a helper indexing the statement (the reviewer's)": ("""
        probe_run(conn, ['UPDATE tr_key_limit SET reserved=0'])
    """, "def probe_run(c, batch):\n    c.execute(batch[0] + ', day_limit_micro=1')\n"),
    "a helper unpacking the statement (the reviewer's)": ("""
        probe_run(conn, ['UPDATE tr_key_limit SET reserved=0'])
    """, "def probe_run(c, batch):\n    q, = batch\n    c.execute(q + ', day_limit_micro=1')\n"),
    "a helper taking **kwargs (the reviewer's)": ("""
        probe_run(conn, sql='UPDATE tr_key_limit SET reserved=0')
    """, "def probe_run(c, **kw):\n    c.execute(kw['sql'] + ', day_limit_micro=1')\n"),
    "a lambda helper (the reviewer's)": ("""
        probe_run(conn, 'UPDATE tr_key_limit SET reserved=0')
    """, "probe_run = lambda c, q: c.execute(q + ', day_limit_micro=1')\n"),
    "a callable object (the reviewer's)": ("""
        probe_run(conn, 'UPDATE tr_key_limit SET reserved=0')
    """, "class ProbeRunner:\n    def __call__(self, c, q):\n        c.execute(q + ', day_limit_micro=1')\n"
         "probe_run = ProbeRunner()\n"),
    "a generator": ("""
        probe_run_all(conn, (s for s in ['UPDATE tr_key_limit SET reserved=0']))
    """, "probe_run_all = lambda c, statements: [c.execute(q + ', day_limit_micro=1') for q in statements]\n"),
    "functools.reduce": ("""
        conn.execute(functools.reduce(operator.add, ['UPDATE tr_key_limit SET reserved=0', ', day_limit_micro=1']))
    """, "import functools\nimport operator\n"),
    "a dataclass field": ("""
        probe_statement = ProbeStatement('UPDATE tr_key_limit SET reserved=0')
        conn.execute(probe_statement.sql + ', day_limit_micro=1')
    """, "import dataclasses\n@dataclasses.dataclass\nclass ProbeStatement:\n    sql: str\n"),
    "a walrus": ("""
        if (probe_sql := 'UPDATE tr_key_limit SET reserved=0'):
            probe_run(conn, probe_sql)
    """, "probe_run = lambda c, q: c.execute(q + ', day_limit_micro=1')\n"),
    "map": ("""
        list(map(lambda q: conn.execute(q + ', day_limit_micro=1'), ['UPDATE tr_key_limit SET reserved=0']))
    """, ""),
}


@pytest.mark.parametrize("label", list(_ROUND_10))
def test_the_round_10_injections_into_a_listed_scope_are_caught(tmp_path: Path, label: str) -> None:
    change, helpers = _ROUND_10[label]
    scope = "PostgresStore._release_key_hold_tx"
    source = (ROOT / _PG).read_text()
    where = source.index(_RELEASE_END)
    source = (source[:where] + "\n" + textwrap.indent(textwrap.dedent(change), "        ") + source[where:]
              + "\n\n" + helpers)
    detail = _injected_detail(tmp_path, _PG, source)
    assert detail.get(("key_limit.escape", "storage_postgres.py", scope)) is True, label
    assert ("storage_postgres.py", scope) in limit_column_writers(detail)


# Review round 11: the reviewer's four forms, and more routes by which an
# allowed step could hand the value to user code, each injected into
# PostgresStore._release_key_hold_tx (helpers beside the module).
_PROBE_RUNNER = ("class ProbeRunner:\n    def __init__(self, c):\n        self.c = c\n"
                 "    def run(self, q):\n        self.c.execute(q + ', day_limit_micro=1')\n        return True\n"
                 "    def __call__(self, q):\n        return self.run(q)\n    def write(self, q):\n        self.run(q)\n"
                 "    def __getitem__(self, q):\n        return self.run(q)\n    def __contains__(self, q):\n"
                 "        return self.run(q)\n    def __radd__(self, q):\n        return self.run(q)\n"
                 "    def __eq__(self, q):\n        return self.run(q)\n")
_ROUND_11 = {
    "a dict's setdefault result (the reviewer's)": ("""
        probe_statements = {'sql': 'UPDATE tr_key_limit SET reserved=0'}
        probe_run(conn, probe_statements.setdefault('sql', ''))
    """, "probe_run = lambda c, q: c.execute(q + ', day_limit_micro=1')\n"),
    "a helper returning a dict's setdefault result (the reviewer's)": ("""
        ProbeRunner(conn).run(probe_identity(conn, 'UPDATE tr_key_limit SET reserved=0'))
    """, _PROBE_RUNNER + "def probe_identity(c, q):\n    c.execute(q)\n    queries = {'sql': q}\n"
         "    return queries.setdefault('sql', '')\n"),
    "sorted with a key callback (the reviewer's)": ("""
        sorted(['UPDATE tr_key_limit SET reserved=0'], key=ProbeRunner(conn))
    """, _PROBE_RUNNER),
    "print to a writer (the reviewer's)": ("""
        print('UPDATE tr_key_limit SET reserved=0', file=ProbeRunner(conn))
    """, _PROBE_RUNNER),
    "a key looked up in an object (the reviewer's)": ("""
        ProbeRunner(conn)['UPDATE tr_key_limit SET reserved=0']
    """, _PROBE_RUNNER),
    "__contains__": ("""
        probe_found = 'UPDATE tr_key_limit SET reserved=0' in ProbeRunner(conn)
    """, _PROBE_RUNNER),
    "__radd__": ("""
        probe_sum = 'UPDATE tr_key_limit SET reserved=0' + ProbeRunner(conn)
    """, _PROBE_RUNNER),
    "__eq__": ("""
        probe_same = ProbeRunner(conn) == 'UPDATE tr_key_limit SET reserved=0'
    """, _PROBE_RUNNER),
    "max with a default": ("""
        probe_pick = max([], default=ProbeRunner(conn)) or max(['UPDATE tr_key_limit SET reserved=0'], key=ProbeRunner(conn))
    """, _PROBE_RUNNER),
}


@pytest.mark.parametrize("label", list(_ROUND_11))
def test_the_round_11_injections_into_a_listed_scope_are_caught(tmp_path: Path, label: str) -> None:
    change, helpers = _ROUND_11[label]
    scope = "PostgresStore._release_key_hold_tx"
    source = (ROOT / _PG).read_text()
    where = source.index(_RELEASE_END)
    source = (source[:where] + "\n" + textwrap.indent(textwrap.dedent(change), "        ") + source[where:]
              + "\n\n" + helpers)
    detail = _injected_detail(tmp_path, _PG, source)
    assert detail.get(("key_limit.escape", "storage_postgres.py", scope)) is True, label
    assert ("storage_postgres.py", scope) in limit_column_writers(detail)


# Review round 12: the reviewer's forms (and the other bindings, routes and
# namespaces of each of the six items), each injected into
# PostgresStore._release_key_hold_tx (helpers beside the module). A class
# body nested in the scope is a scope of its own: the class's escape makes
# it a new limit-column scope, which the inventory fails on too.
_PROBE_RUNNER_12 = _PROBE_RUNNER + "    def __lt__(self, q):\n        return self.run(q)\n"
_PROBE_RUN = "probe_run = lambda c, q: c.execute(q + ', day_limit_micro=1')\n"
_PROBE_ON_BIND = ("class ProbeOnBind:\n    def __init__(self, c):\n        self.c = c\n"
                  "    def __set_name__(self, owner, name):\n"
                  "        self.c.execute(vars(owner)['sql'] + ', day_limit_micro=1')\n")
_ROUND_12 = {
    "1. an unpacked operand (the reviewer's)": ("""
        probe_runner, = [ProbeRunner(conn)]
        probe_runner['UPDATE tr_key_limit SET reserved=0']
    """, _PROBE_RUNNER_12),
    "1. an unpacked operand compared": ("""
        probe_runner, = [ProbeRunner(conn)]
        probe_same = probe_runner == 'UPDATE tr_key_limit SET reserved=0'
    """, _PROBE_RUNNER_12),
    "1. a with target as an operand": ("""
        with probe_open(conn) as probe_runner:
            probe_runner['UPDATE tr_key_limit SET reserved=0']
    """, _PROBE_RUNNER_12 + "probe_open = ProbeRunner\n"),
    "2. an element of a nested container changed (the reviewer's)": ("""
        probe_boxes = [[]]
        probe_boxes[0].append(ProbeRunner(conn))
        probe_boxes[0][0]['UPDATE tr_key_limit SET reserved=0']
    """, _PROBE_RUNNER_12),
    "2. an alias of a nested container changed (the reviewer's)": ("""
        probe_boxes = [[]]
        probe_inner = probe_boxes[0]
        probe_inner.append(ProbeRunner(conn))
        probe_boxes[0][0]['UPDATE tr_key_limit SET reserved=0']
    """, _PROBE_RUNNER_12),
    "2. searched in a nested container changed": ("""
        probe_boxes = [[]]
        probe_boxes[0].append(ProbeRunner(conn))
        probe_found = 'UPDATE tr_key_limit SET reserved=0' in probe_boxes[0]
    """, _PROBE_RUNNER_12),
    "3. the value before += (the reviewer's)": ("""
        probe_q = 'UPDATE tr_key_limit SET reserved=0'
        probe_q += ProbeRunner(conn)
    """, _PROBE_RUNNER_12),
    "3. an element before +=": ("""
        probe_queries = {'q': 'UPDATE tr_key_limit SET reserved=0'}
        probe_queries['q'] += ProbeRunner(conn)
    """, _PROBE_RUNNER_12),
    "4. sort beside an object (the reviewer's)": ("""
        probe_values = ['UPDATE tr_key_limit SET reserved=0', ProbeRunner(conn)]
        probe_values.sort()
    """, _PROBE_RUNNER_12),
    "4. index beside an object": ("""
        probe_values = ['UPDATE tr_key_limit SET reserved=0', ProbeRunner(conn)]
        probe_values.index('x')
    """, _PROBE_RUNNER_12),
    "5. locals() (the reviewer's)": ("""
        probe_sql = 'UPDATE tr_key_limit SET reserved=0'
        probe_run(conn, locals()['probe_sql'])
    """, _PROBE_RUN),
    "5. a frame's locals": ("""
        probe_sql = 'UPDATE tr_key_limit SET reserved=0'
        probe_run(conn, sys._getframe(0).f_locals['probe_sql'])
    """, "import sys\n" + _PROBE_RUN),
    "5. a module constant rebound through globals() (the reviewer's)": ("""
        globals()['probe_suffix'] = ProbeRunner(conn)
        probe_sum = 'UPDATE tr_key_limit SET reserved=0' + probe_suffix
    """, _PROBE_RUNNER_12 + "probe_suffix = ''\n"),
    "6. a descriptor in a plain class (the reviewer's)": ("""
        class ProbeQueries:
            sql = 'UPDATE tr_key_limit SET reserved=0'
            on_bind = ProbeOnBind(conn)
    """, _PROBE_ON_BIND),
    "6. a class's namespace passed on (the reviewer's)": ("""
        class ProbeQueries:
            sql = 'UPDATE tr_key_limit SET reserved=0'
        probe_run(conn, vars(ProbeQueries)['sql'])
    """, _PROBE_RUN),
    "a type indexed with it (list[sql])": ("""
        probe_alias = list['UPDATE tr_key_limit SET reserved=0']
        probe_run(conn, probe_alias.__args__[0])
    """, _PROBE_RUN),
    "a key's KeyError": ("""
        try:
            {}['UPDATE tr_key_limit SET reserved=0']
        except KeyError as probe_error:
            probe_run(conn, probe_error.args[0])
    """, _PROBE_RUN),
}


@pytest.mark.parametrize("label", list(_ROUND_12))
def test_the_round_12_injections_into_a_listed_scope_are_caught(tmp_path: Path, label: str) -> None:
    change, helpers = _ROUND_12[label]
    scope = "PostgresStore._release_key_hold_tx"
    source = (ROOT / _PG).read_text()
    where = source.index(_RELEASE_END)
    source = (source[:where] + "\n" + textwrap.indent(textwrap.dedent(change), "        ") + source[where:]
              + "\n\n" + helpers)
    detail = _injected_detail(tmp_path, _PG, source)
    escapes = {where for style, path, where in detail if style == "key_limit.escape" and path == "storage_postgres.py"
               and (where == scope or where.startswith(scope + "."))}
    assert escapes, label
    assert {("storage_postgres.py", where) for where in escapes} <= limit_column_writers(detail)


# Review round 13: the round-12 reviewer's forms, one or more per fixed item,
# injected into PostgresStore._release_key_hold_tx (helpers beside the
# module). Each makes the scope, a scope nested in it, or a helper it adds,
# a limit-column writer the inventory does not list. (In this module a
# globals() read already escapes, since it holds module constants naming the
# table; item 5's own check is its control.)
_PROBE_SEPARATOR = ("class ProbeSeparator(str):\n    def __eq__(self, q):\n        return ProbeRunner(self.c).run(q)\n")
_ROUND_13 = {
    "1. a + copy of a nested container changed (the reviewer's)": ("""
        probe_boxes = [[]]
        probe_copy = probe_boxes + []
        probe_copy[0].append(ProbeRunner(conn))
        probe_found = 'UPDATE tr_key_limit SET reserved=0' in probe_boxes[0]
    """, _PROBE_RUNNER_12),
    "1. a * copy of a nested container changed (the reviewer's)": ("""
        probe_boxes = [[]]
        (probe_boxes * 1)[0].append(ProbeRunner(conn))
        probe_found = 'UPDATE tr_key_limit SET reserved=0' in probe_boxes[0]
    """, _PROBE_RUNNER_12),
    "1. a container two names share (the reviewer's)": ("""
        probe_values = probe_alias = []
        probe_alias.append(ProbeRunner(conn))
        probe_found = 'UPDATE tr_key_limit SET reserved=0' in probe_values
    """, _PROBE_RUNNER_12),
    "2. a partition separator kept (the reviewer's)": ("""
        probe_part = 'a:b'.partition(ProbeSeparator(':'))[1]
        probe_same = probe_part == 'UPDATE tr_key_limit SET reserved=0'
    """, _PROBE_RUNNER_12 + _PROBE_SEPARATOR),
    "2. a maketrans value kept (the reviewer's)": ("""
        probe_value = ''.maketrans({65: ProbeRunner(conn)})[65]
        probe_same = 'UPDATE tr_key_limit SET reserved=0' == probe_value
    """, _PROBE_RUNNER_12),
    "3. nonlocal from a nested class's method (the reviewer's)": ("""
        probe_runner = 0
        class ProbeChange:
            def change(self):
                nonlocal probe_runner
                probe_runner = ProbeRunner(conn)
        ProbeChange().change()
        probe_same = 'UPDATE tr_key_limit SET reserved=0' == probe_runner
    """, _PROBE_RUNNER_12),
    "4. a local getattr (the reviewer's)": ("""
        getattr = probe_make_getattr(conn)
        getattr('UPDATE tr_key_limit SET reserved=0', 'upper')()
    """, "def probe_make_getattr(c):\n    return lambda q, name: lambda: c.execute(q + ', day_limit_micro=1')\n"),
    "5. a builtin's name rebound through globals() (the reviewer's)": ("""
        globals()['len'] = lambda q: probe_run(conn, q)
        len('UPDATE tr_key_limit SET reserved=0')
    """, _PROBE_RUN),
    "6. a module function's default reading locals() (the reviewer's)": ("""
        probe_default(conn)
    """, _with(_PROBE_RUN, "PROBE_SQL = 'UPDATE tr_key_limit SET reserved=0'\n"
                           "def probe_default(c, q=locals()['PROBE_SQL']):\n    probe_run(c, q)\n")),
    "7. a decorator named cache that changes the statement (the reviewer's)": ("""
        probe_cached_run(conn, 'UPDATE tr_key_limit SET reserved=0')
    """, "def cache(func):\n    def wrapper(c, q):\n        return func(c, q + ', day_limit_micro=1')\n    return wrapper\n"
         "@cache\ndef probe_cached_run(c, q):\n    c.execute(q)\n"),
}


@pytest.mark.parametrize("label", list(_ROUND_13))
def test_the_round_13_injections_into_a_listed_scope_are_caught(tmp_path: Path, label: str) -> None:
    change, helpers = _ROUND_13[label]
    scope = "PostgresStore._release_key_hold_tx"
    source = (ROOT / _PG).read_text()
    where = source.index(_RELEASE_END)
    source = (source[:where] + "\n" + textwrap.indent(textwrap.dedent(change), "        ") + source[where:]
              + "\n\n" + helpers)
    writers = limit_column_writers(_injected_detail(tmp_path, _PG, source))
    assert any(path == "storage_postgres.py" and (where == scope or where.startswith((scope + ".", "probe")))
               for path, where in writers), label


# Review round 14: the round-13 reviewer's five items, each an identity the
# scanner decided by a name's spelling (a helper rebound, getattr shadowed or
# a receiver spelled builtins, a decorator imported under a plain one's name,
# a namespace builtin imported under another name, a binding in an
# annotation). Every identity decision now goes through one resolver
# (key_limit_writers._Identities). Each form is injected into
# PostgresStore._release_key_hold_tx (helpers beside the module) and makes
# the scope, or a helper it adds, a limit-column writer; each control, the
# same shape with its identity intact, does not. An annotation's walrus runs
# only with eager annotations, so for item 5 the copy drops the module's
# postponed annotations (each injected module compiles).
_R14_SQL = "'UPDATE tr_key_limit SET reserved=0'"
_R14_RUN = "def probe_run(c, q):\n    c.execute(q)\n"
_R14_STATIC_RUN = "class ProbeRunner:\n    @staticmethod\n    def probe_run(c, q):\n        c.execute(q)\n"
_R14_MAKE = "def probe_make(c):\n    return lambda q, name=None: lambda: c.execute(q + ', day_limit_micro=1')\n"
_R14_MAKE_LEN = "def probe_make_len(c):\n    return lambda q: c.execute(q + ', day_limit_micro=1')\n"
_R14_CHANGE = "def probe_change(c, q):\n    c.execute(q + ', day_limit_micro=1')\n"
_ROUND_14 = {
    "1. a helper rebound by a decorator's call (the reviewer's)": (
        f"probe_run(conn, {_R14_SQL})\n",
        "def probe_with_limit(func):\n    def wrapped(c, q):\n        return func(c, q + ', day_limit_micro=1')\n"
        "    return wrapped\n" + _R14_RUN + "probe_run = probe_with_limit(probe_run)\n", False),
    "1. a helper rebound to a lambda (the reviewer's)": (
        f"probe_run(conn, {_R14_SQL})\n",
        _R14_RUN + "probe_run = lambda c, q: c.execute(q + ', day_limit_micro=1')\n", False),
    "1. a class's method reassigned (the reviewer's)": (
        f"ProbeRunner.probe_run(conn, {_R14_SQL})\n",
        _R14_STATIC_RUN + "ProbeRunner.probe_run = lambda c, q: c.execute(q + ', day_limit_micro=1')\n", False),
    "1. another object's method of the helper's name": (
        f"probe_other.probe_run(conn, {_R14_SQL})\n",
        "from types import SimpleNamespace\n" + _R14_STATIC_RUN
        + "probe_other = SimpleNamespace(probe_run=lambda c, q: c.execute(q + ', day_limit_micro=1'))\n", False),
    "1. self rebound before a call of the helper's name": (
        "ProbeRunner().probe_call(conn)\n",
        "from types import SimpleNamespace\n" + _R14_STATIC_RUN
        + f"    def probe_call(self, c):\n        self = probe_other\n        self.probe_run(c, {_R14_SQL})\n"
        + "probe_other = SimpleNamespace(probe_run=lambda c, q: c.execute(q + ', day_limit_micro=1'))\n", False),
    "1. an executor's alias rebound": (
        f"probe_execute = conn.execute\nprobe_execute = probe_make(conn)\nprobe_execute({_R14_SQL})()\n",
        _R14_MAKE, False),
    "2. getattr shadowed, then aliased (the reviewer's)": (
        f"getattr = probe_make(conn)\nprobe_fetch = getattr\nprobe_fetch({_R14_SQL}, 'upper')()\n", _R14_MAKE,
        False),
    "2. a receiver spelled builtins (the reviewer's)": (
        "probe_through(SimpleNamespace(getattr=probe_make(conn)))\n",
        "from types import SimpleNamespace\n" + _R14_MAKE
        + f"def probe_through(builtins):\n    probe_fetch = builtins.getattr\n    probe_fetch({_R14_SQL}, 'upper')()\n",
        False),
    "3. singledispatch imported as cache (the reviewer's)": (
        f"probe_run({_R14_SQL}, conn)\n",
        "from functools import singledispatch as cache\n@cache\ndef probe_run(q, c):\n    c.execute(q)\n"
        "@probe_run.register(str)\ndef probe_run_text(q, c):\n    c.execute(q + ', day_limit_micro=1')\n", False),
    "4. a builtin rebound through globals imported under another name (the reviewer's)": (
        f"probe_install(conn)\nlen({_R14_SQL})\n",
        "from builtins import globals as probe_snapshot\ndef probe_install(c):\n"
        "    probe_snapshot()['len'] = lambda q: c.execute(q + ', day_limit_micro=1')\n", False),
    "4. eval imported under another name (the reviewer's)": (
        "probe_read(conn)\n",
        f"from builtins import eval as probe_evaluate\nPROBE_SQL = {_R14_SQL}\n" + _R14_CHANGE
        + "def probe_read(c):\n    probe_change(c, probe_evaluate('PROBE_SQL'))\n", False),
    "4. a default reading locals imported under another name (the reviewer's)": (
        "probe_default(conn)\n",
        f"from builtins import locals as probe_snapshot\nPROBE_SQL = {_R14_SQL}\n" + _R14_CHANGE
        + "def probe_default(c, q=probe_snapshot()['PROBE_SQL']):\n    probe_change(c, q)\n", False),
    "5. a walrus in a parameter's annotation (the reviewer's)": (
        f"def probe_annotated(x: (len := probe_make_len(conn))):\n    pass\nlen({_R14_SQL})\n", _R14_MAKE_LEN,
        True),
    "5. a walrus in a return annotation (the reviewer's)": (
        f"def probe_annotated() -> (len := probe_make_len(conn)):\n    pass\nlen({_R14_SQL})\n", _R14_MAKE_LEN,
        True),
}
_ROUND_14_CONTROLS = {
    "1. the helper not rebound": (f"probe_run(conn, {_R14_SQL})\n", _R14_RUN, False),
    "1. the class's own method, through the class and its self": (
        f"ProbeRunner.probe_run(conn, {_R14_SQL})\nProbeRunner().probe_call(conn)\n",
        _R14_STATIC_RUN + f"    def probe_call(self, c):\n        self.probe_run(c, {_R14_SQL})\n", False),
    "1. an executor's alias not rebound": (f"probe_execute = conn.execute\nprobe_execute({_R14_SQL})\n", "", False),
    "2. the builtin getattr aliased": (f"probe_fetch = getattr\nprobe_fetch({_R14_SQL}, 'upper')()\n", "", False),
    "3. functools.cache imported under another name": (
        f"probe_run({_R14_SQL}, conn)\n",
        "from functools import cache as probe_cached\n@probe_cached\ndef probe_run(q, c):\n    c.execute(q)\n", False),
    "4. len imported from builtins under another name": (
        f"probe_size({_R14_SQL})\n", "from builtins import len as probe_size\n", False),
    "5. an annotation that binds nothing": (f"def probe_annotated(x: int):\n    pass\nlen({_R14_SQL})\n", "", True),
}


def _round_14_writers(tmp_path: Path, change: str, helpers: str, eager: bool) -> set[str]:
    source = (ROOT / _PG).read_text()
    if eager:
        assert source.count("from __future__ import annotations\n") == 1
        source = source.replace("from __future__ import annotations\n", "", 1)
    where = source.index(_RELEASE_END)
    source = (source[:where] + "\n" + textwrap.indent(textwrap.dedent(change), "        ") + source[where:]
              + "\n\n" + helpers)
    compile(source, _PG, "exec", dont_inherit=True)  # (this module's own postponed annotations aside)
    scope = "PostgresStore._release_key_hold_tx"
    return {where for path, where in limit_column_writers(_injected_detail(tmp_path, _PG, source))
            if path == "storage_postgres.py" and (where == scope or where.startswith((scope + ".", "probe", "Probe")))}


@pytest.mark.parametrize("label", list(_ROUND_14))
def test_the_round_14_identity_forms_injected_into_a_listed_scope_are_caught(tmp_path: Path, label: str) -> None:
    assert _round_14_writers(tmp_path, *_ROUND_14[label]), label


@pytest.mark.parametrize("label", list(_ROUND_14_CONTROLS))
def test_the_round_14_controls_with_their_identity_intact_are_not_writers(tmp_path: Path, label: str) -> None:
    assert not _round_14_writers(tmp_path, *_ROUND_14_CONTROLS[label]), label


# The resolver itself: what a name or attribute is, by its bindings.
_IDENTITY_CASES = [
    ("a builtin's name bound nowhere", "len\n", "len", "builtins.len"),
    ("a builtin imported under another name", "from builtins import len as size\nsize\n", "size", "builtins.len"),
    ("a name bound to a builtin", "size = len\nsize\n", "size", "builtins.len"),
    ("a builtin's name bound in some function", "def f():\n    len = str\nlen\n", "len", None),
    ("a builtin's name a parameter somewhere", "def f(len):\n    pass\nlen\n", "len", None),
    ("a builtin's name bound in an annotation", "def f(x: (len := str)):\n    pass\nlen\n", "len", None),
    ("a builtin's name a loop target", "for len in ():\n    pass\nlen\n", "len", None),
    ("a builtin's name deleted", "len = len\ndel len\nlen\n", "len", None),
    ("a builtin's name an except target", "try:\n    pass\nexcept Exception as len:\n    pass\nlen\n", "len",
     None),
    ("a module that imports *", "from os import *\nlen\n", "len", None),
    ("a module that writes its globals", "globals()['len'] = str\nlen\n", "len", None),
    ("a module's own locals written", "locals()['len'] = str\nlen\n", "len", None),
    ("a module object in hand", "import sys\nsys.modules[__name__].len = str\nlen\n", "len", None),
    ("a module's attribute of the name set", "import probe\nprobe.len = str\nlen\n", "len", None),
    ("an import with as", "from functools import singledispatch as cache\ncache\n", "cache",
     "functools.singledispatch"),
    ("a module attribute", "import functools as tools\ntools.cache\n", "tools.cache", "functools.cache"),
    ("a module attribute rebound on an unknown receiver", "import functools as tools\ndef f(o):\n    o.cache = 1\n"
     "tools.cache\n", "tools.cache", None),
    ("builtins as a module", "import builtins as b\nb.getattr\n", "b.getattr", "builtins.getattr"),
    ("a parameter spelled builtins", "def f(builtins):\n    builtins.getattr\n", "builtins.getattr", None),
    ("getattr shadowed, then aliased", "def f(c):\n    getattr = c\n    fetch = getattr\nfetch\n", "fetch", None),
    ("an alias of an alias", "a = len\nb = a\nb\n", "b", "builtins.len"),
    ("a cycle", "a = b\nb = a\na\n", "a", None),
    ("a walrus alias", "(size := len)\nsize\n", "size", "builtins.len"),
    ("an import from a scanned module", "from .local import len\nlen\n", "len", None),
]


@pytest.mark.parametrize(("name", "source", "expression", "expected"), _IDENTITY_CASES,
                         ids=[case[0] for case in _IDENTITY_CASES])
def test_the_identity_resolver_decides_by_bindings_not_spelling(name: str, source: str, expression: str,
                                                                expected: str | None) -> None:
    import ast

    from tests.key_limit_writers import _Identities

    ids = _Identities(ast.parse(source))
    assert ids.of(ast.parse(expression, mode="eval").body) == expected, name


def test_the_identity_resolver_follows_a_def_only_while_every_binding_is_it() -> None:
    import ast

    from tests.key_limit_writers import _Identities

    tree = ast.parse("def run():\n    pass\nrun\n")
    definition = tree.body[0]
    assert _Identities(tree).of(ast.Name(id="run", ctx=ast.Load())) == f"def:{id(definition)}"
    for rebinding in ("run = wrap(run)\n", "run = lambda: 0\n", "def g(run):\n    pass\n",
                      "if x:\n    def run():\n        pass\n"):
        assert _Identities(ast.parse("def run():\n    pass\n" + rebinding)).of(
            ast.Name(id="run", ctx=ast.Load())) is None, rebinding


@pytest.mark.parametrize(("source", "kinds"), [
    ("from builtins import globals as snapshot\nsnapshot()\n", {"globals"}),
    ("from builtins import eval as evaluate\nevaluate('x')\n", {"scope"}),
    ("from builtins import vars as names\nnames(object)\n", {"object"}),
    ("def f():\n    from builtins import locals as snapshot\n    snapshot()\n", {"locals"}),
    ("locals()\n", {"locals", "globals", "object"}),
    ("from inspect import stack as frames\nframes()\n", {"frame"}),
    ("import builtins as b\nb.globals()\n", {"globals"}),
    ("snapshot = globals if x else locals\nsnapshot()\n", {"globals", "locals", "frame", "object"}),
    ("from sys import modules as loaded\nloaded\n", {"modules"}),
    ("from importlib import import_module as load\nload('x')\n", {"modules"}),
    ("len()\n", set()),
], ids=["globals as", "eval as", "vars as", "locals as", "module locals", "inspect.stack as", "builtins module",
        "either reader", "sys.modules as", "import_module as", "not a reader"])
def test_a_namespace_reader_counts_by_any_identity_it_may_have(source: str, kinds: set[str]) -> None:
    import ast

    from tests.key_limit_writers import _Identities

    assert {kind for _node, kind in _Identities(ast.parse(source)).namespace_reads} == kinds
