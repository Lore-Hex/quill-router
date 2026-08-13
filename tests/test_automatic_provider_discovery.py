from __future__ import annotations

import importlib
import json

import pytest

from scripts.pricing.base import ModelPrice, PriceTier, ProviderPricingResult
from scripts.pricing.manifest import write_discovered_chat_manifest
from scripts.pricing.parsers import openai as openai_parser
from scripts.pricing.providers import grok, openai
from scripts.pricing.refresh import PROVIDER_SLUGS
from trusted_router.catalog_ingest import _AUTHORITATIVE_PROVIDER_MANIFEST_SLUGS


def test_every_hourly_provider_owns_a_manifest_writer() -> None:
    missing: list[str] = []
    for slug in PROVIDER_SLUGS:
        module = importlib.import_module(
            f"scripts.pricing.providers.{slug.replace('-', '_')}"
        )
        if not callable(getattr(module, "write_provider_manifest", None)):
            missing.append(slug)
        if getattr(module, "MANIFEST_PATH", None) is None:
            missing.append(f"{slug}:manifest")
    assert missing == []


def test_canary_backed_catalogs_are_fail_closed_authorities() -> None:
    assert "grok" in _AUTHORITATIVE_PROVIDER_MANIFEST_SLUGS


def test_openai_discovers_only_live_priced_stable_chat_models(
    tmp_path,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = tmp_path / "openai.json"
    monkeypatch.setattr(openai, "MANIFEST_PATH", manifest)
    monkeypatch.setattr(openai, "UPSTREAM_ID_MAP", {})
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        openai,
        "fetch_provider",
        lambda **_kwargs: ProviderPricingResult(
            slug="openai",
            prices={
                "openai/gpt-5.6-sol": ModelPrice(800_000, 4_000_000),
                "openai/gpt-image-2": ModelPrice(1_000_000, 2_000_000),
            },
            source="deterministic",
            fetched_url=openai.URL,
        ),
    )
    monkeypatch.setattr(
        openai,
        "fetch_json",
        lambda *_args, **_kwargs: {
            "data": [
                {"id": "gpt-5.6-sol", "created": 123},
                {"id": "gpt-5.6-sol-2026-07-09"},
                {"id": "gpt-image-2"},
            ]
        },
    )
    probes: list[dict[str, object]] = []

    def probe(**kwargs: object) -> bool:
        probes.append(kwargs)
        return True

    monkeypatch.setattr(openai, "probe_openai_chat", probe)

    result = openai.fetch()
    notes = openai.write_provider_manifest(result)

    raw = json.loads(manifest.read_text(encoding="utf-8"))
    assert [row["id"] for row in raw["models"]] == ["openai/gpt-5.6-sol"]
    assert raw["models"][0]["upstream_id"] == "gpt-5.6-sol"
    assert raw["models"][0]["input_token_price_per_m"] == 800_000
    assert probes == [
        {
            "base_url": openai.BASE_URL,
            "api_key": "test-key",
            "model": "gpt-5.6-sol",
            "max_tokens_field": "max_completion_tokens",
        }
    ]
    assert notes == [
        "openai: refreshed provider_models/openai.json "
        "(1 priced rows, appended 1)"
    ]


def test_openai_parser_reads_models_hidden_behind_all_models_control() -> None:
    props = json.dumps(
        {
            "tier": [0, "standard"],
            "rows": [
                1,
                [
                    [
                        1,
                        [
                            [0, "gpt-5.5 (<272K context length)"],
                            [0, 5],
                            [0, 0.5],
                            [0, "-"],
                            [0, 30],
                        ],
                    ],
                    [
                        1,
                        [
                            [0, "gpt-5.4-mini"],
                            [0, 0.75],
                            [0, 0.075],
                            [0, "-"],
                            [0, 4.5],
                        ],
                    ],
                    [
                        1,
                        [
                            [0, "gpt-5.2"],
                            [0, 1.75],
                            [0, 0.175],
                            [0, 14],
                        ],
                    ],
                ],
            ],
        }
    )
    parsed = openai_parser.parse(
        f"<html><body><astro-island props='{props}' /></body></html>"
    )

    assert parsed["openai/gpt-5.5"] == {
        "tiers": [
            {
                "max_prompt_tokens": 272_000,
                "prompt_micro_per_m": 5_000_000,
                "completion_micro_per_m": 30_000_000,
                "prompt_cached_micro_per_m": 500_000,
            },
            {
                "max_prompt_tokens": None,
                "prompt_micro_per_m": 10_000_000,
                "completion_micro_per_m": 45_000_000,
                "prompt_cached_micro_per_m": 1_000_000,
            },
        ]
    }
    assert parsed["openai/gpt-5.4-mini"] == {
        "prompt_micro_per_m": 750_000,
        "completion_micro_per_m": 4_500_000,
        "prompt_cached_micro_per_m": 75_000,
    }
    assert parsed["openai/gpt-5.2"] == {
        "prompt_micro_per_m": 1_750_000,
        "completion_micro_per_m": 14_000_000,
        "prompt_cached_micro_per_m": 175_000,
    }


def test_grok_api_discovers_new_model_with_exact_tiered_prices(
    tmp_path,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = tmp_path / "grok.json"
    manifest.write_text(
        json.dumps(
            {
                "provider": "grok",
                "models": [
                    {
                        "id": "x-ai/grok-4.6",
                        "upstream_id": "grok-4.6",
                        "input_token_price_per_m": 1,
                        "output_token_price_per_m": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(grok, "MANIFEST_PATH", manifest)
    monkeypatch.setattr(grok, "UPSTREAM_ID_MAP", {})
    monkeypatch.setenv("GROK_API_KEY", "test-key")
    monkeypatch.setattr(
        grok,
        "fetch_json",
        lambda *_args, **_kwargs: {
            "models": [
                {
                    "id": "grok-4.6",
                    "input_modalities": ["text"],
                    "output_modalities": ["text"],
                    "prompt_text_token_price": 20_000,
                    "cached_prompt_text_token_price": 5_000,
                    "completion_text_token_price": 60_000,
                },
                {
                    "id": "grok-6",
                    "context_length": 1_000_000,
                    "input_modalities": ["text", "image"],
                    "output_modalities": ["text"],
                    "prompt_text_token_price": 10_000,
                    "cached_prompt_text_token_price": 1_000,
                    "completion_text_token_price": 30_000,
                    "long_context_threshold": 200_000,
                    "prompt_text_token_price_long_context": 20_000,
                    "cached_prompt_text_token_price_long_context": 2_000,
                    "completion_text_token_price_long_context": 60_000,
                },
                {
                    "id": "grok-imagine-image",
                    "input_modalities": ["text"],
                    "output_modalities": ["image"],
                    "prompt_text_token_price": 10_000,
                    "completion_text_token_price": 30_000,
                },
            ]
        },
    )
    probed: list[str] = []
    monkeypatch.setattr(
        grok,
        "probe_openai_chat",
        lambda **kwargs: probed.append(str(kwargs["model"])) or True,
    )

    result = grok.fetch()
    grok.write_provider_manifest(result)

    raw = json.loads(manifest.read_text(encoding="utf-8"))
    by_id = {row["id"]: row for row in raw["models"]}
    assert set(by_id) == {"x-ai/grok-4.6", "x-ai/grok-6"}
    assert probed == ["grok-6"]
    assert by_id["x-ai/grok-6"]["price_tiers"] == [
        {
            "max_prompt_tokens": 200_000,
            "input_token_price_per_m": 1_000_000,
            "output_token_price_per_m": 3_000_000,
            "cached_input_token_price_per_m": 100_000,
        },
        {
            "max_prompt_tokens": None,
            "input_token_price_per_m": 2_000_000,
            "output_token_price_per_m": 6_000_000,
            "cached_input_token_price_per_m": 200_000,
        },
    ]


def test_grok_failed_new_model_canary_is_published_dark(
    tmp_path,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = tmp_path / "grok.json"
    monkeypatch.setattr(grok, "MANIFEST_PATH", manifest)
    monkeypatch.setattr(grok, "UPSTREAM_ID_MAP", {})
    monkeypatch.setenv("GROK_API_KEY", "test-key")
    monkeypatch.setattr(
        grok,
        "fetch_json",
        lambda *_args, **_kwargs: {
            "models": [
                {
                    "id": "grok-4.6",
                    "input_modalities": ["text"],
                    "output_modalities": ["text"],
                    "prompt_text_token_price": 20_000,
                    "completion_text_token_price": 60_000,
                },
                {
                    "id": "grok-4.5",
                    "input_modalities": ["text"],
                    "output_modalities": ["text"],
                    "prompt_text_token_price": 20_000,
                    "completion_text_token_price": 60_000,
                },
            ]
        },
    )
    monkeypatch.setattr(grok, "probe_openai_chat", lambda **_kwargs: False)

    grok.write_provider_manifest(grok.fetch())

    rows = json.loads(manifest.read_text(encoding="utf-8"))["models"]
    assert all(row["routable"] is False for row in rows)
    assert all(row["routable_reason"] == "provider-canary-failed" for row in rows)


def test_shared_writer_bootstraps_manifest_and_preserves_multi_tier_prices(
    tmp_path,  # noqa: ANN001
) -> None:
    manifest = tmp_path / "future-provider.json"
    price = ModelPrice(
        tiers=[
            PriceTier(100, 1, 2, 1),
            PriceTier(None, 3, 4, 2),
        ]
    )
    result = ProviderPricingResult(
        slug="future-provider",
        prices={"vendor/future-model": price},
        source="api",
        fetched_url="https://provider.example/v1/models",
    )

    write_discovered_chat_manifest(
        result,
        manifest_path=manifest,
        discovered_rows={
            "vendor/future-model": {
                "id": "vendor/future-model",
                "upstream_id": "future-model",
                "endpoints": ["chat/completions"],
            }
        },
        source_url="https://provider.example/v1/models",
    )

    raw = json.loads(manifest.read_text(encoding="utf-8"))
    assert raw["provider"] == "future-provider"
    assert raw["models"][0]["price_tiers"] == [
        {
            "max_prompt_tokens": 100,
            "input_token_price_per_m": 1,
            "output_token_price_per_m": 2,
            "cached_input_token_price_per_m": 1,
        },
        {
            "max_prompt_tokens": None,
            "input_token_price_per_m": 3,
            "output_token_price_per_m": 4,
            "cached_input_token_price_per_m": 2,
        },
    ]
