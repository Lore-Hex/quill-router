from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace
from typing import Any

import pytest

from clickhouse.backfill_generation_records import _iter_recent
from tests.fakes.spanner import make_fake_store
from trusted_router.storage import CreditAccount, create_store
from trusted_router.storage_gcp_authorize import AuthorizeOutcome
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE
from trusted_router.storage_models import Generation


class _CapturingBigtableTable:
    def __init__(self) -> None:
        self.filter: Any = None

    def read_rows(self, **kwargs: Any) -> list[Any]:
        self.filter = kwargs["filter_"]
        return []


def test_generation_backfill_uses_sdk_timestamp_range_object() -> None:
    table = _CapturingBigtableTable()
    cutoff = dt.datetime(2026, 7, 1, tzinfo=dt.UTC)

    assert list(_iter_recent(table, cutoff=cutoff)) == []
    assert table.filter.filters[0].range_.start == cutoff


def _seed_credit(store: Any, workspace_id: str, total: int = 5_000_000) -> None:
    store._write_entity(
        "credit",
        workspace_id,
        CreditAccount(workspace_id=workspace_id),
    )
    store._database.typed.setdefault(CREDIT_BALANCE_TABLE, {})[
        (workspace_id, 0)
    ] = {
        "workspace_id": workspace_id,
        "shard": 0,
        "total_credits": total,
        "total_usage": 0,
        "reserved": 0,
        "source_updated_at": None,
        "updated_at": None,
    }


def _authorize(store: Any, workspace_id: str) -> tuple[Any, Any]:
    _seed_credit(store, workspace_id)
    _raw, key = store.api_keys.create(
        workspace_id=workspace_id,
        name="migration-test",
        creator_user_id=None,
        limit_microdollars=5_000_000,
    )
    outcome, authorization = store.authorize_gateway_typed(
        workspace_id=workspace_id,
        key_hash=key.hash,
        estimate=1_000_000,
        has_credit_candidate=True,
        reservation_usage_type="Credits",
        model_id="anthropic/claude-haiku-4.5",
        provider="anthropic",
        requested_model_id=None,
        candidate_model_ids=["anthropic/claude-haiku-4.5"],
        region="us-central1",
        endpoint_id="anthropic/claude-haiku-4.5@anthropic",
        candidate_endpoint_ids=["anthropic/claude-haiku-4.5@anthropic"],
        idempotency_key=None,
        idempotency_fingerprint=None,
        expires_at="2026-08-01T12:00:00Z",
    )
    assert outcome == AuthorizeOutcome.ACCEPTED
    assert authorization is not None
    return authorization, key


def _generation(authorization: Any, key_hash: str) -> Generation:
    return Generation(
        id="gen-atomic-clickhouse",
        request_id="req-atomic-clickhouse",
        workspace_id=authorization.workspace_id,
        key_hash=key_hash,
        model="anthropic/claude-haiku-4.5",
        provider="anthropic",
        provider_name="Anthropic",
        app="retirement-test",
        tokens_prompt=10,
        tokens_completion=4,
        total_cost_microdollars=900_000,
        usage_type="Credits",
        speed_tokens_per_second=8.0,
        finish_reason="stop",
        status="success",
        streamed=False,
        tool_calls=[
            {
                "function": {
                    "name": "private_tool",
                    "arguments": "customer-secret-tool-output",
                }
            }
        ],
        created_at="2026-08-01T10:00:00Z",
    )


def test_typed_settlement_atomically_persists_generation_and_activity_outbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, database, bigtable = make_fake_store(
        request_record_write_mode="typed",
        operational_analytics_outbox_enabled=True,
        generation_records_enabled=True,
    )
    authorization, key = _authorize(store, "ws-atomic-clickhouse")
    generation = _generation(authorization, key.hash)

    def fail_bigtable(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("migration mirror unavailable")

    monkeypatch.setattr(
        "trusted_router.storage_gcp_generations._bt_write_generation",
        fail_bigtable,
    )

    result = store.typed_finalize_gateway_authorization_result(
        authorization.id,
        success=True,
        actual_microdollars=900_000,
        selected_usage_type="Credits",
        generation=generation,
    )

    assert result.finalized is True
    assert result.activity_indexed is True
    assert generation.id in database.generation_records
    assert len(database.operational_analytics_outbox) == 1
    event = database.operational_analytics_outbox[0]
    assert event["event_kind"] == "activity"
    assert event["event_id"] == generation.id
    assert bigtable.committed
    assert all(key.startswith(b"benchmark") for key in bigtable.committed)
    restored = store.get_generation(generation.id)
    assert restored is not None
    assert restored.id == generation.id
    assert restored.tool_calls is None
    generation_record = database.generation_records[generation.id]
    assert "customer-secret-tool-output" not in str(generation_record["payload"])
    assert "tool_calls" not in json.loads(str(generation_record["payload"]))

    payload = json.loads(str(event["payload"]))
    serialized = json.dumps(payload).lower()
    assert set(payload).isdisjoint({"prompt", "input", "output", "messages"})
    assert "authorization" not in serialized


def test_outbox_failure_rolls_back_charge_and_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, database, _bigtable = make_fake_store(
        request_record_write_mode="typed",
        operational_analytics_outbox_enabled=True,
        generation_records_enabled=True,
    )
    authorization, key = _authorize(store, "ws-outbox-rollback")
    generation = _generation(authorization, key.hash)

    def fail_enqueue(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("Spanner outbox unavailable")

    monkeypatch.setattr(
        store._operational_analytics_outbox,
        "enqueue_activity_tx",
        fail_enqueue,
    )

    with pytest.raises(RuntimeError, match="outbox unavailable"):
        store.typed_finalize_gateway_authorization_result(
            authorization.id,
            success=True,
            actual_microdollars=900_000,
            selected_usage_type="Credits",
            generation=generation,
        )

    reservation = database.reservations[authorization.credit_reservation_id]
    credit = database.typed[CREDIT_BALANCE_TABLE][
        (authorization.workspace_id, 0)
    ]
    assert reservation["settled"] is False
    assert credit["total_usage"] == 0
    assert credit["reserved"] == 1_000_000
    assert generation.id not in database.generation_records
    assert database.operational_analytics_outbox == []


def test_clickhouse_only_read_never_invokes_bigtable() -> None:
    from trusted_router.storage_gcp import SpannerBigtableStore

    store = object.__new__(SpannerBigtableStore)
    store._analytics_read_mode = "clickhouse-only"

    value = store._analytics_read(
        "test",
        bigtable=lambda: pytest.fail("Bigtable must not be called"),
        clickhouse=lambda: ["clickhouse"],
    )

    assert value == ["clickhouse"]


def test_spanner_clickhouse_factory_disables_bigtable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_store(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(
        "trusted_router.storage_gcp.SpannerBigtableStore",
        fake_store,
    )
    settings = SimpleNamespace(
        storage_backend="spanner-clickhouse",
        gcp_project_id="project",
        spanner_instance_id="instance",
        spanner_database_id="database",
        bigtable_instance_id=None,
        bigtable_generation_table="unused",
        generation_records_enabled=True,
        operational_analytics_outbox_enabled=True,
        operational_analytics_clickhouse_url="http://clickhouse",
        operational_analytics_clickhouse_password="sec" + "ret",
    )

    create_store(settings)

    assert captured["bigtable_enabled"] is False
    assert captured["bigtable_writes_enabled"] is False
    assert captured["analytics_read_mode"] == "clickhouse-only"
    assert captured["generation_records_enabled"] is True
