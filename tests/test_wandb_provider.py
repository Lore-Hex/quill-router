from __future__ import annotations

import json

import pytest

from scripts.pricing.base import ModelPrice
from scripts.pricing.providers import wandb
from scripts.pricing.refresh import PROVIDER_SLUGS
from trusted_router.catalog import (
    GATEWAY_PREPAID_PROVIDER_SLUGS,
    PROVIDERS,
    endpoints_for_model,
)
from trusted_router.services.inference_errors import default_provider_secret_ref

_MODELS = (
    ("DeepSeek V4-Pro", "deepseek-ai/DeepSeek-V4-Pro", "Text", "1049k"),
    ("DeepSeek V4-Flash", "deepseek-ai/DeepSeek-V4-Flash", "Text", "1049k"),
    ("Z.AI GLM 5.2", "zai-org/GLM-5.2", "Text", "262k"),
    ("MiniMax M3", "MiniMaxAI/MiniMax-M3", "Text, Vision", "262k"),
    ("Moonshot AI Kimi K2.7 Code", "moonshotai/Kimi-K2.7-Code", "Text, Vision", "262k"),
    ("Qwen3.8 27B", "Qwen/Qwen3.8-27B", "Text, Vision", "262k"),
    ("OpenAI GPT OSS 120B", "openai/gpt-oss-120b", "Text", "131k"),
    ("IBM Granite 4.1 8B", "ibm-granite/granite-4.1-8b", "Text", "131k"),
    ("Meta Llama 3.3 70B", "meta-llama/Llama-3.3-70B-Instruct", "Text", "128k"),
    ("OpenPipe Qwen3 14B Instruct", "OpenPipe/Qwen3-14B-Instruct", "Text", "32.8k"),
)


def _model_docs_html() -> str:
    rows = "".join(
        "<tr>"
        f"<td>{label}</td><td><code>{native_id}</code></td>"
        f"<td>{modalities}</td><td>{context}</td><td>parameters</td><td>description</td>"
        "</tr>"
        for label, native_id, modalities, context in _MODELS
    )
    return (
        "<table><thead><tr>"
        "<th>Model</th><th>Model ID (for API usage)</th><th>Type</th>"
        "<th>Context Window</th><th>Parameters</th><th>Description</th>"
        f"</tr></thead><tbody>{rows}</tbody></table>"
    )


def _pricing_html() -> str:
    rows = "".join(
        "<tr class='compare-data-row'>"
        f"<th><span data-compare='row-label'>{label}</span></th>"
        f"<td>${index / 10:.2f}</td><td>${index / 5:.2f}</td>"
        f"<td>{'$0.05' if index == 3 else '-'}</td></tr>"
        for index, (label, *_rest) in enumerate(_MODELS, start=1)
    )
    return (
        "<p>Prices shown are per 1 million tokens.</p>"
        "<div class='compare-table'><table data-compare='header-table'><thead><tr>"
        "<th>Model</th><th>Input Tokens</th><th>Output Tokens</th><th>Cache Hit</th>"
        "</tr></thead></table><table data-compare='body-table'><tbody>"
        f"{rows}</tbody></table></div>"
        "<table><thead><tr><th>Model</th><th>Input / 1M</th>"
        "<th>Cached Input / 1M</th><th>Output / 1M</th></tr></thead>"
        "<tbody><tr><td>gpt-5.5</td><td>$5.50</td><td>$1.00</td>"
        "<td>$30.50</td></tr></tbody></table>"
    )


def test_wandb_parses_first_party_models_prices_and_capabilities() -> None:
    documented = wandb._parse_model_docs(_model_docs_html())
    prices = wandb._parse_prices(_pricing_html(), documented_models=documented)

    assert documented["MiniMaxAI/MiniMax-M3"]["input_modalities"] == ["text", "image"]
    assert documented["deepseek-ai/DeepSeek-V4-Pro"]["context_length"] == 1_048_576
    assert prices["z-ai/glm-5.2"] == ModelPrice(
        300_000,
        600_000,
        prompt_cached_micro_per_m=50_000,
    )
    assert prices["deepseek/deepseek-v4-pro"] == ModelPrice(100_000, 200_000)
    assert "openai/gpt-5.5" not in prices


def test_wandb_rejects_layout_or_catalog_collapse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(RuntimeError, match="fewer than 10"):
        wandb._parse_model_docs("<table></table>")
    documented = wandb._parse_model_docs(_model_docs_html())
    with pytest.raises(RuntimeError, match="fewer than 10"):
        wandb._parse_prices(
            "<p>Prices shown are per 1 million tokens.</p><table></table>",
            documented_models=documented,
        )
    bad_context = _model_docs_html().replace("1049k", "2m", 1)
    with pytest.raises(RuntimeError, match="unsupported context window"):
        wandb._parse_model_docs(bad_context)
    with pytest.raises(RuntimeError, match="per-million-token units"):
        wandb._parse_prices(
            _pricing_html().replace("Prices shown are per 1 million tokens.", ""),
            documented_models=documented,
        )
    monkeypatch.setattr(wandb, "_load_model_docs", lambda: documented)
    with pytest.raises(RuntimeError, match="fewer than 10"):
        wandb._normalize_rows([{"id": "deepseek-ai/DeepSeek-V4-Pro"}])


def test_wandb_is_prepaid_standard_privacy_and_hourly_discovered() -> None:
    assert wandb.SLUG in PROVIDER_SLUGS
    assert wandb.SLUG in GATEWAY_PREPAID_PROVIDER_SLUGS
    provider = PROVIDERS[wandb.SLUG]
    assert provider.supports_prepaid is True
    assert provider.supports_byok is False
    assert provider.provider_zero_data_retention is False
    assert provider.provider_confidential_compute is False
    assert provider.provider_e2ee is False
    assert default_provider_secret_ref(wandb.SLUG) == "env://WANDB_API_KEY"


def test_wandb_manifest_is_priced_and_preserves_exact_upstream_ids() -> None:
    raw = json.loads(wandb.MANIFEST_PATH.read_text(encoding="utf-8"))
    assert raw["provider"] == wandb.SLUG
    assert raw["model_count"] >= 20
    rows = {row["id"]: row for row in raw["models"]}
    assert all(row["upstream_id"] for row in rows.values())
    assert all(row["input_token_price_per_m"] > 0 for row in rows.values())
    assert all(row["output_token_price_per_m"] > 0 for row in rows.values())
    assert any(row["upstream_id"] != model_id for model_id, row in rows.items())
    model_id, row = next(iter(rows.items()))
    endpoints = [
        endpoint
        for endpoint in endpoints_for_model(model_id)
        if endpoint.provider == wandb.SLUG
    ]
    assert len(endpoints) == 1
    assert endpoints[0].upstream_id == row["upstream_id"]
