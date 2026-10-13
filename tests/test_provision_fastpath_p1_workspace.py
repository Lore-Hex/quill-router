"""The one-step provisioner of rollout P1's synthetic workspace."""

from __future__ import annotations

import copy
import json
from typing import Any

from scripts import provision_fastpath_p1_workspace as p1
from tests.fakes.spanner import make_fake_store
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE

EMAIL = "owner@example.com"


def _run(store: Any, capsys: Any, *extra: str) -> tuple[int, dict[str, Any], str]:
    code = p1.main(["--email", EMAIL, *extra], store=store)
    captured = capsys.readouterr()
    body = captured.out.split("\nDRY-RUN", 1)[0] if "DRY-RUN" in captured.out else captured.out
    return code, (json.loads(body) if body.strip().startswith("{") else {}), captured.err


def _owner(store: Any) -> Any:
    return store.ensure_user(EMAIL, email=EMAIL)


def test_dry_run_plans_and_changes_nothing(capsys: Any) -> None:
    store, db = make_fake_store()
    user = _owner(store)
    code, plan, _ = _run(store, capsys)
    assert code == 0 and plan["would_create_workspace"] and plan["funding_microdollars"] == 100_000_000
    assert [w for w in store.list_workspaces_for_user(user.id) if w.name == "fastpath-synthetic"] == []
    # On an existing workspace too: no row moves, no override is set.
    workspace = store.create_workspace(user.id, "fastpath-synthetic", trial_credit_microdollars=0)
    before = copy.deepcopy(db.typed)
    code, plan, _ = _run(store, capsys)
    assert code == 0 and not plan["would_create_workspace"] and plan["workspace_id"] == workspace.id
    assert db.typed == before and "funded" not in plan and "override" not in plan


def test_apply_creates_funds_and_tiers_the_workspace_once(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setenv("TR_STORAGE_BACKEND", "spanner-clickhouse")
    store, db = make_fake_store()
    user = _owner(store)
    code, result, _ = _run(store, capsys, "--apply")
    assert code == 0 and result["created_workspace"] and result["funded"] == "applied"
    ws = result["workspace_id"]
    assert [w.id for w in store.list_workspaces_for_user(user.id) if w.name == "fastpath-synthetic"] == [ws]
    assert store.list_keys(ws) == []
    row = db.typed[CREDIT_BALANCE_TABLE][(ws, 0)]
    assert row["total_credits"] == 100_000_000 and row["trust_override_tier"] == 1 and row["trust_tier"] >= 1
    assert result["override"]["tier"] == 1
    # The grant is a support grant: no lifetime top-up for the user, and
    # the override bypasses no identity check.
    assert not any(pk[0] == user.id for pk in db.typed.get("tr_user_lifetime_topup", {}))
    override = next(rec for pk, rec in db.typed["tr_trust_override"].items() if pk[0] == ws)
    assert override["tier"] == 1 and not override["identity_bypass"]
    # Again: the same workspace, the grant's event already applied, the
    # credit unchanged.
    code, again, _ = _run(store, capsys, "--apply")
    assert code == 0 and again["workspace_id"] == ws and again["funded"] == "already applied"
    assert "created_workspace" not in again
    assert db.typed[CREDIT_BALANCE_TABLE][(ws, 0)]["total_credits"] == 100_000_000


def test_a_sharded_workspace_has_every_shard_raised_and_counted(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setenv("TR_STORAGE_BACKEND", "spanner-clickhouse")
    store, db = make_fake_store()
    user = _owner(store)
    workspace = store.create_workspace(user.id, "fastpath-synthetic", trial_credit_microdollars=0)
    store._write_entity("credit", workspace.id, {"workspace_id": workspace.id, "shard_count": 2})
    db.typed[CREDIT_BALANCE_TABLE][(workspace.id, 1)] = dict(db.typed[CREDIT_BALANCE_TABLE][(workspace.id, 0)], shard=1)
    code, result, _ = _run(store, capsys, "--apply")
    assert code == 0, result
    rows = [db.typed[CREDIT_BALANCE_TABLE][(workspace.id, shard)] for shard in (0, 1)]
    assert all(r["trust_override_tier"] == 1 and r["trust_tier"] >= 1 for r in rows)
    assert sum(r["total_credits"] for r in rows) == 100_000_000
    assert len(result["shards_after"]) == 2


def test_only_the_users_own_workspace_is_taken(monkeypatch: Any, capsys: Any) -> None:
    """A membership in another owner's workspace of the name is not it: the
    operator's own is made, and the other's rows stay."""
    monkeypatch.setenv("TR_STORAGE_BACKEND", "spanner-clickhouse")
    store, db = make_fake_store()
    user = _owner(store)
    other = store.ensure_user("other@example.com", email="other@example.com")
    theirs = store.create_workspace(other.id, "fastpath-synthetic", trial_credit_microdollars=0)
    store.add_members(theirs.id, [EMAIL])
    assert any(w.id == theirs.id for w in store.list_workspaces_for_user(user.id))
    before = copy.deepcopy(db.typed[CREDIT_BALANCE_TABLE][(theirs.id, 0)])
    code, result, _ = _run(store, capsys, "--apply")
    assert code == 0 and result["created_workspace"] and result["workspace_id"] != theirs.id
    assert db.typed[CREDIT_BALANCE_TABLE][(theirs.id, 0)] == before


def test_refuses_what_leases_alone_could_not_spend_from(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setenv("TR_STORAGE_BACKEND", "spanner-clickhouse")
    store, db = make_fake_store()
    code, _, err = _run(store, capsys)
    assert code == 1 and "no user" in err
    user = _owner(store)
    workspace = store.create_workspace(user.id, "fastpath-synthetic", trial_credit_microdollars=0)
    _raw, key = store.api_keys.create(workspace_id=workspace.id, name="k", creator_user_id=None, limit_microdollars=None)
    code, _, err = _run(store, capsys, "--apply")
    assert code == 1 and "API keys" in err
    store.update_key(key.hash, {"disabled": True})
    code, _, err = _run(store, capsys, "--apply")
    assert code == 1 and "API keys" in err, "a disabled key can be enabled again"
    assert store.delete_key(key.hash)
    db.reservations["r-open"] = {"reservation_id": "r-open", "workspace_id": workspace.id, "credit_reserved_micro": 10_000,
                                 "settled": False}
    code, _, err = _run(store, capsys, "--apply")
    assert code == 1 and "unsettled reservations" in err
    db.reservations["r-open"]["settled"] = True
    store.create_workspace(user.id, "fastpath-synthetic", trial_credit_microdollars=0)
    code, _, err = _run(store, capsys, "--apply")
    assert code == 1 and "2 workspaces named" in err


def test_refuses_bad_inputs_and_rows_that_do_not_reflect_the_work(monkeypatch: Any, capsys: Any) -> None:
    store, _db = make_fake_store()
    _owner(store)
    monkeypatch.delenv("TR_STORAGE_BACKEND", raising=False)
    assert p1.main(["--email", EMAIL, "--apply"], store=store) == 2
    monkeypatch.setenv("TR_STORAGE_BACKEND", "spanner-clickhouse")
    for bad in (["--fund-usd", "0"], ["--fund-usd", "-5"], ["--tier", "4"]):
        assert p1.main(["--email", EMAIL, *bad], store=store) == 1, bad
    capsys.readouterr()
    # A grant the rows do not reflect fails the apply, even on a workspace
    # that already holds the amount from elsewhere.
    user = store.find_user_by_email(EMAIL)
    workspace = store.create_workspace(user.id, "fastpath-synthetic", trial_credit_microdollars=0)
    assert store.credit_workspace_typed_direct(workspace.id, 100_000_000, "another_grant",
                                               provenance=p1.CreditProvenance(source="grant", provider="operator",
                                                                              external_ref=None,
                                                                              occurred_at=p1.datetime.now(p1.UTC)))
    monkeypatch.setattr(store, "credit_workspace_typed_direct", lambda *a, **k: True)
    code, _, err = _run(store, capsys, "--apply")
    assert code == 1 and "rose by 0, not by the funding" in err
    monkeypatch.undo()
    monkeypatch.setenv("TR_STORAGE_BACKEND", "spanner-clickhouse")
    store2, _db2 = make_fake_store()
    _owner(store2)
    # The rows rise by the funding, and a shard's effective tier stays 0.
    reads = iter([[[0, 0, None, 0]], [[0, 0, 1, 100_000_000]]])
    monkeypatch.setattr(p1, "_shard_tiers", lambda s, w: next(reads, [[0, 0, 1, 100_000_000]]))
    code, _, err = _run(store2, capsys, "--apply")
    assert code == 1 and "below the override" in err


def test_a_funding_event_applied_elsewhere_is_not_this_workspaces(monkeypatch: Any, capsys: Any) -> None:
    """The store deduplicates the event globally: once applied to one
    workspace, a second owned workspace of the name, holding credit from
    another event, gets nothing, and the script says so instead of taking
    that credit for the funding."""
    monkeypatch.setenv("TR_STORAGE_BACKEND", "spanner-clickhouse")
    store, _db = make_fake_store()
    user = _owner(store)
    code, first, _ = _run(store, capsys, "--apply")
    assert code == 0 and first["funded"] == "applied"
    store.update_workspace(first["workspace_id"], name="fastpath-synthetic-old")
    second = store.create_workspace(user.id, "fastpath-synthetic", trial_credit_microdollars=0)
    assert store.credit_workspace_typed_direct(second.id, 100_000_000, "another_grant",
                                               provenance=p1.CreditProvenance(source="grant", provider="operator",
                                                                              external_ref=None,
                                                                              occurred_at=p1.datetime.now(p1.UTC)))
    code, _, err = _run(store, capsys, "--apply")
    assert code == 1 and "applied elsewhere" in err


def test_a_payment_fact_of_the_events_id_proves_no_grant(monkeypatch: Any, capsys: Any) -> None:
    """A payment event of the funding event's ID, whose credited amount is
    less than it charged, is not the grant: the replay fails."""
    monkeypatch.setenv("TR_STORAGE_BACKEND", "spanner-clickhouse")
    store, _db = make_fake_store()
    user = _owner(store)
    workspace = store.create_workspace(user.id, "fastpath-synthetic", trial_credit_microdollars=0)
    assert store.credit_workspace_typed_direct(
        workspace.id, 95_000_000, "evt_synthetic_checkout",
        provenance=p1.CreditProvenance(source="checkout", provider="stripe", external_ref="pi_synthetic",
                                       occurred_at=p1.datetime.now(p1.UTC)),
        payment_amount_microdollars=100_000_000, currency="usd")
    code, _, err = _run(store, capsys, "--apply", "--funding-event-id", "evt_synthetic_checkout")
    assert code == 1 and "applied elsewhere" in err


def test_the_audit_baseline_is_the_rows_total(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setenv("TR_STORAGE_BACKEND", "spanner-clickhouse")
    store, _db = make_fake_store()
    user = _owner(store)
    workspace = store.create_workspace(user.id, "fastpath-synthetic", trial_credit_microdollars=0)
    assert store.credit_workspace_typed_direct(workspace.id, 100_000_000, "another_grant",
                                               provenance=p1.CreditProvenance(source="grant", provider="operator",
                                                                              external_ref=None,
                                                                              occurred_at=p1.datetime.now(p1.UTC)))
    code, plan, _ = _run(store, capsys)
    assert code == 0 and plan["credits_total_microdollars"] == 100_000_000
    code, result, _ = _run(store, capsys, "--apply")
    assert code == 0 and result["funded"] == "applied" and result["credits_total_microdollars"] == 200_000_000
