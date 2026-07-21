from __future__ import annotations

import json
from pathlib import Path
from types import TracebackType
from typing import Any

from pytest import MonkeyPatch

from scripts.pricing.providers import cerebras


class _FakeCerebrasResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {
            "data": [
                {
                    "id": "zai-glm-4.7",
                    "name": "Z.ai GLM 4.7",
                    "pricing": {
                        "prompt": "0.00000225",
                        "completion": "0.00000275",
                    },
                    "capabilities": {"reasoning": True, "vision": False},
                    "limits": {
                        "max_context_length": 131_072,
                        "max_completion_tokens": 40_960,
                    },
                    "deprecated": False,
                },
                {
                    "id": "gemma-4-31b",
                    "name": "Gemma 4 31B",
                    "pricing": {
                        "prompt": "0.00000099",
                        "completion": "0.00000149",
                    },
                    "capabilities": {
                        "function_calling": True,
                        "reasoning": True,
                        "structured_outputs": True,
                        "vision": True,
                    },
                    "limits": {
                        "max_context_length": 131_072,
                        "max_completion_tokens": 40_960,
                    },
                    "deprecated": False,
                },
                {
                    "id": "gpt-oss-120b",
                    "name": "OpenAI GPT OSS",
                    "pricing": {
                        "prompt": "0.00000035",
                        "completion": "0.00000075",
                    },
                    "capabilities": {"reasoning": True, "vision": False},
                    "limits": {
                        "max_context_length": 131_072,
                        "max_completion_tokens": 40_960,
                    },
                    "deprecated": False,
                },
                {
                    "id": "retired-model",
                    "pricing": {
                        "prompt": "0.00000001",
                        "completion": "0.00000001",
                    },
                    "deprecated": True,
                },
            ]
        }


class _FakeCerebrasClient:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def __enter__(self) -> _FakeCerebrasClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    def get(self, url: str, *, headers: dict[str, str]) -> _FakeCerebrasResponse:
        assert url == cerebras.URL
        assert headers["Accept"] == "application/json"
        assert "Authorization" not in headers
        return _FakeCerebrasResponse()


def test_cerebras_public_api_discovers_models_prices_and_capabilities(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(cerebras.httpx, "Client", _FakeCerebrasClient)

    result = cerebras.fetch()

    gemma = result.prices["google/gemma-4-31b-it"]
    assert gemma.prompt_micro_per_m == 990_000
    assert gemma.completion_micro_per_m == 1_490_000
    assert result.prices["cerebras/gemma-4-31b"] == gemma
    assert result.prices["openai/gpt-oss-120b"].prompt_micro_per_m == 350_000
    assert result.prices["z-ai/glm-4.7"].completion_micro_per_m == 2_750_000

    row = cerebras._DISCOVERED_MANIFEST_ROWS["google/gemma-4-31b-it"]
    assert row["upstream_id"] == "gemma-4-31b"
    assert row["input_modalities"] == ["text", "image"]
    assert row["context_length"] == 131_072
    assert row["max_output_tokens"] == 40_960
    assert "function-calling" in row["features"]
    assert "structured-outputs" in row["features"]


def test_cerebras_refresh_writes_new_models_and_aliases(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(cerebras.httpx, "Client", _FakeCerebrasClient)
    manifest_path = tmp_path / "cerebras.json"
    manifest_path.write_text(
        json.dumps({"provider": "cerebras", "models": []}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cerebras, "MANIFEST_PATH", manifest_path)

    result = cerebras.fetch()
    cerebras.write_provider_manifest(result)

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows = {row["id"]: row for row in payload["models"]}
    assert payload["source"] == cerebras.URL
    assert payload["model_count"] == 6
    assert {
        "openai/gpt-oss-120b",
        "cerebras/gpt-oss-120b",
        "z-ai/glm-4.7",
        "cerebras/zai-glm-4.7",
        "google/gemma-4-31b-it",
        "cerebras/gemma-4-31b",
    } == set(rows)
    assert rows["google/gemma-4-31b-it"]["input_token_price_per_m"] == 990_000
    assert rows["google/gemma-4-31b-it"]["upstream_id"] == "gemma-4-31b"
