"""Independent ZDR hosting is a tie-breaker, not a routing constraint."""

from dataclasses import replace

import pytest

from trusted_router import catalog, routing_candidates
from trusted_router.catalog import MODEL_ENDPOINTS, MODELS, PROVIDERS
from trusted_router.catalog_data import DEEPSEEK_V4_1_FLASH_1017_MODEL_ID
from trusted_router.routing import RoutePreferences, _sort_endpoint_candidates


def candidates(monkeypatch, lab="anthropic", host="amazon-bedrock", *, zdr=True, health=0, band=0):
    base = next(e for e in MODEL_ENDPOINTS.values() if e.usage_type == "Credits")
    model = replace(MODELS[base.model_id], id=f"{lab}/unit", supports_chat=True)
    items = [(model, replace(base, id=provider, model_id=model.id, provider=provider,
                            prompt_price_microdollars_per_million_tokens=100 + index,
                            completion_price_microdollars_per_million_tokens=100))
             for index, provider in enumerate((lab, host))]
    monkeypatch.setattr("trusted_router.routing.measured_provider_rank",
                        lambda provider, sort: health if provider == host else 0)
    monkeypatch.setattr("trusted_router.routing.endpoint_meets_privacy_requirement",
                        lambda endpoint, requirement: endpoint.provider == lab or zdr)
    monkeypatch.setattr("trusted_router.routing.workload_ranks", lambda *args: {
        "default": {lab: 0, host: band}, "latency": {lab: 0, host: 1},
        "throughput": {lab: 0, host: 1},
    })
    return items


@pytest.mark.parametrize("lab", ["anthropic", "openai"])
@pytest.mark.parametrize("host", ["amazon-bedrock", "google-vertex"])
def test_eligible_independent_host_wins_comparable_zdr_tie(monkeypatch, lab, host):
    items = candidates(monkeypatch, lab, host)
    assert _sort_endpoint_candidates(items, RoutePreferences())[0][1].provider == host


@pytest.mark.parametrize("change", [{"zdr": False}, {"health": 1000}, {"health": 2000}, {"band": 1}])
def test_privacy_health_and_performance_take_precedence(monkeypatch, change):
    items = candidates(monkeypatch, **change)
    assert _sort_endpoint_candidates(items, RoutePreferences())[0][1].provider == "anthropic"


@pytest.mark.parametrize("prefs", [RoutePreferences(order=("anthropic",)),
    RoutePreferences(sort="price"), RoutePreferences(sort="latency"),
    RoutePreferences(sort="throughput")])
def test_explicit_provider_and_sort_preferences_win(monkeypatch, prefs):
    items = candidates(monkeypatch)
    assert _sort_endpoint_candidates(items, prefs)[0][1].provider == "anthropic"


def test_does_not_promote_another_model_or_open_weight_host(monkeypatch):
    items = candidates(monkeypatch)
    model, endpoint = items[1]
    other = replace(model, id="anthropic/another")
    different_models = [items[0], (other, replace(endpoint, model_id=other.id))]
    assert _sort_endpoint_candidates(different_models, RoutePreferences()) == different_models
    open_model = replace(model, id="openai/gpt-oss-unit")
    hosts = [(open_model, replace(item[1], model_id=open_model.id,
                                  provider="tinfoil" if i == 0 else "amazon-bedrock"))
             for i, item in enumerate(items)]
    monkeypatch.setattr("trusted_router.routing.workload_ranks", lambda *args: {})
    assert _sort_endpoint_candidates(hosts, RoutePreferences()) == hosts


@pytest.mark.parametrize("build", [routing_candidates.zdr_candidate_models,
    routing_candidates.e2e_candidate_models, routing_candidates.trusted_candidate_models])
def test_privacy_ladders_prefer_1017_only_when_eligible(monkeypatch, build):
    target = DEEPSEEK_V4_1_FLASH_1017_MODEL_ID
    monkeypatch.setattr(routing_candidates, "endpoint_meets_privacy_requirement", lambda *args: True)
    assert build(limit=1)[0].id == target
    assert build(limit=0) == []
    ids = [model.id for model in build()]
    assert len(ids) == len(set(ids))
    monkeypatch.setattr(routing_candidates, "endpoint_meets_privacy_requirement",
                        lambda endpoint, requirement: endpoint.model_id != target)
    assert build()
    assert target not in {model.id for model in build()}


def test_preferred_model_cannot_bypass_provider_allowlist(monkeypatch):
    target = DEEPSEEK_V4_1_FLASH_1017_MODEL_ID
    monkeypatch.setattr(routing_candidates, "endpoint_meets_privacy_requirement", lambda *args: True)
    assert routing_candidates._privacy_candidate_models(
        requirement=2, preferred_models=(target,), allowed_providers=frozenset(),
    ) == []


def test_public_list_preserves_privacy_groups(monkeypatch):
    for slug in ("amazon-bedrock", "google-vertex", "anthropic", "openai"):
        monkeypatch.setitem(PROVIDERS, slug, replace(PROVIDERS[slug],
            provider_confidential_compute=False, provider_e2ee=False,
            provider_zero_data_retention=True, prepaid_zero_data_retention=True))
    ordered = [p.slug for p in catalog.providers_for_display()]
    assert ordered[0] == "trustedrouter"
    for host in ("amazon-bedrock", "google-vertex"):
        assert ordered.index("tinfoil") < ordered.index(host)
        for lab in ("anthropic", "openai"):
            assert ordered.index(host) < ordered.index(lab)
    monkeypatch.setitem(PROVIDERS, "amazon-bedrock", replace(PROVIDERS["amazon-bedrock"],
        provider_zero_data_retention=False, prepaid_zero_data_retention=False))
    ordered = [p.slug for p in catalog.providers_for_display()]
    assert ordered.index("anthropic") < ordered.index("amazon-bedrock")
