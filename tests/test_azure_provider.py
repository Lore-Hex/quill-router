from __future__ import annotations

import base64
import json
import struct
import zlib
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

import scripts.providers.sync_azure_foundry as sync
from scripts.pricing.providers import azure
from scripts.providers.sync_azure_foundry import (
    DEPLOYMENT_VERSION_UPGRADE_OPTION,
    AzureManagementClient,
    DeploymentCandidate,
    _stream_text,
    canary,
    canary_with_retries,
    deployment_needs_reconcile,
    select_deployment_candidates,
    write_manifest,
)
from scripts.smoke_all_providers import PROBES
from trusted_router.catalog import MODEL_ENDPOINTS, PROVIDERS

_EXPECTED_AZURE_LAUNCH_IDS = frozenset(
    {
        "cohere/command-a",
        "moonshotai/kimi-k2.5",
        "moonshotai/kimi-k2.6",
        "moonshotai/kimi-k2.7-code",
        "openai/gpt-5-mini",
        "x-ai/grok-4.1-fast-non-reasoning",
        "x-ai/grok-4.1-fast-reasoning",
        "x-ai/grok-4.20-non-reasoning",
        "x-ai/grok-4.20-reasoning",
    }
)
_EXPECTED_AZURE_HOLD_IDS = frozenset(
    {
        "cohere/command-a-plus-05-2026",
        "deepseek/deepseek-v3.2",
        "deepseek/deepseek-v3.2-speciale",
        "deepseek/deepseek-v4-flash",
        "deepseek/deepseek-v4-pro",
        "meta-llama/llama-3.3-70b-instruct",
        "meta-llama/llama-4-maverick",
        "microsoft/phi-4",
        "microsoft/phi-4-mini-instruct",
        "microsoft/phi-4-mini-reasoning",
        "microsoft/phi-4-multimodal-instruct",
        "microsoft/phi-4-reasoning",
        "mistralai/codestral-2501",
        "mistralai/mistral-large-3",
        "openai/gpt-5.4-mini",
        "openai/gpt-oss-120b",
        "x-ai/grok-4.3",
    }
)


def _canary_candidate(
    model_id: str,
    *,
    model_format: str = "OpenAI",
) -> DeploymentCandidate:
    native_name = model_id.split("/", 1)[1]
    return DeploymentCandidate(
        canonical_id=model_id,
        native_name=native_name,
        version="1",
        model_format=model_format,
        deployment_name=native_name.replace(".", "-"),
        sku="GlobalStandard",
        capacity=sync.MINIMUM_LAUNCH_CAPACITY,
        is_default_version=True,
    )


def _openai_tool_response(
    *,
    finish_reason: str = "tool_calls",
    call_id: object = "call_1",
    call_type: object = "function",
    name: object = "pong",
    arguments: object = "{}",
) -> dict[str, object]:
    return {
        "choices": [
            {
                "finish_reason": finish_reason,
                "message": {
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": call_type,
                            "function": {"name": name, "arguments": arguments},
                        }
                    ]
                },
            }
        ]
    }


def _http_status_error(
    status_code: int,
    *,
    retry_after: str | None = None,
) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://azure.example.test/chat/completions")
    headers = {"Retry-After": retry_after} if retry_after is not None else None
    response = httpx.Response(status_code, headers=headers, request=request)
    return httpx.HTTPStatusError(
        f"HTTP {status_code}",
        request=request,
        response=response,
    )


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


def _minimum_retail_rows(model_id: str) -> list[dict[str, object]]:
    rule = azure._RETAIL_RULES[model_id]  # noqa: SLF001
    tier_rules = rule.price_tiers or (azure.RetailTierRule(None),)
    rows: list[dict[str, object]] = []
    for index, tier_rule in enumerate(tier_rules):
        qualifier = " ".join(tier_rule.required_words)
        suffix = f" {qualifier}" if qualifier else ""
        rows.extend(
            [
                _price_row(
                    product=rule.product,
                    name=f"{rule.stems[0]} Inp Glbl{suffix}",
                    price=("0.001" if index == 0 else "0.002"),
                ),
                _price_row(
                    product=rule.product,
                    name=f"{rule.stems[0]} Outp Glbl{suffix}",
                    price=("0.003" if index == 0 else "0.004"),
                ),
            ]
        )
        if rule.require_cached or tier_rule.require_cached:
            rows.append(
                _price_row(
                    product=rule.product,
                    name=f"{rule.stems[0]} Cached Inp Glbl{suffix}",
                    price=("0.0001" if index == 0 else "0.0002"),
                )
            )
    return rows


@pytest.fixture
def deepseek_v4_flash_retail_rows() -> list[dict[str, object]]:
    return [
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
            name="V4 Flash 0731 Inp glbl",
            price="0.00044",
        ),
        _price_row(
            product="Azure Deepseek Models",
            name="V4 Flash 0731 Outp glbl",
            price="0.00132",
        ),
        _price_row(
            product="Azure Deepseek Models",
            name="V4 Flash 0731 cached glbl",
            price="0.000014",
        ),
        _price_row(
            product="Azure Deepseek Models",
            name="V4 Flash Inp DZ",
            price="0.00021",
        ),
    ]


@pytest.fixture
def grok_43_retail_rows() -> list[dict[str, object]]:
    return [
        _price_row(
            product="Azure Grok Models",
            name="4.3 Inp Glbl",
            price="0.00125",
        ),
        _price_row(
            product="Azure Grok Models",
            name="4.3 Outp Glbl",
            price="0.0025",
        ),
        _price_row(
            product="Azure Grok Models",
            name="4.3 Cached Inp Glbl",
            price="0.0002",
        ),
        _price_row(
            product="Azure Grok Models",
            name="4.3 Inp Glbl L",
            price="0.0025",
        ),
        _price_row(
            product="Azure Grok Models",
            name="4.3 Outp Glbl L",
            price="0.005",
        ),
        _price_row(
            product="Azure Grok Models",
            name="4.3 Cached Inp Glbl L",
            price="0.0004",
        ),
        _price_row(
            product="Azure Grok Models",
            name="4.3 Inp DZ L",
            price="0.00275",
        ),
    ]


def test_azure_retail_parser_keeps_base_checkpoint_separate_and_ignores_data_zone(
    deepseek_v4_flash_retail_rows: list[dict[str, object]],
) -> None:
    rows = [
        *deepseek_v4_flash_retail_rows,
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
    assert "anthropic/claude-opus-5" not in prices


def test_azure_grok_43_uses_live_short_and_long_context_meters(
    grok_43_retail_rows: list[dict[str, object]],
) -> None:
    grok = azure.parse_retail_prices(grok_43_retail_rows)["x-ai/grok-4.3"]

    assert [
        (
            tier.max_prompt_tokens,
            tier.prompt_micro_per_m,
            tier.completion_micro_per_m,
            tier.prompt_cached_micro_per_m,
        )
        for tier in grok.tiers
    ] == [
        (200_000, 1_250_000, 2_500_000, 200_000),
        (None, 2_500_000, 5_000_000, 400_000),
    ]


@pytest.mark.parametrize(
    "missing_name",
    ["4.3 Outp Glbl L", "4.3 Cached Inp Glbl L"],
)
def test_azure_grok_43_incomplete_long_context_meters_fail_closed(
    grok_43_retail_rows: list[dict[str, object]],
    missing_name: str,
) -> None:
    rows = [
        row
        for row in grok_43_retail_rows
        if row.get("skuName") != missing_name
    ]

    prices = azure.parse_retail_prices(rows)

    assert "x-ai/grok-4.3" not in prices


def test_azure_production_holds_do_not_block_healthy_routes() -> None:
    healthy_model_id = "cohere/command-a"
    rows = _minimum_retail_rows(healthy_model_id)
    model_versions = {
        healthy_model_id: azure._ALLOWED_MODEL_VERSIONS[healthy_model_id][0],  # noqa: SLF001
    }
    for model_id in _EXPECTED_AZURE_HOLD_IDS:
        rows.extend(_minimum_retail_rows(model_id))
        model_versions[model_id] = azure._ALLOWED_MODEL_VERSIONS[model_id][0]  # noqa: SLF001

    prices = azure.parse_retail_prices(
        rows,
        model_versions=model_versions,
    )

    configured_holds = frozenset(
        model_id
        for model_id, rule in azure._RETAIL_RULES.items()  # noqa: SLF001
        if rule.production_hold_reason is not None
    )
    assert set(prices) == {healthy_model_id}
    assert configured_holds == _EXPECTED_AZURE_HOLD_IDS
    assert configured_holds.isdisjoint(azure.retail_model_ids())


def test_azure_live_tool_failures_have_evidence_specific_production_holds() -> None:
    expected_reasons = {
        "cohere/command-a-plus-05-2026": "openai-tool-call-response-nonconformant",
        "mistralai/mistral-large-3": "openai-named-tool-choice-unsupported",
        "openai/gpt-oss-120b": "openai-tool-use-unsupported",
    }

    assert {
        model_id: azure._RETAIL_RULES[model_id].production_hold_reason  # noqa: SLF001
        for model_id in expected_reasons
    } == expected_reasons


def test_azure_phi_reasoning_does_not_attach_plus_sibling_meters() -> None:
    base_rows = [
        _price_row(
            product="Azure Phi Models",
            name="Phi-4-reasoning-Input",
            price="0.000125",
        ),
        _price_row(
            product="Azure Phi Models",
            name="Phi-4-reasoning-Output",
            price="0.0005",
        ),
    ]
    plus_rows = [
        _price_row(
            product="Azure Phi Models",
            name="Phi-4-reasoning-plus-input",
            price="0.000125",
        ),
        _price_row(
            product="Azure Phi Models",
            name="Phi-4-reasoning-plus-output",
            price="0.0005",
        ),
    ]

    prices = azure.parse_retail_prices([*base_rows, *plus_rows])
    phi = prices["microsoft/phi-4-reasoning"]
    assert phi.prompt_micro_per_m == 125_000
    assert phi.completion_micro_per_m == 500_000
    assert "microsoft/phi-4-reasoning" not in azure.parse_retail_prices(plus_rows)


def test_azure_grok_41_fast_requires_its_proven_meter_family() -> None:
    current_rows = [
        _price_row(
            product="Azure Grok Models",
            name="Grok 4.1 Inp Glbl",
            price="0.0002",
        ),
        _price_row(
            product="Azure Grok Models",
            name="Grok 4.1 Outp Glbl",
            price="0.0005",
        ),
    ]
    legacy_rows = [
        _price_row(
            product="Azure Grok Models",
            name="Grok4 Fast Inp glbl",
            price="0.0002",
        ),
        _price_row(
            product="Azure Grok Models",
            name="Grok4 Fast Outp glbl",
            price="0.0005",
        ),
    ]

    prices = azure.parse_retail_prices([*current_rows, *legacy_rows])
    for model_id in (
        "x-ai/grok-4.1-fast-non-reasoning",
        "x-ai/grok-4.1-fast-reasoning",
    ):
        assert prices[model_id].prompt_micro_per_m == 200_000
        assert prices[model_id].completion_micro_per_m == 500_000
        assert model_id not in azure.parse_retail_prices(legacy_rows)


def test_azure_0731_meters_remain_a_distinct_unpublished_checkpoint(
    deepseek_v4_flash_retail_rows: list[dict[str, object]],
) -> None:
    rows = [
        row
        for row in deepseek_v4_flash_retail_rows
        if "0731" in str(row.get("skuName", ""))
    ]
    rates = {
        str(row["skuName"]): azure._micro_per_million(row)  # noqa: SLF001
        for row in rows
    }
    assert rates == {
        "V4 Flash 0731 Inp glbl": 440_000,
        "V4 Flash 0731 Outp glbl": 1_320_000,
        "V4 Flash 0731 cached glbl": 14_000,
    }

    prices = azure.parse_retail_prices(
        rows,
        model_versions={"deepseek/deepseek-v4-flash": "2026-07-31"},
    )

    assert "deepseek/deepseek-v4-flash" not in prices


def test_azure_retail_price_unknown_version_ambiguity_fails_closed(
    deepseek_v4_flash_retail_rows: list[dict[str, object]],
) -> None:
    prices = azure.parse_retail_prices(
        deepseek_v4_flash_retail_rows,
        model_versions={"deepseek/deepseek-v4-flash": "2026-08-20"},
    )

    assert "deepseek/deepseek-v4-flash" not in prices


def test_azure_retail_price_unknown_version_with_only_0731_fails_closed(
    deepseek_v4_flash_retail_rows: list[dict[str, object]],
) -> None:
    rows = [
        row
        for row in deepseek_v4_flash_retail_rows
        if "0731" in str(row.get("skuName", ""))
    ]

    prices = azure.parse_retail_prices(
        rows,
        model_versions={"deepseek/deepseek-v4-flash": "2026-08-20"},
    )

    assert "deepseek/deepseek-v4-flash" not in prices


def test_azure_retail_price_same_version_meter_ambiguity_fails_closed() -> None:
    rows = [
        _price_row(
            product="Cohere Models",
            name="Command A Inp Glbl",
            price="0.0025",
        ),
        _price_row(
            product="Cohere Models",
            name="Command A Input Glbl",
            price="0.0026",
        ),
        _price_row(
            product="Cohere Models",
            name="Command A Outp Glbl",
            price="0.01",
        ),
    ]

    with pytest.raises(ValueError, match="ambiguous Azure input price"):
        azure.parse_retail_prices(
            rows,
            model_versions={"cohere/command-a": "1"},
        )


def test_azure_distinct_equal_rate_meters_are_ambiguous() -> None:
    rows = [
        _price_row(
            product="Cohere Models",
            name="Command A Inp Glbl",
            price="0.0025",
        ),
        _price_row(
            product="Cohere Models",
            name="Command A Input Glbl",
            price="0.0025",
        ),
        _price_row(
            product="Cohere Models",
            name="Command A Outp Glbl",
            price="0.01",
        ),
    ]

    with pytest.raises(ValueError, match="ambiguous Azure input price"):
        azure.parse_retail_prices(
            rows,
            model_versions={"cohere/command-a": "1"},
        )


def test_azure_absent_model_ambiguity_does_not_block_other_prices() -> None:
    rows = [
        _price_row(
            product="Azure Mistral Models",
            name="Large 3 Inp Glbl",
            price="0.0005",
        ),
        _price_row(
            product="Azure Mistral Models",
            name="Large 3 Input Glbl",
            price="0.0006",
        ),
        _price_row(
            product="Cohere Models",
            name="Command A Inp Glbl",
            price="2.5",
            unit="1M",
        ),
        _price_row(
            product="Cohere Models",
            name="Command A Outp Glbl",
            price="10",
            unit="1M",
        ),
    ]

    prices = azure.parse_retail_prices(
        rows,
        model_versions={"cohere/command-a": "1"},
    )

    assert "mistralai/mistral-large-3" not in prices
    assert prices["cohere/command-a"].prompt_micro_per_m == 2_500_000


@pytest.mark.parametrize(
    ("model_id", "known_version"),
    sorted(
        (model_id, versions[0])
        for model_id, versions in azure._ALLOWED_MODEL_VERSIONS.items()  # noqa: SLF001
    ),
)
def test_azure_every_retail_rule_requires_its_exact_model_version(
    model_id: str,
    known_version: str,
) -> None:
    rows = _minimum_retail_rows(model_id)

    known = azure.parse_retail_prices(
        rows,
        model_versions={model_id: known_version},
    )
    unknown = azure.parse_retail_prices(
        rows,
        model_versions={model_id: "2099-12-31"},
    )
    absent = azure.parse_retail_prices(rows, model_versions={})

    rule = azure._RETAIL_RULES[model_id]  # noqa: SLF001
    if rule.production_hold_reason is None:
        assert set(known) == {model_id}
    else:
        assert known == {}
    assert unknown == {}
    assert absent == {}


@pytest.mark.parametrize(
    "model_id",
    sorted(
        model_id
        for model_id, rule in azure._RETAIL_RULES.items()  # noqa: SLF001
        if rule.require_cached or any(tier.require_cached for tier in rule.price_tiers)
    ),
)
def test_azure_cache_meter_contract_fails_closed_when_meter_disappears(
    model_id: str,
) -> None:
    rows = [
        row
        for row in _minimum_retail_rows(model_id)
        if "cached" not in str(row.get("skuName", "")).lower()
    ]

    prices = azure.parse_retail_prices(rows)

    assert model_id not in prices


def test_azure_manifest_versions_match_retail_contract() -> None:
    raw = json.loads(azure.MANIFEST_PATH.read_text(encoding="utf-8"))

    assert {row["id"] for row in raw["models"]} == _EXPECTED_AZURE_LAUNCH_IDS
    for row in raw["models"]:
        model_id = row["id"]
        assert row["azure_model_version"] in azure._ALLOWED_MODEL_VERSIONS[model_id]  # noqa: SLF001


def test_azure_fetch_uses_current_pricing_validation_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_id = "cohere/command-a"
    monkeypatch.setattr(
        azure,
        "fetch_retail_rows",
        lambda: _minimum_retail_rows(model_id),
    )

    result = azure.fetch()

    assert result.slug == "azure"
    assert result.prices[model_id].prompt_micro_per_m == 1_000_000


def test_azure_fetch_rejects_an_empty_retail_feed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(azure, "fetch_retail_rows", lambda: [])

    with pytest.raises(RuntimeError, match="empty pricing dict"):
        azure.fetch()


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
    assert selected[0].capacity == sync.MINIMUM_LAUNCH_CAPACITY


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


def test_azure_candidate_selection_prefers_allowed_checkpoint_over_new_default() -> None:
    usage_name = "AIServices.GlobalStandard.Kimi-K2.7-Code"
    allowed = _model(
        "Kimi-K2.7-Code",
        version="2026-06-12",
        default=False,
        usage_name=usage_name,
    )
    unknown_default = _model(
        "Kimi-K2.7-Code",
        version="2099-12-31",
        default=True,
        usage_name=usage_name,
    )
    version_contract = azure.retail_model_versions()

    selected = select_deployment_candidates(
        [allowed, unknown_default],
        [_usage(usage_name)],
        frozenset(version_contract),
        allowed_versions=version_contract,
    )
    unknown_only = select_deployment_candidates(
        [unknown_default],
        [_usage(usage_name)],
        frozenset(version_contract),
        allowed_versions=version_contract,
    )

    assert [candidate.version for candidate in selected] == ["2026-06-12"]
    assert unknown_only == []


def test_azure_candidate_selection_requires_uniform_launch_capacity_quota() -> None:
    usage_name = "AIServices.GlobalStandard.Kimi-K2.7-Code"
    selected = select_deployment_candidates(
        [_model("Kimi-K2.7-Code", usage_name=usage_name)],
        [_usage(usage_name, limit=10, current=1)],
        frozenset({"moonshotai/kimi-k2.7-code"}),
    )

    assert selected == []


def test_azure_candidate_selection_honors_higher_catalog_minimum_capacity() -> None:
    usage_name = "AIServices.GlobalStandard.Kimi-K2.7-Code"
    selected = select_deployment_candidates(
        [_model("Kimi-K2.7-Code", usage_name=usage_name, minimum_capacity=25)],
        [_usage(usage_name, limit=30)],
        frozenset({"moonshotai/kimi-k2.7-code"}),
    )

    assert len(selected) == 1
    assert selected[0].capacity == 25


@pytest.mark.parametrize("minimum", [-1, True, 1.5, "10"])
def test_azure_candidate_selection_fails_closed_on_malformed_catalog_minimum(
    minimum: object,
) -> None:
    usage_name = "AIServices.GlobalStandard.Kimi-K2.7-Code"
    model = _model("Kimi-K2.7-Code", usage_name=usage_name)
    sku = model["skus"][0]
    assert isinstance(sku, dict)
    capacity = sku["capacity"]
    assert isinstance(capacity, dict)
    capacity["minimum"] = minimum

    selected = select_deployment_candidates(
        [model],
        [_usage(usage_name, limit=100)],
        frozenset({"moonshotai/kimi-k2.7-code"}),
    )

    assert selected == []


@pytest.mark.parametrize("remaining", [float("nan"), float("inf")])
def test_azure_candidate_selection_fails_closed_on_nonfinite_quota(
    remaining: float,
) -> None:
    usage_name = "AIServices.GlobalStandard.Kimi-K2.7-Code"

    assert sync._choose_sku(  # noqa: SLF001
        _model("Kimi-K2.7-Code", usage_name=usage_name),
        {usage_name: remaining},
    ) is None


def test_azure_candidate_selection_rejects_skus_without_matching_prices() -> None:
    usage_name = "AIServices.DataZoneStandard.gpt-5.4-mini"
    model = _model("gpt-5.4-mini", usage_name=usage_name)
    sku = model["skus"][0]
    assert isinstance(sku, dict)
    sku["name"] = "DataZoneStandard"

    selected = select_deployment_candidates(
        [model],
        [_usage(usage_name)],
        frozenset({"openai/gpt-5.4-mini"}),
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
        (
            'data: {"choices":[],"usage":{"prompt_tokens":4,'
            '"completion_tokens":1,"total_tokens":5}}'
        ),
        "data: [DONE]",
    ]
    anthropic_lines = [
        "event: content_block_delta",
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"PONG"}}',
    ]

    assert _stream_text(openai_lines, protocol="openai") == "PONG"
    assert _stream_text(anthropic_lines, protocol="anthropic") == "PONG"


def test_azure_openai_stream_consumes_terminal_usage_after_pong() -> None:
    consumed: list[str] = []

    def lines() -> Iterator[str]:
        values = [
            'data: {"choices":[{"delta":{"content":"PONG"}}]}',
            (
                'data: {"choices":[],"usage":{"prompt_tokens":7,'
                '"completion_tokens":3,"total_tokens":10}}'
            ),
            "data: [DONE]",
        ]
        for value in values:
            consumed.append(value)
            yield value

    text, usage, saw_done = sync._stream_canary(lines(), protocol="openai")  # noqa: SLF001

    assert len(consumed) == 3
    assert text == "PONG"
    assert usage == {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
    assert saw_done is True


@pytest.mark.parametrize(
    "usage",
    [
        {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        {
            "prompt_tokens": 7,
            "completion_tokens": 0,
            "total_tokens": 12,
            "completion_tokens_details": {"reasoning_tokens": 5},
        },
        {"prompt_tokens": 7, "total_tokens": 12},
    ],
)
def test_azure_openai_stream_usage_accepts_positive_direct_or_computed_output(
    usage: dict[str, object],
) -> None:
    sync._validate_openai_stream_usage(  # noqa: SLF001
        usage,
        saw_done=True,
        deployment_name="test",
    )


@pytest.mark.parametrize(
    ("usage", "saw_done"),
    [
        (None, True),
        ({}, True),
        ({"prompt_tokens": True, "completion_tokens": 1}, True),
        ({"prompt_tokens": 0, "completion_tokens": 1}, True),
        ({"prompt_tokens": "7", "completion_tokens": 1}, True),
        ({"prompt_tokens": 7, "completion_tokens": True}, True),
        ({"prompt_tokens": 7, "completion_tokens": -1}, True),
        ({"prompt_tokens": 7, "completion_tokens": 0}, True),
        ({"prompt_tokens": 7, "completion_tokens": 0, "total_tokens": 7}, True),
        ({"prompt_tokens": 7, "total_tokens": 6}, True),
        ({"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 8}, True),
        ({"prompt_tokens": 7, "completion_tokens": 1, "total_tokens": False}, True),
        ({"prompt_tokens": 7, "completion_tokens": 1, "total_tokens": "8"}, True),
        ({"prompt_tokens": 7, "completion_tokens": 1, "total_tokens": 8}, False),
    ],
)
def test_azure_openai_stream_usage_rejects_missing_zero_bool_or_incoherent_values(
    usage: object,
    saw_done: bool,
) -> None:
    with pytest.raises(RuntimeError, match="Azure text canary"):
        sync._validate_openai_stream_usage(  # noqa: SLF001
            usage,
            saw_done=saw_done,
            deployment_name="test",
        )


def test_azure_canary_requires_tools_for_every_launch_route_and_images_for_exact_five(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text_calls: list[str] = []
    tool_calls: list[str] = []
    image_calls: list[str] = []

    monkeypatch.setattr(
        sync,
        "_text_canary",
        lambda candidate, *, account_key: text_calls.append(candidate.canonical_id),
    )
    monkeypatch.setattr(
        sync,
        "_tool_canary",
        lambda candidate, *, account_key: tool_calls.append(candidate.canonical_id),
    )
    monkeypatch.setattr(
        sync,
        "_image_canary",
        lambda candidate, *, account_key: image_calls.append(candidate.canonical_id),
    )

    for model_id in sorted(_EXPECTED_AZURE_LAUNCH_IDS):
        canary(_canary_candidate(model_id), account_key="test")

    assert text_calls == sorted(_EXPECTED_AZURE_LAUNCH_IDS)
    assert tool_calls == sorted(_EXPECTED_AZURE_LAUNCH_IDS)
    assert frozenset(image_calls) == sync._IMAGE_CANARY_MODEL_IDS  # noqa: SLF001
    assert sync._IMAGE_CANARY_MODEL_IDS == frozenset(  # noqa: SLF001
        {
            "moonshotai/kimi-k2.5",
            "moonshotai/kimi-k2.6",
            "moonshotai/kimi-k2.7-code",
            "openai/gpt-5-mini",
            "x-ai/grok-4.20-reasoning",
        }
    )


def test_azure_openai_tool_canary_forces_structured_zero_argument_pong_for_all_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[tuple[str, dict[str, object], httpx.Timeout]] = []

    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        request = kwargs["json"]
        timeout = kwargs["timeout"]
        assert isinstance(request, dict)
        assert isinstance(timeout, httpx.Timeout)
        requests.append((url, request, timeout))
        return httpx.Response(
            200,
            json=_openai_tool_response(),
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(sync.httpx, "post", fake_post)

    for model_id in sorted(_EXPECTED_AZURE_LAUNCH_IDS):
        sync._tool_canary(_canary_candidate(model_id), account_key="test")  # noqa: SLF001

    assert len(requests) == len(_EXPECTED_AZURE_LAUNCH_IDS) == 9
    for model_id, (url, request, timeout) in zip(
        sorted(_EXPECTED_AZURE_LAUNCH_IDS), requests, strict=True
    ):
        assert url == f"{sync.OPENAI_BASE_URL}/chat/completions"
        assert timeout is sync.CANARY_TIMEOUT
        assert request["stream"] is False
        assert request["messages"] == [
            {"role": "user", "content": "Call the pong tool now. Do not answer in text."}
        ]
        assert request["tool_choice"] == {
            "type": "function",
            "function": {"name": "pong"},
        }
        tools = request["tools"]
        assert isinstance(tools, list)
        function = tools[0]["function"]
        assert function["name"] == "pong"
        assert function["parameters"] == {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        }
        expected_token_field = "max_completion_tokens" if model_id == "openai/gpt-5-mini" else "max_tokens"
        assert request[expected_token_field] == 1024
        assert ({"max_tokens", "max_completion_tokens"} & request.keys()) == {
            expected_token_field
        }


@pytest.mark.parametrize(
    "payload",
    [
        {"choices": [{"finish_reason": "stop", "message": {"content": "PONG"}}]},
        _openai_tool_response(finish_reason="stop"),
        _openai_tool_response(call_id=""),
        _openai_tool_response(call_type="custom"),
        _openai_tool_response(name="not_pong"),
        _openai_tool_response(arguments='{"unexpected": true}'),
        _openai_tool_response(arguments="not-json"),
        _openai_tool_response(arguments={}),
    ],
)
def test_azure_openai_tool_canary_rejects_unstructured_or_malformed_results(
    payload: dict[str, object],
) -> None:
    with pytest.raises(RuntimeError, match="Azure tool canary"):
        sync._validate_openai_tool_call(payload, deployment_name="test")  # type: ignore[arg-type]  # noqa: SLF001


def test_azure_anthropic_tool_canary_uses_and_requires_native_tool_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, object]] = []
    responses: list[dict[str, object]] = [
        {
            "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "id": "tool_1", "name": "pong", "input": {}}],
        },
        {"stop_reason": "end_turn", "content": [{"type": "text", "text": "PONG"}]},
    ]

    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        request = kwargs["json"]
        assert isinstance(request, dict)
        requests.append(request)
        return httpx.Response(
            200,
            json=responses.pop(0),
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(sync.httpx, "post", fake_post)
    candidate = _canary_candidate("anthropic/example", model_format="Anthropic")

    sync._tool_canary(candidate, account_key="test")  # noqa: SLF001
    with pytest.raises(RuntimeError, match="structured tool use"):
        sync._tool_canary(candidate, account_key="test")  # noqa: SLF001

    request = requests[0]
    assert request["tool_choice"] == {
        "type": "tool",
        "name": "pong",
        "disable_parallel_tool_use": True,
    }
    tools = request["tools"]
    assert isinstance(tools, list)
    assert tools[0]["input_schema"] == {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }


def test_azure_image_canary_assets_are_distinct_valid_solid_color_pngs() -> None:
    expected_rgb = {
        "RED": bytes((255, 0, 0)),
        "GREEN": bytes((0, 255, 0)),
        "BLUE": bytes((0, 0, 255)),
        "YELLOW": bytes((255, 255, 0)),
    }
    decoded_assets: set[bytes] = set()

    for label, data_url in sync._IMAGE_CANARY_ASSETS:  # noqa: SLF001
        prefix, encoded = data_url.split(",", 1)
        assert prefix == "data:image/png;base64"
        png = base64.b64decode(encoded, validate=True)
        assert png.startswith(b"\x89PNG\r\n\x1a\n")
        decoded_assets.add(png)

        offset = 8
        chunks: dict[bytes, list[bytes]] = {}
        while offset < len(png):
            length = int.from_bytes(png[offset : offset + 4], "big")
            chunk_type = png[offset + 4 : offset + 8]
            chunk_data = png[offset + 8 : offset + 8 + length]
            chunks.setdefault(chunk_type, []).append(chunk_data)
            offset += 12 + length
        width, height, depth, color_type, compression, filtering, interlace = struct.unpack(
            ">IIBBBBB", chunks[b"IHDR"][0]
        )
        assert (width, height, depth, color_type, compression, filtering, interlace) == (
            64,
            64,
            8,
            2,
            0,
            0,
            0,
        )
        scanlines = zlib.decompress(b"".join(chunks[b"IDAT"]))
        stride = 1 + width * 3
        assert len(scanlines) == stride * height
        pixels: set[bytes] = set()
        for row_start in range(0, len(scanlines), stride):
            row = scanlines[row_start : row_start + stride]
            assert row[0] == 0
            pixels.update(row[index : index + 3] for index in range(1, stride, 3))
        assert pixels == {expected_rgb[label]}

    assert {label for label, _data_url in sync._IMAGE_CANARY_ASSETS} == set(expected_rgb)  # noqa: SLF001
    assert len(decoded_assets) == len(expected_rgb) == 4


def test_azure_image_canary_sends_ordered_distinct_pair_for_exact_five_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, object]] = []
    label_by_url = dict(sync._IMAGE_CANARY_ASSETS)  # noqa: SLF001
    label_by_url = {data_url: label for label, data_url in label_by_url.items()}

    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        request = kwargs["json"]
        assert isinstance(request, dict)
        requests.append(request)
        messages = request["messages"]
        assert isinstance(messages, list)
        content = messages[0]["content"]
        assert isinstance(content, list)
        expected_answer = ",".join(
            label_by_url[item["image_url"]["url"]] for item in content[1:]
        )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": expected_answer.lower()}}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(sync.httpx, "post", fake_post)

    for model_id in sorted(sync._IMAGE_CANARY_MODEL_IDS):  # noqa: SLF001
        sync._image_canary(_canary_candidate(model_id), account_key="test")  # noqa: SLF001

    assert len(requests) == 5
    for model_id, request in zip(
        sorted(sync._IMAGE_CANARY_MODEL_IDS),  # noqa: SLF001
        requests,
        strict=True,
    ):
        messages = request["messages"]
        assert isinstance(messages, list)
        content = messages[0]["content"]
        assert isinstance(content, list)
        assert len(content) == 3
        prompt = content[0]["text"]
        assert isinstance(prompt, str)
        assert all(label in prompt for label in label_by_url.values())
        route_urls: list[str] = []
        for image in content[1:]:
            data_url = image["image_url"]["url"]
            assert isinstance(data_url, str)
            assert data_url.startswith("data:image/png;base64,")
            route_urls.append(data_url)
        assert len(set(route_urls)) == 2
        expected_token_field = (
            "max_completion_tokens" if model_id == "openai/gpt-5-mini" else "max_tokens"
        )
        assert request[expected_token_field] == 4096
        assert ({"max_tokens", "max_completion_tokens"} & request.keys()) == {
            expected_token_field
        }


@pytest.mark.parametrize("answer", ["RED,BLUE", " red , BlUe "])
def test_azure_image_canary_accepts_exact_labels_with_flexible_case_and_spacing(
    monkeypatch: pytest.MonkeyPatch,
    answer: str,
) -> None:
    assets = dict(sync._IMAGE_CANARY_ASSETS)  # noqa: SLF001
    monkeypatch.setattr(
        sync,
        "_image_canary_challenges",
        lambda: [("RED", assets["RED"]), ("BLUE", assets["BLUE"])],
    )

    def fake_post(url: str, **_kwargs: object) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": answer}}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(sync.httpx, "post", fake_post)

    sync._image_canary(  # noqa: SLF001
        _canary_candidate("openai/gpt-5-mini"),
        account_key="test",
    )


@pytest.mark.parametrize(
    "answer",
    [
        "BLUE, RED",
        "RED, PURPLE",
        "The colors are RED, BLUE",
        "RED",
        "RED BLUE",
        "RED, BLUE, GREEN",
        "",
        None,
    ],
)
def test_azure_image_canary_requires_exact_selected_label(
    monkeypatch: pytest.MonkeyPatch,
    answer: object,
) -> None:
    red = dict(sync._IMAGE_CANARY_ASSETS)["RED"]  # noqa: SLF001
    blue = dict(sync._IMAGE_CANARY_ASSETS)["BLUE"]  # noqa: SLF001
    monkeypatch.setattr(
        sync,
        "_image_canary_challenges",
        lambda: [("RED", red), ("BLUE", blue)],
    )

    def fake_post(url: str, **_kwargs: object) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": answer}}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(sync.httpx, "post", fake_post)

    with pytest.raises(RuntimeError, match="Azure image canary"):
        sync._image_canary(  # noqa: SLF001
            _canary_candidate("x-ai/grok-4.20-reasoning"),
            account_key="test",
        )


def test_azure_image_canary_rejects_fixed_red_responder_for_non_red_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assets = dict(sync._IMAGE_CANARY_ASSETS)  # noqa: SLF001
    monkeypatch.setattr(
        sync,
        "_image_canary_challenges",
        lambda: [("GREEN", assets["GREEN"]), ("YELLOW", assets["YELLOW"])],
    )

    def fake_post(url: str, **_kwargs: object) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "RED, RED"}}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(sync.httpx, "post", fake_post)

    with pytest.raises(RuntimeError, match="Azure image canary"):
        sync._image_canary(  # noqa: SLF001
            _canary_candidate("openai/gpt-5-mini"),
            account_key="test",
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"choices": [{"message": {"content": None}}]},
        {"choices": []},
    ],
)
def test_azure_image_canary_rejects_missing_answer(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object],
) -> None:
    assets = dict(sync._IMAGE_CANARY_ASSETS)  # noqa: SLF001
    monkeypatch.setattr(
        sync,
        "_image_canary_challenges",
        lambda: [("RED", assets["RED"]), ("BLUE", assets["BLUE"])],
    )

    def fake_post(url: str, **_kwargs: object) -> httpx.Response:
        return httpx.Response(200, json=payload, request=httpx.Request("POST", url))

    monkeypatch.setattr(sync.httpx, "post", fake_post)

    with pytest.raises(RuntimeError, match="Azure image canary"):
        sync._image_canary(  # noqa: SLF001
            _canary_candidate("x-ai/grok-4.20-reasoning"),
            account_key="test",
        )


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


def test_azure_existing_deployment_reconciles_wrong_sku_when_model_matches() -> None:
    candidate = DeploymentCandidate(
        canonical_id="deepseek/deepseek-v4-flash",
        native_name="DeepSeek-V4-Flash",
        version="2026-08-01",
        model_format="DeepSeek",
        deployment_name="deepseek-v4-flash",
        sku="GlobalStandard",
        capacity=1,
        is_default_version=True,
    )
    matching_model = {"name": candidate.native_name, "version": candidate.version}
    wrong_sku = {
        "properties": {
            "model": matching_model,
            "versionUpgradeOption": DEPLOYMENT_VERSION_UPGRADE_OPTION,
        },
        "sku": {"name": "DataZoneStandard", "capacity": candidate.capacity},
    }
    matching_deployment = {
        "properties": {
            "model": matching_model,
            "versionUpgradeOption": DEPLOYMENT_VERSION_UPGRADE_OPTION,
        },
        "sku": {"name": candidate.sku, "capacity": candidate.capacity},
    }

    assert deployment_needs_reconcile(wrong_sku, candidate) is True
    assert deployment_needs_reconcile(matching_deployment, candidate) is False


def test_azure_existing_deployment_reconciles_auto_upgrade_policy() -> None:
    candidate = DeploymentCandidate(
        canonical_id="deepseek/deepseek-v4-flash",
        native_name="DeepSeek-V4-Flash",
        version="2026-04-23",
        model_format="DeepSeek",
        deployment_name="deepseek-v4-flash",
        sku="GlobalStandard",
        capacity=1,
        is_default_version=True,
    )
    current = {
        "properties": {
            "model": {"name": candidate.native_name, "version": candidate.version},
            "versionUpgradeOption": "OnceNewDefaultVersionAvailable",
        },
        "sku": {"name": candidate.sku, "capacity": candidate.capacity},
    }

    assert deployment_needs_reconcile(current, candidate) is True


@pytest.mark.parametrize(
    ("capacity", "expected"),
    [(9, True), (10, False), (25, False), (None, True), ("10", True), (True, True)],
)
def test_azure_existing_deployment_preserves_greater_integer_capacity(
    capacity: object,
    expected: bool,
) -> None:
    candidate = DeploymentCandidate(
        canonical_id="deepseek/deepseek-v4-flash",
        native_name="DeepSeek-V4-Flash",
        version="2026-08-01",
        model_format="DeepSeek",
        deployment_name="deepseek-v4-flash",
        sku="GlobalStandard",
        capacity=sync.MINIMUM_LAUNCH_CAPACITY,
        is_default_version=True,
    )
    current = {
        "properties": {
            "model": {"name": candidate.native_name, "version": candidate.version},
            "versionUpgradeOption": DEPLOYMENT_VERSION_UPGRADE_OPTION,
        },
        "sku": {"name": candidate.sku, "capacity": capacity},
    }

    assert deployment_needs_reconcile(current, candidate) is expected


def test_azure_deploy_fails_closed_when_succeeded_resource_has_wrong_sku() -> None:
    candidate = DeploymentCandidate(
        canonical_id="deepseek/deepseek-v4-flash",
        native_name="DeepSeek-V4-Flash",
        version="2026-08-01",
        model_format="DeepSeek",
        deployment_name="deepseek-v4-flash",
        sku="GlobalStandard",
        capacity=1,
        is_default_version=True,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            return httpx.Response(200)
        return httpx.Response(
            200,
            json={
                "properties": {
                    "model": {"name": candidate.native_name, "version": candidate.version},
                    "versionUpgradeOption": DEPLOYMENT_VERSION_UPGRADE_OPTION,
                    "provisioningState": "Succeeded",
                },
                "sku": {"name": "DataZoneStandard", "capacity": candidate.capacity},
            },
        )

    client = object.__new__(AzureManagementClient)
    client._base = "https://management.azure.test/account"
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(
            RuntimeError,
            match="unexpected model, version, upgrade policy, SKU, or capacity",
        ):
            client.deploy(candidate)
    finally:
        client.close()


def test_azure_deploy_put_pins_exact_version_without_auto_upgrade() -> None:
    candidate = DeploymentCandidate(
        canonical_id="deepseek/deepseek-v4-flash",
        native_name="DeepSeek-V4-Flash",
        version="2026-04-23",
        model_format="DeepSeek",
        deployment_name="deepseek-v4-flash",
        sku="GlobalStandard",
        capacity=1,
        is_default_version=True,
    )
    put_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            payload = json.loads(request.content)
            assert isinstance(payload, dict)
            put_payload.update(payload)
            return httpx.Response(200)
        return httpx.Response(
            200,
            json={
                "properties": {
                    "model": {"name": candidate.native_name, "version": candidate.version},
                    "versionUpgradeOption": DEPLOYMENT_VERSION_UPGRADE_OPTION,
                    "provisioningState": "Succeeded",
                },
                "sku": {"name": candidate.sku, "capacity": candidate.capacity},
            },
        )

    client = object.__new__(AzureManagementClient)
    client._base = "https://management.azure.test/account"
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        client.deploy(candidate)
    finally:
        client.close()

    properties = put_payload["properties"]
    assert isinstance(properties, dict)
    assert properties["versionUpgradeOption"] == "NoAutoUpgrade"
    assert properties["model"] == {
        "format": candidate.model_format,
        "name": candidate.native_name,
        "version": candidate.version,
    }


def test_azure_manifest_serializes_grok_43_long_context_price_tiers(
    grok_43_retail_rows: list[dict[str, object]],
) -> None:
    candidate = DeploymentCandidate(
        canonical_id="x-ai/grok-4.3",
        native_name="grok-4.3",
        version="1",
        model_format="xAI",
        deployment_name="grok-4-3",
        sku="GlobalStandard",
        capacity=1,
        is_default_version=True,
    )
    price = azure.parse_retail_prices(grok_43_retail_rows)[candidate.canonical_id]

    row = sync.manifest_row(candidate, price)

    assert row["input_token_price_per_m"] == 1_250_000
    assert row["output_token_price_per_m"] == 2_500_000
    assert row["cached_input_token_price_per_m"] == 200_000
    assert row["price_tiers"] == [
        {
            "max_prompt_tokens": 200_000,
            "input_token_price_per_m": 1_250_000,
            "output_token_price_per_m": 2_500_000,
            "cached_input_token_price_per_m": 200_000,
        },
        {
            "max_prompt_tokens": None,
            "input_token_price_per_m": 2_500_000,
            "output_token_price_per_m": 5_000_000,
            "cached_input_token_price_per_m": 400_000,
        },
    ]


@pytest.mark.parametrize(
    "model_id",
    [*sorted(sync._IMAGE_CANARY_MODEL_IDS), "cohere/command-a"],  # noqa: SLF001
)
def test_azure_manifest_advertises_images_only_after_required_canary_models(
    model_id: str,
) -> None:
    row = sync.manifest_row(_canary_candidate(model_id), azure.ModelPrice(1, 2))

    expected = ["text", "image"] if model_id in sync._IMAGE_CANARY_MODEL_IDS else ["text"]  # noqa: SLF001
    assert row["input_modalities"] == expected


def test_azure_admission_isolates_failed_capability_canary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed = _canary_candidate("x-ai/grok-4.20-reasoning")
    healthy = _canary_candidate("cohere/command-a")
    candidates = [failed, healthy]
    existing = {
        candidate.deployment_name: {
            "properties": {
                "model": {"name": candidate.native_name, "version": candidate.version},
                "versionUpgradeOption": DEPLOYMENT_VERSION_UPGRADE_OPTION,
            },
            "sku": {"name": candidate.sku, "capacity": candidate.capacity},
        }
        for candidate in candidates
    }
    canary_calls: list[str] = []

    def fake_canary(candidate: DeploymentCandidate, *, account_key: str) -> None:
        canary_calls.append(candidate.canonical_id)
        if candidate == failed:
            raise RuntimeError("Azure image canary failed")

    monkeypatch.setattr(sync, "canary_with_retries", fake_canary)
    management = object.__new__(AzureManagementClient)

    rows, failures = sync._admit_candidates(  # noqa: SLF001
        candidates,
        {candidate.canonical_id: azure.ModelPrice(1, 2) for candidate in candidates},
        management=management,
        existing=existing,
        account_key="test",
    )

    assert canary_calls == [failed.canonical_id, healthy.canonical_id]
    assert [row["id"] for row in rows] == [healthy.canonical_id]
    assert len(failures) == 1
    assert failed.canonical_id in failures[0]
    assert "Azure image canary failed" in failures[0]


def test_azure_publish_admission_writes_only_exact_nine_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates = [_canary_candidate(model_id) for model_id in sorted(_EXPECTED_AZURE_LAUNCH_IDS)]
    rows = [{"id": candidate.canonical_id} for candidate in candidates]
    writes: list[list[dict[str, object]]] = []

    def fake_write(payload: list[dict[str, object]]) -> bool:
        writes.append(payload)
        return False

    monkeypatch.setattr(sync, "write_manifest", fake_write)

    assert sync._publish_admission(candidates, rows, []) is False  # type: ignore[arg-type]  # noqa: SLF001
    assert writes == [rows]


@pytest.mark.parametrize(
    "case",
    ["missing_candidate", "failed_canary", "zero_healthy"],
)
def test_azure_publish_admission_fails_before_write_on_incomplete_launch(
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    candidates = [_canary_candidate(model_id) for model_id in sorted(_EXPECTED_AZURE_LAUNCH_IDS)]
    rows: list[dict[str, object]] = [
        {"id": candidate.canonical_id} for candidate in candidates
    ]
    failures: list[str] = []
    if case == "missing_candidate":
        candidates = candidates[:-1]
        rows = rows[:-1]
    elif case == "failed_canary":
        failed = candidates[-1].canonical_id
        rows = rows[:-1]
        failures = [f"{failed}: RuntimeError: Azure image canary failed"]
    else:
        rows = []
    writes: list[list[dict[str, object]]] = []
    monkeypatch.setattr(sync, "write_manifest", lambda payload: writes.append(payload) or True)

    with pytest.raises(RuntimeError, match="before manifest write"):
        sync._publish_admission(  # type: ignore[arg-type]  # noqa: SLF001
            candidates,
            rows,
            failures,
        )

    assert writes == []


def test_azure_canary_retries_only_failed_phase_without_replaying_passed_phases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _canary_candidate("x-ai/grok-4.20-reasoning")
    phases: list[str] = []
    sleeps: list[float] = []
    text_attempts = 0
    tool_attempts = 0
    image_attempts = 0

    def fake_text(candidate: DeploymentCandidate, *, account_key: str) -> None:
        nonlocal text_attempts
        phases.append("text")
        text_attempts += 1
        if text_attempts == 1:
            raise httpx.ConnectError("temporary")

    def fake_tool(candidate: DeploymentCandidate, *, account_key: str) -> None:
        nonlocal tool_attempts
        phases.append("tool")
        tool_attempts += 1
        if tool_attempts == 1:
            raise _http_status_error(503)

    def fake_image(candidate: DeploymentCandidate, *, account_key: str) -> None:
        nonlocal image_attempts
        phases.append("image")
        image_attempts += 1
        if image_attempts == 1:
            raise httpx.ConnectError("temporary")

    monkeypatch.setattr(sync, "_text_canary", fake_text)
    monkeypatch.setattr(sync, "_tool_canary", fake_tool)
    monkeypatch.setattr(sync, "_image_canary", fake_image)
    monkeypatch.setattr(sync.time, "sleep", sleeps.append)

    canary_with_retries(candidate, account_key="test")

    assert phases == ["text", "text", "tool", "tool", "image", "image"]
    assert sleeps == [1.0, 1.0, 1.0]


@pytest.mark.parametrize(
    ("retry_after", "expected_delay"),
    [("17", 17.0), (None, 60.0), ("not-a-number", 60.0), ("-1", 60.0)],
)
def test_azure_canary_429_honors_numeric_retry_after_with_safe_fallback(
    monkeypatch: pytest.MonkeyPatch,
    retry_after: str | None,
    expected_delay: float,
) -> None:
    candidate = _canary_candidate("cohere/command-a")
    text_attempts = 0
    phases: list[str] = []
    sleeps: list[float] = []

    def fake_text(candidate: DeploymentCandidate, *, account_key: str) -> None:
        nonlocal text_attempts
        phases.append("text")
        text_attempts += 1
        if text_attempts == 1:
            raise _http_status_error(429, retry_after=retry_after)

    def fake_tool(candidate: DeploymentCandidate, *, account_key: str) -> None:
        phases.append("tool")

    monkeypatch.setattr(sync, "_text_canary", fake_text)
    monkeypatch.setattr(sync, "_tool_canary", fake_tool)
    monkeypatch.setattr(sync.time, "sleep", sleeps.append)

    canary_with_retries(candidate, account_key="test")

    assert phases == ["text", "text", "tool"]
    assert sleeps == [expected_delay]


def test_azure_canary_does_not_retry_no_first_byte_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _canary_candidate("cohere/command-a")
    attempts = 0
    tool_calls = 0
    sleeps: list[float] = []

    def fake_text(candidate: DeploymentCandidate, *, account_key: str) -> None:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("no first byte")

    def fake_tool(candidate: DeploymentCandidate, *, account_key: str) -> None:
        nonlocal tool_calls
        tool_calls += 1

    monkeypatch.setattr(sync, "_text_canary", fake_text)
    monkeypatch.setattr(sync, "_tool_canary", fake_tool)
    monkeypatch.setattr(sync.time, "sleep", sleeps.append)

    with pytest.raises(httpx.ReadTimeout):
        canary_with_retries(candidate, account_key="test")
    assert attempts == 1
    assert tool_calls == 0
    assert sleeps == []


def test_azure_openai_text_canary_requires_terminal_usage_for_all_launch_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, object]] = []
    seen_timeouts: list[httpx.Timeout] = []

    class FakeStream:
        def __enter__(self) -> FakeStream:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def raise_for_status(self) -> None:
            return None

        def iter_lines(self) -> list[str]:
            return [
                'data: {"choices":[{"delta":{"content":"PONG"}}]}',
                (
                    'data: {"choices":[],"usage":{"prompt_tokens":4,'
                    '"completion_tokens":1,"total_tokens":5}}'
                ),
                "data: [DONE]",
            ]

    def fake_stream(*_args: object, **kwargs: object) -> FakeStream:
        value = kwargs.get("timeout")
        request = kwargs.get("json")
        assert isinstance(value, httpx.Timeout)
        assert isinstance(request, dict)
        seen_timeouts.append(value)
        requests.append(request)
        return FakeStream()

    monkeypatch.setattr(sync.httpx, "stream", fake_stream)

    for model_id in sorted(_EXPECTED_AZURE_LAUNCH_IDS):
        sync._text_canary(_canary_candidate(model_id), account_key="test")  # noqa: SLF001

    assert len(requests) == len(seen_timeouts) == len(_EXPECTED_AZURE_LAUNCH_IDS) == 9
    assert all(timeout is sync.CANARY_TIMEOUT for timeout in seen_timeouts)
    assert all(request["stream_options"] == {"include_usage": True} for request in requests)


def test_azure_manifest_registers_prepaid_only_gateway_routes() -> None:
    raw = json.loads(azure.MANIFEST_PATH.read_text(encoding="utf-8"))
    rows = raw["models"]

    assert PROVIDERS["azure"].supports_prepaid is True
    assert PROVIDERS["azure"].supports_byok is False
    assert raw["model_count"] == len(rows) == len(_EXPECTED_AZURE_LAUNCH_IDS) == 9
    assert {row["id"] for row in rows} == _EXPECTED_AZURE_LAUNCH_IDS
    assert azure.retail_model_ids() == _EXPECTED_AZURE_LAUNCH_IDS
    assert {
        row["id"]
        for row in rows
        if row["input_modalities"] == ["text", "image"]
    } == sync._IMAGE_CANARY_MODEL_IDS  # noqa: SLF001
    assert all(
        row["input_modalities"] in (["text"], ["text", "image"])
        for row in rows
    )
    assert all(row["azure_deployment_sku"] == "GlobalStandard" for row in rows)
    assert all(not row["id"].startswith("anthropic/") for row in rows)
    assert all(row["id"] != "openai/gpt-5.4-mini" for row in rows)
    assert all(row["id"] != "x-ai/grok-4.3" for row in rows)
    assert "x-ai/grok-4.3@azure/prepaid" not in MODEL_ENDPOINTS
    for row in rows:
        model_id = row["id"]
        endpoint = MODEL_ENDPOINTS[f"{model_id}@azure/prepaid"]
        assert endpoint.provider == "azure"
        assert endpoint.usage_type == "Credits"
        assert endpoint.upstream_id == row["upstream_id"]
        assert endpoint.prompt_price_microdollars_per_million_tokens > 0
        assert endpoint.completion_price_microdollars_per_million_tokens > 0
        assert f"{model_id}@azure/byok" not in MODEL_ENDPOINTS


@pytest.mark.parametrize(
    "model_id",
    [
        "cohere/command-a-plus-05-2026",
        "mistralai/mistral-large-3",
        "openai/gpt-oss-120b",
    ],
)
def test_azure_tool_incompatible_routes_are_absent_from_runtime_catalog(
    model_id: str,
) -> None:
    assert f"{model_id}@azure/prepaid" not in MODEL_ENDPOINTS


def test_all_provider_smoke_includes_azure() -> None:
    assert ("azure", "cohere/command-a") in PROBES


def test_azure_sync_leaves_unchanged_manifest_byte_identical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = tmp_path / "azure.json"
    rows = [
        {
            "id": "deepseek/deepseek-v4-flash",
            "upstream_id": "deepseek-v4-flash",
            "input_token_price_per_m": 190_000,
            "output_token_price_per_m": 510_000,
        }
    ]
    existing = {
        "_about": (
            "Azure AI Foundry deployments verified for this TrustedRouter subscription. "
            "The account sync publishes only synchronous chat deployments with "
            "remaining quota, exact pricing, and successful direct text, tool-call, "
            "and required image capability canaries."
        ),
        "provider": "azure",
        "source": (
            "https://management.azure.com/providers/Microsoft.CognitiveServices/"
            "locations/eastus2/models"
        ),
        "pricing_source": sync.PRICING_URL,
        "generated_at": "2026-01-01T00:00:00Z",
        "price_scale": "microdollars_per_million",
        "model_count": 1,
        "models": rows,
    }
    manifest_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    before = manifest_path.read_bytes()
    monkeypatch.setattr(sync, "MANIFEST_PATH", manifest_path)

    assert write_manifest(rows) is False
    assert manifest_path.read_bytes() == before


def test_azure_price_refresh_preserves_discovery_and_pricing_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = tmp_path / "azure.json"
    raw = json.loads(azure.MANIFEST_PATH.read_text(encoding="utf-8"))
    raw["pricing_source"] = "https://stale.example.test/prices"
    manifest_path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    monkeypatch.setattr(azure, "MANIFEST_PATH", manifest_path)

    prices = {
        row["id"]: azure.ModelPrice(
            row["input_token_price_per_m"],
            row["output_token_price_per_m"],
            prompt_cached_micro_per_m=row.get("cached_input_token_price_per_m"),
        )
        for row in raw["models"]
    }
    result = azure.ProviderPricingResult(
        slug="azure",
        prices=prices,
        source="api",
        fetched_url=azure.URL,
    )

    azure.write_provider_manifest(result)
    first = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert first["source"] == azure.DISCOVERY_URL
    assert first["pricing_source"] == azure.URL

    azure.write_provider_manifest(result)
    second = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert second["source"] == azure.DISCOVERY_URL
    assert second["pricing_source"] == azure.URL
    first.pop("generated_at")
    second.pop("generated_at")
    assert second == first


def test_azure_secret_uploader_is_managed() -> None:
    root = azure.MANIFEST_PATH.parents[4]
    secrets_script = (root / "scripts/deploy/secrets.sh").read_text(encoding="utf-8")

    assert '"AZURE_API_KEY"' in secrets_script
    assert '"trustedrouter-azure-api-key"' in secrets_script
