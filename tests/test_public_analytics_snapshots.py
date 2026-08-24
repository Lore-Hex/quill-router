from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

import clickhouse.build_public_snapshots as snapshot_builder
from clickhouse.build_public_snapshots import _clickhouse_string_array, build_snapshots
from trusted_router.config import Settings
from trusted_router.public_analytics_snapshots import current_public_analytics_snapshot
from trusted_router.routes import public as public_routes
from trusted_router.storage_models import (
    ProviderBenchmarkSample,
    SyntheticProbeSample,
    SyntheticRollup,
)


def test_snapshot_worker_imports_without_control_plane_dependencies() -> None:
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join((str(root), str(root / "src")))
    code = textwrap.dedent(
        """
        import builtins

        original_import = builtins.__import__

        def import_without_pydantic(name, *args, **kwargs):
            if name == "pydantic" or name.startswith("pydantic."):
                raise ModuleNotFoundError("pydantic is intentionally absent")
            return original_import(name, *args, **kwargs)

        builtins.__import__ = import_without_pydantic
        import clickhouse.build_public_snapshots
        """
    )

    result = subprocess.run(  # noqa: S603 - interpreter and test program are fixed
        [sys.executable, "-c", code],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_current_public_analytics_snapshot_accepts_fresh_utc_payload() -> None:
    payload = {"generated_at": "2026-08-10T19:00:00Z", "total_samples": 7}

    assert (
        current_public_analytics_snapshot(
            "leaderboard",
            reader=lambda _name: payload,
            now=dt.datetime(2026, 8, 10, 19, 9, tzinfo=dt.UTC),
        )
        is payload
    )


def test_current_public_analytics_snapshot_rejects_stale_future_and_malformed_payloads() -> None:
    now = dt.datetime(2026, 8, 10, 19, 20, tzinfo=dt.UTC)
    invalid = [
        {"generated_at": "2026-08-10T19:00:00Z"},
        {"generated_at": "2026-08-10T19:21:00Z"},
        {"generated_at": "not-a-date"},
        {"total_samples": 1},
    ]

    for payload in invalid:
        assert (
            current_public_analytics_snapshot(
                "leaderboard",
                reader=lambda _name, value=payload: value,
                now=now,
            )
            is None
        )


def test_snapshot_builder_precomputes_video_and_status_inputs() -> None:
    generated_at = "2026-08-15T09:00:00Z"
    video_sample = ProviderBenchmarkSample(
        id="video-1",
        model="minimax/hailuo-3",
        provider="minimax",
        provider_name="MiniMax",
        status="success",
        usage_type="Credits",
        streamed=False,
        route_type="videos",
        elapsed_milliseconds=12_000,
        created_at=generated_at,
    )
    status_sample = SyntheticProbeSample(
        id="status-1",
        probe_type="tls_health",
        target="canonical",
        target_url="https://api.trustedrouter.com/health",
        monitor_region="us-central1",
        status="up",
        created_at=generated_at,
    )
    rollup = SyntheticRollup(
        id="rollup-1",
        period="hour",
        period_start="2026-08-15T08:00:00Z",
        component="canonical_api",
        target="canonical",
        probe_type="tls_health",
        monitor_region="us-central1",
        sample_count=1,
        up_count=1,
    )

    snapshots = build_snapshots(
        [],
        generated_at=generated_at,
        video_samples=[video_sample],
        status_samples=[status_sample],
        status_rollups=[rollup],
    )

    assert set(snapshots) == {
        "apps",
        "client_reliability",
        "leaderboard",
        "status_inputs",
        "video_leaderboard",
    }
    assert snapshots["video_leaderboard"]["total_samples"] == 1
    assert snapshots["status_inputs"]["samples"][0]["id"] == "status-1"
    assert snapshots["status_inputs"]["rollups"][0]["id"] == "rollup-1"
    assert snapshots["client_reliability"]["published"] is False


def test_snapshot_builder_encodes_clickhouse_array_parameters() -> None:
    assert _clickhouse_string_array(["model/a", "model/o'clock", r"model\b"]) == (
        r"['model/a','model/o\'clock','model\\b']"
    )


def test_client_reliability_signals_query_contains_bounded_aggregates(monkeypatch) -> None:
    queries: list[str] = []

    def fake_query(_password: str, sql: str, **_kwargs: object) -> str:
        queries.append(sql)
        return (
            '{"canary_last_received_at":"2026-08-17 11:59:00",'
            '"canary_last_24h":12,"newest_received_at":"2026-08-17 11:59:30"}\n'
        )

    monkeypatch.setattr(snapshot_builder, "_query", fake_query)

    row = snapshot_builder._client_reliability_signals(
        "test-password",
        now=dt.datetime(2026, 8, 17, 12, tzinfo=dt.UTC),
    )

    assert row["canary_last_24h"] == 12
    [query] = queries
    assert "maxIf(received_at, synthetic = 1)" in query
    assert "countIf(synthetic = 1 AND received_at >= now() - INTERVAL 24 HOUR)" in query
    assert "max(received_at)" in query
    assert "WHERE received_at >= now() - INTERVAL 48 HOUR" in query


def test_video_page_uses_shared_snapshot_without_raw_scan(monkeypatch) -> None:
    payload = {
        "generated_at": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
        "models": [],
        "providers": [],
        "model_count": 0,
        "provider_count": 0,
        "total_samples": 0,
    }
    monkeypatch.setattr(public_routes, "_VIDEO_LEADERBOARD_CACHE", None)
    monkeypatch.setattr(
        public_routes,
        "_precomputed_public_analytics_snapshot",
        lambda name: payload if name == "video_leaderboard" else None,
    )

    def raw_scan(**_kwargs):
        raise AssertionError("fresh shared snapshot must avoid a raw video scan")

    monkeypatch.setattr(public_routes, "public_video_benchmark_samples", raw_scan)

    assert public_routes._video_leaderboard_snapshot(Settings(environment="local")) is payload


def test_status_page_uses_shared_inputs_without_raw_scan(monkeypatch) -> None:
    now = dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")
    payload = {
        "generated_at": now,
        "samples": [
            SyntheticProbeSample(
                id="status-live",
                probe_type="tls_health",
                target="canonical",
                target_url="https://api.trustedrouter.com/health",
                monitor_region="us-central1",
                status="up",
                latency_milliseconds=12,
                created_at=now,
            ).public_dict()
        ],
        "rollups": [],
    }
    monkeypatch.setattr(public_routes, "_STATUS_CACHE", None)
    monkeypatch.setattr(
        public_routes,
        "_precomputed_public_analytics_snapshot",
        lambda name: payload if name == "status_inputs" else None,
    )
    monkeypatch.setattr(
        public_routes,
        "_status_samples",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("fresh shared snapshot must avoid a raw status scan")
        ),
    )

    snapshot = public_routes._status_snapshot(Settings(environment="local"))

    assert snapshot["monitor_freshness"]["is_stale"] is False
    assert any(sample["id"] == "status-live" for sample in snapshot["samples"])


def test_client_reliability_rows_are_fetched_per_scope_with_separate_limits(monkeypatch) -> None:
    queries: list[str] = []

    def fake_query(_password: str, sql: str, **_kwargs: object) -> str:
        queries.append(sql)
        return '{"scope":"x"}\n'

    monkeypatch.setattr(snapshot_builder, "_query", fake_query)
    now = dt.datetime(2026, 8, 21, 12, tzinfo=dt.UTC)

    published = snapshot_builder._client_reliability_rows("test-password", now=now)
    calibration = snapshot_builder._client_reliability_rows(
        "test-password", now=now, scope="fleet_all"
    )

    assert published == calibration == [{"scope": "x"}]
    published_sql, calibration_sql = queries
    assert "WHERE scope = 'fleet'\n" in published_sql
    assert "WHERE scope = 'fleet_all'\n" in calibration_sql
    limit = f"LIMIT {snapshot_builder.CLIENT_ROLLUP_LIMIT}"
    assert published_sql.count(limit) == 1
    assert calibration_sql.count(limit) == 1
    with pytest.raises(ValueError, match="scope"):
        snapshot_builder._client_reliability_rows("test-password", now=now, scope="tenant")


def _client_rollup(scope: str, **updates: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "period": "hour",
        "period_start": "2026-08-15T08:00:00Z",
        "scope": scope,
        "host": "",
        "endpoint": "",
        "sdk": "",
        "requests": 50,
        "successes": 49,
        "tr_fault_failures": 1,
        "excluded_failures": 0,
        "aborted": 0,
        "attempts": 50,
        "attempt_tr_fault": 1,
        "distinct_tenants": 1,
        "capped_requests": 0,
        "coverage_requests": 60,
        "total_ms_hist": {"lt800": 50},
        "first_event_ms_hist": {"lt400": 50},
    }
    value.update(updates)
    return value


def test_snapshot_builder_adds_all_traffic_without_touching_the_published_payload() -> None:
    generated_at = "2026-08-15T09:00:00Z"
    published = _client_rollup("fleet")
    calibration = _client_rollup(
        "fleet_all",
        requests=5_000,
        successes=4_990,
        tr_fault_failures=10,
        coverage_requests=5_100,
    )

    without = build_snapshots([], generated_at=generated_at, client_reliability_rows=[published])
    with_all = build_snapshots(
        [],
        generated_at=generated_at,
        client_reliability_rows=[published],
        client_reliability_all_traffic_rows=[calibration],
    )

    assert "all_traffic" not in without["client_reliability"]
    payload = with_all["client_reliability"]
    assert {key: value for key, value in payload.items() if key != "all_traffic"} == (
        without["client_reliability"]
    )
    assert payload["windows"]["24h"]["requests"] == 50
    assert payload["windows"]["24h"]["availability_percent"] is None
    assert payload["all_traffic"]["includes_synthetic"] is True
    assert payload["all_traffic"]["gated"] is False
    assert payload["all_traffic"]["windows"]["24h"]["requests"] == 5_000
    assert payload["all_traffic"]["windows"]["24h"]["availability_percent"] == 99.8
    assert payload["all_traffic"]["windows"]["24h"]["coverage"] == 0.9784


def test_snapshot_worker_main_wires_both_rollup_scopes_into_one_insert(monkeypatch) -> None:
    queries: list[str] = []
    inserted: list[bytes] = []

    def fake_query(
        _password: str,
        sql: str,
        *,
        input_bytes: bytes | None = None,
        params: dict[str, str] | None = None,
    ) -> str:
        _ = params
        queries.append(sql)
        if sql.startswith("INSERT INTO public_analytics_snapshots"):
            assert input_bytes is not None
            inserted.append(input_bytes)
        return ""

    monkeypatch.setattr(snapshot_builder, "_query", fake_query)
    monkeypatch.setenv("CH_PASSWORD", "test-password")

    assert snapshot_builder.main() == 0

    rollup_queries = [sql for sql in queries if "FROM client_availability_rollups FINAL" in sql]
    assert sorted(sql.split("WHERE scope = ", 1)[1].split("\n", 1)[0] for sql in rollup_queries) == [
        "'fleet'",
        "'fleet_all'",
    ]
    [body] = inserted
    payloads = {
        json.loads(line)["name"]: json.loads(json.loads(line)["payload"])
        for line in body.decode().splitlines()
    }
    client = payloads["client_reliability"]
    assert client["published"] is False
    assert client["all_traffic"]["includes_synthetic"] is True
    assert client["all_traffic"]["gated"] is False
    assert set(client["all_traffic"]["windows"]) == {"5m", "1h", "24h", "7d", "30d"}
    assert set(client["windows"]) == {"5m", "1h", "24h", "7d", "30d"}
