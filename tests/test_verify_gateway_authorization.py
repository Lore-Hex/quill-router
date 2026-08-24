from __future__ import annotations

import datetime as dt
import importlib.util
import re
from pathlib import Path
from typing import Any

from trusted_router.storage_gcp_keys import _gateway_authorization_idempotency_index_id

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_gateway_authorization.py"
SPEC = importlib.util.spec_from_file_location("verify_gateway_authorization", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify)


class FakeSnapshot:
    def __init__(self) -> None:
        self.queries: list[tuple[str, dict[str, Any]]] = []

    def execute_sql(
        self,
        sql: str,
        *,
        params: dict[str, Any],
        param_types: dict[str, Any],
    ) -> list[tuple[Any, ...]]:
        del param_types
        self.queries.append((sql, params))
        now = dt.datetime(2026, 8, 22, tzinfo=dt.UTC)
        if sql == verify.RESERVATION_BY_SCOPE_SQL:
            return [("res-1", "gwa-1")]
        if sql == verify.AUTHORIZATION_BY_ID_SQL:
            return [
                (
                    "gwa-1",
                    "ws-1",
                    "key-1",
                    "res-1",
                    "openai/gpt-5-mini",
                    "azure",
                    "Credits",
                    100,
                    True,
                    now,
                    now,
                    '{"finalization_outcome":"settled",'
                    '"finalized_cost_microdollars":42,'
                    '"finalized_generation_id":"gen-1",'
                    '"finalized_model_id":"openai/gpt-5-mini",'
                    '"finalized_provider":"azure"}',
                )
            ]
        if sql == verify.RESERVATION_BY_ID_SQL:
            return [
                (
                    "res-1",
                    "ws-1",
                    "key-1",
                    100,
                    0,
                    42,
                    "Credits",
                    "Credits",
                    True,
                    now,
                    now,
                    now,
                )
            ]
        if sql == verify.OUTBOX_BY_AUTHORIZATION_SQL:
            return [
                ("settle", "inline", 42, "openai/gpt-5-mini", "Credits", "done", 1, now, now, now)
            ]
        if sql == verify.GENERATION_BY_ID_SQL:
            return [
                (
                    "gen-1",
                    "ws-1",
                    "key-1",
                    now,
                    now,
                    '{"model":"openai/gpt-5-mini","provider":"azure",'
                    '"usage_type":"Credits","status":"completed",'
                    '"finish_reason":"stop","total_cost_microdollars":42}',
                )
            ]
        raise AssertionError(f"unexpected SQL: {sql}")


def test_idempotency_verification_uses_index_then_primary_keys() -> None:
    snapshot = FakeSnapshot()

    result = verify.verify_authorization(
        snapshot,
        workspace_id="ws-1",
        key_hash="key-1",
        idempotency_key="canary-1",
    )

    assert result["authorization"]["authorization_id"] == "gwa-1"
    assert result["reservation"]["actual_microdollars"] == 42
    assert result["outbox"][0]["status"] == "done"
    assert result["generation"]["generation_id"] == "gen-1"
    assert "FORCE_INDEX=tr_reservation_by_idemp" in snapshot.queries[0][0]
    assert all(
        "JSON_VALUE" not in sql.split("WHERE", maxsplit=1)[-1] for sql, _ in snapshot.queries
    )


def test_operational_code_does_not_scan_authorization_payload_json() -> None:
    offenders: list[str] = []
    forbidden = re.compile(
        r"from\s+tr_gateway_authorization\b.{0,4000}?where\b.{0,4000}?"
        r"(?:json_value\([^)]*payload|starts_with\(\s*json_value\([^)]*payload)",
        flags=re.DOTALL,
    )
    for root in (ROOT / "scripts", ROOT / ".github" / "workflows"):
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix not in {".py", ".sh", ".yml", ".yaml"}:
                continue
            normalized = " ".join(path.read_text(errors="ignore").lower().split())
            if forbidden.search(normalized):
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_scope_matches_the_application_idempotency_contract() -> None:
    assert verify.idempotency_scope("ws-1", "key-1", "canary-1") == (
        _gateway_authorization_idempotency_index_id("ws-1", "key-1", "canary-1")
    )


def test_authorization_id_path_skips_the_secondary_index_lookup() -> None:
    snapshot = FakeSnapshot()

    result = verify.verify_authorization(snapshot, authorization_id="gwa-1")

    assert result["authorization"]["authorization_id"] == "gwa-1"
    assert snapshot.queries[0][0] == verify.AUTHORIZATION_BY_ID_SQL
    assert all(sql != verify.RESERVATION_BY_SCOPE_SQL for sql, _ in snapshot.queries)


def test_cli_uses_multi_read_snapshot_without_metrics_export() -> None:
    source = SCRIPT.read_text()

    assert "disable_builtin_metrics=True" in source
    assert "snapshot(multi_use=True)" in source
