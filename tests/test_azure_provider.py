from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

import scripts.providers.sync_azure_foundry as sync
from scripts.pricing.providers import azure
from scripts.providers.sync_azure_foundry import (
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
    assert "anthropic/claude-opus-5" not in prices


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


def test_azure_fetch_uses_current_pricing_validation_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    ]
    monkeypatch.setattr(azure, "fetch_retail_rows", lambda: rows)

    result = azure.fetch()

    assert result.slug == "azure"
    assert result.prices["deepseek/deepseek-v4-flash"].prompt_micro_per_m == 190_000


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
        "properties": {"model": matching_model},
        "sku": {"name": "DataZoneStandard", "capacity": candidate.capacity},
    }
    matching_deployment = {
        "properties": {"model": matching_model},
        "sku": {"name": candidate.sku, "capacity": candidate.capacity},
    }

    assert deployment_needs_reconcile(wrong_sku, candidate) is True
    assert deployment_needs_reconcile(matching_deployment, candidate) is False


@pytest.mark.parametrize(
    ("capacity", "expected"),
    [(0, True), (1, False), (2, False), (None, True), ("1", True), (True, True)],
)
def test_azure_existing_deployment_requires_minimum_integer_capacity(
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
        capacity=1,
        is_default_version=True,
    )
    current = {
        "properties": {
            "model": {"name": candidate.native_name, "version": candidate.version}
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
                    "provisioningState": "Succeeded",
                },
                "sku": {"name": "DataZoneStandard", "capacity": candidate.capacity},
            },
        )

    client = object.__new__(AzureManagementClient)
    client._base = "https://management.azure.test/account"
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(RuntimeError, match="unexpected model, version, SKU, or capacity"):
            client.deploy(candidate)
    finally:
        client.close()


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


def test_azure_openai_canary_uses_bounded_timeout(
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
    seen_timeout: httpx.Timeout | None = None

    class FakeStream:
        def __enter__(self) -> FakeStream:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def raise_for_status(self) -> None:
            return None

        def iter_lines(self) -> list[str]:
            return ['data: {"choices":[{"delta":{"content":"PONG"}}]}']

    def fake_stream(*_args: object, **kwargs: object) -> FakeStream:
        nonlocal seen_timeout
        value = kwargs.get("timeout")
        assert isinstance(value, httpx.Timeout)
        seen_timeout = value
        return FakeStream()

    monkeypatch.setattr(sync.httpx, "stream", fake_stream)

    canary(candidate, account_key="test")
    assert seen_timeout is sync.CANARY_TIMEOUT


def test_azure_manifest_registers_prepaid_only_gateway_routes() -> None:
    raw = json.loads(azure.MANIFEST_PATH.read_text(encoding="utf-8"))
    rows = raw["models"]

    assert PROVIDERS["azure"].supports_prepaid is True
    assert PROVIDERS["azure"].supports_byok is False
    assert len(rows) >= 20
    assert all(row["azure_deployment_sku"] == "GlobalStandard" for row in rows)
    assert all(not row["id"].startswith("anthropic/") for row in rows)
    assert all(row["id"] != "openai/gpt-5.4-mini" for row in rows)
    for row in rows:
        model_id = row["id"]
        endpoint = MODEL_ENDPOINTS[f"{model_id}@azure/prepaid"]
        assert endpoint.provider == "azure"
        assert endpoint.usage_type == "Credits"
        assert endpoint.upstream_id == row["upstream_id"]
        assert endpoint.prompt_price_microdollars_per_million_tokens > 0
        assert endpoint.completion_price_microdollars_per_million_tokens > 0
        assert f"{model_id}@azure/byok" not in MODEL_ENDPOINTS


def test_all_provider_smoke_includes_azure() -> None:
    assert ("azure", "deepseek/deepseek-v4-flash") in PROBES


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
            "remaining quota, exact pricing, and a successful direct PONG canary."
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


def test_azure_secret_uploader_is_managed() -> None:
    root = azure.MANIFEST_PATH.parents[4]
    secrets_script = (root / "scripts/deploy/secrets.sh").read_text(encoding="utf-8")

    assert '"AZURE_API_KEY"' in secrets_script
    assert '"trustedrouter-azure-api-key"' in secrets_script
