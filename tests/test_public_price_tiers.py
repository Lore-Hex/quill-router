"""Public tariffs must describe the same controlled routes settlement charges."""

import json
from dataclasses import replace
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from tests.fixture_routes import bypass_catalog_caches, drop_routes, serve_on_fixture_route
from tests.rerank_manifest_fixtures import install_public_rerank_models
from trusted_router import catalog
from trusted_router.authorize_candidates import endpoint_cost_microdollars
from trusted_router.catalog_data import Model
from trusted_router.pricing import PriceTier, cache_token_prices_microdollars
from trusted_router.routes.catalog import _endpoint_pricing_payload
from trusted_router.speech import SPEECH_MODELS
from trusted_router.stage_d import (
    endpoint_cost_microdollars_from_candidate,
    endpoint_pricing_candidate,
)
from trusted_router.transcription import TRANSCRIPTION_MODELS

MODEL_ID = "openai/public-tier-fixture"
TIERS = (
    PriceTier(100_000, 263_750, 1_318_750, 26_375),
    PriceTier(None, 527_500, 2_637_500, 52_750),
)
TIERED_PRICING = {
    "prompt": "0.00000026375",
    "completion": "0.00000131875",
    "input_cache_read": "0.000000026375",
    "tiers": [
        {
            "max_prompt_tokens": 100_000,
            "prompt": "0.00000026375",
            "completion": "0.00000131875",
            "input_cache_read": "0.000000026375",
        },
        {
            "max_prompt_tokens": None,
            "prompt": "0.0000005275",
            "completion": "0.0000026375",
            "input_cache_read": "0.00000005275",
        },
    ],
}
# Frozen pre-change payload for the same controlled flat route.
FLAT_PRICING = {"prompt": "0.000001", "completion": "0.000003"}
LONG_LINE = "Prompts over 100,000 tokens: $0.5275 in / $2.6375 out per 1M"


@pytest.fixture
def routes(monkeypatch):
    bypass_catalog_caches(monkeypatch)
    model = Model(
        id=MODEL_ID, name="Public Tier Fixture", provider="openai", context_length=500_000,
        prepaid_available=True,
        prompt_price_microdollars_per_million_tokens=263_750,
        completion_price_microdollars_per_million_tokens=1_318_750,
        price_tiers=TIERS, published_price_tiers=TIERS,
    )
    monkeypatch.setitem(catalog.MODELS, MODEL_ID, model)
    tiered = serve_on_fixture_route(
        monkeypatch, MODEL_ID, "openai", author="openai", model=model,
        prompt_price_microdollars_per_million_tokens=263_750,
        completion_price_microdollars_per_million_tokens=1_318_750,
        price_tiers=TIERS, published_price_tiers=TIERS,
    )
    flat_tiers = (PriceTier(None, 1_000_000, 3_000_000),)
    flat = serve_on_fixture_route(
        monkeypatch, MODEL_ID, "deepinfra", author="openai",
        price_tiers=flat_tiers, published_price_tiers=flat_tiers,
    )
    return tiered, flat


def test_exact_public_pricing(client, routes):
    tiered, flat = routes
    response = client.get(f"/v1/models/{MODEL_ID}/endpoints")
    assert response.status_code == 200
    endpoints = {row["endpoint_id"]: row["pricing"] for row in response.json()["data"]}
    assert endpoints == {tiered.id: TIERED_PRICING, flat.id: FLAT_PRICING}
    for path in ("/v1/models", "/v1/models?include=endpoints"):
        response = client.get(path)
        assert response.status_code == 200
        row = next(row for row in response.json()["data"] if row["id"] == MODEL_ID)
        assert row["pricing"] == TIERED_PRICING
    assert catalog.model_to_openrouter_shape(catalog.MODELS[MODEL_ID])["pricing"] == TIERED_PRICING


@pytest.mark.parametrize("total", [100_000, 100_001])
@pytest.mark.parametrize("cached,created", [(0, 0), (90_000, 0), (0, 90_000), (40_000, 50_000)])
def test_published_rates_equal_settlement(routes, total, cached, created):
    tiered, _ = routes
    published = _endpoint_pricing_payload(tiered)["tiers"][total > 100_000]
    uncached, output = total - cached - created, 7_123

    def charge(tokens, rate):
        return int((Decimal(rate) * tokens * 1_000_000).to_integral_value(rounding=ROUND_HALF_UP))

    # Cache creation has a provider multiplier rather than a published tier
    # field. Use the existing policy to isolate selection of the prompt tier.
    _, creation_rate = cache_token_prices_microdollars(
        tiered.provider, int(Decimal(published["prompt"]) * 1_000_000_000_000)
    )
    expected = (
        charge(uncached, published["prompt"])
        + charge(output, published["completion"])
        + charge(cached, published["input_cache_read"])
        + charge(created, Decimal(creation_rate) / 1_000_000_000_000)
    )
    assert endpoint_cost_microdollars(
        tiered, uncached, output, cache_read_tokens=cached, cache_creation_tokens=created,
    ) == expected
    assert endpoint_cost_microdollars_from_candidate(
        endpoint_pricing_candidate(tiered), uncached, output,
        cache_read_tokens=cached, cache_creation_tokens=created,
    ) == expected


def test_three_tiers_without_cache_prices(client, routes, monkeypatch):
    tiered, _ = routes
    tiers = (
        PriceTier(100_000, 263_750, 1_318_750),
        PriceTier(200_000, 527_500, 2_637_500),
        PriceTier(None, 791_250, 3_956_250),
    )
    monkeypatch.setitem(catalog.MODEL_ENDPOINTS, tiered.id, replace(
        tiered, price_tiers=tiers, published_price_tiers=tiers,
    ))
    expected = [
        {"max_prompt_tokens": 100_000, "prompt": "0.00000026375", "completion": "0.00000131875"},
        {"max_prompt_tokens": 200_000, "prompt": "0.0000005275", "completion": "0.0000026375"},
        {"max_prompt_tokens": None, "prompt": "0.00000079125", "completion": "0.00000395625"},
    ]
    rows = client.get(f"/v1/models/{MODEL_ID}/endpoints").json()["data"]
    assert next(row for row in rows if row["endpoint_id"] == tiered.id)["pricing"]["tiers"] == expected
    assert catalog.model_to_openrouter_shape(catalog.MODELS[MODEL_ID])["pricing"]["tiers"] == expected
    page = BeautifulSoup(client.get(f"/models/{MODEL_ID}/pricing").text, "html.parser")
    notes = page.select_one(".model-tier-prices").get_text(" ", strip=True)
    assert "Prompts over 100,000 tokens (up to 200,000)" in notes
    assert "Prompts over 200,000 tokens: $0.79125 in / $3.95625 out per 1M" in notes


def test_flat_headline_payload_bytes_and_scope(client, routes, monkeypatch):
    tiered, flat = routes
    monkeypatch.delitem(catalog.MODEL_ENDPOINTS, tiered.id)
    assert json.dumps(_endpoint_pricing_payload(flat), separators=(",", ":")).encode() == (
        b'{"prompt":"0.000001","completion":"0.000003"}'
    )
    listing = client.get("/v1/models").json()["data"]
    assert next(row for row in listing if row["id"] == MODEL_ID)["pricing"] == FLAT_PRICING
    for suffix in ("", "/providers", "/pricing"):
        html = client.get(f"/models/{MODEL_ID}{suffix}").text
        assert "for prompts up to" not in html
        assert "Prompts over" not in html


def test_model_pages_publish_route_tiers(client, routes):
    for suffix in ("", "/providers", "/pricing"):
        response = client.get(f"/models/{MODEL_ID}{suffix}")
        assert response.status_code == 200
        soup = BeautifulSoup(response.text, "html.parser")
        if suffix:
            table = soup.select_one("main table")
            assert len(table.select("thead th")) <= 5
            note = table.select_one(".model-tier-prices")
            assert note.get_text(" ", strip=True) == LONG_LINE
            assert "OpenAI" in note.find_previous_sibling("tr").get_text()
            assert len(table.select(".model-tier-prices")) == 1
        else:
            assert soup.select_one("#provider-openai .md-tier-prices").get_text(strip=True) == LONG_LINE
            assert soup.select_one("#provider-deepinfra .md-tier-prices") is None
            assert "for prompts up to 100,000 tokens" in soup.select_one("#faq").get_text()
        if suffix == "/pricing":
            description = soup.find("meta", attrs={"name": "description"})["content"]
            assert "for prompts up to 100,000 tokens" in description


def test_divergent_publication_tariff_is_not_disclosed(client, routes, monkeypatch):
    tiered, _ = routes
    monkeypatch.setitem(catalog.MODEL_ENDPOINTS, tiered.id, replace(
        tiered, published_price_tiers=(PriceTier(None, 1, 2),),
    ))
    assert "tiers" not in _endpoint_pricing_payload(catalog.MODEL_ENDPOINTS[tiered.id])
    assert "tiers" not in catalog.model_to_openrouter_shape(catalog.MODELS[MODEL_ID])["pricing"]
    assert "Prompts over" not in client.get(f"/models/{MODEL_ID}/pricing").text


def test_model_tiers_follow_prompt_headline_route(routes, monkeypatch):
    tiered, flat = routes
    # The pre-existing completion headline is an independent minimum. Never
    # attach another route's tiers just because it has the cheapest output.
    monkeypatch.setitem(catalog.MODEL_ENDPOINTS, flat.id, replace(
        flat, completion_price_microdollars_per_million_tokens=100_000,
    ))
    pricing = catalog.model_to_openrouter_shape(catalog.MODELS[MODEL_ID])["pricing"]
    assert pricing["completion"] == "0.0000001"
    assert pricing["tiers"] == TIERED_PRICING["tiers"]


@pytest.mark.parametrize("suffix", ["aaa-byok", "zzz-byok"])
def test_a_flat_byok_route_at_the_same_price_does_not_hide_credits_tiers(routes, monkeypatch, suffix):
    tiered, _flat = routes
    flat_tiers = (PriceTier(None, 263_750, 1_318_750),)
    # Same headline prompt price as the tiered Credits route, on either side of it in route order.
    twin = replace(tiered, id=f"{MODEL_ID}@{suffix}", usage_type="BYOK",
                   price_tiers=flat_tiers, published_price_tiers=flat_tiers)
    monkeypatch.setitem(catalog.MODEL_ENDPOINTS, twin.id, twin)
    pricing = catalog.model_to_openrouter_shape(catalog.MODELS[MODEL_ID])["pricing"]
    assert pricing["prompt"] == TIERED_PRICING["prompt"]
    assert pricing["tiers"] == TIERED_PRICING["tiers"]
    assert "tiers" not in _endpoint_pricing_payload(twin)


def test_non_token_and_embedding_payloads_unchanged(monkeypatch, tmp_path):
    bypass_catalog_caches(monkeypatch)
    install_public_rerank_models(monkeypatch, tmp_path)
    for model_id, expected in (
        ("voyage/rerank-3", {"prompt": "0.00000005275", "completion": "0"}),
    ):
        model = catalog.MODELS[model_id]
        assert catalog.model_to_openrouter_shape(model)["pricing"] == expected
        for endpoint in catalog.endpoints_for_model(model_id):
            assert _endpoint_pricing_payload(endpoint) == expected
    # Embedding prices come from rows a scheduled refresh rewrites, so only
    # the shape is asserted: no tier list on an untiered embedding model.
    embedding = next(model for model in catalog.MODELS.values() if model.supports_embeddings)
    assert set(catalog.model_to_openrouter_shape(embedding)["pricing"]) <= {"prompt", "completion", "input_cache_read"}
    for endpoint in catalog.endpoints_for_model(embedding.id):
        assert "tiers" not in _endpoint_pricing_payload(endpoint)

    monkeypatch.setitem(SPEECH_MODELS, "x-ai/grok-voice-tts-1.0", replace(
        SPEECH_MODELS["x-ai/grok-voice-tts-1.0"],
        cost_microdollars_per_million_characters=15_000_000,
    ))
    monkeypatch.setitem(TRANSCRIPTION_MODELS, "openai/whisper-1", replace(
        TRANSCRIPTION_MODELS["openai/whisper-1"], minute_cost_microdollars=6_000,
    ))
    # Static non-token specifications, with controlled model/endpoint rows.
    for model_id, provider, expected in (
        ("deepseek/fetch", "deepseek", {"request": "0.000792"}),
        ("openai/whisper-1", "openai", {"input_audio_minute": "0.00633"}),
        ("x-ai/grok-voice-tts-1.0", "grok", {"input_character": "0.000015825"}),
    ):
        drop_routes(monkeypatch, model_id)
        monkeypatch.setitem(catalog.MODELS, model_id, Model(
            id=model_id, name=model_id, provider=provider, context_length=0, supports_chat=False,
        ))
        endpoint = serve_on_fixture_route(monkeypatch, model_id, provider, author=provider)
        assert _endpoint_pricing_payload(endpoint) == expected
        assert catalog.model_to_openrouter_shape(catalog.MODELS[model_id])["pricing"] == expected


def test_public_openapi_documents_tiers():
    asset = Path("src/trusted_router/static/openapi-public.json")
    spec = json.loads(asset.read_text())
    schema = spec["paths"]["/v1/models/{author}/{slug}/endpoints"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    tiers = schema["properties"]["data"]["items"]["properties"]["pricing"]["properties"]["tiers"]
    assert tiers["minItems"] == 2
    assert "total prompt tokens including cached tokens" in tiers["description"]
    assert tiers["items"]["required"] == ["max_prompt_tokens", "prompt", "completion"]
    assert tiers["items"]["properties"]["max_prompt_tokens"]["anyOf"] == [
        {"type": "integer"}, {"type": "null"},
    ]


@pytest.mark.parametrize("model_id,provider,flags,expected", [
    ("recraft/recraftv4_1", "recraft", {"output_modalities": ("image",)},
     {"prompt": "0", "completion": "0", "image": "0.036925"}),
    ("bytedance/seedance-2.0-fast", "byteplus",
     {"supports_video": True, "output_modalities": ("video",)},
     {"prompt": "0", "completion": "0"}),
])
def test_fixed_media_payloads_unchanged(monkeypatch, model_id, provider, flags, expected):
    bypass_catalog_caches(monkeypatch)
    drop_routes(monkeypatch, model_id)
    if model_id == "recraft/recraftv4_1":
        from trusted_router.image_generation import FIXED_IMAGE_PRICES_MICRODOLLARS

        monkeypatch.setitem(FIXED_IMAGE_PRICES_MICRODOLLARS, model_id, {"image": 35_000})
    model = Model(
        id=model_id, name=model_id, provider=provider, context_length=0,
        supports_chat=False, **flags,
    )
    monkeypatch.setitem(catalog.MODELS, model_id, model)
    endpoint = serve_on_fixture_route(
        monkeypatch, model_id, provider, author=provider,
        prompt_price_microdollars_per_million_tokens=0,
        completion_price_microdollars_per_million_tokens=0,
    )
    assert _endpoint_pricing_payload(endpoint) == {"prompt": "0", "completion": "0"}
    assert catalog.model_to_openrouter_shape(model)["pricing"] == expected


def test_tiered_non_headline_route_has_note_without_headline_scope(client, routes, monkeypatch):
    _, flat = routes
    monkeypatch.setitem(catalog.MODEL_ENDPOINTS, flat.id, replace(
        flat, prompt_price_microdollars_per_million_tokens=100_000,
        completion_price_microdollars_per_million_tokens=100_000,
    ))
    for suffix in ("", "/pricing"):
        response = client.get(f"/models/{MODEL_ID}{suffix}")
        assert LONG_LINE in response.text
        assert "for prompts up to" not in response.text
    assert "tiers" not in catalog.model_to_openrouter_shape(catalog.MODELS[MODEL_ID])["pricing"]


def test_private_context_basis_is_not_published_as_total_prompt_tiers(monkeypatch):
    from tests.test_gateway_tiered_pricing import _sakana_fugu_pricing_fixture

    bypass_catalog_caches(monkeypatch)
    endpoint = _sakana_fugu_pricing_fixture()
    model = Model(
        id=endpoint.model_id, name="Fugu fixture", provider=endpoint.provider,
        context_length=1_000_000, price_tiers=endpoint.price_tiers,
        published_price_tiers=endpoint.price_tiers,
    )
    drop_routes(monkeypatch, model.id)
    monkeypatch.setitem(catalog.MODELS, model.id, model)
    monkeypatch.setitem(catalog.MODEL_ENDPOINTS, endpoint.id, endpoint)
    assert endpoint_cost_microdollars(endpoint, 272_001, 100, price_tier_input_tokens=6) != (
        endpoint_cost_microdollars(endpoint, 272_001, 100)
    )
    assert "tiers" not in _endpoint_pricing_payload(endpoint)
    assert "tiers" not in catalog.model_to_openrouter_shape(model)["pricing"]
    from trusted_router.dashboard import _long_prompt_price_lines

    assert _long_prompt_price_lines(endpoint, model) == []
