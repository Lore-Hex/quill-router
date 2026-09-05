from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from clickhouse import build_public_snapshots as worker
from trusted_router.config import Settings
from trusted_router.dashboard import public_leaderboard_html
from trusted_router.routes import public
from trusted_router.storage_models import ProviderBenchmarkSample
from trusted_router.synthetic.leaderboard import aggregate_leaderboard
from trusted_router.synthetic.probes import (
    SyntheticTarget,
    provider_rotation_probe,
    rotation_candidates,
)


def sample(**values):
    return ProviderBenchmarkSample(
        **{
            "id": "evidence-1",
            "model": "openai/gpt-5-mini",
            "provider": "openai",
            "provider_name": "OpenAI",
            "status": "success",
            "usage_type": "Credits",
            "streamed": True,
            "first_token_milliseconds": 100,
            "created_at": "2026-09-05T00:00:00Z",
            **values,
        }
    )


def test_config_only_routes_remain_visible_without_fake_availability() -> None:
    row = sample(
        status="unsupported", error_type="probe_config_error", first_token_milliseconds=None
    )
    payload = aggregate_leaderboard(
        [row], model_rank_min_samples=10, provider_rank_min_samples=30, rank_min_ttft_samples=3
    )
    for result in [payload["models"][0], payload["providers"][0]]:
        assert result["sample_count"] == 0
        assert result["excluded_count"] == 1
        assert result["rank"] is None
        assert result["completion_rate"] is None
        assert result["provider_availability"] is None
        assert result["errors"] == {}
    assert payload["models"][0]["last_seen"] == row.created_at


def test_evidence_does_not_blend_into_recent_sample_or_lower_rank_floor() -> None:
    recent = [sample(status="error", error_type="provider_error", error_status=503)]
    historical = [sample(id=f"older-{n}") for n in range(10)]
    snapshots = worker.build_snapshots(
        recent, evidence_samples=historical, generated_at="2026-09-05T00:00:00Z"
    )
    current, evidence = snapshots["leaderboard"], snapshots["leaderboard_evidence"]
    assert current["total_samples"] == 1
    assert current["models"][0]["completion_rate"] == 0
    assert current["models"][0]["rank"] is None
    assert evidence["total_samples"] == 10
    assert evidence["models"][0]["rank"] == 1
    assert evidence["providers"][0]["rank"] is None
    assert evidence["rank_minimums"] == current["rank_minimums"]
    assert evidence["window"] == "7d"


def test_evidence_query_is_bounded_balanced_and_does_not_filter_failures(monkeypatch) -> None:
    queries = []
    monkeypatch.setattr(worker, "_query", lambda password, query: queries.append(query) or "")
    assert worker._evidence_samples("unused") == []
    sql = queries[0]
    for fragment in [
        "INTERVAL 7 DAY",
        "PARTITION BY provider, model, source",
        "route_rank <= 30",
        "provider_rank <= 500",
        "LIMIT 10000",
    ]:
        assert fragment in sql
    assert "status =" not in sql
    assert "error_type =" not in sql


def test_monthly_worker_query_uses_stored_months_and_preserves_histograms(monkeypatch) -> None:
    queries = []
    monkeypatch.setattr(worker, "_query", lambda password, query: queries.append(query) or "")
    worker._status_inputs("unused")
    sql = queries[-1]
    assert "period = 'month'" in sql
    assert "INTERVAL 23 MONTH" in sql
    assert "period = 'hour'" in sql
    assert "histogram" not in sql.split("FROM")[0]  # SELECT * retains histograms.


def test_evidence_build_failure_does_not_stop_status_publication(monkeypatch, capsys) -> None:
    def fail(password):
        raise RuntimeError("query resource limit")
    inserted = []
    monkeypatch.setenv("CH_PASSWORD", "test-placeholder")
    monkeypatch.setattr(worker, "_evidence_samples", fail)
    monkeypatch.setattr(worker, "_status_inputs", lambda password: ([], []))
    monkeypatch.setattr(worker, "_samples", lambda password: [])
    monkeypatch.setattr(worker, "_video_samples", lambda password: [])
    monkeypatch.setattr(worker, "_client_reliability_rows", lambda *args, **kwargs: [])
    monkeypatch.setattr(worker, "_client_reliability_signals", lambda *args, **kwargs: {})
    monkeypatch.setattr(worker, "_query", lambda *args, **kwargs: inserted.extend(json.loads(line) for line in kwargs["input_bytes"].splitlines()) or "")
    assert worker.main() == 0
    assert "status_inputs" in {row["name"] for row in inserted}
    assert "leaderboard" in {row["name"] for row in inserted}
    assert "leaderboard_evidence" not in {row["name"] for row in inserted}
    assert "leaderboard_evidence_build_failed" in capsys.readouterr().err


def test_monthly_public_read_rejects_truncated_history(monkeypatch) -> None:
    monkeypatch.setattr(public, "STATUS_MONTH_ROLLUP_LIMIT", 2)
    monkeypatch.setattr(
        public, "STORE", SimpleNamespace(synthetic_rollups=lambda **kw: [object(), object()])
    )
    with pytest.raises(RuntimeError, match="refusing partial history"):
        public._status_rollups("monthly")


@pytest.mark.parametrize(
    "precomputed", [None, {"generated_at": "2026-09-05T00:00:00Z", "total_samples": 42}]
)
def test_evidence_never_scans_raw_samples_and_keeps_separate_cache(
    monkeypatch, precomputed
) -> None:
    def no_scan(**kwargs):
        raise AssertionError("seven-day page must not scan raw rows")

    names = []
    monkeypatch.setattr(public, "_LEADERBOARD_EVIDENCE_CACHE", None)
    monkeypatch.setattr(public, "_LEADERBOARD_CACHE", (0, {"total_samples": 999}))
    monkeypatch.setattr(public, "public_benchmark_samples", no_scan)
    monkeypatch.setattr(
        public,
        "_precomputed_public_analytics_snapshot",
        lambda name: names.append(name) or precomputed,
    )
    payload = public._leaderboard_snapshot(Settings(environment="local"), window="7d")
    assert payload["total_samples"] == (42 if precomputed else 0)
    assert names == ["leaderboard_evidence"]
    assert public._LEADERBOARD_CACHE[1]["total_samples"] == 999
    assert public._leaderboard_snapshot(Settings(environment="local"), window="7d") is payload


def test_unknown_evidence_window_is_rejected(client) -> None:
    assert client.get("/leaderboard?window=forever").status_code == 400


def test_rotation_does_not_schedule_router_names_as_direct_providers() -> None:
    pool = rotation_candidates()
    assert pool
    assert {"openrouter", "trustedrouter"}.isdisjoint(pool)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 500, 503])
async def test_new_reasoning_probe_keeps_failures_and_does_not_retry(status) -> None:
    bodies = []

    def respond(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(
            status, json={"error": {"type": "provider_error", "message": "upstream unavailable"}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await provider_rotation_probe(
            client,
            SyntheticTarget("rotation", "https://example.test/v1", "us-central1"),
            monitor_region="us-central1",
            api_key="test-placeholder",
            provider="azure",
            model="openai/gpt-5-mini",
        )
    assert len(bodies) == 1
    assert bodies[0]["max_tokens"] == 512
    assert "temperature" not in bodies[0]
    assert result.status == "error"
    assert result.error_status == status
    assert result.error_type != "probe_config_error"


def test_public_details_escape_model_names_and_never_render_error_body() -> None:
    row = sample(
        model='publisher/<script>alert("oops")</script>',
        status="error",
        error_type="provider_error",
        error_message="PRIVATE-ERROR-BODY",
        error_status=503,
    )
    snapshot = aggregate_leaderboard([row])
    snapshot["generated_at"] = "2026-09-05T00:00:00Z"
    html = public_leaderboard_html(Settings(environment="test"), snapshot)
    assert "PRIVATE-ERROR-BODY" not in html
    assert '<script>alert("oops")</script>' not in html
    assert "&lt;script&gt;" in html
    assert "data-lb-controls" in html
