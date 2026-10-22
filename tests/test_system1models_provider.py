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
    for slug in ("decisionmodelsio", "decisionmodelsio-eu"):
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
    "tier,slug,cost", [("global", "decisionmodelsio", 25_000), ("eu", "decisionmodelsio-eu", 34_000)]
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
        assert rows[model]["display_name"] == f"Decision Models {rows[model]['upstream_id']} ({tier.upper()})"
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
    with pytest.raises(RuntimeError):
        System1Catalog("eu").discover(payload)


def test_october_catalog_additions_do_not_freeze_supported_routes():
    payload = catalog("s1-fast", "s1-fast-2", "s1-pro-2", "quyet-1-large", "s1-vision-2", "s1-llm-auto-router")
    by_id = {row["id"]: row for row in payload["data"]}
    by_id["s1-fast-2"]["max_input_tokens"] = 8192
    by_id["s1-vision-2"]["modalities"] = ["text", "image"]
    by_id["s1-vision-2"]["prices"]["image_requests"] = {
        "applies_to": "requests_with_image", "unit": "per_million_input_tokens",
        "output_tokens": "free", "USD": {"eu": "0.228", "global": "0.217"},
    }
    by_id["s1-llm-auto-router"]["fixed_questions"] = {"category": "choice"}
    prices, rows = System1Catalog("eu").discover(payload)
    assert len(prices) == len(rows) == 6
    assert rows["decisionmodelsio-eu/s1-fast-2"]["context_length"] == 8192
    for native in ("s1-fast", "s1-fast-2", "s1-pro-2"):
        assert rows[f"decisionmodelsio-eu/{native}"]["decision_contract_hold"] is None
    for native, reason in (
        ("quyet-1-large", "decision-model-namespace-unsupported"),
        ("s1-vision-2", "decision-image-pricing-unsupported"),
        ("s1-llm-auto-router", "decision-fixed-questions-unsupported"),
    ):
        row = rows[f"decisionmodelsio-eu/{native}"]
        assert row["routable"] is False and row["routable_reason"] == reason
        assert "example_input" not in row["documentation"]
    assert rows["decisionmodelsio-eu/s1-vision-2"]["decision_image_input_price_per_m"] == 228_000


@pytest.mark.parametrize("change", ["unit", "output_tokens", "applies_to", "USD"])
def test_image_tariff_must_be_exact_and_understood(change):
    payload = catalog("s1-vision")
    payload["data"][0]["modalities"] = ["text", "image"]
    image = copy.deepcopy(payload["data"][0]["prices"])
    image["applies_to"] = "requests_with_image"
    image[change] = "unexpected"
    payload["data"][0]["prices"]["image_requests"] = image
    with pytest.raises(RuntimeError, match="exact USD"):
        System1Catalog("eu").discover(payload)


@pytest.mark.parametrize("tier,public,legacy", [("eu", "0.084", 40_000), ("global", "0.08", 38_000)])
@pytest.mark.parametrize("native", ["s1-pro", "s1-pro-2"])
def test_existing_customer_pro_tariff_has_exact_cutover(tier, public, legacy, native):
    from datetime import date

    from trusted_router.pricing_approvals import approved_dated_price_transition

    payload = catalog(native)
    payload["data"][0]["prices"]["USD"][tier] = public
    adapter = System1Catalog(tier)
    model = f"{adapter.slug}/{native}"
    before, rows = adapter.discover(payload, today=date(2026, 11, 11))
    after, later_rows = adapter.discover(payload, today=date(2026, 11, 12))
    assert before[model].prompt_micro_per_m == legacy
    public_micro = 84_000 if tier == "eu" else 80_000
    assert after[model].prompt_micro_per_m == public_micro
    assert rows[model]["pricing_valid_until"] == "2026-11-12T00:00:00Z"
    assert later_rows[model]["pricing_valid_until"] is None
    route = f"{model} [{adapter.slug}:{adapter.slug}:{native}]"
    assert not approved_dated_price_transition(route, (legacy, 0), (public_micro, 0), today=date(2026, 11, 11))
    assert approved_dated_price_transition(route, (legacy, 0), (public_micro, 0), today=date(2026, 11, 12))
    assert not approved_dated_price_transition(route, (legacy, 0), (public_micro * 2, 0), today=date(2026, 11, 12))
    payload["data"][0]["prices"]["USD"][tier] = "0.2"
    changed, _ = adapter.discover(payload, today=date(2026, 11, 11))
    assert changed[model].prompt_micro_per_m == 200_000


def test_current_models_rechecked_and_recover_without_clearing_contract_holds(monkeypatch, tmp_path):
    from scripts.pricing.providers import _system1models
    from trusted_router.provider_manifest_policy import (
        EXPIRED_PROVIDER_MANIFEST,
        provider_manifest_canary_quarantine_valid_until,
    )

    adapter = System1Catalog("global")
    adapter.manifest_path = tmp_path / "decisionmodelsio.json"
    payload = catalog("s1-fast", "s1-pro", "s1-vision")
    monkeypatch.setenv(adapter.key_env, "test")
    monkeypatch.setattr(_system1models, "fetch_json", lambda *_a: payload)
    monkeypatch.setattr(adapter, "probe", lambda *_a: True)
    adapter.write_provider_manifest(adapter.fetch())
    payload["data"].extend(catalog("quyet-1-large", "s1-fast-2")["data"])
    vision = payload["data"][2]
    vision["modalities"] = ["text", "image"]
    vision["prices"]["image_requests"] = {
        "applies_to": "requests_with_image", "unit": "per_million_input_tokens",
        "output_tokens": "free", "USD": {"global": "0.217", "eu": "0.228"},
    }
    checked = []

    def failed(_key, row):
        checked.append(row["upstream_id"])
        return False

    monkeypatch.setattr(adapter, "probe", failed)
    adapter.write_provider_manifest(adapter.fetch())
    assert set(checked) == {"s1-fast", "s1-fast-2", "s1-pro"}
    raw = json.loads(adapter.manifest_path.read_text())
    assert len(raw["models"]) == 5
    assert all(row["routable"] is False for row in raw["models"])
    assert provider_manifest_canary_quarantine_valid_until(adapter.slug, raw) != EXPIRED_PROVIDER_MANIFEST
    monkeypatch.setattr(adapter, "probe", lambda *_a: True)
    adapter.write_provider_manifest(adapter.fetch())
    rows = {row["upstream_id"]: row for row in json.loads(adapter.manifest_path.read_text())["models"]}
    assert all(rows[native]["routable"] is True for native in checked)
    assert rows["s1-vision"]["routable_reason"] == "decision-image-pricing-unsupported"
    assert rows["quyet-1-large"]["routable_reason"] == "decision-model-namespace-unsupported"


def test_quarantine_keeps_only_previously_published_settlement_tariffs(monkeypatch, tmp_path):
    from scripts.pricing.providers import _system1models
    from trusted_router import catalog_ingest, catalog_registry
    from trusted_router.pricing import _customer_price
    from trusted_router.routes.internal.gateway import _endpoint_for_id_compat

    adapter = System1Catalog("eu")
    adapter.manifest_path = tmp_path / "decisionmodelsio-eu.json"
    payload = catalog("s1-old")
    monkeypatch.setenv(adapter.key_env, "test")
    monkeypatch.setattr(_system1models, "fetch_json", lambda *_a: payload)
    monkeypatch.setattr(adapter, "probe", lambda *_a: True)
    adapter.write_provider_manifest(adapter.fetch())
    payload["data"][0]["prices"]["USD"]["eu"] = "0.1"
    payload["data"].extend(catalog("s1-new")["data"])
    monkeypatch.setattr(adapter, "probe", lambda *_a: False)
    adapter.write_provider_manifest(adapter.fetch())
    # Repeated failures must not replace the pre-hold tariff with new prices.
    adapter.write_provider_manifest(adapter.fetch())
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", tmp_path)
    models, active = catalog_ingest._supplemental_provider_models_and_endpoints()
    assert not models and not active
    held_models, held = catalog_ingest._supplemental_provider_models_and_endpoints(held_decisions_only=True)
    identity = "decisionmodelsio-eu/s1-old@decisionmodelsio-eu/prepaid"
    assert set(held) == {identity}
    assert set(held_models) == {"decisionmodelsio-eu/s1-old"}
    endpoint = held[identity]
    assert endpoint.prompt_price_microdollars_per_million_tokens == _customer_price(34_000)
    assert endpoint.completion_price_microdollars_per_million_tokens == 0
    assert endpoint.usage_type == "Credits"
    monkeypatch.setattr(catalog_registry, "HELD_PREPAID_SETTLEMENT_ENDPOINTS", held)
    assert _endpoint_for_id_compat(identity) == endpoint
    assert _endpoint_for_id_compat("decisionmodelsio-eu/s1-new@decisionmodelsio-eu/prepaid") is None


@pytest.mark.parametrize("price", [None, True, -1, "34000"])
def test_malformed_settlement_history_cannot_create_endpoint(monkeypatch, tmp_path, price):
    from trusted_router import catalog_ingest

    adapter = System1Catalog("eu")
    _, rows = adapter.discover(catalog("s1-old"))
    row = rows["decisionmodelsio-eu/s1-old"]
    row.update(routable=False, input_token_price_per_m=34_000, output_token_price_per_m=0,
               settlement_prices={"input_token_price_per_m": price, "output_token_price_per_m": 0})
    (tmp_path / "decisionmodelsio-eu.json").write_text(json.dumps({"provider": adapter.slug, "models": [row]}))
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", tmp_path)
    assert catalog_ingest._supplemental_provider_models_and_endpoints(held_decisions_only=True) == ({}, {})


def test_unsupported_vision_remains_visible_but_not_routable():
    payload = catalog("s1-future-vision")
    payload["data"][0]["modalities"] = ["text", "image"]
    _, rows = System1Catalog("eu").discover(payload)
    assert rows["decisionmodelsio-eu/s1-future-vision"]["routable_reason"] == "decision-image-contract-unsupported"


@pytest.mark.parametrize("hold", [[], {}, True, "unknown", "decision-image-pricing-unsupported"])
def test_contract_holds_cannot_be_made_routable(hold):
    from trusted_router.provider_manifest_policy import decision_manifest_price_is_valid

    _, rows = System1Catalog("eu").discover(catalog("s1-fast"))
    row = rows["decisionmodelsio-eu/s1-fast"]
    row.update(input_token_price_per_m=34_000, output_token_price_per_m=0, routable=True, decision_contract_hold=hold)
    assert not decision_manifest_price_is_valid(row)


def test_legacy_rate_manifest_expires_at_cutover(monkeypatch, tmp_path):
    from datetime import UTC, date, datetime

    from scripts.pricing.providers import _system1models
    from trusted_router.provider_manifest_policy import provider_manifest_valid_until

    adapter = System1Catalog("eu")
    adapter.manifest_path = tmp_path / "eu.json"
    payload = catalog("s1-pro")
    payload["data"][0]["prices"]["USD"]["eu"] = "0.084"
    original = adapter.discover
    monkeypatch.setattr(adapter, "discover", lambda value: original(value, today=date(2026, 11, 11)))
    monkeypatch.setattr(_system1models, "fetch_json", lambda *_a: payload)
    monkeypatch.setattr(adapter, "probe", lambda *_a: True)
    monkeypatch.setenv(adapter.key_env, "test")
    adapter.write_provider_manifest(adapter.fetch())
    raw = json.loads(adapter.manifest_path.read_text())
    raw["generated_at"] = "2026-11-11T00:00:00Z"
    assert provider_manifest_valid_until(adapter.slug, raw) == datetime(2026, 11, 12, tzinfo=UTC)
    monkeypatch.setattr(adapter, "discover", lambda value: original(value, today=date(2026, 11, 12)))
    adapter.write_provider_manifest(adapter.fetch())
    assert "pricing_valid_until" not in json.loads(adapter.manifest_path.read_text())


def test_tiers_and_availability_never_cross():
    payload = catalog("s1-fast", "s1-pro", "s1-unavailable")
    payload["data"][1]["tier_availability"]["eu"] = False
    payload["data"][2]["status"] = "unavailable"
    eu, _ = System1Catalog("eu").discover(payload)
    global_prices, _ = System1Catalog("global").discover(payload)
    assert set(eu) == {"decisionmodelsio-eu/s1-fast"}
    assert set(global_prices) == {"decisionmodelsio/s1-fast", "decisionmodelsio/s1-pro"}


def test_canary_gates_new_routes_and_failed_fetch_cannot_publish(monkeypatch, tmp_path):
    from scripts.pricing.providers import _system1models

    adapter = System1Catalog("eu")
    adapter.manifest_path = tmp_path / "decisionmodelsio-eu.json"
    monkeypatch.setenv(adapter.key_env, "test-eu")
    monkeypatch.setattr(_system1models, "fetch_json", lambda *_a: catalog("s1-fast", "s1-pro"))
    monkeypatch.setattr(
        adapter, "probe", lambda key, row: key == "test-eu" and row["upstream_id"] == "s1-fast"
    )
    result = adapter.fetch()
    assert result.include_in_price_index is False
    adapter.write_provider_manifest(result)
    rows = {r["id"]: r for r in json.loads(adapter.manifest_path.read_text())["models"]}
    assert rows["decisionmodelsio-eu/s1-fast"]["routable"] is True
    assert rows["decisionmodelsio-eu/s1-pro"]["routable"] is False
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
        assert url == "https://api.decisionmodels.io/v1/systemone"
        assert json["model"] == "s1-fast"
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
@pytest.mark.parametrize("slug", ["decisionmodelsio", "decisionmodelsio-eu"])
@pytest.mark.parametrize("legacy", [False, True])
async def test_gateway_authorizes_only_the_requested_tier_and_rejects_chat(slug, legacy):
    from tests.test_decide_models import _authorize

    body = {
        "model": f"{slug.replace('decisionmodelsio', 'system1models') if legacy else slug}/s1-fixture",
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
@pytest.mark.parametrize("slug", ["decisionmodelsio", "decisionmodelsio-eu"])
def test_public_pages_identify_decision_api_and_provider(client, slug):
    model_id = f"{slug}/s1-fixture"
    page = client.get(f"/models/{model_id}/api")
    assert page.status_code == 200
    assert "/decide" in page.text and "chat.completions.create" not in page.text
    provider = client.get(f"/providers/{slug}")
    assert provider.status_code == 200
    assert "Decision Models" in provider.text
    assert "Formerly System1 Models" in provider.text
    assert "https://decisionmodels.io/" in provider.text
    assert "Finland" in provider.text


@pytest.mark.parametrize("tier", ["global", "eu"])
def test_rebrand_preserves_keys_prices_and_residency(tier):
    from scripts.pricing.providers._system1models import URL
    from trusted_router.catalog_data import PROVIDERS
    from trusted_router.provider_branding import provider_homepage_url
    from trusted_router.provider_locations import PROVIDER_INFERENCE_LOCATIONS

    adapter = System1Catalog(tier)
    slug = "decisionmodelsio-eu" if tier == "eu" else "decisionmodelsio"
    assert adapter.slug == slug
    assert adapter.key_env == f"SYSTEM1MODELS_{tier.upper()}_API_KEY"
    assert URL == "https://api.decisionmodels.io/v1/models"
    assert PROVIDERS[slug].name == f"Decision Models ({'EU' if tier == 'eu' else 'Global'})"
    assert PROVIDERS[slug].provider_policy_url == "https://decisionmodels.io/legal/privacy"
    assert provider_homepage_url(slug) == "https://decisionmodels.io/" + ("eu" if tier == "eu" else "")
    assert all(url.startswith("https://decisionmodels.io/") for _, url in PROVIDER_INFERENCE_LOCATIONS[slug].sources)
    manifest = json.loads(adapter.manifest_path.read_text())
    assert manifest["provider"] == slug
    for row in manifest["models"]:
        assert row["id"] == f"{slug}/{row['upstream_id']}"
        assert row["display_name"].startswith("Decision Models ")
        assert row["output_token_price_per_m"] == 0


def test_hourly_refresh_and_native_cloud_secret_contract():
    from scripts.check_price_coverage import _DISCOVERABLE_MANIFEST_PROVIDERS
    from scripts.pricing.providers import system1models, system1models_eu
    from scripts.pricing.refresh import _PRICING_RESULT_PROVIDER_ALIASES

    root = Path(__file__).resolve().parents[1]
    for module in (system1models, system1models_eu):
        assert _PRICING_RESULT_PROVIDER_ALIASES[module.__name__.rsplit(".", 1)[-1]] == (module.SLUG,)
        assert any(row[0] == module.SLUG for row in _DISCOVERABLE_MANIFEST_PROVIDERS)
        coord = (
            f"{module.CATALOG.key_env}:trustedrouter-system1models-{module.CATALOG.tier}-api-key"
        )
        assert coord in (root / ".github/workflows/refresh-prices.yml").read_text()


@pytest.mark.usefixtures("served_system1")
@pytest.mark.parametrize("suffix", ["", "-eu"])
def test_legacy_public_links_and_api_ids_resolve_within_same_tier(client, suffix):
    from trusted_router.model_aliases import canonical_router_model_id, router_model_aliases
    from trusted_router.routing import _provider_slug

    old, new = f"system1models{suffix}", f"decisionmodelsio{suffix}"
    model_id = f"{new}/s1-fixture"
    assert canonical_router_model_id(f"{old}/s1-fixture") == model_id
    assert router_model_aliases(model_id) == [f"{old}/s1-fixture"]
    assert _provider_slug(old) == new
    for path in (f"/providers/{old}", f"/models/{old}/s1-fixture"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code in {301, 308}
        assert response.headers["location"] == path.replace(old, new)


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
