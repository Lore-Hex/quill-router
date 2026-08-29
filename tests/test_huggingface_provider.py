from __future__ import annotations

import pytest

from scripts.pricing.base import ModelPrice
from scripts.pricing.openai_catalog import discover_openai_chat_catalog
from scripts.pricing.providers import huggingface
from trusted_router.catalog import GATEWAY_PREPAID_PROVIDER_SLUGS, PROVIDERS


def test_normalizer_pins_cheapest_live_priced_downstream() -> None:
    rows = huggingface._normalize_rows(
        [
            {
                "id": "zai-org/GLM-5.3",
                "architecture": {
                    "input_modalities": ["text"],
                    "output_modalities": ["text"],
                },
                "providers": [
                    {
                        "provider": "expensive",
                        "status": "live",
                        "context_length": 262_144,
                        "pricing": {"input": 2.0, "output": 8.0},
                    },
                    {
                        "provider": "best",
                        "status": "live",
                        "context_length": 1_048_576,
                        "pricing": {"input": 1.4, "output": 4.4},
                    },
                    {
                        "provider": "offline",
                        "status": "staging",
                        "pricing": {"input": 0.1, "output": 0.1},
                    },
                ],
            }
        ]
    )

    assert rows == [
        {
            "id": "zai-org/GLM-5.3",
            "upstream_id": "zai-org/GLM-5.3:best",
            "name": "zai-org/GLM-5.3 via best",
            "pricing": {"input": "0.0000014", "output": "0.0000044"},
            "context_length": 1_048_576,
            "input_modalities": ["text"],
            "output_modalities": ["text"],
        }
    ]

    upstream_ids: dict[str, str] = {}
    prices, discovered = discover_openai_chat_catalog(
        rows,
        explicit_map={},
        upstream_id_map=upstream_ids,
        accept_source_upstream_id=True,
    )
    assert prices == {"z-ai/glm-5.3": ModelPrice(1_400_000, 4_400_000)}
    assert discovered["z-ai/glm-5.3"]["upstream_id"] == "zai-org/GLM-5.3:best"
    assert upstream_ids == {"z-ai/glm-5.3": "zai-org/GLM-5.3:best"}


def test_normalizer_rejects_unpriced_or_nonlive_downstreams() -> None:
    assert (
        huggingface._normalize_rows(
            [
                {
                    "id": "vendor/model",
                    "providers": [
                        {"provider": "missing-price", "status": "live"},
                        {
                            "provider": "offline",
                            "status": "staging",
                            "pricing": {"input": 1, "output": 1},
                        },
                    ],
                }
            ]
        )
        == []
    )


def test_normalizer_rejects_unsafe_downstream_slug() -> None:
    assert (
        huggingface._normalize_rows(
            [
                {
                    "id": "vendor/model",
                    "providers": [
                        {
                            "provider": "bad\nroute",
                            "status": "live",
                            "pricing": {"input": 1, "output": 1},
                        }
                    ],
                }
            ]
        )
        == []
    )


def test_normalizer_fails_closed_on_conflicting_duplicate_model_rows() -> None:
    with pytest.raises(RuntimeError, match="duplicate model rows"):
        huggingface._normalize_rows(
            [
                {
                    "id": "vendor/model",
                    "providers": [
                        {
                            "provider": "first",
                            "status": "live",
                            "pricing": {"input": 1, "output": 1},
                        }
                    ],
                },
                {
                    "id": "VENDOR/MODEL",
                    "providers": [
                        {
                            "provider": "second",
                            "status": "live",
                            "pricing": {"input": 1, "output": 1},
                        }
                    ],
                },
            ]
        )


def test_huggingface_is_prepaid_standard_only() -> None:
    provider = PROVIDERS["huggingface"]

    assert "huggingface" in GATEWAY_PREPAID_PROVIDER_SLUGS
    assert provider.supports_prepaid is True
    assert provider.supports_byok is False
    assert provider.provider_zero_data_retention is not True
    assert provider.provider_e2ee is not True
    assert huggingface.CATALOG.spec.canary_concurrency == 8
    assert huggingface.CATALOG.spec.canary_max_tokens == 4
