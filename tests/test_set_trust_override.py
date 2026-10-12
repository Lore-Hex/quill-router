"""The operator path that sets a workspace's trust override (rollout P1's
synthetic workspace, which no payment raises to the tier leases need)."""

from __future__ import annotations

from typing import Any

from scripts import set_trust_override
from tests.fakes.spanner import make_fake_store
from trusted_router.storage import Workspace
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE


def _workspace(store: Any, db: Any, ws: str) -> None:
    store._write_entity("user", "u1", {"id": "u1", "email": "owner@example.com", "identity_status": "none"})
    store._write_entity("workspace", ws, Workspace(id=ws, name="fastpath-synthetic", owner_user_id="u1"))
    store._write_entity("credit", ws, {"workspace_id": ws, "shard_count": 1})
    db.typed.setdefault(CREDIT_BALANCE_TABLE, {})[(ws, 0)] = {
        "workspace_id": ws, "shard": 0, "total_credits": 100_000_000, "total_usage": 0, "reserved": 0,
        "trust_tier": 0,
    }


def test_dry_run_sets_nothing(capsys) -> None:
    store, db = make_fake_store()
    _workspace(store, db, "ws_p1")
    assert set_trust_override.main(["--workspace-id", "ws_p1", "--tier", "1", "--operator", "op", "--reason", "P1"],
                                   store=store) == 0
    out = capsys.readouterr().out
    assert "DRY-RUN" in out and "(0, 0, None)" in out
    assert db.typed[CREDIT_BALANCE_TABLE][("ws_p1", 0)].get("trust_override_tier") is None


def test_apply_raises_every_shard_to_the_tier(monkeypatch, capsys) -> None:
    monkeypatch.setenv("TR_STORAGE_BACKEND", "spanner-clickhouse")
    store, db = make_fake_store()
    _workspace(store, db, "ws_p1")
    assert set_trust_override.main(["--workspace-id", "ws_p1", "--tier", "1", "--operator", "op", "--reason", "P1",
                                    "--apply"], store=store) == 0
    row = db.typed[CREDIT_BALANCE_TABLE][("ws_p1", 0)]
    assert row["trust_override_tier"] == 1 and row["trust_tier"] >= 1
    assert "result: override tier 1" in capsys.readouterr().out


def test_unknown_workspace_and_bad_tier_fail(capsys) -> None:
    store, _db = make_fake_store()
    assert set_trust_override.main(["--workspace-id", "nope", "--tier", "1", "--operator", "op", "--reason", "r"],
                                   store=store) == 1
    assert set_trust_override.main(["--workspace-id", "nope", "--tier", "4", "--operator", "op", "--reason", "r"],
                                   store=store) == 1
