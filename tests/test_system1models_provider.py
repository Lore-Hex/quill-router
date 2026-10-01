from __future__ import annotations

import copy
import json
from pathlib import Path

import httpx
import pytest

from scripts.pricing.providers._system1models import System1Catalog


@pytest.fixture
def served_system1(monkeypatch):
    from tests.fixture_routes import serve_on_fixture_route
    from trusted_router.catalog_data import Model

    # Product rules must not block hourly discovery when a vendor delists a model.
    for slug in ("system1models", "system1models-eu"):
        model_id = f"{slug}/s1-fixture"
        model = Model(
            id=model_id,
            name="System1 fixture",
            provider=slug,
            context_length=0,
            supports_chat=False,
            supports_decide=True,
            byok_available=False,
        )
        serve_on_fixture_route(
            monkeypatch,
            model_id,
            slug,
            author=slug,
            model=model,
            upstream_id="s1-fixture",
            completion_price_microdollars_per_million_tokens=0,
        )


def catalog(*models: str) -> dict:
    return {
        "data": [
            {
                "id": model,
                "name": model,
                "status": "available",
                "tier_availability": {"eu": True, "global": True},
                "modalities": ["text"],
                "question_types": ["noul", "choice", "score"],
                "prices": {
                    "unit": "per_million_input_tokens",
                    "output_tokens": "free",
                    "USD": {"eu": "0.034", "global": "0.025"},
                    "EUR": {"eu": "0.030", "global": "0.022"},
                },
            }
            for model in models
        ]
    }


@pytest.mark.parametrize(
    "tier,slug,cost", [("global", "system1models", 25_000), ("eu", "system1models-eu", 34_000)]
)
def test_discovery_uses_exact_regional_usd_not_eur(tier, slug, cost):
    adapter = System1Catalog(tier)
    prices, rows = adapter.discover(catalog("s1-fast", "s1-future"))
    assert set(prices) == {f"{slug}/s1-fast", f"{slug}/s1-future"}
    for model, price in prices.items():
        assert price.prompt_micro_per_m == cost
        assert price.completion_micro_per_m == 0
        assert rows[model]["endpoints"] == ["decide"]
        assert rows[model]["model_type"] == "decision"
        assert "chat/completions" not in rows[model]["endpoints"]


@pytest.mark.parametrize(
    "change",
    [
        "missing_usd",
        "wrong_unit",
        "output_billed",
        "negative",
        "nan",
        "zero",
        "precision",
        "float",
        "duplicate",
        "bad_id",
        "empty",
        "unsupported_question",
        "bad_availability",
        "bad_question_types",
        "unknown_vision",
    ],
)
def test_ambiguous_catalog_and_prices_fail_closed(change):
    payload = catalog("s1-fast")
    row = payload["data"][0]
    rates = row["prices"]
    if change == "missing_usd":
        del rates["USD"]
    elif change == "wrong_unit":
        rates["unit"] = "per_token"
    elif change == "output_billed":
        rates["output_tokens"] = "0.01"
    elif change in {"negative", "nan", "zero", "precision", "float"}:
        rates["USD"]["eu"] = {
            "negative": "-1",
            "nan": "NaN",
            "zero": "0",
            "precision": "0.0000001",
            "float": 0.034,
        }[change]
    elif change == "duplicate":
        payload["data"].append(copy.deepcopy(row))
    elif change == "bad_id":
        row["id"] = "https://example.com/model"
    elif change == "empty":
        payload["data"] = []
    elif change == "unsupported_question":
        row["question_types"] = ["chat"]
    elif change == "bad_availability":
        row["tier_availability"] = None
    elif change == "bad_question_types":
        row["question_types"] = [{}]
    elif change == "unknown_vision":
        row["id"] = "s1-future-vision"
        row["modalities"] = ["text", "image"]
    with pytest.raises(RuntimeError):
        System1Catalog("eu").discover(payload)


def test_tiers_and_availability_never_cross():
    payload = catalog("s1-fast", "s1-pro", "s1-unavailable")
    payload["data"][1]["tier_availability"]["eu"] = False
    payload["data"][2]["status"] = "unavailable"
    eu, _ = System1Catalog("eu").discover(payload)
    global_prices, _ = System1Catalog("global").discover(payload)
    assert set(eu) == {"system1models-eu/s1-fast"}
    assert set(global_prices) == {"system1models/s1-fast", "system1models/s1-pro"}


def test_canary_gates_new_routes_and_failed_fetch_cannot_publish(monkeypatch, tmp_path):
    from scripts.pricing.providers import _system1models

    adapter = System1Catalog("eu")
    adapter.manifest_path = tmp_path / "system1models-eu.json"
    monkeypatch.setenv(adapter.key_env, "test-eu")
    monkeypatch.setattr(_system1models, "fetch_json", lambda *_a: catalog("s1-fast", "s1-pro"))
    monkeypatch.setattr(
        adapter, "probe", lambda key, row: key == "test-eu" and row["upstream_id"] == "s1-fast"
    )
    result = adapter.fetch()
    assert result.include_in_price_index is False
    adapter.write_provider_manifest(result)
    rows = {r["id"]: r for r in json.loads(adapter.manifest_path.read_text())["models"]}
    assert rows["system1models-eu/s1-fast"]["routable"] is True
    assert rows["system1models-eu/s1-pro"]["routable"] is False
    monkeypatch.delenv(adapter.key_env)
    with pytest.raises(RuntimeError, match="required"):
        adapter.fetch()
    with pytest.raises(RuntimeError, match="fetch must succeed"):
        adapter.write_provider_manifest(result)


@pytest.mark.parametrize("tier", ["eu", "global"])
@pytest.mark.parametrize(
    "change", [None, "header", "tier", "model", "tokens", "output", "decisions"]
)
def test_canary_requires_matching_tier_model_and_billable_usage(monkeypatch, tier, change):
    from scripts.pricing.providers import _system1models

    adapter = System1Catalog(tier)
    body = {
        "model": "s1-fast",
        "tier": tier,
        "answers": {"color": {"choice": "red"}},
        "usage": {"input_tokens": 100, "output_tokens": 0, "decisions": 1},
    }
    if change in {"tier", "model"}:
        body[change] = "wrong"
    elif change == "tokens":
        body["usage"]["input_tokens"] = True
    elif change == "output":
        body["usage"]["output_tokens"] = 1
    elif change == "decisions":
        body["usage"]["decisions"] = 2

    def post(url, *, headers, json, timeout):
        assert headers == {"Authorization": f"Bearer test-{tier}", "S1-Region": tier}
        assert url.endswith("/systemone") and json["model"] == "s1-fast"
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            headers={"S1-Region": "wrong" if change == "header" else tier},
            json=body,
        )

    monkeypatch.setattr(_system1models.httpx, "post", post)
    assert adapter.probe(f"test-{tier}", {"upstream_id": "s1-fast"}) is (change is None)


@pytest.mark.parametrize("tier,cost", [("global", 25_000), ("eu", 34_000)])
def test_catalog_integration_no_chat_no_byok_no_cross_tier(monkeypatch, tmp_path, tier, cost):
    from scripts.pricing.providers import _system1models
    from trusted_router import catalog_ingest
    from trusted_router.catalog_data import GATEWAY_PREPAID_PROVIDER_SLUGS
    from trusted_router.pricing import _customer_price
    from trusted_router.services.inference_errors import default_provider_secret_ref

    adapter = System1Catalog(tier)
    adapter.manifest_path = tmp_path / f"{adapter.slug}.json"
    monkeypatch.setenv(adapter.key_env, "test")
    monkeypatch.setattr(_system1models, "fetch_json", lambda *_a: catalog("s1-fast"))
    monkeypatch.setattr(adapter, "probe", lambda *_a: True)
    adapter.write_provider_manifest(adapter.fetch())
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", tmp_path)
    models, endpoints = catalog_ingest._supplemental_provider_models_and_endpoints()
    model = models[f"{adapter.slug}/s1-fast"]
    assert model.supports_decide and not model.supports_chat and not model.byok_available
    assert len(endpoints) == 1
    endpoint = next(iter(endpoints.values()))
    assert endpoint.provider == adapter.slug and endpoint.upstream_id == "s1-fast"
    assert endpoint.completion_price_microdollars_per_million_tokens == 0
    assert endpoint.prompt_price_microdollars_per_million_tokens == _customer_price(cost)
    assert adapter.slug in GATEWAY_PREPAID_PROVIDER_SLUGS
    assert default_provider_secret_ref(adapter.slug) == f"env://{adapter.key_env}"


@pytest.mark.asyncio
@pytest.mark.usefixtures("served_system1")
@pytest.mark.parametrize("slug", ["system1models", "system1models-eu"])
async def test_gateway_authorizes_only_the_requested_tier_and_rejects_chat(slug):
    from tests.test_decide_models import _authorize

    body = {
        "model": f"{slug}/s1-fixture",
        "route_type": "decide",
        "estimated_input_tokens": 100,
        "max_output_tokens": 1,
    }
    response = await _authorize(body)
    assert response.status_code == 200, response.text
    payload = response.json().get("data", response.json())
    assert payload["provider"] == slug
    assert [(c["provider"], c["upstream_model"]) for c in payload["route_candidates"]] == [
        (slug, "s1-fixture")
    ]
    rejected = await _authorize({**body, "route_type": "chat"})
    assert rejected.status_code == 400


@pytest.mark.usefixtures("served_system1")
@pytest.mark.parametrize("slug", ["system1models", "system1models-eu"])
def test_public_pages_identify_decision_api_and_provider(client, slug):
    model_id = f"{slug}/s1-fixture"
    page = client.get(f"/models/{model_id}/api")
    assert page.status_code == 200
    assert "/decide" in page.text and "chat.completions.create" not in page.text
    provider = client.get(f"/providers/{slug}")
    assert provider.status_code == 200
    assert "System1" in provider.text
    assert "Finland" in provider.text


def test_hourly_refresh_and_native_cloud_secret_contract():
    from scripts.check_price_coverage import _DISCOVERABLE_MANIFEST_PROVIDERS
    from scripts.pricing.providers import system1models, system1models_eu

    root = Path(__file__).resolve().parents[1]
    for module in (system1models, system1models_eu):
        assert any(row[0] == module.SLUG for row in _DISCOVERABLE_MANIFEST_PROVIDERS)
        coord = (
            f"{module.CATALOG.key_env}:trustedrouter-system1models-{module.CATALOG.tier}-api-key"
        )
        assert coord in (root / ".github/workflows/refresh-prices.yml").read_text()


@pytest.mark.parametrize("tier", ["eu", "global"])
@pytest.mark.parametrize(
    "change",
    [None, "chat", "output", "missing_output", "bool_price", "cache", "tiers", "namespace"],
)
def test_manifest_expiry_and_runtime_share_strict_decision_price_validation(tier, change):
    from datetime import UTC, datetime, timedelta

    from trusted_router.provider_manifest_policy import (
        EXPIRED_PROVIDER_MANIFEST,
        decision_manifest_price_is_valid,
        provider_manifest_valid_until,
    )

    adapter = System1Catalog(tier)
    _, rows = adapter.discover(catalog("s1-fast"))
    row = rows[f"{adapter.slug}/s1-fast"]
    row.update(input_token_price_per_m=25_000, output_token_price_per_m=0)
    if change == "chat":
        row["endpoints"] = ["chat/completions"]
    elif change == "output":
        row["output_token_price_per_m"] = 1
    elif change == "missing_output":
        del row["output_token_price_per_m"]
    elif change == "bool_price":
        row["input_token_price_per_m"] = True
    elif change == "cache":
        row["cached_input_token_price_per_m"] = 0
    elif change == "tiers":
        row["price_tiers"] = []
    elif change == "namespace":
        row["id"] = "other/s1-fast"
    assert decision_manifest_price_is_valid(row) is (change is None)
    generated = datetime(2026, 10, 1, tzinfo=UTC)
    deadline = provider_manifest_valid_until(
        adapter.slug,
        {"models": [row], "generated_at": generated.isoformat()},
    )
    assert deadline == (
        generated + timedelta(days=14) if change is None else EXPIRED_PROVIDER_MANIFEST
    )
