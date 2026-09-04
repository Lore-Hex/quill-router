from __future__ import annotations

import dataclasses
import inspect
import logging
import re
import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from tests.fakes.postgres import postgres_store_on, sqlite_postgres_conn
from tests.fakes.spanner import make_fake_store
from trusted_router.config import (
    SERVICE_SURFACE_SECRET_OWNERS,
    Settings,
    operator_credential_setting_names,
)
from trusted_router.main import create_app
from trusted_router.routes.internal._shared import internal_service_credential
from trusted_router.storage import InMemoryStore, configure_store
from trusted_router.storage_gcp_credit_shard_admin import reshard_credit_account
from trusted_router.storage_models import CreditAccount, User
from trusted_router.trust_ownership import (
    OwnerTrustMutationBudgetExceeded,
    WorkspaceOwnerLimitExceeded,
    require_owner_trust_budget,
)

ROOT = Path(__file__).resolve().parents[1]


def _create_columns(ddl: str, table: str) -> tuple[str, ...]:
    match = re.search(
        rf"CREATE TABLE(?: IF NOT EXISTS)? {table} \((.*?)\n\s*(?:\) PRIMARY KEY|\);)",
        ddl,
        flags=re.DOTALL,
    )
    assert match is not None
    return tuple(
        line.strip().split()[0]
        for line in match.group(1).splitlines()
        if line.strip()
        and not line.strip().startswith(("CONSTRAINT", "PRIMARY KEY", "CHECK"))
    )


def _trust_rows(database: Any, workspace_id: str) -> list[dict[str, object]]:
    typed = database.typed
    return [
        row
        for (candidate, _shard), row in typed["tr_credit_balance"].items()
        if candidate == workspace_id
    ]


def _seed_grandfathered_owner_fanout(
    store: Any,
    database: Any,
    *,
    workspace_count: int,
) -> tuple[User, list[str]]:
    user = store.ensure_user("grandfathered@example.com", trial_credit_microdollars=0)
    store.set_user_identity_status(
        user.id, status="approved", session_id="grandfathered-session"
    )
    database.typed["tr_owner_workspace"].clear()
    workspace_ids = [f"grandfathered-{index:02d}" for index in range(workspace_count)]
    for workspace_id in workspace_ids:
        store._write_entity(
            "credit",
            workspace_id,
            CreditAccount(workspace_id=workspace_id, shard_count=64),
        )
        database.typed["tr_owner_workspace"][(user.id, workspace_id)] = {
            "owner_user_id": user.id,
            "workspace_id": workspace_id,
        }
        for shard in range(64):
            database.typed["tr_credit_balance"][(workspace_id, shard)] = {
                "workspace_id": workspace_id,
                "shard": shard,
                "trust_tier": 3,
            }
    return user, workspace_ids


def test_in_memory_owner_inventory_lifecycle_limit_transfer_and_backfill() -> None:
    store = InMemoryStore(max_workspaces_per_owner=2)
    old_owner = store.ensure_user("old@example.com", trial_credit_microdollars=0)
    first = store.list_workspaces_for_user(old_owner.id)[0]
    second = store.create_workspace(old_owner.id, "second", trial_credit_microdollars=0)
    assert old_owner.owner_workspace_count == 2
    with pytest.raises(WorkspaceOwnerLimitExceeded):
        store.create_workspace(old_owner.id, "third", trial_credit_microdollars=0)

    new_owner = store.ensure_user("new@example.com", trial_credit_microdollars=0)
    new_personal = store.list_workspaces_for_user(new_owner.id)[0]
    store.update_workspace(new_personal.id, deleted=True)
    transferred = store.transfer_workspace_ownership(second.id, new_owner.id)
    assert transferred.owner_user_id == new_owner.id
    assert old_owner.owner_workspace_count == 1
    assert new_owner.owner_workspace_count == 1

    store.update_workspace(first.id, deleted=True)
    assert (old_owner.id, first.id) not in store.owner_workspaces
    assert all(
        row["trust_latched_at"] is not None and row["trust_tier"] == 0
        for (workspace_id, _shard), row in store.credit_trust_shards.items()
        if workspace_id == first.id
    )
    store.update_workspace(first.id, deleted=False)
    assert (old_owner.id, first.id) in store.owner_workspaces

    store.owner_workspaces.clear()
    old_owner.owner_workspace_count = 99
    assert store.backfill_owner_inventory(source_version="rev-1", environment="test") == 2
    assert old_owner.owner_workspace_count == 1
    assert store.trust_backfills[("owner_inventory", "local", "test")][
        "completed_at"
    ] is not None


def test_spanner_account_and_wallet_bootstraps_seed_owner_inventory() -> None:
    store, database, _ = make_fake_store()
    email_user = store.ensure_user("bootstrap@example.com", trial_credit_microdollars=0)
    wallet_user = store.create_wallet_user("0x" + "A" * 40)

    for user in (email_user, wallet_user):
        assert user.owner_workspace_count == 1
        inventory = [
            key
            for key in database.typed["tr_owner_workspace"]
            if key[0] == user.id
        ]
        assert len(inventory) == 1


def test_postgres_wallet_bootstrap_seeds_inventory_and_counter() -> None:
    conn = sqlite_postgres_conn()
    store = postgres_store_on(conn)
    user = store.create_wallet_user("0x" + "B" * 40)
    repeated = store.create_wallet_user("0x" + "b" * 40)
    row = conn.execute(
        "SELECT workspace_id FROM tr_owner_workspace WHERE owner_user_id = ?",
        (user.id,),
    ).fetchone()
    persisted = store.get_user(user.id)

    assert repeated.id == user.id
    assert row is not None
    assert persisted is not None and persisted.owner_workspace_count == 1


def test_owner_budget_uses_actual_shards_times_seven() -> None:
    require_owner_trust_budget([64] * 44)
    with pytest.raises(OwnerTrustMutationBudgetExceeded):
        require_owner_trust_budget([64] * 45)


def test_fanout_increasing_reshard_rejects_grandfathered_owner_budget() -> None:
    store, database, _ = make_fake_store()
    user = store.ensure_user("fanout@example.com", trial_credit_microdollars=0)
    workspace = store.list_workspaces_for_user(user.id)[0]
    store.update_workspace(workspace.id, billing_paused=True)
    for index in range(44):
        workspace_id = f"grandfathered-{index:02d}"
        store._write_entity(
            "credit",
            workspace_id,
            CreditAccount(workspace_id=workspace_id, shard_count=64),
        )
        database.typed.setdefault("tr_owner_workspace", {})[(
            user.id,
            workspace_id,
        )] = {"owner_user_id": user.id, "workspace_id": workspace_id}
    with pytest.raises(OwnerTrustMutationBudgetExceeded):
        reshard_credit_account(store, workspace.id, 64, apply=True)
    assert store.get_credit_account(workspace.id).shard_count == 16


def test_slice_1c_schemas_have_exact_columns_and_keys() -> None:
    spanner = (ROOT / "scripts/deploy/migrate_typed_counters.sh").read_text()
    postgres = (
        ROOT / "src/trusted_router/storage_postgres_schema.sql"
    ).read_text()
    expected = {
        "tr_owner_workspace": ("owner_user_id", "workspace_id"),
        "tr_trust_override": (
            "workspace_id",
            "tier",
            "identity_bypass",
            "operator_identity",
            "reason",
            "set_at",
        ),
        "tr_trust_demotion_remainder": (
            "owner_user_id",
            "workspace_id",
            "target_identity_ceiling",
            "created_at",
            "attempts",
            "last_error",
        ),
        "tr_trust_backfill": (
            "provider",
            "account_id",
            "environment",
            "source",
            "source_version",
            "history_start",
            "closed_through",
            "consistency_delay_seconds",
            "unmatched_count",
            "semantic_mismatch_count",
            "completed_at",
        ),
    }
    for table, columns in expected.items():
        assert _create_columns(spanner, table) == columns
        assert _create_columns(postgres, table) == columns
    assert ") PRIMARY KEY (owner_user_id, workspace_id)" in spanner
    assert "PRIMARY KEY (provider, account_id, environment)" in postgres


def test_spanner_creation_inventory_and_concurrent_owner_limit() -> None:
    barrier = threading.Barrier(2)
    store, database, _ = make_fake_store(ready_barrier=barrier)
    store.max_workspaces_per_owner = 1
    owner = User(id="owner", email="owner@example.com")
    store._write_entity("user", owner.id, owner)
    results: list[str] = []

    def create(name: str) -> None:
        try:
            store.create_workspace(owner.id, name, trial_credit_microdollars=0)
            results.append("created")
        except WorkspaceOwnerLimitExceeded:
            results.append("limited")

    threads = [threading.Thread(target=create, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert sorted(results) == ["created", "limited"]
    assert len(database.typed["tr_owner_workspace"]) == 1
    persisted = store.get_user(owner.id)
    assert persisted is not None and persisted.owner_workspace_count == 1


def test_spanner_owner_backfill_repairs_both_directions_and_marks_complete() -> None:
    store, database, _ = make_fake_store()
    user = store.ensure_user("backfill@example.com", trial_credit_microdollars=0)
    workspace = store.list_workspaces_for_user(user.id)[0]
    database.typed["tr_owner_workspace"].clear()
    database.typed["tr_owner_workspace"][("stale-owner", "stale-workspace")] = {
        "owner_user_id": "stale-owner",
        "workspace_id": "stale-workspace",
    }
    assert store.backfill_owner_inventory(
        source_version="revision-7", environment="test"
    ) == 2
    assert set(database.typed["tr_owner_workspace"]) == {(user.id, workspace.id)}
    marker = database.typed["tr_trust_backfill"][(
        "owner_inventory",
        "local",
        "test",
    )]
    assert marker["source_version"] == "revision-7"
    assert marker["completed_at"] is not None


def test_veriff_demotion_is_atomic_with_claim_and_all_shards() -> None:
    store, database, _ = make_fake_store()
    user = store.ensure_user("demote@example.com", trial_credit_microdollars=0)
    workspace = store.list_workspaces_for_user(user.id)[0]
    store.set_user_identity_status(
        user.id, status="approved", session_id="session-1"
    )
    for row in _trust_rows(database, workspace.id):
        row["trust_tier"] = 3

    assert (
        store.apply_veriff_identity_decision(
            user.id,
            event_id="decision-1",
            session_id="session-1",
            status="declined",
            decision_code=9102,
        )
        == "applied"
    )
    assert {row["trust_tier"] for row in _trust_rows(database, workspace.id)} == {1}
    assert store.get_user(user.id).identity_status == "declined"
    assert store.apply_veriff_identity_decision(
        user.id,
        event_id="decision-1",
        session_id="session-1",
        status="declined",
        decision_code=9102,
    ) == "replayed"

    store.set_user_identity_status(user.id, status="approved", session_id="session-2")
    missing_key = (workspace.id, 15)
    database.typed["tr_credit_balance"].pop(missing_key)
    with pytest.raises(RuntimeError, match="shard set is incomplete"):
        store.apply_veriff_identity_decision(
            user.id,
            event_id="decision-atomic",
            session_id="session-2",
            status="declined",
            decision_code=9102,
        )
    assert store.get_user(user.id).identity_status == "approved"
    assert store._read_entity("webhook_event", "veriff#decision-atomic", dict) is None


def test_spanner_owner_demotion_over_budget_commits_fit_and_queues_remainder(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, database, _ = make_fake_store()
    user, workspace_ids = _seed_grandfathered_owner_fanout(
        store, database, workspace_count=46
    )

    with caplog.at_level(logging.ERROR, logger="trusted_router.storage_gcp"):
        store.set_user_identity_status(user.id, status="declined")

    fit = workspace_ids[:44]
    remainder = workspace_ids[44:]
    assert all(
        {row["trust_tier"] for row in _trust_rows(database, workspace_id)} == {1}
        for workspace_id in fit
    )
    assert all(
        {row["trust_tier"] for row in _trust_rows(database, workspace_id)} == {3}
        for workspace_id in remainder
    )
    remainder_rows = database.typed["tr_trust_demotion_remainder"]
    assert len(remainder_rows) == len(remainder)
    assert set(remainder_rows) == {
        (user.id, workspace_id) for workspace_id in remainder
    }
    assert [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("trust.identity_demotion_remainder ")
    ] == [
        f"trust.identity_demotion_remainder owner={user.id} workspace={workspace_id}"
        for workspace_id in remainder
    ]


def test_spanner_owner_demotion_under_budget_writes_no_remainder() -> None:
    store, database, _ = make_fake_store()
    user, workspace_ids = _seed_grandfathered_owner_fanout(
        store, database, workspace_count=44
    )

    store.set_user_identity_status(user.id, status="declined")

    assert all(
        {row["trust_tier"] for row in _trust_rows(database, workspace_id)} == {1}
        for workspace_id in workspace_ids
    )
    assert not database.typed.get("tr_trust_demotion_remainder")


def test_spanner_demotion_remainder_is_bounded_and_honors_identity_bypass() -> None:
    store, database, _ = make_fake_store()
    user = store.ensure_user("remainder@example.com", trial_credit_microdollars=0)
    workspace = store.list_workspaces_for_user(user.id)[0]
    store.set_workspace_trust_override(
        workspace.id,
        tier=3,
        identity_bypass=True,
        operator_identity="ops@example.com",
        reason="approved exception",
    )
    database.typed.setdefault("tr_trust_demotion_remainder", {})[(
        user.id,
        workspace.id,
    )] = {
        "owner_user_id": user.id,
        "workspace_id": workspace.id,
        "target_identity_ceiling": 1,
        "created_at": None,
        "attempts": 0,
        "last_error": None,
    }

    assert store.process_trust_demotion_remainders(limit=0) == 0
    assert store.process_trust_demotion_remainders(limit=1) == 1
    assert {row["trust_tier"] for row in _trust_rows(database, workspace.id)} == {3}
    assert not database.typed["tr_trust_demotion_remainder"]


def test_spanner_override_rewrites_all_shards_and_audits_bypass() -> None:
    store, database, _ = make_fake_store()
    user = store.ensure_user("override@example.com", trial_credit_microdollars=0)
    workspace = store.list_workspaces_for_user(user.id)[0]
    record = store.set_workspace_trust_override(
        workspace.id,
        tier=3,
        identity_bypass=False,
        operator_identity="ops@example.com",
        reason="reviewed",
    )
    assert record.tier == 3
    assert {row["trust_tier"] for row in _trust_rows(database, workspace.id)} == {1}
    bypassed = store.set_workspace_trust_override(
        workspace.id,
        tier=3,
        identity_bypass=True,
        operator_identity="ops@example.com",
        reason="verified exception",
    )
    assert bypassed.identity_bypass is True
    assert {row["trust_tier"] for row in _trust_rows(database, workspace.id)} == {3}
    override = database.typed["tr_trust_override"][(workspace.id,)]
    assert override["operator_identity"] == "ops@example.com"
    assert override["reason"] == "verified exception"


def test_spanner_abuse_is_idempotent_and_clear_never_unlatches() -> None:
    store, database, _ = make_fake_store()
    user = store.ensure_user("abuse@example.com", trial_credit_microdollars=0)
    workspace = store.list_workspaces_for_user(user.id)[0]
    assert store.record_workspace_abuse_and_demote(
        workspace.id,
        abuse_ref="case-7",
        operator_identity="ops@example.com",
        reason="confirmed abuse",
    )
    assert not store.record_workspace_abuse_and_demote(
        workspace.id,
        abuse_ref="case-7",
        operator_identity="ops@example.com",
        reason="confirmed abuse",
    )
    audit = store._read_entity(  # noqa: SLF001 - verifies the durable audit row
        "trust_abuse", f"{workspace.id}#case-7", dict
    )
    assert audit is not None
    assert audit["operator_identity"] == "ops@example.com"
    assert audit["reason"] == "confirmed abuse"
    assert all(
        row["trust_tier"] == 0
        and row["trust_latched_at"] is not None
        and row["billing_pause_causes"] == ["abuse"]
        for row in _trust_rows(database, workspace.id)
    )
    assert store.clear_workspace_abuse_pause(
        workspace.id,
        abuse_ref="clear-7",
        operator_identity="ops@example.com",
        reason="appeal accepted",
    )
    assert all(
        row["trust_latched_at"] is not None
        and row["billing_pause_causes"] == []
        for row in _trust_rows(database, workspace.id)
    )


def test_spanner_archive_excludes_inventory_latches_and_retires_leases() -> None:
    store, database, _ = make_fake_store()
    user = store.ensure_user("archive@example.com", trial_credit_microdollars=0)
    workspace = store.list_workspaces_for_user(user.id)[0]
    store._write_entity(
        "spend_lease",
        "lease-1",
        {"workspace_id": workspace.id, "state": "ACTIVE", "closing_at": None},
    )
    store._write_entity(
        "regional_quota_lease",
        "regional-1",
        {"workspace_id": workspace.id, "state": "active", "last_error": None},
    )
    assert store.update_workspace(workspace.id, deleted=True) is None
    assert (user.id, workspace.id) not in database.typed["tr_owner_workspace"]
    assert all(
        row["trust_tier"] == 0 and row["trust_latched_at"] is not None
        for row in _trust_rows(database, workspace.id)
    )
    spend = store._read_entity("spend_lease", "lease-1", dict)
    regional = store._read_entity("regional_quota_lease", "regional-1", dict)
    assert spend["state"] == "TOMBSTONED"
    assert regional["state"] == "quarantined"


def test_operator_routes_require_token_identity_and_replay_abuse() -> None:
    store = InMemoryStore()
    configure_store(store)
    user = store.ensure_user("route@example.com", trial_credit_microdollars=0)
    workspace = store.list_workspaces_for_user(user.id)[0]
    settings = Settings(
        environment="test",
        service_surface="internal",
        operator_token="operator-secret",  # noqa: S106 - test credential.
        operator_identities="ops@example.com",
    )
    client = TestClient(
        create_app(settings, configure_store_arg=False, init_observability=False)
    )
    url = f"/internal/admin/workspaces/{workspace.id}/abuse"
    body = {"abuse_ref": "route-case", "reason": "confirmed"}
    assert client.post(url, json=body).status_code == 401
    assert client.post(
        url,
        headers={
            "authorization": "Bearer operator-secret",
            "x-trustedrouter-operator-identity": "unknown@example.com",
        },
        json=body,
    ).status_code == 403
    headers = {
        "authorization": "Bearer operator-secret",
        "x-trustedrouter-operator-identity": "ops@example.com",
    }
    first = client.post(url, headers=headers, json=body)
    replay = client.post(url, headers=headers, json=body)
    assert first.status_code == 200 and first.json()["data"]["applied"] is True
    assert replay.status_code == 200 and replay.json()["data"]["replayed"] is True
    override = client.post(
        f"/v1/internal/admin/workspaces/{workspace.id}/trust-override",
        headers=headers,
        json={"tier": 2, "identity_bypass": True, "reason": "manual review"},
    )
    assert override.status_code == 200
    assert override.json()["data"]["operator_identity"] == "ops@example.com"
    for prefix in ("", "/v1"):
        assert internal_service_credential(
            settings, f"{prefix}/internal/admin/workspaces/{workspace.id}/abuse"
        ) == ("operator", "operator-secret")
        assert internal_service_credential(
            settings,
            f"{prefix}/internal/admin/workspaces/{workspace.id}/trust-override",
        ) == ("operator", "operator-secret")
    assert internal_service_credential(
        settings, f"/internal/admin/workspaces/{workspace.id}/abuse/extra"
    )[0] == "gateway"


def test_operator_credential_discovery_names_are_pinned_and_values_are_isolated() -> None:
    prefixed = {
        name
        for name in Settings.model_fields
        if name.startswith(("internal_", "observer_", "synthetic_", "federation_"))
        and name.endswith(("_token", "_tokens", "_api_key"))
    }
    assert prefixed == {
        "internal_gateway_token",
        "observer_internal_token",
        "synthetic_monitor_api_key",
        "federation_peer_token",
        "federation_home_token",
        "federation_credit_inbound_token",
        "federation_credit_peer_token",
        "federation_settlement_inbound_tokens",
        "federation_settlement_home_token",
    }
    assert operator_credential_setting_names(Settings) == (
        set(SERVICE_SURFACE_SECRET_OWNERS) | prefixed
    ) - {"operator_token"}
    with pytest.raises(ValidationError, match="TR_FEDERATION_SETTLEMENT_INBOUND_TOKENS"):
        Settings(
            operator_token="same-secret",  # noqa: S106 - test credential.
            operator_identities="ops@example.com",
            federation_settlement_inbound_tokens="aws=same-secret",
        )


def test_postgres_bootstrap_maintains_inventory_and_explicit_conflict_target() -> None:
    conn = sqlite_postgres_conn()
    store = postgres_store_on(conn)
    user = store.ensure_user("postgres-owner@example.com", trial_credit_microdollars=0)
    inventory = conn._raw.execute(
        "SELECT owner_user_id, workspace_id FROM tr_owner_workspace"
    ).fetchall()
    assert len(inventory) == 1 and inventory[0][0] == user.id
    assert store.get_user(user.id).owner_workspace_count == 1
    source = inspect.getsource(store._insert_owner_inventory_tx)
    assert "ON CONFLICT (owner_user_id, workspace_id) DO NOTHING" in source


def test_trust_arm_flag_remains_pinned_off() -> None:
    assert Settings().spend_lease_trust_eligibility_enabled is False


def test_settings_reject_owner_limit_above_pinned_mutation_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TR_CREDIT_SHARDS_MAX", "64")
    monkeypatch.setenv("TR_MAX_WORKSPACES_PER_OWNER", "45")
    with pytest.raises(ValidationError, match="pinned 20000-mutation trust budget"):
        Settings(environment="test")

    monkeypatch.delenv("TR_CREDIT_SHARDS_MAX")
    monkeypatch.delenv("TR_MAX_WORKSPACES_PER_OWNER")
    settings = Settings(environment="test")
    assert settings.max_workspaces_per_owner == 25


def test_user_owner_counter_is_a_persisted_dataclass_field() -> None:
    assert "owner_workspace_count" in {
        field.name for field in dataclasses.fields(User)
    }
