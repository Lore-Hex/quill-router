from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts.pricing import openai_catalog
from scripts.pricing.base import ProviderPricingResult
from scripts.pricing.providers import zero_g
from scripts.pricing.refresh import (
    _PRICING_RESULT_PROVIDER_ALIASES,
    PROVIDER_SLUGS,
)
from trusted_router.catalog import MODEL_ENDPOINTS, PROVIDERS
from trusted_router.catalog_data import (
    GATEWAY_PREPAID_PROVIDER_SLUGS,
    PRIVACY_TIER_CONFIDENTIAL,
    Model,
)
from trusted_router.catalog_privacy import provider_privacy_tier
from trusted_router.providers import OPENAI_COMPATIBLE_PROVIDERS, ProviderClient
from trusted_router.services.inference_errors import default_provider_secret_ref


def _marketplace_html() -> str:
    models = [
        {
            "id": "0gm-1.0-35b-a3b",
            "name": "0GM 1.0 35B A3B",
            "context_length": 262_144,
            "max_completion_tokens": 32_768,
        },
        {
            "id": "0gm-1.0-35b-a3b-sia",
            "name": "0GM 1.0 35B A3B SIA",
            "context_length": 32_768,
            "max_completion_tokens": 16_384,
        },
        {
            "id": "glm-5.2",
            "name": "GLM 5.2",
            "context_length": 1_048_576,
            "max_completion_tokens": 131_072,
        },
    ]

    def route(
        model_id: str,
        *,
        prompt: str,
        completion: str,
        cached: str | None = None,
        trust_mode: str = "private",
        verifiability: str = "TeeML",
        model_type: str = "chatbot",
        healthy: bool = True,
    ) -> dict[str, Any]:
        return {
            "model_id": model_id,
            "canonical_id": model_id,
            "name": model_id,
            "service_type": model_type,
            "type": model_type,
            "trust_mode": trust_mode,
            "verifiability": verifiability,
            "tee_attested": verifiability == "TeeML",
            "is_healthy": healthy,
            "tee_type": "TDX",
            "tee_verifier": "0g",
            "context_length": 1_048_576,
            "max_completion_tokens": 131_072,
            "architecture": {
                "input_modalities": ["text"],
                "output_modalities": ["text"],
            },
            "supported_parameters": ["tools", "response_format", "thinking"],
            "pricing_usd": {
                "prompt": prompt,
                "completion": completion,
                "cached_prompt": cached,
            },
        }

    providers = [
        route(
            "0gm-1.0-35b-a3b",
            prompt="0.00000008",
            completion="0.00000048",
            cached="0.0000000266667",
        ),
        route(
            "0gm-1.0-35b-a3b-sia",
            prompt="0.00000008",
            completion="0.00000048",
            cached="0.0000000266667",
        ),
        route(
            "glm-5.2",
            prompt="0.0000009",
            completion="0.000003",
            cached="0.00000018",
        ),
        # A second private provider can be selected by 0G's router. The
        # highest active private price must win so settlement cannot underbill.
        route(
            "glm-5.2",
            prompt="0.000001",
            completion="0.0000032",
            cached="0.0000002",
        ),
        # TeeTLS attests only the routing proxy. It must never enter the
        # confidential provider manifest, even if it is cheaper.
        route(
            "glm-5.2",
            prompt="0.0000001",
            completion="0.0000002",
            trust_mode="verified",
            verifiability="TeeTLS",
        ),
        route(
            "claude-opus-4-8",
            prompt="0.000005",
            completion="0.000025",
            trust_mode="standard",
            verifiability="None",
        ),
        route(
            "z-image",
            prompt="0.01",
            completion="0.01",
            model_type="image",
        ),
        route(
            "dead-private-model",
            prompt="0.01",
            completion="0.01",
            healthy=False,
        ),
    ]
    flight = {
        "dehydratedState": {
            "queries": [
                {"queryKey": ["models"], "state": {"data": models}},
                {"queryKey": ["providers"], "state": {"data": providers}},
            ]
        }
    }
    pushed = [1, "1:" + json.dumps(flight, separators=(",", ":"))]
    return (
        "<html><body><script>self.__next_f.push("
        + json.dumps(pushed, separators=(",", ":"))
        + ")</script></body></html>"
    )


def test_zero_g_parser_admits_only_healthy_private_teeml_chat_routes() -> None:
    prices, rows = zero_g.parse_private_catalog(_marketplace_html())

    assert set(rows) == {
        "zero-g/0gm-1.0-35b-a3b",
        "zero-g/0gm-1.0-35b-a3b-sia",
        "z-ai/glm-5.2",
    }
    assert rows["z-ai/glm-5.2"]["private_provider_count"] == 2
    assert rows["z-ai/glm-5.2"]["trust_mode"] == "private"
    assert rows["z-ai/glm-5.2"]["verifiability"] == "TeeML"
    assert rows["z-ai/glm-5.2"]["tee_attested"] is True
    assert rows["z-ai/glm-5.2"]["supported_features"] == [
        "chat",
        "completion",
        "private_inference",
        "teeml",
        "tools",
        "json_mode",
        "structured_outputs",
        "reasoning",
        "prompt_caching",
    ]

    glm = prices["z-ai/glm-5.2"]
    assert glm.prompt_micro_per_m == 1_000_000
    assert glm.completion_micro_per_m == 3_200_000
    assert glm.tiers[0].prompt_cached_micro_per_m == 200_000
    assert prices["zero-g/0gm-1.0-35b-a3b"].prompt_micro_per_m == 80_000
    assert prices["zero-g/0gm-1.0-35b-a3b"].completion_micro_per_m == 480_000


def test_zero_g_future_private_model_families_normalize_without_allowlists() -> None:
    assert zero_g._canonical_model_id("claude-opus-5") == "anthropic/claude-opus-5"
    assert zero_g._canonical_model_id("gpt-5.6-sol") == "openai/gpt-5.6-sol"
    assert zero_g._canonical_model_id("kimi-k3") == "moonshotai/kimi-k3"
    assert zero_g._canonical_model_id("qwen3.7-plus") == "qwen/qwen3.7-plus"


def test_zero_g_fetch_forces_private_canary_and_enables_only_after_pong(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def probe(**kwargs: Any) -> bool:
        captured.update(kwargs)
        return True

    monkeypatch.setenv("ZERO_G_API_KEY", "test-key")
    monkeypatch.setattr(zero_g, "fetch_html", lambda _url: _marketplace_html())
    monkeypatch.setattr(zero_g, "probe_openai_chat", probe)
    monkeypatch.setattr(zero_g, "_DISCOVERED_MANIFEST_ROWS", {})
    monkeypatch.setattr(zero_g, "_LIVE_CANARY_OK", False)

    result = zero_g.fetch()

    assert result.slug == "zero-g"
    assert captured == {
        "base_url": "https://router-api.0g.ai/v1",
        "api_key": "test-key",
        "model": "0gm-1.0-35b-a3b",
        "extra_headers": {"X-0G-Provider-Trust-Mode": "private"},
        "expected_content": "PONG",
    }
    assert zero_g._LIVE_CANARY_OK is True
    assert all(
        row["routable"] is True
        for row in zero_g._DISCOVERED_MANIFEST_ROWS.values()
    )


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"choices": [{"message": {"content": "PONG"}}]}, True),
        ({"choices": [{"message": {"content": "not pong"}}]}, False),
        ({"choices": []}, False),
        ({"unexpected": "shape"}, False),
    ],
)
def test_zero_g_canary_requires_exact_openai_pong(
    payload: dict[str, Any],
    expected: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json() -> dict[str, Any]:
            return payload

    def post(url: str, **kwargs: Any) -> FakeResponse:
        captured["url"] = url
        captured.update(kwargs)
        return FakeResponse()

    monkeypatch.setattr(openai_catalog.httpx, "post", post)

    assert (
        openai_catalog.probe_openai_chat(
            base_url=zero_g.BASE_URL,
            api_key="test-key",
            model="0gm-1.0-35b-a3b",
            extra_headers=zero_g.PRIVATE_TRUST_HEADERS,
            expected_content="PONG",
        )
        is expected
    )
    assert captured["url"] == "https://router-api.0g.ai/v1/chat/completions"
    assert captured["headers"]["X-0G-Provider-Trust-Mode"] == "private"


def test_zero_g_missing_key_keeps_every_route_dark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ZERO_G_API_KEY", raising=False)
    monkeypatch.setattr(zero_g, "fetch_html", lambda _url: _marketplace_html())
    monkeypatch.setattr(zero_g, "probe_openai_chat", lambda **_kwargs: False)
    monkeypatch.setattr(zero_g, "_DISCOVERED_MANIFEST_ROWS", {})
    monkeypatch.setattr(zero_g, "_LIVE_CANARY_OK", True)

    zero_g.fetch()

    assert zero_g._LIVE_CANARY_OK is False
    assert zero_g._DISCOVERED_MANIFEST_ROWS
    assert all(
        row["routable"] is False
        and row["routable_reason"] == "provider-canary-failed"
        for row in zero_g._DISCOVERED_MANIFEST_ROWS.values()
    )


def test_zero_g_manifest_writer_preserves_dark_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prices, rows = zero_g.parse_private_catalog(_marketplace_html())
    manifest_path = tmp_path / "zero-g.json"
    manifest_path.write_text(
        json.dumps({"provider": "zero-g", "models": []}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(zero_g, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(zero_g, "_DISCOVERED_MANIFEST_ROWS", rows)
    monkeypatch.setattr(zero_g, "_LIVE_CANARY_OK", False)
    result = ProviderPricingResult(
        slug="zero-g",
        prices=prices,
        source="api",
        fetched_url=zero_g.URL,
    )

    zero_g.write_provider_manifest(result)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["model_count"] == 3
    assert all(
        row["routable"] is False
        and row["routable_reason"] == "provider-canary-failed"
        for row in manifest["models"]
    )


def test_zero_g_catalog_and_local_adapter_are_confidential_credits_only() -> None:
    provider = PROVIDERS["zero-g"]
    assert provider.supports_prepaid is True
    assert provider.supports_byok is False
    assert provider.provider_zero_data_retention is None
    assert provider.provider_confidential_compute is True
    assert provider.provider_e2ee is True
    assert provider_privacy_tier(provider) == PRIVACY_TIER_CONFIDENTIAL
    assert "TeeTLS" in provider.provider_policy
    assert "zero-g" in GATEWAY_PREPAID_PROVIDER_SLUGS
    assert OPENAI_COMPATIBLE_PROVIDERS["zero-g"] == (
        ("ZERO_G_API_KEY",),
        "https://router-api.0g.ai/v1",
    )
    model = Model(
        id="z-ai/glm-5.2",
        name="GLM 5.2",
        provider="zero-g",
        context_length=1_048_576,
        upstream_id="glm-5.2",
    )
    assert ProviderClient._provider_extra_headers(model) == {
        "X-0G-Provider-Trust-Mode": "private"
    }
    assert default_provider_secret_ref("zero-g") == "env://ZERO_G_API_KEY"

    # The committed onboarding manifest is dark until a real account canary
    # passes, so importing the production catalog must not create a route yet.
    assert not any(
        endpoint.provider == "zero-g" for endpoint in MODEL_ENDPOINTS.values()
    )


def test_zero_g_hourly_refresh_and_optional_secret_wiring_are_complete() -> None:
    assert "zero_g" in PROVIDER_SLUGS
    assert _PRICING_RESULT_PROVIDER_ALIASES["zero_g"] == ("zero-g",)

    root = Path(__file__).resolve().parents[1]
    secrets = (root / "scripts/deploy/secrets.sh").read_text(encoding="utf-8")
    rollout = (root / "scripts/deploy/rollout.sh").read_text(encoding="utf-8")
    workflow = (root / ".github/workflows/refresh-prices.yml").read_text(
        encoding="utf-8"
    )
    assert (
        'ensure_secret_from_env_file "ZERO_G_API_KEY" '
        '"trustedrouter-zero-g-api-key"'
    ) in secrets
    assert (
        'grant_tr_deploy_secret_access "trustedrouter-zero-g-api-key"'
        in secrets
    )
    assert (
        'add_secret_env_if_exists "ZERO_G_API_KEY" '
        '"trustedrouter-zero-g-api-key"'
    ) in rollout
    assert "Pull optional 0G Private Computer key" in workflow
    assert "ZERO_G_API_KEY=${KEY}" in workflow


def test_zero_g_public_provider_page_is_prepared_while_routes_are_dark(
    client: Any,
) -> None:
    page = client.get("/providers/zero-g")
    assert page.status_code == 200
    assert "0G Private Computer" in page.text
    assert "TeeML" in page.text
    assert "TeeTLS" in page.text

    providers = {
        row["id"]: row for row in client.get("/v1/providers").json()["data"]
    }
    provider = providers["zero-g"]
    assert provider["supports_prepaid"] is True
    assert provider["supports_byok"] is False
    assert provider["provider_confidential_compute"] is True
    assert provider["provider_e2ee"] is True

    sitemap = client.get("/sitemap-providers.xml")
    assert sitemap.status_code == 200
    assert "https://trustedrouter.com/providers/zero-g" in sitemap.text
