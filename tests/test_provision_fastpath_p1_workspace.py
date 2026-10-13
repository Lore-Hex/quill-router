"""The one-step provisioner of rollout P1's synthetic workspace."""

from __future__ import annotations

import json
from typing import Any

from scripts import provision_fastpath_p1_workspace as p1
from tests.fakes.spanner import make_fake_store
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE


def _run(store: Any, capsys: Any, *extra: str) -> tuple[int, dict[str, Any]]:
    code = p1.main(["--email", "owner@example.com", *extra], store=store)
    out = capsys.readouterr().out
    body = out.split("\nDRY-RUN", 1)[0] if "DRY-RUN" in out else out
    return code, (json.loads(body) if body.strip().startswith("{") else {})


def test_dry_run_plans_and_changes_nothing(capsys: Any) -> None:
    store, _db = make_fake_store()
    user = store.ensure_user("owner@example.com", email="owner@example.com")
    code, plan = _run(store, capsys)
    assert code == 0 and plan["would_create_workspace"] and plan["funding_microdollars"] == 100_000_000
    assert [w for w in store.list_workspaces_for_user(user.id) if w.name == "fastpath-synthetic"] == []


def test_apply_creates_funds_and_tiers_the_workspace_once(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setenv("TR_STORAGE_BACKEND", "spanner-clickhouse")
    store, db = make_fake_store()
    user = store.ensure_user("owner@example.com", email="owner@example.com")
    code, result = _run(store, capsys, "--apply")
    assert code == 0 and result["created_workspace"] and result["funded"] == "applied"
    ws = result["workspace_id"]
    assert [w.id for w in store.list_workspaces_for_user(user.id) if w.name == "fastpath-synthetic"] == [ws]
    assert store.list_keys(ws) == []
    row = db.typed[CREDIT_BALANCE_TABLE][(ws, 0)]
    assert row["total_credits"] == 100_000_000 and row["trust_override_tier"] == 1 and row["trust_tier"] >= 1
    assert result["override"]["tier"] == 1
    # Again: the same workspace, the grant's event already applied, the
    # credit unchanged.
    code, again = _run(store, capsys, "--apply")
    assert code == 0 and again["workspace_id"] == ws and again["funded"] == "already applied"
    assert "created_workspace" not in again
    assert db.typed[CREDIT_BALANCE_TABLE][(ws, 0)]["total_credits"] == 100_000_000


def test_refuses_an_unknown_user_and_a_workspace_with_a_key(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setenv("TR_STORAGE_BACKEND", "spanner-clickhouse")
    store, _db = make_fake_store()
    assert p1.main(["--email", "nobody@example.com"], store=store) == 1
    assert "no user" in capsys.readouterr().err
    user = store.ensure_user("owner@example.com", email="owner@example.com")
    workspace = store.create_workspace(user.id, "fastpath-synthetic", trial_credit_microdollars=0)
    store.api_keys.create(workspace_id=workspace.id, name="k", creator_user_id=None, limit_microdollars=None)
    assert p1.main(["--email", "owner@example.com", "--apply"], store=store) == 1
    assert "active API keys" in capsys.readouterr().err
