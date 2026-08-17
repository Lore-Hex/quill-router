from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
import textwrap
from pathlib import Path

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
