from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from scripts.check_price_coverage import _DISCOVERABLE_MANIFEST_PROVIDERS
from scripts.pricing.provider_contract_catalog import (
    discover_provider_contract_catalog,
)
from scripts.pricing.providers import neurometric
from trusted_router.catalog import MODEL_ENDPOINTS, MODELS, PROVIDERS, model_open_weights
from trusted_router.provider_contract import PROVIDER_CATALOG_V2_EXAMPLE


def _model_row(
    model_id: str = "ibm-granite/granite-4.1-8b",
    *,
    status: str = "active",
) -> dict[str, Any]:
    return {
        "id": model_id,
        "object": "model",
        "owned_by": model_id.split("/", 1)[0],
        "name": "Granite 4.1 8B",
        "type": "chat",
        "context_length": 32768,
        "max_output_tokens": 16384,
        "endpoints": ["chat/completions"],
        "input_modalities": ["text"],
        "output_modalities": ["text"],
        "capabilities": {
            "streaming": True,
            "tools": True,
            "structured_output": True,
            "reasoning": False,
            "prompt_caching": False,
        },
        "pricing": {
            "currency": "USD",
            "unit": "per_1m_tokens",
            "input": "0.050000",
            "output": "0.100000",
            "cached_input": None,
            "cache_write": None,
            "minimum_request": "0",
        },
        "lifecycle": {
            "status": status,
            "deprecation_at": None,
            "retirement_at": None,
            "replacement_model_id": None,
        },
    }


def _payload(*rows: dict[str, Any]) -> dict[str, Any]:
    return {"object": "list", "data": list(rows)}


def test_canonical_contract_parser_preserves_exact_price_and_capabilities() -> None:
    upstream_ids: dict[str, str] = {}
    prices, discovered = discover_provider_contract_catalog(
        _payload(_model_row()),
        upstream_id_map=upstream_ids,
    )

    price = prices["ibm-granite/granite-4.1-8b"]
    assert price.prompt_micro_per_m == 50_000
    assert price.completion_micro_per_m == 100_000
    assert upstream_ids == {
        "ibm-granite/granite-4.1-8b": "ibm-granite/granite-4.1-8b"
    }
    row = discovered["ibm-granite/granite-4.1-8b"]
    assert row["routable"] is True
    assert row["supported_features"] == [
        "chat",
        "completion",
        "tools",
        "json_mode",
        "structured_outputs",
    ]


def test_reliability_contract_v2_preserves_deadlines_and_error_contract() -> None:
    payload = copy.deepcopy(PROVIDER_CATALOG_V2_EXAMPLE)

    _prices, discovered = discover_provider_contract_catalog(
        payload,
        upstream_id_map={},
    )

    row = discovered["acme/atlas-70b"]
    assert row["reliability"]["first_token_timeout_seconds"] == 20
    assert row["reliability"]["completion_timeout_seconds"] == 120
    assert row["provider_reliability"]["request_id_header"] == "x-request-id"
    assert row["provider_reliability"]["account_quota_error_codes"] == [
        "account_quota_exceeded"
    ]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda row: row.__setitem__("unexpected", True), "fields invalid"),
        (
            lambda row: row["pricing"].__setitem__("minimum_request", "0.01"),
            "minimum_request is not supported",
        ),
        (
            lambda row: row["pricing"].__setitem__("input", "0.0000001"),
            "exceeds microdollar-per-million precision",
        ),
        (
            lambda row: row["capabilities"].__setitem__("prompt_caching", True),
            "must match pricing.cached_input",
        ),
    ],
)
def test_canonical_contract_parser_fails_closed(
    mutation: Any,
    message: str,
) -> None:
    row = _model_row()
    mutation(row)

    with pytest.raises(RuntimeError, match=message):
        discover_provider_contract_catalog(
            _payload(row),
            upstream_id_map={},
        )


def test_canonical_contract_parser_excludes_retired_models() -> None:
    prices, discovered = discover_provider_contract_catalog(
        _payload(_model_row(status="retired")),
        upstream_id_map={},
    )

    assert prices == {}
    assert discovered == {}


def test_neurometric_fetch_discovers_new_models_and_runs_canary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        _model_row(),
        _model_row("qwen/qwen3-vl-8b-instruct"),
    ]

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return _payload(*rows)

    class FakeClient:
        def __init__(self, **_kwargs: object) -> None:
            return None

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def get(self, *_args: object, **_kwargs: object) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setenv("NEUROMETRIC_API_KEY", "test-key")
    monkeypatch.setattr(neurometric.httpx, "Client", FakeClient)
    monkeypatch.setattr(neurometric, "probe_openai_chat", lambda **_kwargs: True)

    result = neurometric.fetch()

    assert set(result.prices) == {
        "ibm-granite/granite-4.1-8b",
        "qwen/qwen3-vl-8b-instruct",
    }
    assert set(neurometric._DISCOVERED_MANIFEST_ROWS) == set(result.prices)
    assert neurometric._LIVE_CANARY_OK is True


def test_neurometric_manifest_tombstones_only_after_repeated_fresh_miss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = tmp_path / "neurometric.json"
    raw = json.loads(neurometric.MANIFEST_PATH.read_text(encoding="utf-8"))
    manifest_path.write_text(json.dumps(raw) + "\n", encoding="utf-8")
    target_id = "qwen/qwen3-vl-8b-thinking"
    existing_ids = {
        row["id"]
        for row in raw["models"]
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    assert target_id in existing_ids
    # Keep every other live row present. This test exercises one repeated
    # delisting and must not become a mass-prune test when Neurometric adds
    # unrelated models to its live catalog.
    payload = _payload(
        *(_model_row(model_id) for model_id in sorted(existing_ids - {target_id}))
    )

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return copy.deepcopy(payload)

    class FakeClient:
        def __init__(self, **_kwargs: object) -> None:
            return None

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def get(self, *_args: object, **_kwargs: object) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setenv("NEUROMETRIC_API_KEY", "test-key")
    monkeypatch.setattr(neurometric, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(neurometric.httpx, "Client", FakeClient)
    monkeypatch.setattr(neurometric, "probe_openai_chat", lambda **_kwargs: True)

    result = neurometric.fetch()
    neurometric.write_provider_manifest(result)
    first = json.loads(manifest_path.read_text(encoding="utf-8"))
    first_rows = {row["id"]: row for row in first["models"]}
    assert first_rows[target_id]["missing_since"]
    assert first_rows[target_id].get("routable") is not False

    result = neurometric.fetch()
    neurometric.write_provider_manifest(result)
    second = json.loads(manifest_path.read_text(encoding="utf-8"))
    second_rows = {row["id"]: row for row in second["models"]}
    assert second_rows[target_id]["routable"] is False
    assert second_rows[target_id]["routable_reason"] == "delisted-upstream"


def test_neurometric_catalog_routes_are_prepaid_only_and_no_store() -> None:
    provider = PROVIDERS["neurometric"]
    assert provider.supports_prepaid is True
    assert provider.supports_byok is False
    assert provider.stores_content is False
    assert provider.provider_zero_data_retention is False
    assert provider.provider_confidential_compute is False
    assert provider.provider_e2ee is False

    assert "ibm-granite/granite-4.1-8b" in MODELS
    assert model_open_weights(MODELS["ibm-granite/granite-4.1-8b"]) is True
    endpoints = [
        endpoint
        for endpoint in MODEL_ENDPOINTS.values()
        if endpoint.provider == "neurometric"
    ]
    assert endpoints
    assert {endpoint.usage_type for endpoint in endpoints} == {"Credits"}
    assert {
        endpoint.upstream_id
        for endpoint in endpoints
    } >= {
        "ibm-granite/granite-4.1-8b",
        "qwen/qwen3-vl-8b-instruct",
        "qwen/qwen3-vl-8b-thinking",
    }


def test_neurometric_public_api_exposes_provider_and_exact_endpoint(client: Any) -> None:
    providers = {
        row["id"]: row for row in client.get("/v1/providers").json()["data"]
    }
    provider = providers["neurometric"]
    assert provider["name"] == "Neurometric AI"
    assert provider["supports_prepaid"] is True
    assert provider["supports_byok"] is False
    assert provider["stores_content"] is False
    assert provider["provider_zero_data_retention"] is False
    assert provider["provider_confidential_compute"] is False

    response = client.get(
        "/v1/models/ibm-granite/granite-4.1-8b/endpoints"
    )
    assert response.status_code == 200
    endpoints = response.json()["data"]
    neurometric = next(
        endpoint for endpoint in endpoints if endpoint["provider_name"] == "Neurometric AI"
    )
    assert neurometric["upstream_id"] == "ibm-granite/granite-4.1-8b"
    assert neurometric["pricing"]["prompt"] == "0.00000005275"
    assert neurometric["pricing"]["completion"] == "0.0000001055"


def test_neurometric_hourly_refresh_and_secret_wiring_are_complete() -> None:
    discoverable = {
        slug: (url, env_names)
        for slug, url, env_names, _normalize in _DISCOVERABLE_MANIFEST_PROVIDERS
    }
    assert discoverable["neurometric"] == (
        "https://wharf.neurometric.ai/v1/models",
        ("NEUROMETRIC_API_KEY",),
    )

    root = Path(__file__).resolve().parents[1]
    secrets = (root / "scripts/deploy/secrets.sh").read_text(encoding="utf-8")
    workflow = (root / ".github/workflows/refresh-prices.yml").read_text(
        encoding="utf-8"
    )
    assert (
        'ensure_secret_from_env_file "NEUROMETRIC_API_KEY" '
        '"trustedrouter-neurometric-api-key"'
    ) in secrets
    assert (
        "trustedrouter-neurometric-api-key"
        in secrets.split("DETACHED_PROVIDER_SECRET_NAMES=(", 1)[1].split(")", 1)[0]
    )
    assert (
        "NEUROMETRIC_API_KEY:trustedrouter-neurometric-api-key"
        in workflow
    )
