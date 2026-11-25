"""SiliconFlow prices come from its model library, not the curated /pricing page.

From 2026-08-13, when discovery first wrote every authenticated /v1/models row
into the manifest, the chat models that https://www.siliconflow.com/pricing
does not feature (DeepSeek-R1, Qwen2.5, Ling, Seed-OSS, Qwen3-VL, ...) sat at
``awaiting-price``: the parser read only that page's "LLM - Desktop" cards.
https://www.siliconflow.com/models lists every serverless model as a CMS card
with labelled price fields. The fixture is a capture of that page.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.pricing import base
from scripts.pricing.base import configure_runtime_required_models, normalize_parser_input
from scripts.pricing.model_ids import canonicalize_native_model_id
from scripts.pricing.parsers import siliconflow as parser
from scripts.pricing.providers import siliconflow

_FIXTURE = Path(__file__).parent / "fixtures" / "pricing" / "siliconflow.html"

# USD per 1M tokens on the 2026-11-24 capture, as microdollars per 1M.
_LIBRARY_PRICES = {
    "deepseek/deepseek-r1": (500_000, 2_180_000, None),
    "qwen/qwen2.5-7b-instruct": (50_000, 50_000, None),
    "inclusionai/ling-flash-2.0": (140_000, 570_000, None),
    "bytedance-seed/seed-oss-36b-instruct": (210_000, 570_000, None),
    "qwen/qwen3-vl-30b-a3b-thinking": (290_000, 1_000_000, None),
    "qwen/qwen3-vl-32b-thinking": (200_000, 1_500_000, None),
    "meituan-longcat/longcat-2.0": (750_000, 2_950_000, 15_000),
    "stepfun-ai/step-3.5-flash": (100_000, 300_000, None),
    "tencent/hy4-preview": (834_000, 2_501_000, 42_000),
    "deepseek/deepseek-v3.2": (270_000, 420_000, 140_000),
    "moonshotai/kimi-k2.5": (500_000, 2_500_000, 150_000),
}

_NON_CHAT_NATIVE_IDS = (
    "black-forest-labs/FLUX.1-dev",
    "Qwen/Qwen-Image",
    "Tongyi-MAI/Z-Image-Turbo",
    "fishaudio/fish-speech-1.5",
    "FunAudioLLM/CosyVoice2-0.5B",
    "IndexTeam/IndexTTS-2",
    "Qwen/Qwen3-Embedding-8B",
    "Qwen/Qwen3-Reranker-8B",
    "MiniMaxAI/MiniMax-H3",
)


def _parse_fixture() -> dict:
    return parser.parse(normalize_parser_input(_FIXTURE.read_text(encoding="utf-8")))


def test_model_library_prices_chat_models_missing_from_pricing_page() -> None:
    prices = _parse_fixture()

    for model_id, (prompt, completion, cached) in _LIBRARY_PRICES.items():
        row = prices[model_id]
        assert row["prompt_micro_per_m"] == prompt, model_id
        assert row["completion_micro_per_m"] == completion, model_id
        assert row.get("prompt_cached_micro_per_m") == cached, model_id


def test_model_library_never_prices_non_token_cards_as_chat() -> None:
    prices = _parse_fixture()
    lowered = {key.split("/", 1)[-1] for key in prices}

    for native_id in _NON_CHAT_NATIVE_IDS:
        assert native_id.split("/", 1)[1].casefold() not in lowered, native_id
    # The systemone Kev-4B card has no output price and is not chat.
    assert "kev-4b" not in lowered


def test_missing_cms_number_drops_only_the_cached_rate() -> None:
    # Qwen3-Coder-480B-A35B's card renders its cached input as "nan".
    row = _parse_fixture()["qwen/qwen3-coder-480b-a35b"]

    assert row == {"prompt_micro_per_m": 250_000, "completion_micro_per_m": 1_000_000}


def _unexpected_self_heal(**_kwargs: object) -> str:
    pytest.fail("the committed model-library fixture must parse deterministically")


def test_refresh_publishes_library_priced_rows_and_keeps_non_chat_dark(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    chat_native_ids = (
        "deepseek-ai/DeepSeek-R1",
        "Qwen/Qwen2.5-7B-Instruct",
        "inclusionAI/Ling-flash-2.0",
        "ByteDance-Seed/Seed-OSS-36B-Instruct",
        "tencent/Hy4-preview",
    )
    manifest = tmp_path / "siliconflow.json"
    manifest.write_text(
        json.dumps(
            {
                "provider": "siliconflow",
                "models": [
                    {
                        "id": base_id,
                        "upstream_id": native,
                        "model_type": "chat",
                        "endpoints": ["chat/completions"],
                        "routable": False,
                        "routable_reason": "awaiting-price",
                        "unresolved_since": "2026-08-13",
                    }
                    for native in (*chat_native_ids, *_NON_CHAT_NATIVE_IDS)
                    for base_id in [canonicalize_native_model_id(native)]
                    if base_id
                ],
            }
        ),
        encoding="utf-8",
    )
    fixture = _FIXTURE.read_text(encoding="utf-8")
    monkeypatch.setenv("SILICON_FLOW_API_KEY", "test-key")
    monkeypatch.setattr(siliconflow, "UPSTREAM_ID_MAP", {})
    monkeypatch.setattr(siliconflow, "MANIFEST_PATH", manifest)
    monkeypatch.setattr(base, "fetch_html", lambda *_args, **_kwargs: fixture)
    monkeypatch.setattr(base, "self_heal_parser", _unexpected_self_heal)
    monkeypatch.setattr(
        siliconflow,
        "fetch_json",
        lambda *_args, **_kwargs: {
            "data": [{"id": native} for native in (*chat_native_ids, *_NON_CHAT_NATIVE_IDS)]
        },
    )
    configure_runtime_required_models({})
    try:
        result = siliconflow.fetch()
        siliconflow.write_provider_manifest(result)
    finally:
        configure_runtime_required_models({})

    assert result.fetched_url == "https://www.siliconflow.com/models"
    rows = {row["id"]: row for row in json.loads(manifest.read_text())["models"]}
    assert rows["deepseek/deepseek-r1"]["input_token_price_per_m"] == 500_000
    assert rows["deepseek/deepseek-r1"]["output_token_price_per_m"] == 2_180_000
    for native in chat_native_ids:
        row = rows[canonicalize_native_model_id(native)]
        assert row.get("routable") is not False, native
        assert "routable_reason" not in row and "unresolved_since" not in row, native
    for native in _NON_CHAT_NATIVE_IDS:
        row = rows[canonicalize_native_model_id(native)]
        assert row["routable"] is False, native
        assert "input_token_price_per_m" not in row, native
