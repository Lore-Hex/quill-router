from __future__ import annotations

import json

import httpx
import pytest

import scripts.providers.sync_azure_foundry as sync
from scripts.pricing.providers import azure
from scripts.providers.sync_azure_foundry import (
    AzureManagementClient,
    DeploymentCandidate,
    _stream_text,
    canary_with_retries,
    select_deployment_candidates,
)
from trusted_router.catalog import MODEL_ENDPOINTS, PROVIDERS


def _price_row(
    *,
    product: str,
    name: str,
    price: str,
    unit: str = "1K",
) -> dict[str, object]:
    return {
        "productName": product,
        "skuName": name,
        "meterName": f"{name} Tokens",
        "retailPrice": price,
        "unitOfMeasure": unit,
    }


def _model(
    name: str,
    *,
    version: str = "1",
    default: bool = True,
    usage_name: str | None = None,
    lifecycle: str = "GenerallyAvailable",
    chat: str = "true",
    minimum_capacity: int | None = None,
) -> dict[str, object]:
    usage = usage_name or f"AIServices.GlobalStandard.{name}"
    return {
        "name": name,
        "version": version,
        "format": "DeepSeek",
        "isDefaultVersion": default,
        "lifecycleStatus": lifecycle,
        "capabilities": {"chatCompletion": chat},
        "skus": [
            {
                "name": "GlobalStandard",
                "usageName": usage,
                "capacity": {"minimum": minimum_capacity},
            }
        ],
    }


def _usage(name: str, *, limit: float = 20, current: float = 0) -> dict[str, object]:
    return {
        "name": {"value": name},
        "limit": limit,
        "currentValue": current,
    }


def test_azure_retail_prices_use_decimal_units_and_ignore_data_zone() -> None:
    rows = [
        _price_row(
            product="Azure Deepseek Models",
            name="V4 Flash Inp glbl",
            price="0.00019",
        ),
        _price_row(
            product="Azure Deepseek Models",
            name="V4 Flash Outp glbl",
            price="0.00051",
        ),
        _price_row(
            product="Azure Deepseek Models",
            name="V4 Flash cached glbl",
            price="0.000028",
        ),
        _price_row(
            product="Azure Deepseek Models",
            name="V4 Flash Inp DZ",
            price="0.00021",
        ),
        _price_row(
            product="Cohere Models",
            name="Command A Plus Inp Glbl",
            price="0.8",
            unit="1M",
        ),
        _price_row(
            product="Cohere Models",
            name="Command A Plus Outp Glbl",
            price="3.2",
            unit="1M",
        ),
    ]

    prices = azure.parse_retail_prices(rows)

    flash = prices["deepseek/deepseek-v4-flash"]
    assert flash.prompt_micro_per_m == 190_000
    assert flash.completion_micro_per_m == 510_000
    assert flash.tiers[0].prompt_cached_micro_per_m == 28_000
    command = prices["cohere/command-a-plus-05-2026"]
    assert command.prompt_micro_per_m == 800_000
    assert command.completion_micro_per_m == 3_200_000


def test_azure_retail_price_ambiguity_fails_closed() -> None:
    rows = [
        _price_row(
            product="Azure Deepseek Models",
            name="V4 Flash Inp glbl",
            price="0.00019",
        ),
        _price_row(
            product="Azure Deepseek Models",
            name="V4 Flash Input glbl",
            price="0.00020",
        ),
        _price_row(
            product="Azure Deepseek Models",
            name="V4 Flash Outp glbl",
            price="0.00051",
        ),
    ]

    try:
        azure.parse_retail_prices(rows)
    except ValueError as exc:
        assert "ambiguous Azure input price" in str(exc)
    else:
        raise AssertionError("ambiguous Azure prices must not be published")


def test_azure_candidate_selection_requires_chat_price_and_remaining_sync_quota() -> None:
    valid_usage = "AIServices.GlobalStandard.DeepSeek-V4-Flash"
    no_quota_usage = "AIServices.GlobalStandard.DeepSeek-V4-Pro"
    fine_tune_usage = "AIServices.GlobalStandard.Qwen3-32B-finetune"
    rows = [
        _model("DeepSeek-V4-Flash", version="1", default=False, usage_name=valid_usage),
        _model("DeepSeek-V4-Flash", version="2", default=True, usage_name=valid_usage),
        _model("DeepSeek-V4-Pro", usage_name=no_quota_usage),
        _model("qwen3-32b", usage_name=fine_tune_usage),
        _model("DeepSeek-V3.2", chat="false"),
        _model("Unknown-Model"),
    ]
    usage = [
        _usage(valid_usage, limit=20, current=1),
        _usage(no_quota_usage, limit=20, current=20),
        _usage(fine_tune_usage, limit=200, current=0),
    ]

    selected = select_deployment_candidates(
        rows,
        usage,
        frozenset(
            {
                "deepseek/deepseek-v4-flash",
                "deepseek/deepseek-v4-pro",
                "deepseek/deepseek-v3.2",
            }
        ),
    )

    assert len(selected) == 1
    assert selected[0].canonical_id == "deepseek/deepseek-v4-flash"
    assert selected[0].version == "2"
    assert selected[0].deployment_name == "deepseek-v4-flash"
    assert selected[0].capacity == 1


def test_azure_candidate_selection_uses_newest_version_without_default() -> None:
    usage_name = "AIServices.GlobalStandard.Kimi-K2.7-Code"
    rows = [
        _model("Kimi-K2.7-Code", version="2026-06-01", default=False, usage_name=usage_name),
        _model("Kimi-K2.7-Code", version="2026-06-12", default=False, usage_name=usage_name),
    ]

    selected = select_deployment_candidates(
        rows,
        [_usage(usage_name)],
        frozenset({"moonshotai/kimi-k2.7-code"}),
    )

    assert [candidate.version for candidate in selected] == ["2026-06-12"]


def test_azure_candidate_selection_requires_enough_quota_for_minimum_capacity() -> None:
    usage_name = "AIServices.GlobalStandard.Kimi-K2.7-Code"
    selected = select_deployment_candidates(
        [_model("Kimi-K2.7-Code", usage_name=usage_name, minimum_capacity=10)],
        [_usage(usage_name, limit=10, current=1)],
        frozenset({"moonshotai/kimi-k2.7-code"}),
    )

    assert selected == []


def test_azure_canonical_ids_are_stable_provider_independent_ids() -> None:
    assert azure.canonical_model_id("DeepSeek-V4-Flash") == ("deepseek/deepseek-v4-flash")
    assert azure.canonical_model_id("claude-opus-5") == "anthropic/claude-opus-5"
    assert azure.canonical_model_id("Kimi-K2.7-Code") == "moonshotai/kimi-k2.7-code"
    assert azure.canonical_model_id("unknown") is None


def test_azure_stream_canary_parses_openai_and_anthropic_sse() -> None:
    openai_lines = [
        'data: {"choices":[{"delta":{"content":"PO"}}]}',
        'data: {"choices":[{"delta":{"content":"NG"}}]}',
        "data: [DONE]",
    ]
    anthropic_lines = [
        "event: content_block_delta",
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"PONG"}}',
    ]

    assert _stream_text(openai_lines, protocol="openai") == "PONG"
    assert _stream_text(anthropic_lines, protocol="anthropic") == "PONG"


def test_non_anthropic_azure_models_do_not_require_marketplace_preflight() -> None:
    candidate = DeploymentCandidate(
        canonical_id="deepseek/deepseek-v4-flash",
        native_name="DeepSeek-V4-Flash",
        version="1",
        model_format="DeepSeek",
        deployment_name="deepseek-v4-flash",
        sku="GlobalStandard",
        capacity=1,
        is_default_version=True,
    )
    client = object.__new__(AzureManagementClient)

    assert client.marketplace_terms_accepted(candidate) is True


def test_azure_canary_retries_only_transient_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = DeploymentCandidate(
        canonical_id="deepseek/deepseek-v4-flash",
        native_name="DeepSeek-V4-Flash",
        version="1",
        model_format="DeepSeek",
        deployment_name="deepseek-v4-flash",
        sku="GlobalStandard",
        capacity=1,
        is_default_version=True,
    )
    attempts = 0

    def fake_canary(candidate: DeploymentCandidate, *, account_key: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise httpx.ConnectError("temporary")

    monkeypatch.setattr(sync, "canary", fake_canary)
    monkeypatch.setattr(sync.time, "sleep", lambda _seconds: None)

    canary_with_retries(candidate, account_key="test")
    assert attempts == 3


def test_azure_canary_does_not_retry_no_first_byte_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = DeploymentCandidate(
        canonical_id="microsoft/phi-4-multimodal-instruct",
        native_name="Phi-4-multimodal-instruct",
        version="1",
        model_format="Microsoft",
        deployment_name="phi-4-multimodal-instruct",
        sku="GlobalStandard",
        capacity=1,
        is_default_version=True,
    )
    attempts = 0

    def fake_canary(candidate: DeploymentCandidate, *, account_key: str) -> None:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("no first byte")

    monkeypatch.setattr(sync, "canary", fake_canary)

    with pytest.raises(httpx.ReadTimeout):
        canary_with_retries(candidate, account_key="test")
    assert attempts == 1


def test_azure_manifest_registers_prepaid_only_gateway_routes() -> None:
    raw = json.loads(azure.MANIFEST_PATH.read_text(encoding="utf-8"))
    rows = raw["models"]

    assert PROVIDERS["azure"].supports_prepaid is True
    assert PROVIDERS["azure"].supports_byok is False
    assert len(rows) >= 20
    for row in rows:
        model_id = row["id"]
        endpoint = MODEL_ENDPOINTS[f"{model_id}@azure/prepaid"]
        assert endpoint.provider == "azure"
        assert endpoint.usage_type == "Credits"
        assert endpoint.upstream_id == row["upstream_id"]
        assert endpoint.prompt_price_microdollars_per_million_tokens > 0
        assert endpoint.completion_price_microdollars_per_million_tokens > 0
        assert f"{model_id}@azure/byok" not in MODEL_ENDPOINTS
