from __future__ import annotations

from pathlib import Path

from scripts.provision_synthetic_monitor import provision
from trusted_router.storage import InMemoryStore


def test_provision_synthetic_monitor_dry_run_is_read_only() -> None:
    store = InMemoryStore()

    result = provision(
        store,
        email="synthetic-monitor@trustedrouter.internal",
        workspace_name="TrustedRouter Synthetic Monitoring",
        key_name="Synthetic monitor",
        funding_microdollars=1_000_000_000,
        funding_event_id="synthetic_monitor_workspace_funding_v1",
        target_shards=16,
        apply=False,
        key_output_file=None,
    )

    assert result["would_create_user"] is True
    assert store.find_user_by_email("synthetic-monitor@trustedrouter.internal") is None


def test_provision_synthetic_monitor_is_isolated_funding_limited_and_idempotent(
    tmp_path: Path,
) -> None:
    store = InMemoryStore()
    key_file = tmp_path / "monitor.key"
    kwargs = {
        "email": "synthetic-monitor@trustedrouter.internal",
        "workspace_name": "TrustedRouter Synthetic Monitoring",
        "key_name": "Synthetic monitor",
        "funding_microdollars": 1_000_000_000,
        "funding_event_id": "synthetic_monitor_workspace_funding_v1",
        "target_shards": 16,
        "apply": True,
    }

    first = provision(store, key_output_file=key_file, **kwargs)
    second = provision(store, key_output_file=None, **kwargs)

    assert first["created_user"] is True
    assert first["created_key"] is True
    assert first["credited"] is True
    assert second["created_key"] is False
    assert second["credited"] is False
    assert second["workspace_id"] == first["workspace_id"]
    assert second["key_id"] == first["key_id"]
    assert key_file.stat().st_mode & 0o777 == 0o600
    assert key_file.read_text(encoding="utf-8").startswith("sk-tr-v1-")
    user = store.find_user_by_email("synthetic-monitor@trustedrouter.internal")
    assert user is not None
    workspaces = store.list_workspaces_for_user(user.id)
    assert len(workspaces) == 1
    assert workspaces[0].name == "TrustedRouter Synthetic Monitoring"
    keys = store.list_keys(workspaces[0].id)
    assert len(keys) == 1
    assert keys[0].management is False
    assert keys[0].limit_daily_microdollars is None
    assert keys[0].limit_monthly_microdollars is None
    assert keys[0].budget_alert_only is False
    assert keys[0].tags["purpose"] == "synthetic_monitoring"
    assert keys[0].tags["spend_control"] == "workspace_funding_only"
    assert first["target_shards"] == 16
    assert first["current_shards"] == 1
    assert first["requires_reshard"] is True
    snapshot = store.credit_money_snapshot(workspaces[0].id)
    assert snapshot is not None
    assert snapshot[0] == 1_000_000_000


def test_provision_synthetic_monitor_rejects_reused_capped_key(tmp_path: Path) -> None:
    store = InMemoryStore()
    first = provision(
        store,
        email="synthetic-monitor@trustedrouter.internal",
        workspace_name="TrustedRouter Synthetic Monitoring",
        key_name="Synthetic monitor",
        funding_microdollars=1_000_000_000,
        funding_event_id="synthetic_monitor_workspace_funding_v1",
        target_shards=16,
        apply=True,
        key_output_file=tmp_path / "monitor.key",
    )
    key = store.get_key_by_hash(first["key_id"])
    assert key is not None
    key.limit_daily_microdollars = 10_000

    try:
        provision(
            store,
            email="synthetic-monitor@trustedrouter.internal",
            workspace_name="TrustedRouter Synthetic Monitoring",
            key_name="Synthetic monitor",
            funding_microdollars=1_000_000_000,
            funding_event_id="synthetic_monitor_workspace_funding_v1",
            target_shards=16,
            apply=True,
            key_output_file=None,
        )
    except ValueError as exc:
        assert str(exc) == "existing synthetic monitor key has unsafe configuration"
    else:
        raise AssertionError("unsafe existing key was accepted")
