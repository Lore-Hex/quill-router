from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from pathlib import Path
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


def test_evidence_ranking_does_not_sort_full_metadata_rows(monkeypatch) -> None:
    queries = []
    monkeypatch.setattr(worker, "_query", lambda password, query: queries.append(query) or "")
    worker._evidence_samples("unused")
    sql = queries[0]
    assert "(provider, model, created_at, id) IN" in sql
    selection = sql.split("(provider, model, created_at, id) IN", 1)[1]
    assert "SELECT *" not in selection
    assert "SELECT provider, model, source, created_at, id," in selection
    assert "LIMIT 30 BY provider, model, source" in selection
    assert "max_memory_usage = 268435456" in sql
    assert "max_bytes_before_external_sort = 67108864" in sql
    assert "max_execution_time = 15" in sql
    assert "max_threads = 2" in sql


def test_worker_deploy_gate_requires_all_current_snapshot_products() -> None:
    script = (Path(__file__).parents[1] / "scripts/deploy/sync_public_analytics_snapshots.sh").read_text()
    snapshots = worker.build_snapshots([], generated_at="2026-09-20T00:00:00Z")
    for name in snapshots:
        assert f"'{name}'" in script.replace("\\'", "'").replace("'''", "'")
    assert len(snapshots) == 6
    assert '!= 6 ]; then' in script
    assert "publishing all six products" in script


def test_evidence_key_selection_preserves_fair_caps_ties_errors_and_full_keys(monkeypatch) -> None:
    queries = []
    monkeypatch.setattr(worker, "_query", lambda password, query: queries.append(query) or "")
    worker._evidence_samples("unused")
    # SQLite executes the window/tuple selection locally. Its dialect lacks
    # LIMIT BY; the existing route_rank <= 30 guard gives the same membership.
    # The opt-in ClickHouse test also executes the early LIMIT BY in real SQL.
    sql = queries[0].split("SETTINGS", 1)[0].replace("* EXCEPT ingest_version", "*")
    sql = sql.replace(" FINAL", "").replace("now64(3) - INTERVAL 7 DAY", "100")
    sql = sql.replace("LIMIT 30 BY provider, model, source", "")
    rows = [
        (f"sample-{p:02}-{m:02}-{source}-{n:02}", 100 + n // 3, f"p{p:02}", f"m{m:02}", source,
         "error" if n % 3 == 0 else "success", "metadata")
        for p in range(23)
        for m in range(20)
        for source in ("organic", "synthetic")
        for n in range(35)
    ]
    # An old row reuses a current id. Membership and the outer date predicate
    # must not fetch an out-of-window row along with its selected counterpart.
    rows.append((rows[-1][0], 99, "old", "old", "organic", "error", "old"))
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE provider_benchmark_samples "
                   "(id TEXT, created_at INTEGER, provider TEXT, model TEXT, "
                   "source TEXT, status TEXT, error_message TEXT)")
        db.executemany("INSERT INTO provider_benchmark_samples VALUES (?,?,?,?,?,?,?)", rows)
        actual = db.execute(sql).fetchall()

    routes = defaultdict(list)
    for row in rows:
        if row[1] >= 100:
            routes[(row[2], row[3], row[4])].append(row)
    providers = defaultdict(list)
    for route in routes.values():
        for rank, row in enumerate(sorted(route, key=lambda row: (row[1], row[0]), reverse=True)[:30], 1):
            providers[row[2]].append((rank, row))
    expected = []
    for provider in providers.values():
        provider.sort(key=lambda item: (item[1][1], item[1][0]), reverse=True)
        provider.sort(key=lambda item: item[0])
        expected.extend((rank, row) for rank, (_, row) in enumerate(provider[:500], 1))
    expected.sort(key=lambda item: (item[1][1], item[1][0]), reverse=True)
    expected.sort(key=lambda item: item[0])
    assert {tuple(row) for row in actual} == {row for _, row in expected[:10000]}
    assert len(actual) == 10000
    assert {row[5] for row in actual} == {"success", "error"}


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
