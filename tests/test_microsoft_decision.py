"""Microsoft's native decision protocol stays distinct from Azure chat."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.pricing.providers import azure
from scripts.providers import sync_azure_foundry as sync
from tests.lifecycle_clock import CATALOG_CLOCK
from tests.test_azure_provider import _canary_candidate, _minimum_retail_rows, _model, _usage
from tests.test_decide_models import _authorize
from trusted_router import catalog_ingest, provider_lifecycle
from trusted_router.catalog import endpoints_for_model, model_to_openrouter_shape
from trusted_router.catalog_ingest import _authoritative_provider_model_ids
from trusted_router.provider_lifecycle import provider_model_retired

MODEL = azure.DECISION_MODEL_ID


def test_microsoft_decision_catalog_and_price() -> None:
    model = catalog_ingest._decision_models()[MODEL]
    assert model.supports_decide and not model.supports_chat and not model.supports_messages
    assert model.context_length == 32768
    assert model.prompt_price_microdollars_per_million_tokens == 44_310
    assert model.completion_price_microdollars_per_million_tokens == 0
    assert model.upstream_id == "tr-decision-1"
    if provider_model_retired("azure", MODEL, at=CATALOG_CLOCK):
        assert not endpoints_for_model(MODEL)
        assert MODEL not in _authoritative_provider_model_ids("azure")
    else:
        assert [(e.provider, e.upstream_id) for e in endpoints_for_model(MODEL)] == [("azure", "tr-decision-1")]
        assert MODEL in _authoritative_provider_model_ids("azure")
    public = model_to_openrouter_shape(model)
    assert public["architecture"]["output_modalities"] == ["decision"]


@pytest.mark.asyncio
async def test_microsoft_decision_authorizes_exact_azure_deployment() -> None:
    response = await _authorize({
        "model": MODEL, "route_type": "decide",
        "estimated_input_tokens": 400, "max_output_tokens": 3,
    })
    if provider_model_retired("azure", MODEL, at=CATALOG_CLOCK):
        assert response.status_code == 400
        return
    assert response.status_code == 200, response.text
    data = response.json().get("data", response.json())
    assert [(c["provider"], c["upstream_model"]) for c in data["route_candidates"]] == [("azure", "tr-decision-1")]


def test_microsoft_decision_live_meter_and_checkpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: datetime(2026, 10, 12, tzinfo=UTC))
    rows = _minimum_retail_rows(MODEL)
    price = azure.parse_retail_prices(rows, model_versions={MODEL: "1"})[MODEL]
    assert (price.prompt_micro_per_m, price.completion_micro_per_m) == (42_000, 0)
    assert not azure.parse_retail_prices(rows, model_versions={MODEL: "2"})
    assert not azure.parse_retail_prices([], model_versions={MODEL: "1"})
    assert azure.deployment_name("Microsoft-Decision-1") == "tr-decision-1"
    with pytest.raises(ValueError, match="ambiguous"):
        azure.parse_retail_prices(rows + [{**rows[0], "retailPrice": "0.1"}])
    with pytest.raises(ValueError, match="output tariff changed"):
        azure.parse_retail_prices(rows + [{
            **rows[0], "meterName": "Microsoft Decision 1 Output Tokens", "retailPrice": "0.1",
        }])
    assert not provider_model_retired("azure", MODEL, at=datetime(2027, 2, 3, tzinfo=UTC))
    assert provider_model_retired("azure", MODEL, at=datetime(2027, 2, 4, tzinfo=UTC))


def test_price_refresh_preserves_decision_protocol(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    row = sync.manifest_row(_canary_candidate(MODEL), azure.ModelPrice(42_000, 0))
    path = tmp_path / "azure.json"
    path.write_text(json.dumps({"provider": "azure", "models": [row]}))
    monkeypatch.setattr(azure, "MANIFEST_PATH", path)
    result = azure.ProviderPricingResult(
        slug="azure", prices={MODEL: azure.ModelPrice(43_000, 0)}, source="api", fetched_url=azure.URL,
    )
    azure.write_provider_manifest(result)
    updated = json.loads(path.read_text())["models"][0]
    assert updated["model_type"] == "decision" and updated["endpoints"] == ["decide"]
    assert updated["input_token_price_per_m"] == 43_000
    assert updated["output_token_price_per_m"] == 0
    assert updated["azure_model_version"] == "1"


def test_decision_discovery_does_not_need_chat_or_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: datetime(2026, 10, 12, tzinfo=UTC))
    row = _model("Microsoft-Decision-1", chat="false", lifecycle="Preview")
    row["capabilities"] = {"decision": "true", "chatCompletion": "false"}
    row["format"] = "Microsoft"
    selected = sync.select_deployment_candidates(
        [row], [_usage("AIServices.GlobalStandard.Microsoft-Decision-1")],
        frozenset({MODEL}), allowed_versions={MODEL: frozenset({"1"})},
    )
    assert len(selected) == 1
    candidate = selected[0]
    assert candidate.deployment_name == "tr-decision-1"
    calls: list[str] = []
    monkeypatch.setattr(sync, "_decision_canary", lambda *a, **k: calls.append("decision"))
    monkeypatch.setattr(sync, "_text_canary", lambda *a, **k: pytest.fail("chat canary"))
    monkeypatch.setattr(sync, "_tool_canary", lambda *a, **k: pytest.fail("tool canary"))
    sync.canary(candidate, account_key="test")
    sync.canary_with_retries(candidate, account_key="test")
    assert calls == ["decision", "decision"]
    price = azure.parse_retail_prices(_minimum_retail_rows(MODEL))[MODEL]
    manifest = sync.manifest_row(candidate, price)
    assert manifest["model_type"] == "decision"
    assert manifest["endpoints"] == ["decide"]
    assert manifest["context_length"] == 32768


def test_decision_canary_wire_and_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    result = {
        "model": "microsoft-decision-1",
        "answers": {
            "refund": {"type": "noul", "noul": 0.9},
            "team": {"type": "choice", "choice": "billing", "probabilities": {"billing": 0.9, "engineering": 0.1}},
            "urgency": {"type": "score", "score": 1, "probabilities": {"0": 0.1, "1": 0.8, "2": 0.1}},
        },
        "usage": {"input_tokens": 93, "output_tokens": 3},
    }
    def post(url, *, headers, request):
        assert url.endswith("/providers/microsoft/v1/systemone")
        assert headers == {"api-key": "test"}
        assert request["questions"]["refund"]["type"] == "noul"
        return result
    monkeypatch.setattr(sync, "_post_json", post)
    sync.canary(_canary_candidate(MODEL), account_key="test")
    for usage in ({}, {"input_tokens": 0, "output_tokens": 3}, {"input_tokens": 93, "output_tokens": -1}):
        with pytest.raises(RuntimeError, match="decision canary"):
            sync._validate_decision_canary({**result, "usage": usage})
