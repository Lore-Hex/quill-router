from __future__ import annotations

import json
from pathlib import Path

from scripts.pricing.base import ModelPrice, ProviderPricingResult
from scripts.pricing.providers import baseten, wafer


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def test_baseten_fetch_discovers_prices_without_float_drift(monkeypatch) -> None:  # noqa: ANN001
    payload = {
        "data": [
            {
                "id": "zai-org/GLM-5.2",
                "context_length": 524288,
                "pricing": {
                    "prompt": "0.0000014",
                    "completion": "0.0000044",
                    "input_cache_read": "0.00000014",
                },
            },
            {
                "id": "zai-org/GLM-5.2-Fast",
                "context_length": 524288,
                "pricing": {
                    "prompt": "0.0000021",
                    "completion": "0.0000066",
                    "input_cache_read": "0.00000021",
                },
            },
            {
                "id": "moonshotai/Kimi-K2.7-Code",
                "pricing": {
                    "prompt": "0.00000095",
                    "completion": "0.000004",
                    "input_cache_read": "0.00000016",
                },
            },
            {
                "id": "thinkingmachines/inkling",
                "pricing": {
                    "prompt": "0.000001",
                    "completion": "0.00000405",
                    "input_cache_read": "0.00000017",
                },
            },
        ]
    }

    class FakeClient:
        def __init__(self, **_kwargs) -> None:  # noqa: ANN003
            return None

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def get(self, *_args, **_kwargs) -> FakeResponse:  # noqa: ANN002, ANN003
            return FakeResponse(payload)

    monkeypatch.setattr(baseten.httpx, "Client", FakeClient)

    result = baseten.fetch()
    glm = result.prices["z-ai/glm-5.2"]
    glm_fast = result.prices["z-ai/glm-5.2-fast"]
    kimi = result.prices["moonshotai/kimi-k2.7-code"]
    inkling = result.prices["thinkingmachines/inkling-1m"]

    assert glm.prompt_micro_per_m == 1_400_000
    assert glm.completion_micro_per_m == 4_400_000
    assert glm.tiers[0].prompt_cached_micro_per_m == 140_000
    assert glm_fast.prompt_micro_per_m == 2_100_000
    assert glm_fast.completion_micro_per_m == 6_600_000
    assert glm_fast.tiers[0].prompt_cached_micro_per_m == 210_000
    assert kimi.prompt_micro_per_m == 950_000
    assert kimi.completion_micro_per_m == 4_000_000
    assert inkling.prompt_micro_per_m == 1_000_000
    assert inkling.completion_micro_per_m == 4_050_000
    assert inkling.tiers[0].prompt_cached_micro_per_m == 170_000
    assert baseten.UPSTREAM_ID_MAP["z-ai/glm-5.2"] == "zai-org/GLM-5.2"
    assert baseten.UPSTREAM_ID_MAP["z-ai/glm-5.2-fast"] == "zai-org/GLM-5.2-Fast"
    assert baseten.UPSTREAM_ID_MAP["thinkingmachines/inkling-1m"] == "thinkingmachines/inkling"


def test_baseten_provider_appends_new_priced_models_to_manifest(
    tmp_path: Path, monkeypatch
) -> None:  # noqa: ANN001
    manifest_path = tmp_path / "baseten.json"
    manifest_path.write_text(
        json.dumps(
            {
                "provider": "baseten",
                "source": baseten.URL,
                "generated_at": "2026-01-01T00:00:00Z",
                "model_count": 1,
                "models": [
                    {
                        "id": "z-ai/glm-5.2",
                        "upstream_id": "zai-org/GLM-5.2",
                        "display_name": "GLM 5.2",
                        "context_length": 262144,
                        "endpoints": ["chat/completions"],
                        "input_token_price_per_m": 1,
                        "output_token_price_per_m": 1,
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(baseten, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(
        baseten,
        "_DISCOVERED_MANIFEST_ROWS",
        {
            "z-ai/glm-5.2": {
                "id": "z-ai/glm-5.2",
                "upstream_id": "zai-org/GLM-5.2",
                "display_name": "GLM 5.2",
                "context_length": 524288,
                "endpoints": ["chat/completions"],
            },
            "z-ai/glm-5.2-fast": {
                "id": "z-ai/glm-5.2-fast",
                "upstream_id": "zai-org/GLM-5.2-Fast",
                "display_name": "GLM 5.2 Fast",
                "context_length": 524288,
                "endpoints": ["chat/completions"],
            },
        },
    )
    result = ProviderPricingResult(
        slug="baseten",
        source="api",
        fetched_url=baseten.URL,
        prices={
            "z-ai/glm-5.2": ModelPrice(
                1_400_000,
                4_400_000,
                prompt_cached_micro_per_m=140_000,
            ),
            "z-ai/glm-5.2-fast": ModelPrice(
                2_100_000,
                6_600_000,
                prompt_cached_micro_per_m=210_000,
            ),
        },
    )

    notes = baseten.write_provider_manifest(result)

    assert notes == ["baseten: refreshed provider_models/baseten.json (2 priced rows, appended 1)"]
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    by_id = {row["id"]: row for row in raw["models"]}
    assert raw["model_count"] == 2
    assert by_id["z-ai/glm-5.2"]["context_length"] == 524288
    assert by_id["z-ai/glm-5.2"]["cached_input_token_price_per_m"] == 140_000
    assert by_id["z-ai/glm-5.2-fast"] == {
        "display_name": "GLM 5.2 Fast",
        "title": "zai-org/GLM-5.2-Fast",
        "model_type": "chat",
        "input_modalities": ["text"],
        "output_modalities": ["text"],
        "endpoints": ["chat/completions"],
        "status": 1,
        "id": "z-ai/glm-5.2-fast",
        "upstream_id": "zai-org/GLM-5.2-Fast",
        "context_length": 524288,
        "input_token_price_per_m": 2_100_000,
        "output_token_price_per_m": 6_600_000,
        "cached_input_token_price_per_m": 210_000,
    }


def test_wafer_fetch_discovers_prices_and_native_ids(monkeypatch) -> None:  # noqa: ANN001
    payload = {
        "data": [
            {
                "id": "GLM-5.1",
                "wafer": {
                    "pricing": {
                        "input_cents_per_million": 100,
                        "output_cents_per_million": 320,
                        "cache_read_cents_per_million": 10,
                    }
                },
            },
            {
                "id": "GLM-5.2",
                "wafer": {
                    "display_name": "GLM-5.2",
                    "context_length": 1048576,
                    "capabilities": {
                        "zdr": {"supported": True},
                        "chat_completions": {"vision": False},
                    },
                    "pricing": {
                        "input_cents_per_million": 120,
                        "output_cents_per_million": 410,
                        "cache_read_cents_per_million": 20,
                    },
                },
            },
            {
                "id": "glm5.2-fast",
                "wafer": {
                    "display_name": "GLM-5.2-Fast",
                    "context_length": 1048576,
                    "capabilities": {
                        "zdr": {"supported": True},
                        "chat_completions": {"vision": False},
                    },
                    "pricing": {
                        "input_cents_per_million": 300,
                        "output_cents_per_million": 1025,
                        "cache_read_cents_per_million": 50,
                    },
                },
            },
            {
                "id": "Kimi-K2.6",
                "wafer": {
                    "capabilities": {
                        "vision": True,
                        "zdr": {"supported": False},
                        "chat_completions": {"supported": True},
                    },
                    "pricing": {
                        "input_cents_per_million": 114,
                        "output_cents_per_million": 480,
                        "cache_read_cents_per_million": 19,
                    },
                },
            },
            {
                "id": "MiniMax-M3",
                "wafer": {
                    "pricing": {
                        "input_cents_per_million": 33,
                        "output_cents_per_million": 132,
                        "cache_read_cents_per_million": 7,
                    }
                },
            },
        ]
    }

    class FakeClient:
        def __init__(self, **_kwargs) -> None:  # noqa: ANN003
            return None

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def get(self, *_args, **_kwargs) -> FakeResponse:  # noqa: ANN002, ANN003
            return FakeResponse(payload)

    monkeypatch.setattr(wafer.httpx, "Client", FakeClient)

    result = wafer.fetch()
    glm = result.prices["z-ai/glm-5.2"]
    fast = result.prices["z-ai/glm-5.2-fast"]
    kimi = result.prices["moonshotai/kimi-k2.6"]
    minimax = result.prices["minimax/minimax-m3"]

    assert glm.prompt_micro_per_m == 1_200_000
    assert glm.completion_micro_per_m == 4_100_000
    assert glm.tiers[0].prompt_cached_micro_per_m == 200_000
    assert fast.prompt_micro_per_m == 3_000_000
    assert fast.completion_micro_per_m == 10_250_000
    assert fast.tiers[0].prompt_cached_micro_per_m == 500_000
    assert kimi.prompt_micro_per_m == 1_140_000
    assert minimax.completion_micro_per_m == 1_320_000
    assert wafer.UPSTREAM_ID_MAP["z-ai/glm-5.2"] == "GLM-5.2"
    assert wafer.UPSTREAM_ID_MAP["z-ai/glm-5.2-fast"] == "glm5.2-fast"
    assert wafer.UPSTREAM_ID_MAP["moonshotai/kimi-k2.6"] == "Kimi-K2.6"


def test_wafer_provider_appends_new_priced_models_to_manifest(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    manifest_path = tmp_path / "wafer.json"
    manifest_path.write_text(
        json.dumps(
            {
                "provider": "wafer",
                "source": wafer.URL,
                "generated_at": "2026-01-01T00:00:00Z",
                "model_count": 3,
                "models": [
                    {
                        "id": "z-ai/glm-5.2",
                        "upstream_id": "GLM-5.2",
                        "display_name": "GLM 5.2",
                        "context_length": 1048576,
                        "endpoints": ["chat/completions"],
                        "input_token_price_per_m": 1,
                        "output_token_price_per_m": 1,
                    },
                    {
                        "id": "moonshotai/kimi-k2.7-code",
                        "upstream_id": "Kimi-K2.7-Code",
                        "display_name": "Kimi K2.7 Code",
                        "context_length": 262144,
                        "endpoints": ["chat/completions"],
                        "input_token_price_per_m": 1,
                        "output_token_price_per_m": 1,
                    },
                    {
                        "id": "minimax/minimax-m3",
                        "upstream_id": "MiniMax-M3",
                        "display_name": "MiniMax M3",
                        "context_length": 1048576,
                        "endpoints": ["chat/completions"],
                        "input_token_price_per_m": 1,
                        "output_token_price_per_m": 1,
                    },
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(wafer, "MANIFEST_PATH", manifest_path)

    payload = {
        "data": [
            {
                "id": "GLM-5.1",
                "wafer": {
                    "pricing": {
                        "input_cents_per_million": 100,
                        "output_cents_per_million": 320,
                        "cache_read_cents_per_million": 10,
                    }
                },
            },
            {
                "id": "GLM-5.2",
                "wafer": {
                    "display_name": "GLM-5.2",
                    "context_length": 1048576,
                    "capabilities": {
                        "zdr": {"supported": True},
                        "chat_completions": {"vision": False},
                    },
                    "pricing": {
                        "input_cents_per_million": 120,
                        "output_cents_per_million": 410,
                        "cache_read_cents_per_million": 20,
                    },
                },
            },
            {
                "id": "glm5.2-fast",
                "wafer": {
                    "display_name": "GLM-5.2-Fast",
                    "context_length": 1048576,
                    "capabilities": {
                        "zdr": {"supported": True},
                        "chat_completions": {"vision": False},
                    },
                    "pricing": {
                        "input_cents_per_million": 300,
                        "output_cents_per_million": 1025,
                        "cache_read_cents_per_million": 50,
                    },
                },
            },
            {
                "id": "Kimi-K2.6",
                "wafer": {
                    "capabilities": {
                        "vision": True,
                        "zdr": {"supported": False},
                        "chat_completions": {"supported": True},
                    },
                    "pricing": {
                        "input_cents_per_million": 114,
                        "output_cents_per_million": 480,
                        "cache_read_cents_per_million": 19,
                    },
                },
            },
            {
                "id": "MiniMax-M3",
                "wafer": {
                    "pricing": {
                        "input_cents_per_million": 33,
                        "output_cents_per_million": 132,
                        "cache_read_cents_per_million": 7,
                    }
                },
            },
        ]
    }

    class FakeClient:
        def __init__(self, **_kwargs) -> None:  # noqa: ANN003
            return None

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def get(self, *_args, **_kwargs) -> FakeResponse:  # noqa: ANN002, ANN003
            return FakeResponse(payload)

    monkeypatch.setattr(wafer.httpx, "Client", FakeClient)

    result = wafer.fetch()
    notes = wafer.write_provider_manifest(result)

    assert notes == [
        "wafer: refreshed provider_models/wafer.json "
        "(5 priced rows, appended 3)"
    ]
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    by_id = {row["id"]: row for row in raw["models"]}
    assert raw["model_count"] == 6
    assert by_id["moonshotai/kimi-k2.7-code"]["missing_since"]
    assert by_id["moonshotai/kimi-k2.7-code"].get("routable") is not False
    assert by_id["z-ai/glm-5.2"]["input_token_price_per_m"] == 1_200_000
    assert by_id["z-ai/glm-5.2-fast"]["upstream_id"] == "glm5.2-fast"
    assert by_id["z-ai/glm-5.2-fast"]["input_token_price_per_m"] == 3_000_000
    assert by_id["z-ai/glm-5.2-fast"]["output_token_price_per_m"] == 10_250_000
    assert by_id["z-ai/glm-5.2-fast"]["cached_input_token_price_per_m"] == 500_000
    assert by_id["z-ai/glm-5.2-fast"]["zdr_supported"] is True
    assert by_id["moonshotai/kimi-k2.6"]["input_modalities"] == ["text", "image"]


def test_wafer_delisted_expected_model_reaches_manifest_prune(
    tmp_path: Path, monkeypatch, capsys
) -> None:  # noqa: ANN001
    manifest_path = tmp_path / "wafer.json"
    live_ids = {
        "z-ai/glm-5.1": "GLM-5.1",
        "z-ai/glm-5.2": "GLM-5.2",
        "z-ai/glm-5.2-fast": "glm5.2-fast",
        "moonshotai/kimi-k2.6": "Kimi-K2.6",
    }
    old_ids = {**live_ids, "minimax/minimax-m3": "MiniMax-M3"}
    manifest_path.write_text(
        json.dumps(
            {
                "provider": "wafer",
                "models": [
                    {
                        "id": model_id,
                        "upstream_id": native_id,
                        "endpoints": ["chat/completions"],
                        "input_token_price_per_m": 1,
                        "output_token_price_per_m": 1,
                    }
                    for model_id, native_id in old_ids.items()
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    prices = {model_id: wafer.ModelPrice(100, 200) for model_id in live_ids}
    discovered = {
        model_id: {
            "id": model_id,
            "upstream_id": native_id,
            "endpoints": ["chat/completions"],
        }
        for model_id, native_id in live_ids.items()
    }
    result = wafer.ProviderPricingResult(slug="wafer", prices=prices, source="api")
    monkeypatch.setattr(wafer, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(wafer, "_DISCOVERED_MANIFEST_ROWS", discovered)

    assert wafer.validate(prices, wafer.EXPECTED_MODELS) == []
    wafer.write_provider_manifest(result)

    first_raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    first_missing = next(
        row for row in first_raw["models"] if row["id"] == "minimax/minimax-m3"
    )
    assert first_missing["missing_since"]
    assert first_missing.get("routable") is not False

    wafer.write_provider_manifest(result)

    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    missing = next(row for row in raw["models"] if row["id"] == "minimax/minimax-m3")
    assert missing["routable"] is False
    assert missing["routable_reason"] == "delisted-upstream"
    assert missing["missing_since"] == first_missing["missing_since"]
    assert "minimax/minimax-m3" in capsys.readouterr().err
