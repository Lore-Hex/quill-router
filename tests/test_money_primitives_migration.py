from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_spanner_money_primitives_migration_has_guarded_tables_and_index() -> None:
    path = ROOT / "scripts/deploy/migrate_money_primitives.sh"
    migration = path.read_text()

    assert os.access(path, os.X_OK)
    for table in (
        "tr_earnings_balance",
        "tr_credit_movement",
        "tr_user_lifetime_topup",
    ):
        assert f"if table_exists {table}" in migration
        assert f"CREATE TABLE {table}" in migration
    assert "ROW DELETION POLICY (OLDER_THAN(created_at, INTERVAL 400 DAY))" in migration
    assert "CREATE INDEX tr_credit_movement_by_time" in migration
    assert "ON tr_credit_movement (account_id, created_at DESC)" in migration


def test_postgres_schema_has_money_primitive_twins_and_documents_ttl_sweep() -> None:
    schema = (ROOT / "src/trusted_router/storage_postgres_schema.sql").read_text()

    for table in (
        "tr_earnings_balance",
        "tr_credit_movement",
        "tr_user_lifetime_topup",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in schema
    assert "future sweep" in schema
    assert "tr_credit_movement_by_time" in schema
