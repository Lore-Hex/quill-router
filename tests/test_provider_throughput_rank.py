from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta

import pytest

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


def test_cloudflare_credit_preference_does_not_change_measurements_or_explicit_sort(monkeypatch):
    from dataclasses import replace

    from trusted_router.catalog import MODEL_ENDPOINTS, MODELS
    from trusted_router.routing import RoutePreferences, _sort_endpoint_candidates

    base = next(e for e in MODEL_ENDPOINTS.values() if e.usage_type == "Credits")
    model = MODELS[base.model_id]
    cloudflare = replace(base, provider="cloudflare-workers-ai", id="cf", prompt_price_microdollars_per_million_tokens=200)
    direct = replace(base, provider="deepinfra", id="direct", prompt_price_microdollars_per_million_tokens=100)
    candidates = [(model, direct), (model, cloudflare)]
    monkeypatch.setattr("trusted_router.routing.measured_provider_rank", lambda provider, sort: 1 if provider == "deepinfra" else 2)
    measured = copy.deepcopy(ranking._RANKS)
    assert _sort_endpoint_candidates(candidates, RoutePreferences())[0][1] == cloudflare
    for sort in ("price", "latency", "throughput"):
        assert _sort_endpoint_candidates(candidates, RoutePreferences(sort=sort))[0][1] == direct
    assert _sort_endpoint_candidates(candidates, RoutePreferences(order=("deepinfra",)))[0][1] == direct
    byok = [(model, direct), (model, replace(cloudflare, usage_type="BYOK"))]
    assert _sort_endpoint_candidates(byok, RoutePreferences())[0][1] == direct
    assert ranking._RANKS == measured


def test_cloudflare_preference_preserves_primary_model_order():
    from dataclasses import replace

    from trusted_router.catalog import MODEL_ENDPOINTS, MODELS
    from trusted_router.routing import RoutePreferences, _sort_endpoint_candidates

    base = next(e for e in MODEL_ENDPOINTS.values() if e.usage_type == "Credits")
    primary = MODELS[base.model_id]
    fallback = replace(primary, id="test/fallback")
    direct = replace(base, provider="deepinfra", id="direct")
    cloudflare = replace(base, provider="cloudflare-workers-ai", id="cf", model_id=fallback.id)
    candidates = [(primary, direct), (fallback, cloudflare)]
    assert _sort_endpoint_candidates(candidates, RoutePreferences()) == candidates


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
