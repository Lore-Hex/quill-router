from __future__ import annotations

from pathlib import Path

import pytest

from scripts.pricing import base, refresh
from scripts.pricing.base import configure_runtime_required_models
from scripts.pricing.providers import deepseek, siliconflow

_DATED_MODEL = "deepseek/deepseek-v4-flash-0731"
_GENERIC_MODEL = "deepseek/deepseek-v4-flash"
_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "pricing"


def _unexpected_self_heal(**_kwargs: object) -> str:
    pytest.fail("approved version aliases must not invoke parser self-healing")


def test_siliconflow_discovers_dated_model_and_reuses_family_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native_id = "deepseek-ai/DeepSeek-V4-Flash-0731"
    fixture = (_FIXTURE_DIR / "siliconflow.html").read_text(encoding="utf-8")
    monkeypatch.setenv("SILICON_FLOW_API_KEY", "test-key")
    monkeypatch.setattr(siliconflow, "UPSTREAM_ID_MAP", {})
    monkeypatch.setattr(base, "fetch_html", lambda *_args, **_kwargs: fixture)
    monkeypatch.setattr(base, "self_heal_parser", _unexpected_self_heal)
    monkeypatch.setattr(
        siliconflow,
        "fetch_json",
        lambda *_args, **_kwargs: {"data": [{"id": native_id}]},
    )
    configure_runtime_required_models({})
    try:
        result = siliconflow.fetch()
    finally:
        configure_runtime_required_models({})

    assert result.source == "deterministic"
    assert result.prices[_DATED_MODEL] == result.prices[_GENERIC_MODEL]
    assert siliconflow.UPSTREAM_ID_MAP[_DATED_MODEL] == native_id
    assert any("approved price aliases" in note for note in result.notes)

    merged = refresh._merge_snapshot(
        {
            "models": [
                {
                    "id": _DATED_MODEL,
                    "name": "DeepSeek V4 Flash 0731",
                    "created": 1,
                    "context_length": 1_048_576,
                    "architecture": {"output_modalities": ["text"]},
                    "pricing": {"prompt": "0.00000014", "completion": "0.00000028"},
                    "endpoints": [
                        {
                            "provider_name": "SiliconFlow",
                            "tr_provider_slug": "siliconflow",
                            "model_id": _DATED_MODEL,
                            "context_length": 1_048_576,
                            "pricing": {
                                "prompt": "0.00000014",
                                "completion": "0.00000028",
                            },
                        }
                    ],
                }
            ],
            "tr_keyed_providers": ["siliconflow"],
        },
        {_DATED_MODEL: {"siliconflow": result.prices[_DATED_MODEL]}},
        set(),
    )
    model = merged["models"][0]
    assert model["id"] == _DATED_MODEL
    assert model["endpoints"][0]["model_id"] == native_id
    assert model["endpoints"][0]["pricing"]["prompt"] == (
        refresh._micro_per_m_to_dollars_per_token(
            result.prices[_DATED_MODEL].prompt_micro_per_m
        )
    )


def test_deepseek_dated_model_uses_official_family_price_and_native_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = (_FIXTURE_DIR / "deepseek.html").read_text(encoding="utf-8")
    monkeypatch.setattr(deepseek, "UPSTREAM_ID_MAP", {})
    monkeypatch.setattr(base, "fetch_html", lambda *_args, **_kwargs: fixture)
    monkeypatch.setattr(base, "self_heal_parser", _unexpected_self_heal)
    configure_runtime_required_models({})
    try:
        result = deepseek.fetch()
    finally:
        configure_runtime_required_models({})

    assert result.source == "deterministic"
    assert result.prices[_DATED_MODEL] == result.prices[_GENERIC_MODEL]
    assert deepseek.UPSTREAM_ID_MAP[_DATED_MODEL] == "deepseek-v4-flash"
    assert any("approved price aliases" in note for note in result.notes)
