from __future__ import annotations

import dataclasses
import datetime as dt
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from clickhouse.ingest_operational_outbox import OperationalOutboxRow, normalise_operational_event
from clickhouse.operational_fingerprint import canonical_fingerprint
from clickhouse.routing_traffic import METRICS, summarize
from tests.fakes.requested_model_extension import compare_with_requested_model
from trusted_router.operational_analytics import OperationalAnalyticsClient, _generation
from trusted_router.storage_activity import generation_events
from trusted_router.storage_models import GatewayAuthorization, Generation
from trusted_router.storage_operational_analytics import activity_payload
from trusted_router.types import UsageType


@pytest.mark.parametrize("requested", [
    "trustedrouter/auto", "nyte/auto-routing", "trustedrouter/auto:zdr",
    "deepseek/deepseek-v4.1-flash", "trustedrouter/prometheus-3.0", None,
])
def test_request_alias_survives_settlement_replay_and_activity(requested: str | None) -> None:
    authorization = GatewayAuthorization(
        id="auth-route", workspace_id="ws-route", key_hash="private-key-hash",
        model_id="selected-before-fallback", provider="deepinfra",
        usage_type=UsageType.CREDITS, estimated_microdollars=100,
        requested_model_id=requested,
    )
    kwargs: dict[str, Any] = {
        "authorization": authorization, "provider_name": "Hixtube", "provider": "hixtube",
        "model_id": "selected-after-fallback", "input_tokens": 20, "output_tokens": 3,
        "actual_cost_microdollars": 42,
        "body": {"requested_model": "spoofed", "requested_model_id": "spoofed"},
    }
    generation = Generation.from_settle_body(**kwargs)
    replay = Generation.from_settle_body(**kwargs)
    assert replay.id == generation.id
    assert generation.requested_model == (requested or "")
    assert generation.model == "selected-after-fallback"
    assert generation.provider == "hixtube"
    assert generation.total_cost_microdollars == 42
    assert dataclasses.asdict(generation)["requested_model"] == (requested or "")
    assert Generation(**dataclasses.asdict(generation)) == generation

    payload = activity_payload(generation)
    [event] = normalise_operational_event(OperationalOutboxRow(
        shard=0, commit_ts=dt.datetime.now(dt.UTC), event_kind="activity",
        event_id=generation.id, payload=json.dumps(payload),
    ))
    assert event.row["requested_model"] == (requested or "")
    assert canonical_fingerprint(event.row, surface="activity") == canonical_fingerprint(
        activity_payload(replay), surface="activity",
    )
    restored = _generation(event.row, tenant_id=payload["tenant_id"])
    assert restored.requested_model == (requested or "")
    assert restored.to_openrouter_generation()["requested_model"] == requested
    assert generation_events([restored])[0]["requested_model"] == requested


def test_legacy_activity_does_not_invent_request_alias() -> None:
    # Schema defaults and old outbox payloads have the same delivery fingerprint.
    old = {"model": "deepseek/deepseek-v4.1-flash"}
    assert canonical_fingerprint(old, surface="activity") == canonical_fingerprint(
        {**old, "requested_model": ""}, surface="activity",
    )
    assert canonical_fingerprint(old, surface="activity") != canonical_fingerprint(
        {**old, "requested_model": "trustedrouter/auto"}, surface="activity",
    )


def _row(alias: str, count: int = 1) -> dict[str, Any]:
    return {
        "day": "2026-10-27", "requested_model": alias,
        "served_model": "deepseek/deepseek-v4.1-flash", "provider": "hixtube",
        "usage_type": "Credits", **dict.fromkeys(METRICS, "0"),
        "generations": str(count), "input_tokens": str(100 * count),
        "cached_input_tokens": str(80 * count), "output_tokens": str(5 * count),
        "customer_charge_microdollars": str(10 * count),
    }


def test_report_groups_aliases_without_double_counting_or_hiding_unknowns() -> None:
    rows = [_row(alias) for alias in (
        "trustedrouter/auto", "nyte/auto", "trustedrouter/auto-routing", "nyte/auto-routing",
    )]
    rows.extend([
        _row("", 6), _row("nyte/auto:zdr"),
        {**_row("trustedrouter/auto"), "provider": "tinfoil"},
        {**_row("trustedrouter/auto"), "usage_type": "BYOK"},
    ])
    report = summarize(rows, cloud="gcp")
    assert report["cloud"] == "gcp"
    assert report["totals"]["generations"] == 13
    assert report["totals"]["input_tokens"] == 1300
    assert report["totals"]["cached_input_tokens"] == 1040
    assert report["totals"]["customer_charge_microdollars"] == 130
    assert report["attribution_coverage"] == {
        "known_generations": 7, "unknown_generations": 6, "known_fraction": 7 / 13,
    }
    [auto] = [r for r in report["routes"] if (
        r["requested_route"] == "trustedrouter/auto" and r["provider"] == "hixtube"
        and r["usage_type"] == "Credits"
    )]
    assert auto["generations"] == 4
    assert len(auto["requested_aliases"]) == 4
    assert len(report["routes"]) == 5
    assert summarize([], cloud="azure")["attribution_coverage"]["known_fraction"] is None


def test_traffic_query_is_bounded_deduplicated_and_parameterized() -> None:
    start = dt.datetime(2026, 10, 26, tzinfo=dt.UTC)
    end = start + dt.timedelta(days=1)

    def handler(request: httpx.Request) -> httpx.Response:
        sql = request.content.decode()
        assert "FROM activity_generations FINAL" in sql
        assert "synthetic = 0 AND status = 'success'" in sql
        assert "created_at >= parseDateTime64BestEffort({start_at:String}, 3)" in sql
        assert "created_at < parseDateTime64BestEffort({end_at:String}, 3)" in sql
        assert "LIMIT 100001" in sql
        assert "SETTINGS" not in sql  # protected readers refuse setting overrides
        assert "getSetting('group_by_overflow_mode') != 'throw'" in sql
        assert "getSetting('readonly') != 1" in sql
        assert "getSetting('max_execution_time') > 30" in sql
        assert "getSetting('max_memory_usage') > 1073741824" in sql
        assert "AND profile_check = 0" in sql
        assert request.url.params["param_start_at"] == start.isoformat()
        assert request.url.params["param_end_at"] == end.isoformat()
        return httpx.Response(200, json={"data": [_row("nyte/auto")]})

    client = OperationalAnalyticsClient(
        base_url="http://clickhouse.test:8123", user="reader", password="test",  # noqa: S106
        transport=httpx.MockTransport(handler),
    )
    assert client.routing_traffic(start_at=start, end_at=end) == [_row("nyte/auto")]
    for invalid_start, invalid_end in (
        (start, start), (end, start), (start, start + dt.timedelta(days=32)),
        (start.replace(tzinfo=None), end), (start, end.replace(tzinfo=None)),
    ):
        with pytest.raises(ValueError):
            client.routing_traffic(start_at=invalid_start, end_at=invalid_end)


def test_traffic_query_errors_are_not_reported_as_zero() -> None:
    client = OperationalAnalyticsClient(
        base_url="http://clickhouse.test:8123", user="reader", password="test",  # noqa: S106
        transport=httpx.MockTransport(lambda _: httpx.Response(500, text="read limit")),
    )
    with pytest.raises(httpx.HTTPStatusError):
        client.routing_traffic(
            start_at=dt.datetime(2026, 10, 26, tzinfo=dt.UTC),
            end_at=dt.datetime(2026, 10, 27, tzinfo=dt.UTC),
        )


def test_traffic_group_limit_does_not_return_partial_totals(monkeypatch) -> None:
    monkeypatch.setattr(OperationalAnalyticsClient, "_query", lambda *a, **kw: [{}] * 100001)
    client = OperationalAnalyticsClient(
        base_url="http://clickhouse.test:8123", user="reader", password="test",  # noqa: S106
    )
    with pytest.raises(RuntimeError, match="narrow the window"):
        client.routing_traffic(
            start_at=dt.datetime(2026, 10, 26, tzinfo=dt.UTC),
            end_at=dt.datetime(2026, 10, 27, tzinfo=dt.UTC),
        )


def test_requested_model_migrations_are_additive_on_all_clouds() -> None:
    root = Path(__file__).parents[1] / "clickhouse"
    for name in ("027_activity_requested_model_replicated.sql", "028_activity_requested_model_single_node.sql"):
        sql = (root / name).read_text()
        assert "ADD COLUMN IF NOT EXISTS requested_model String DEFAULT '' AFTER model" in sql
        assert "UPDATE" not in sql
        assert ("ON CLUSTER trustedrouter" in sql) == ("replicated" in name)


@pytest.mark.parametrize("id_field", ["id", "generation_id"])
def test_oracle_extension_requires_exact_new_metadata_and_all_old_effects(id_field: str) -> None:
    old = {id_field: "gen-test", "tokens_prompt": 10, "tokens_completion": 2, "total_cost_microdollars": 42}
    expected = {**old, "requested_model": "trustedrouter/auto"}
    left = {"payload": json.dumps(old), "balance": 958, "rpc_count": 3}
    right = {**left, "payload": json.dumps(expected)}
    kwargs = {"generation_id": "gen-test", "requested_model": "trustedrouter/auto"}
    compare_with_requested_model(left, right, **kwargs)
    for wrong in (
        {**right, "balance": 959}, {**right, "rpc_count": 4}, left,
        {**right, "payload": json.dumps({**expected, "total_cost_microdollars": 41})},
        {**right, "payload": json.dumps({**expected, "requested_model": "spoofed"})},
        {**right, "payload": json.dumps({**expected, "tokens_prompt": 11})},
        {**right, "payload": json.dumps({**expected, "extra": True})},
        {**right, "payload": json.dumps({**expected, id_field: "unrelated-generation"})},
    ):
        with pytest.raises(AssertionError):
            compare_with_requested_model(left, wrong, **kwargs)
    with pytest.raises(AssertionError):
        compare_with_requested_model(
            {"payload": '{"balance": 958}'},
            {"payload": '{"balance": 958, "requested_model": "trustedrouter/auto"}'}, **kwargs,
        )
