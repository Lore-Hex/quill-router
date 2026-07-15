from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.pricing import refresh
from scripts.pricing.base import ModelPrice, ProviderPricingResult, ast_whitelist_check
from scripts.pricing.parsers import cohere as cohere_parser
from scripts.pricing.parsers import voyage as voyage_parser
from scripts.pricing.providers import cohere, voyage
from trusted_router import catalog_ingest

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "pricing"


def test_cohere_parser_reads_embed4_token_cost_not_image_or_instance_cost() -> None:
    prices = cohere_parser.parse(
        (FIXTURE_DIR / "cohere.html").read_text(encoding="utf-8")
    )

    assert prices == {
        "cohere/embed-v4.0": {
            "prompt_micro_per_m": 120_000,
            "completion_micro_per_m": 0,
        }
    }


def test_voyage_parser_reads_current_and_older_text_embedding_tables() -> None:
    prices = voyage_parser.parse(
        (FIXTURE_DIR / "voyage.html").read_text(encoding="utf-8")
    )

    assert prices["voyage/voyage-4-large"]["prompt_micro_per_m"] == 120_000
    assert prices["voyage/voyage-finance-2"]["prompt_micro_per_m"] == 120_000
    assert prices["voyage/voyage-law-2"]["prompt_micro_per_m"] == 120_000
    assert prices["voyage/voyage-code-2"]["prompt_micro_per_m"] == 120_000
    assert prices["voyage/voyage-3-large"] == {
        "prompt_micro_per_m": 180_000,
        "completion_micro_per_m": 0,
    }
    assert "voyage/voyage-multimodal-3.5" not in prices
    assert "voyage/rerank-2" not in prices


@pytest.mark.parametrize("slug", ["cohere", "voyage"])
def test_embedding_parser_passes_self_heal_ast_policy(slug: str) -> None:
    source = (Path("scripts/pricing/parsers") / f"{slug}.py").read_text(
        encoding="utf-8"
    )
    assert ast_whitelist_check(source) == []


@pytest.mark.parametrize(
    ("provider", "model_id", "cost"),
    [
        (cohere, "cohere/embed-v4.0", 120_000),
        (voyage, "voyage/voyage-3-large", 180_000),
    ],
)
def test_embedding_provider_writer_updates_runtime_manifest(
    provider, model_id: str, cost: int, tmp_path, monkeypatch
) -> None:  # noqa: ANN001
    manifest_path = tmp_path / f"{provider.SLUG}.json"
    manifest_path.write_text(
        json.dumps(
            {
                "provider": provider.SLUG,
                "source": "model-docs",
                "generated_at": "2026-01-01T00:00:00Z",
                "price_scale": "microdollars_per_million",
                "models": [
                    {
                        "id": model_id,
                        "model_type": "embedding",
                        "endpoints": ["embeddings"],
                        "input_token_price_per_m": 1,
                        "output_token_price_per_m": 0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(provider, "MANIFEST_PATH", manifest_path)
    result = ProviderPricingResult(
        slug=provider.SLUG,
        prices={model_id: ModelPrice(cost, 0)},
        source="deterministic",
        fetched_url=provider.URL,
    )

    notes = provider.write_provider_manifest(result)

    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert raw["models"][0]["input_token_price_per_m"] == cost
    assert raw["models"][0]["output_token_price_per_m"] == 0
    assert raw["models"][0]["pricing_source"] == provider.URL
    assert notes == [
        f"{provider.SLUG}: refreshed provider_models/{provider.SLUG}.json "
        "(1 priced rows)"
    ]


def test_embedding_catalog_uses_manifest_cost_with_customer_markup(
    tmp_path, monkeypatch
) -> None:  # noqa: ANN001
    (tmp_path / "voyage.json").write_text(
        json.dumps(
            {
                "provider": "voyage",
                "price_scale": "microdollars_per_million",
                "models": [
                    {
                        "id": "voyage/voyage-3-large",
                        "model_type": "embedding",
                        "endpoints": ["embeddings"],
                        "input_token_price_per_m": 200_000,
                        "output_token_price_per_m": 0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", tmp_path)

    model = catalog_ingest._embedding_models()["voyage/voyage-3-large"]

    assert model.prompt_price_microdollars_per_million_tokens == 220_000
    assert model.completion_price_microdollars_per_million_tokens == 0


def test_embedding_providers_run_in_hourly_refresh() -> None:
    assert {"cohere", "voyage"} <= set(refresh.PROVIDER_SLUGS)


def test_checked_in_voyage_fallback_matches_first_party_rate(
    tmp_path, monkeypatch
) -> None:  # noqa: ANN001
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", tmp_path)
    model = catalog_ingest._embedding_models()["voyage/voyage-3-large"]
    assert model.prompt_price_microdollars_per_million_tokens == 198_000
