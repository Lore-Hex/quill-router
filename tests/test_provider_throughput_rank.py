from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta

import pytest

from scripts import update_provider_throughput_rank as refresh
from scripts.update_provider_throughput_rank import parse_snapshot
from trusted_router import provider_ranking as ranking

NOW = datetime(2026, 10, 9, 15, 5, tzinfo=UTC)


def row(provider="test", *, rate=1.0, tps=100, ttft=100, samples=30, throughput_samples=10):
    return dict(provider=provider, completion_rate=rate, tokens_per_second=tps,
                p50_ttft_ms=ttft, samples=samples, throughput_samples=throughput_samples)


def snapshot(*rows):
    return dict(window="7d", generated_at=NOW.isoformat(), providers=list(rows))


def test_ranks_use_separate_metrics_and_sufficient_samples():
    ranks = ranking.build_ranks([
        row("fast-decode", tps=500, ttft=2000),
        row("fast-first-token", tps=50, ttft=500),
        row("thin", tps=1000, samples=2),
        row("no-speed", tps=None),
        row("flaky", rate=.5, tps=2000),
        row("thin-throughput", throughput_samples=1),
    ])
    assert ranks["throughput"] == {"fast-decode": 0, "fast-first-token": 1}
    assert ranks["latency"]["fast-first-token"] < ranks["latency"]["fast-decode"]
    assert "thin" not in ranks["default"]
    assert ranks["default"]["flaky"] >= 2000
    assert "flaky" not in ranks["throughput"]


def test_stale_future_and_unknown_are_neutral(monkeypatch):
    timestamp, ranks = ranking.validate_snapshot(snapshot(row()))
    monkeypatch.setattr(ranking, "_GENERATED_AT", timestamp)
    monkeypatch.setattr(ranking, "_RANKS", ranks)
    assert ranking.measured_provider_rank("test", "throughput", now=NOW) == 0
    for now in (NOW - timedelta(seconds=1), NOW + timedelta(days=15)):
        assert ranking.measured_provider_rank("test", None, now=now) == ranking.UNKNOWN_RANK
    assert ranking.measured_provider_rank("unknown", None, now=NOW) == ranking.UNKNOWN_RANK


@pytest.mark.parametrize("field,value", [("samples", -1), ("samples", True),
    ("completion_rate", float("nan")), ("completion_rate", 1.1),
    ("tokens_per_second", float("inf")), ("p50_ttft_ms", -1)])
def test_invalid_measurement_rejected(field, value):
    item = row()
    item[field] = value
    with pytest.raises(ValueError):
        ranking.validate_snapshot(snapshot(item))


def test_empty_duplicate_and_unbounded_window_rejected():
    for payload in (snapshot(), snapshot(row(), row()), {**snapshot(row()), "window": "all"}):
        with pytest.raises(ValueError):
            ranking.validate_snapshot(payload)


HTML = '''
<p class="lb-muted">Last updated 2026-10-09T15:03:09.610Z</p>
<section id="lb-providers"><table><tr data-lb-row data-provider="cerebras"
data-completion="1" data-ttft="1002" data-throughput="463.89" data-samples="121">
<td data-label="Effective throughput">464 tok/s<small>10 samples</small></td>
</tr></table></section>
<section id="lb-models"><tr data-lb-row data-provider="wrong"></tr></section>
'''


def test_current_markup_preserves_precise_data_and_ignores_model_rows():
    payload = parse_snapshot(HTML, now=NOW)
    assert payload["providers"] == [row("cerebras", ttft=1002, tps=463.89, samples=121)]
    assert payload["window"] == "7d"
    with pytest.raises(ValueError, match="stale"):
        parse_snapshot(HTML, now=NOW + timedelta(days=2))
    with pytest.raises(ValueError):
        parse_snapshot("<html>failure</html>", now=NOW)


def _refresh_cli(monkeypatch, tmp_path, *, old_age_hours=48, html=None, extra=()):
    now = datetime.now(UTC)
    previous = snapshot(row("previous"))
    previous["generated_at"] = (now - timedelta(hours=old_age_hours)).isoformat()
    destination = tmp_path / "evidence.json"
    destination.write_text(json.dumps(previous))
    source = tmp_path / "leaderboard.html"
    source.write_text(HTML.replace("2026-10-09T15:03:09.610Z", now.isoformat()) if html is None else html)
    monkeypatch.setattr(refresh, "EVIDENCE_PATH", destination)
    monkeypatch.setattr("sys.argv", ["refresh", "--html", str(source), "--write", *extra])
    return destination


def test_daily_refresh_does_not_rewrite_fresh_evidence(monkeypatch, tmp_path):
    destination = _refresh_cli(monkeypatch, tmp_path, old_age_hours=1,
                               html="not even parsed", extra=("--min-age-hours", "24"))
    before = destination.read_bytes()
    refresh.main()
    assert destination.read_bytes() == before


@pytest.mark.parametrize("age", [25, -1])
def test_scheduled_refresh_replaces_old_or_future_evidence(monkeypatch, tmp_path, age):
    destination = _refresh_cli(monkeypatch, tmp_path, old_age_hours=age,
                               extra=("--min-age-hours", "24"))
    refresh.main()
    assert json.loads(destination.read_text())["providers"][0]["provider"] == "cerebras"


def test_manual_refresh_is_immediate(monkeypatch, tmp_path):
    destination = _refresh_cli(monkeypatch, tmp_path, old_age_hours=1)
    refresh.main()
    assert json.loads(destination.read_text())["providers"][0]["provider"] == "cerebras"


@pytest.mark.parametrize("html", ["<html>upstream unavailable</html>", HTML])
def test_failed_or_stale_refresh_preserves_previous_snapshot(monkeypatch, tmp_path, html):
    destination = _refresh_cli(monkeypatch, tmp_path, html=html)
    before = destination.read_bytes()
    with pytest.raises(ValueError):
        refresh.main()
    assert destination.read_bytes() == before


def test_failed_atomic_publication_preserves_previous_snapshot(monkeypatch, tmp_path):
    destination = _refresh_cli(monkeypatch, tmp_path)
    before = destination.read_bytes()
    files = set(tmp_path.iterdir())
    def fail_replace(*args):
        raise OSError("publication failed")
    monkeypatch.setattr(type(destination), "replace", fail_replace)
    with pytest.raises(OSError, match="publication failed"):
        refresh.main()
    assert destination.read_bytes() == before
    assert set(tmp_path.iterdir()) == files


def test_negative_refresh_cadence_is_rejected(monkeypatch, tmp_path):
    destination = _refresh_cli(monkeypatch, tmp_path, extra=("--min-age-hours", "-1"))
    before = destination.read_bytes()
    with pytest.raises(SystemExit):
        refresh.main()
    assert destination.read_bytes() == before


def test_cloudflare_fallback_preference_preserves_measurements_and_explicit_choices(monkeypatch):
    from dataclasses import replace

    from trusted_router.catalog import MODEL_ENDPOINTS, MODELS
    from trusted_router.routing import RoutePreferences, _sort_endpoint_candidates

    base = next(e for e in MODEL_ENDPOINTS.values() if e.usage_type == "Credits")
    model = MODELS[base.model_id]
    cloudflare = replace(base, provider="cloudflare-workers-ai", id="cf", prompt_price_microdollars_per_million_tokens=50)
    direct = replace(base, provider="deepinfra", id="direct", prompt_price_microdollars_per_million_tokens=100)
    candidates = [(model, cloudflare), (model, direct)]
    monkeypatch.setattr("trusted_router.routing.measured_provider_rank", lambda provider, sort: 1 if provider == "deepinfra" else 0)
    measured = copy.deepcopy(ranking._RANKS)
    assert [e for _, e in _sort_endpoint_candidates(candidates, RoutePreferences())] == [direct, cloudflare]
    for sort in ("price", "latency", "throughput"):
        assert _sort_endpoint_candidates(candidates, RoutePreferences(sort=sort))[0][1] == cloudflare
    assert _sort_endpoint_candidates(candidates, RoutePreferences(order=("deepinfra",)))[0][1] == direct
    assert _sort_endpoint_candidates(candidates, RoutePreferences(order=("cloudflare-workers-ai",)))[0][1] == cloudflare
    byok = [(model, direct), (model, replace(cloudflare, usage_type="BYOK"))]
    assert _sort_endpoint_candidates(byok, RoutePreferences())[0][1].provider == "cloudflare-workers-ai"
    assert ranking._RANKS == measured


@pytest.mark.parametrize("direct_rank,cloudflare_rank,first", [
    (0, 0, "deepinfra"),
    (ranking.UNKNOWN_RANK, ranking.UNKNOWN_RANK, "deepinfra"),
    (2_000, 0, "cloudflare-workers-ai"),
    (2_000, 2_001, "deepinfra"),
])
def test_cloudflare_fallback_floor_respects_health(monkeypatch, direct_rank, cloudflare_rank, first):
    from dataclasses import replace

    from trusted_router.catalog import MODEL_ENDPOINTS, MODELS
    from trusted_router.routing import RoutePreferences, _sort_endpoint_candidates

    base = next(e for e in MODEL_ENDPOINTS.values() if e.usage_type == "Credits")
    model = MODELS[base.model_id]
    candidates = [(model, replace(base, provider=p, id=p)) for p in ("cloudflare-workers-ai", "deepinfra")]
    ranks = {"deepinfra": direct_rank, "cloudflare-workers-ai": cloudflare_rank}
    monkeypatch.setattr("trusted_router.routing.measured_provider_rank", lambda provider, sort: ranks[provider])
    assert _sort_endpoint_candidates(candidates, RoutePreferences())[0][1].provider == first


def test_cloudflare_fallback_preserves_primary_model_order():
    from dataclasses import replace

    from trusted_router.catalog import MODEL_ENDPOINTS, MODELS
    from trusted_router.routing import RoutePreferences, _sort_endpoint_candidates

    base = next(e for e in MODEL_ENDPOINTS.values() if e.usage_type == "Credits")
    primary = MODELS[base.model_id]
    fallback = replace(primary, id="test/fallback")
    for first_provider, second_provider in (("deepinfra", "cloudflare-workers-ai"), ("cloudflare-workers-ai", "deepinfra")):
        first = replace(base, provider=first_provider, id="first")
        second = replace(base, provider=second_provider, id="second", model_id=fallback.id)
        candidates = [(primary, first), (fallback, second)]
        assert _sort_endpoint_candidates(candidates, RoutePreferences()) == candidates


@pytest.mark.parametrize("provider_options", [{}, {"only": ["cloudflare-workers-ai"]}, {"order": ["cloudflare-workers-ai"], "allow_fallbacks": False}])
def test_cloudflare_remains_available_when_only_route_or_explicitly_selected(monkeypatch, provider_options):
    from tests.fixture_routes import serve_on_fixture_route
    from trusted_router.config import Settings
    from trusted_router.routing import chat_route_endpoint_candidates

    model_id = "unit/cloudflare-fallback"
    serve_on_fixture_route(monkeypatch, model_id, "cloudflare-workers-ai", author="unit")
    candidates = chat_route_endpoint_candidates(
        {"model": model_id, "provider": {"usage": "credits", **provider_options}},
        Settings(environment="test"),
    )
    assert [endpoint.provider for _, endpoint in candidates] == ["cloudflare-workers-ai"]


def test_cloudflare_preference_never_bypasses_privacy_floor(monkeypatch):
    from dataclasses import replace

    from tests.fixture_routes import serve_on_fixture_route
    from trusted_router.catalog import PROVIDERS
    from trusted_router.config import Settings
    from trusted_router.routing import chat_route_endpoint_candidates

    model_id = "unit/privacy-preference"
    for provider, zdr in (("cloudflare-workers-ai", False), ("greenference", True)):
        monkeypatch.setitem(PROVIDERS, provider, replace(
            PROVIDERS[provider], stores_content=not zdr, provider_zero_data_retention=zdr,
            provider_confidential_compute=False, provider_e2ee=False,
        ))
        serve_on_fixture_route(monkeypatch, model_id, provider, author="unit")
    candidates = chat_route_endpoint_candidates(
        {"model": model_id, "provider": {"usage": "credits", "min_privacy": "zdr"}},
        Settings(environment="test"),
    )
    assert [endpoint.provider for _, endpoint in candidates] == ["greenference"]
