from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.pricing.manifest import set_manifest_canary_state
from scripts.pricing.providers import crusoe


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def test_crusoe_fetch_discovers_prices_and_native_ids(monkeypatch) -> None:  # noqa: ANN001
    payload = {
        "data": [
            {
                "id": "zai/GLM-5.2",
                "pricing": {
                    "prompt": "1.40",
                    "completion": "4.40",
                    "input_cache_reads": "0.26",
                },
            },
            {
                "id": "deepseek-ai/Deepseek-V4-Flash",
                "pricing": {
                    "prompt": "0.14",
                    "completion": "0.28",
                    "input_cache_reads": "0.03",
                },
            },
            {
                "id": "moonshotai/Kimi-K2.6",
                "pricing": {
                    "prompt": "0.70",
                    "completion": "3.50",
                    "input_cache_reads": "0.35",
                },
            },
            {
                "id": "openai/gpt-oss-120b",
                "pricing": {
                    "prompt": "0.05",
                    "completion": "0.25",
                    "input_cache_reads": "0.05",
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

    monkeypatch.setattr(crusoe.httpx, "Client", FakeClient)

    result = crusoe.fetch()
    glm = result.prices["z-ai/glm-5.2"]
    flash = result.prices["deepseek/deepseek-v4-flash"]

    assert glm.prompt_micro_per_m == 1_400_000
    assert glm.completion_micro_per_m == 4_400_000
    assert glm.tiers[0].prompt_cached_micro_per_m == 260_000
    assert flash.prompt_micro_per_m == 140_000
    assert crusoe.UPSTREAM_ID_MAP["z-ai/glm-5.2"] == "zai/GLM-5.2"
    assert (
        crusoe.UPSTREAM_ID_MAP["deepseek/deepseek-v4-flash"]
        == "deepseek-ai/Deepseek-V4-Flash"
    )


def test_crusoe_auth_failure_darks_routes_until_key_is_repaired(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "crusoe.json"
    manifest_path.write_text(
        json.dumps(
            {
                "provider": "crusoe",
                "models": [
                    {
                        "id": "z-ai/glm-5.2",
                        "upstream_id": "zai/GLM-5.2",
                        "endpoints": ["chat/completions"],
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    class AuthFailureResponse:
        status_code = 403

        def raise_for_status(self) -> None:
            raise AssertionError("auth failure must be handled before raise_for_status")

        def json(self) -> dict:
            return {"errors": ["Authentication failed"]}

    class AuthFailureClient:
        def __init__(self, **_kwargs) -> None:  # noqa: ANN003
            return None

        def __enter__(self) -> AuthFailureClient:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def get(self, *_args, **_kwargs) -> AuthFailureResponse:  # noqa: ANN002, ANN003
            return AuthFailureResponse()

    monkeypatch.setattr(crusoe, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(crusoe.httpx, "Client", AuthFailureClient)

    result = crusoe.fetch()
    notes = crusoe.write_provider_manifest(result)

    row = json.loads(manifest_path.read_text(encoding="utf-8"))["models"][0]
    assert result.source == "api_auth_failed"
    assert result.prices == {}
    assert row["routable"] is False
    assert row["routable_reason"] == "provider-canary-failed"
    assert "routes remain dark" in notes[0]

    set_manifest_canary_state(manifest_path, healthy=True)
    restored = json.loads(manifest_path.read_text(encoding="utf-8"))["models"][0]
    assert "routable" not in restored
    assert "routable_reason" not in restored
