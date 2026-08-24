from __future__ import annotations

import json
from pathlib import Path

from scripts.ingest_openrouter_catalog import filter_endpoints
from scripts.pricing import refresh
from scripts.pricing.base import ModelPrice, ProviderPricingResult
from trusted_router.routes.internal.gateway import _endpoint_for_id_compat


def _endpoint(provider_name: str, tag: str, prompt: str) -> dict[str, object]:
    return {
        "name": f"{provider_name} | google/gemini-2.5-flash",
        "model_id": "google/gemini-2.5-flash",
        "provider_name": provider_name,
        "tag": tag,
        "pricing": {"prompt": prompt, "completion": "0.0000025"},
    }


def test_google_endpoint_ingest_keeps_one_standard_route_per_product() -> None:
    endpoints = filter_endpoints(
        [
            _endpoint("Google", "google-vertex/eu", "0.0000003"),
            _endpoint("Google", "google-vertex/global/priority", "0.00000054"),
            _endpoint("Google", "google-vertex/global", "0.0000003"),
            _endpoint("Google AI Studio", "google-ai-studio/flex", "0.00000015"),
            _endpoint("Google AI Studio", "google-ai-studio", "0.0000003"),
        ],
        public_model_id="google/gemini-2.5-flash",
    )

    by_provider = {str(row["tr_provider_slug"]): row for row in endpoints}
    assert set(by_provider) == {"google-ai-studio", "google-vertex"}
    assert by_provider["google-ai-studio"]["tag"] == "google-ai-studio"
    assert by_provider["google-vertex"]["tag"] == "google-vertex/global"
    assert by_provider["google-ai-studio"]["pricing"] == {
        "prompt": "0.0000003",
        "completion": "0.0000025",
    }


def test_committed_snapshot_has_at_most_one_route_per_google_product() -> None:
    snapshot_path = (
        Path(__file__).parents[1]
        / "src"
        / "trusted_router"
        / "data"
        / "openrouter_snapshot.json"
    )
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))

    for model in snapshot["models"]:
        google_products = [
            endpoint["tr_provider_slug"]
            for endpoint in model.get("endpoints", [])
            if endpoint.get("tr_provider_slug")
            in {"google-ai-studio", "google-vertex"}
        ]
        assert len(google_products) == len(set(google_products)), model["id"]


def test_legacy_google_endpoint_ids_preserve_their_original_product() -> None:
    prepaid_chat = _endpoint_for_id_compat(
        "google/gemini-2.5-flash@gemini/prepaid"
    )
    byok_chat = _endpoint_for_id_compat("google/gemini-2.5-flash@gemini/byok")
    prepaid_embedding = _endpoint_for_id_compat(
        "google/gemini-embedding-001@gemini/prepaid"
    )

    assert prepaid_chat is not None
    assert prepaid_chat.provider == "google-vertex"
    assert byok_chat is not None
    assert byok_chat.provider == "google-ai-studio"
    assert prepaid_embedding is not None
    assert prepaid_embedding.provider == "google-ai-studio"


def test_google_vertex_ingest_rejects_third_party_publishers() -> None:
    endpoints = filter_endpoints(
        [_endpoint("Google", "google-vertex/global", "0.0000003")],
        public_model_id="anthropic/claude-sonnet-4.6",
    )

    assert endpoints == []


def test_gemini_price_feed_prices_both_products_without_inventing_vertex_route() -> None:
    model_id = "google/gemini-2.5-flash"
    result = ProviderPricingResult(
        slug="gemini",
        prices={model_id: ModelPrice(300_000, 2_500_000)},
        source="deterministic",
        fetched_url="https://ai.google.dev/gemini-api/docs/pricing",
    )
    provider_index = refresh._index_provider_prices({"gemini": result})
    assert set(provider_index[model_id]) == {"google-ai-studio", "google-vertex"}

    snapshot = {
        "tr_keyed_providers": ["google-ai-studio", "google-vertex"],
        "models": [
            {
                "id": model_id,
                "name": "Gemini 2.5 Flash",
                "context_length": 1_048_576,
                "pricing": {"prompt": "0.0000003", "completion": "0.0000025"},
                "endpoints": [
                    _endpoint("Google AI Studio", "google-ai-studio", "0.0000003")
                    | {"tr_provider_slug": "google-ai-studio"}
                ],
            }
        ],
    }

    merged = refresh._merge_snapshot(snapshot, provider_index, set())
    slugs = {
        row["tr_provider_slug"]
        for row in merged["models"][0]["endpoints"]
    }
    assert slugs == {"google-ai-studio"}


def test_gemini_price_feed_preserves_existing_vertex_route() -> None:
    model_id = "google/gemini-2.5-flash"
    result = ProviderPricingResult(
        slug="gemini",
        prices={model_id: ModelPrice(300_000, 2_500_000)},
        source="deterministic",
        fetched_url="https://ai.google.dev/gemini-api/docs/pricing",
    )
    snapshot = {
        "tr_keyed_providers": ["google-vertex"],
        "models": [
            {
                "id": model_id,
                "name": "Gemini 2.5 Flash",
                "context_length": 1_048_576,
                "pricing": {"prompt": "0.0000003", "completion": "0.0000025"},
                "endpoints": [
                    _endpoint("Google", "google-vertex/global", "0.0000003")
                    | {"tr_provider_slug": "google-vertex"}
                ],
            }
        ],
    }

    merged = refresh._merge_snapshot(
        snapshot,
        refresh._index_provider_prices({"gemini": result}),
        set(),
    )
    endpoint = merged["models"][0]["endpoints"][0]
    assert endpoint["tr_provider_slug"] == "google-vertex"
    assert endpoint["pricing_source"] == "provider_direct"
