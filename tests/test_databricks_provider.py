from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from scripts.pricing.base import ProviderPricingResult
from scripts.pricing.providers import databricks
from scripts.pricing.refresh import PROVIDER_SLUGS
from trusted_router.catalog import PROVIDERS
from trusted_router.providers import OPENAI_COMPATIBLE_PROVIDERS, ProviderClient
from trusted_router.services.inference_errors import default_provider_secret_ref


def _pricing_html() -> str:
    rows = {
        "Kimi K3": ("42.857", "214.286", "4.286"),
        "GLM-5.2": ("20", "62.857", "3.714"),
        "Inkling": ("14.286", "57.857", "2.429"),
        "Qwen 3.5 122B": ("3.143", "31.429", "-"),
        "Qwen 3 Next 80B": ("2.143", "17.143", "-"),
        "GPT OSS 120B": ("2.143", "8.571", "-"),
        "GPT OSS 20B": ("1", "4.286", "-"),
        "Llama 4 Maverick": ("7.143", "21.429", "-"),
        "Llama 3.3 70B": ("7.143", "21.429", "-"),
        "Gemma 3 12B": ("2.143", "7.143", "-"),
        "Llama 3.1 8B": ("2.143", "6.429", "-"),
    }
    body = "".join(
        f"<tr><td>{label}</td><td>{prompt}</td><td>{completion}</td>"
        f"<td>{cached}</td></tr>"
        for label, (prompt, completion, cached) in rows.items()
    )
    return f"<table><tr><th>Model</th><th>Input</th><th>Output</th><th>Cache</th></tr>{body}</table>"


def _supported_models_html() -> str:
    return "\n".join(databricks.UPSTREAM_ID_MAP.values())


def test_databricks_parser_converts_official_dbu_rates_exactly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABRICKS_DBU_USD", raising=False)
    prices, discovered = databricks._parse_catalog(
        _pricing_html(),
        _supported_models_html(),
    )

    assert set(prices) == set(databricks.EXPECTED_MODELS)
    assert prices["moonshotai/kimi-k3"].prompt_micro_per_m == 3_000_000
    assert prices["moonshotai/kimi-k3"].completion_micro_per_m == 15_000_000
    assert prices["moonshotai/kimi-k3"].tiers[0].prompt_cached_micro_per_m == 300_000
    assert prices["z-ai/glm-5.2"].prompt_micro_per_m == 1_400_000
    assert prices["z-ai/glm-5.2"].completion_micro_per_m == 4_400_000
    assert prices["openai/gpt-oss-20b"].prompt_micro_per_m == 70_000
    assert prices["openai/gpt-oss-20b"].completion_micro_per_m == 300_000
    assert discovered["z-ai/glm-5.2"]["upstream_id"] == "databricks-glm-5-2"
    assert discovered["moonshotai/kimi-k3"]["context_length"] == 1_048_576
    assert discovered["moonshotai/kimi-k3"]["input_modalities"] == ["text", "image"]


def test_databricks_parser_prunes_route_when_documented_endpoint_disappears() -> None:
    supported = _supported_models_html().replace("databricks-glm-5-2", "")
    prices, discovered = databricks._parse_catalog(_pricing_html(), supported)

    assert "z-ai/glm-5.2" not in prices
    assert "z-ai/glm-5.2" not in discovered


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "dbc-1234.cloud.databricks.com",
            "https://dbc-1234.cloud.databricks.com",
        ),
        (
            "https://adb-123.azuredatabricks.net/",
            "https://adb-123.azuredatabricks.net",
        ),
        (
            "https://dbc-123.gcp.databricks.com:443",
            "https://dbc-123.gcp.databricks.com",
        ),
    ],
)
def test_databricks_workspace_host_allowlist(raw: str, expected: str) -> None:
    assert databricks.normalize_workspace_host(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "http://dbc-1234.cloud.databricks.com",
        "https://dbc-1234.cloud.databricks.com:8443",
        "https://user@dbc-1234.cloud.databricks.com",
        "https://dbc-1234.cloud.databricks.com/path",
        "https://dbc-1234.cloud.databricks.com?next=evil",
        "https://cloud.databricks.com.evil.example",
        "https://127.0.0.1",
    ],
)
def test_databricks_workspace_host_rejects_unapproved_targets(raw: str) -> None:
    with pytest.raises(RuntimeError, match="approved workspace URL|is empty"):
        databricks.normalize_workspace_host(raw)


def test_databricks_fetch_uses_public_sources_without_operator_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse:
        def __init__(self, text: str) -> None:
            self.text = text

        def raise_for_status(self) -> None:
            return None

    class FakeClient:
        def __init__(self, **_kwargs: object) -> None:
            self.responses = [
                FakeResponse(_pricing_html()),
                FakeResponse(_supported_models_html()),
            ]

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def get(self, *_args: object, **_kwargs: object) -> FakeResponse:
            return self.responses.pop(0)

    monkeypatch.setattr(databricks.httpx, "Client", FakeClient)
    monkeypatch.delenv("DATABRICKS_TOKEN", raising=False)
    monkeypatch.delenv("DATABRICKS_HOST", raising=False)
    monkeypatch.setattr(databricks, "_DISCOVERED_MANIFEST_ROWS", {})
    monkeypatch.setattr(databricks, "_LIVE_CANARY_CHECKED_MODEL_IDS", {"old"})
    monkeypatch.setattr(databricks, "_LIVE_CANARY_HEALTHY_MODEL_IDS", {"old"})

    result = databricks.fetch()

    assert set(result.prices) == set(databricks.EXPECTED_MODELS)
    assert databricks._LIVE_CANARY_CHECKED_MODEL_IDS == set()
    assert databricks._LIVE_CANARY_HEALTHY_MODEL_IDS == set()
    assert len(databricks._DISCOVERED_MANIFEST_ROWS) == len(databricks.EXPECTED_MODELS)


def test_databricks_fetch_runs_private_canary_when_pair_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse:
        def __init__(self, text: str) -> None:
            self.text = text

        def raise_for_status(self) -> None:
            return None

    class FakeClient:
        def __init__(self, **_kwargs: object) -> None:
            self.responses = [
                FakeResponse(_pricing_html()),
                FakeResponse(_supported_models_html()),
            ]

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def get(self, *_args: object, **_kwargs: object) -> FakeResponse:
            return self.responses.pop(0)

    monkeypatch.setattr(databricks.httpx, "Client", FakeClient)
    monkeypatch.setenv("DATABRICKS_TOKEN", "token")
    monkeypatch.setenv("DATABRICKS_HOST", "dbc-1234.cloud.databricks.com")
    calls: list[dict[str, Any]] = []

    def fake_probe(**kwargs: Any) -> bool:
        calls.append(copy.deepcopy(kwargs))
        return kwargs["model"] != "databricks-glm-5-2"

    monkeypatch.setattr(
        databricks,
        "probe_openai_chat",
        fake_probe,
    )

    databricks.fetch()

    assert calls == [
        {
            "base_url": "https://dbc-1234.cloud.databricks.com/serving-endpoints",
            "api_key": "token",
            "model": databricks.UPSTREAM_ID_MAP[model_id],
            "max_tokens": 4,
        }
        for model_id in databricks.EXPECTED_MODELS
    ]
    assert databricks._LIVE_CANARY_CHECKED_MODEL_IDS == set(databricks.EXPECTED_MODELS)
    assert databricks._LIVE_CANARY_HEALTHY_MODEL_IDS == {
        model_id for model_id in databricks.EXPECTED_MODELS if model_id != "z-ai/glm-5.2"
    }


def test_databricks_fetch_rejects_partial_private_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse:
        text = _pricing_html()

        def raise_for_status(self) -> None:
            return None

    class FakeClient:
        def __init__(self, **_kwargs: object) -> None:
            self.calls = 0

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def get(self, *_args: object, **_kwargs: object) -> FakeResponse:
            self.calls += 1
            response = FakeResponse()
            if self.calls == 2:
                response.text = _supported_models_html()
            return response

    monkeypatch.setattr(databricks.httpx, "Client", FakeClient)
    monkeypatch.setenv("DATABRICKS_TOKEN", "token")
    monkeypatch.delenv("DATABRICKS_HOST", raising=False)
    with pytest.raises(RuntimeError, match="must be set together"):
        databricks.fetch()


def test_databricks_manifest_activates_only_models_with_passing_canaries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "databricks.json"
    manifest_path.write_text(
        databricks.MANIFEST_PATH.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    prices, discovered = databricks._parse_catalog(
        _pricing_html(),
        _supported_models_html(),
    )
    checked = set(discovered)
    healthy = {"openai/gpt-oss-20b", "meta-llama/llama-3.1-8b-instruct"}
    monkeypatch.setattr(databricks, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(databricks, "_DISCOVERED_MANIFEST_ROWS", discovered)
    monkeypatch.setattr(databricks, "_LIVE_CANARY_CHECKED_MODEL_IDS", checked)
    monkeypatch.setattr(databricks, "_LIVE_CANARY_HEALTHY_MODEL_IDS", healthy)
    monkeypatch.setenv("DATABRICKS_TOKEN", "token")
    monkeypatch.setenv("DATABRICKS_HOST", "dbc-1234.cloud.databricks.com")

    databricks.write_provider_manifest(
        ProviderPricingResult(
            slug="databricks",
            prices=prices,
            source="api",
            fetched_url=databricks.PRICING_URL,
        )
    )

    by_id = {
        row["id"]: row for row in json.loads(manifest_path.read_text(encoding="utf-8"))["models"]
    }
    for model_id in healthy:
        assert "routable_reason" not in by_id[model_id]
    for model_id in checked - healthy:
        assert by_id[model_id]["routable"] is False
        assert by_id[model_id]["routable_reason"] == "provider-canary-failed"


def test_databricks_is_prepaid_standard_privacy_and_not_byok() -> None:
    provider = PROVIDERS["databricks"]
    assert provider.supports_prepaid is True
    assert provider.supports_byok is False
    assert provider.stores_content is True
    assert provider.provider_zero_data_retention is False
    assert provider.provider_confidential_compute is False
    assert provider.provider_e2ee is False
    assert OPENAI_COMPATIBLE_PROVIDERS["databricks"][0] == ("DATABRICKS_TOKEN",)
    assert default_provider_secret_ref("databricks") == "env://DATABRICKS_TOKEN"
    assert "databricks" in PROVIDER_SLUGS


def test_provider_client_requires_workspace_host() -> None:
    client = ProviderClient({"DATABRICKS_TOKEN": "token"})
    with pytest.raises(RuntimeError, match="DATABRICKS_HOST is required"):
        client._provider_base_url("databricks", "unused")
