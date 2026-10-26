"""Reasoning-effort metadata: OpenRouter's per-model efforts, narrowed to what the gateway honors.

The gateway rules mirror enclave-go/internal/llm (anthropic_reasoning.go,
adapter.go anthropicEffortBudget, vertex_gemini.go, byok.go
googleAIStudioCompatibleReasoningEffort, reasoning_control.go
validateKimiReasoningEffort, privatemode.go). Each case below names the gateway
behavior it pins, so a gateway change that is not reflected here fails loudly.
"""

from __future__ import annotations

from typing import Any

import pytest

from scripts.pricing import openrouter_reasoning
from trusted_router import reasoning_metadata as rm
from trusted_router.reasoning_metadata import EFFORTS, gateway_effort_limit, reasoning_metadata

ALL = set(EFFORTS)


@pytest.mark.parametrize(
    ("provider", "upstream", "efforts", "can_disable"),
    [
        # Adaptive / manual Claude: output_config.effort verbatim; minimal is not an Anthropic effort.
        ("anthropic", "claude-opus-4-8", ALL - {"minimal"}, True),
        ("anthropic", "claude-sonnet-4.6", ALL - {"minimal"}, True),
        ("anthropic", "claude-opus-4-5-20251101", ALL - {"minimal"}, True),
        ("amazon-bedrock", "us.anthropic.claude-fable-5", ALL - {"minimal"}, True),
        ("google-vertex", "claude-opus-5", ALL - {"minimal"}, True),
        # Budget-only Claude: xhigh duplicates high, max and minimal turn thinking off.
        ("anthropic", "claude-sonnet-4-20250514", {"none", "low", "medium", "high"}, True),
        # Vertex Gemini 2.5: budgets; minimal is low's budget; xhigh/max unmapped.
        ("google-vertex", "gemini-2.5-flash", {"none", "low", "medium", "high"}, True),
        # Vertex Gemini 3+: levels; none becomes low, so reasoning cannot be turned off.
        ("google-vertex", "gemini-3.6-flash", {"minimal", "low", "medium", "high"}, False),
        ("google-vertex", "gemini-3-flash-preview", {"minimal", "low", "medium", "high"}, False),
        ("google-vertex", "gemini-3.1-pro-preview", {"low", "medium", "high"}, False),
        ("google-vertex", "gemini-3.8-flash", {"low", "medium", "high"}, False),
        # AI Studio Gemini >= 3.7 rewrites none/minimal to low.
        ("google-ai-studio", "gemini-3.8-flash", ALL - {"none", "minimal"}, False),
        ("gemini", "gemini-4-argon", ALL - {"none", "minimal"}, False),
        # Kimi K3 rejects anything but low/high/max.
        ("kimi", "kimi-k3", {"low", "high", "max"}, False),
        # Privatemode: low/high; medium for gpt-oss-120b; max for glm-*; cannot disable.
        ("privatemode", "glm-5.3", {"low", "high", "max"}, False),
        ("privatemode", "gpt-oss-120b", {"low", "medium", "high"}, False),
    ],
)
def test_gateway_limits_mirror_the_gateway(
    provider: str, upstream: str, efforts: set[str], can_disable: bool
) -> None:
    limit = gateway_effort_limit(provider, upstream)
    assert limit is not None
    assert set(limit.efforts) == efforts
    assert limit.can_disable is can_disable


@pytest.mark.parametrize(
    ("provider", "upstream"),
    [
        ("openai", "gpt-5.4"),  # reasoning_effort verbatim
        ("deepinfra", "anthropic/claude-sonnet-4"),  # pass-through host: no gateway mapping
        ("google-ai-studio", "gemini-3.6-flash"),  # below 3.7: verbatim
        ("kimi", "kimi-k2.6"),
        ("together", "deepseek-ai/DeepSeek-V4"),
    ],
)
def test_verbatim_routes_add_no_limit(provider: str, upstream: str) -> None:
    assert gateway_effort_limit(provider, upstream) is None


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, Any]]:
    models: dict[str, dict[str, Any]] = {
        "anthropic/claude-sonnet-4": {
            "mandatory": False,
            "default_enabled": False,
            "supported_efforts": ["max", "xhigh", "high", "medium", "low", "minimal", "none"],
            "default_effort": "max",
        },
        "google/gemini-3.8-flash": {
            "mandatory": False,
            "default_enabled": True,
            "supported_efforts": ["high", "medium", "low", "minimal", "none"],
            "default_effort": "medium",
        },
    }
    monkeypatch.setattr(rm, "openrouter_reasoning", models.get)
    return models


def test_efforts_are_intersected_across_every_route(published: dict[str, Any]) -> None:
    # Direct Anthropic is budget-only for this model; DeepInfra forwards verbatim.
    # A request can fail over to either, so only what both honor is published.
    result = reasoning_metadata(
        "anthropic/claude-sonnet-4",
        ["tools", "reasoning"],
        [("deepinfra", "anthropic/claude-sonnet-4"), ("anthropic", "claude-sonnet-4-20250514")],
    )
    assert result == {
        "mandatory": False,
        "default_enabled": False,
        "supported_efforts": ["high", "medium", "low", "none"],
        "default_effort": None,  # OpenRouter's default (max) is not honored here
    }


def test_a_route_that_cannot_disable_makes_reasoning_mandatory(published: dict[str, Any]) -> None:
    result = reasoning_metadata(
        "google/gemini-3.8-flash",
        ["reasoning"],
        [("google-ai-studio", "gemini-3.8-flash"), ("google-vertex", "gemini-3.8-flash")],
    )
    assert result == {
        "mandatory": True,
        "default_enabled": True,
        "supported_efforts": ["high", "medium", "low"],
        "default_effort": "medium",
    }


def test_verbatim_routes_publish_openrouters_metadata_unchanged(published: dict[str, Any]) -> None:
    source = published["anthropic/claude-sonnet-4"]
    result = reasoning_metadata("anthropic/claude-sonnet-4", ["reasoning"], [("deepinfra", "x")])
    assert result == source


def test_no_metadata_without_the_reasoning_parameter_or_a_published_source(published: dict[str, Any]) -> None:
    assert reasoning_metadata("anthropic/claude-sonnet-4", ["tools"], [("deepinfra", "x")]) is None
    assert reasoning_metadata("unknown/model", ["reasoning"], [("deepinfra", "x")]) is None


def test_checked_in_data_is_well_formed() -> None:
    data = rm._openrouter_reasoning()
    assert len(data) >= openrouter_reasoning.MIN_MODELS
    for model_id, entry in data.items():
        assert set(entry) == {"mandatory", "default_enabled", "supported_efforts", "default_effort"}, model_id
        assert set(entry["supported_efforts"]) <= ALL, model_id
        assert entry["default_effort"] is None or entry["default_effort"] in ALL, model_id


def test_capture_keeps_only_well_formed_reasoning_rows() -> None:
    document = {
        "data": [
            {"id": "a/ok", "reasoning": {"mandatory": True, "default_enabled": True,
                                         "supported_efforts": ["high", "low"], "default_effort": "high"}},
            {"id": "a/no-reasoning"},
            {"id": "a/bad-effort", "reasoning": {"supported_efforts": ["ultra"]}},
            {"id": "a/odd-default", "reasoning": {"supported_efforts": ["low"], "default_effort": "turbo"}},
            {"reasoning": {"supported_efforts": ["low"]}},
        ]
    }
    assert openrouter_reasoning.parse(document) == {
        "a/ok": {"mandatory": True, "default_enabled": True, "supported_efforts": ["high", "low"], "default_effort": "high"},
        "a/odd-default": {"mandatory": False, "default_enabled": False, "supported_efforts": ["low"], "default_effort": None},
    }
    with pytest.raises(ValueError):
        openrouter_reasoning.parse({"data": "nope"})


def test_capture_keeps_the_existing_file_when_the_fetch_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    target = tmp_path / "openrouter_reasoning.json"
    target.write_text("existing", encoding="utf-8")
    monkeypatch.setattr(openrouter_reasoning, "DATA_PATH", target)

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise openrouter_reasoning.httpx.ConnectError("offline")

    monkeypatch.setattr(openrouter_reasoning.httpx, "get", boom)
    assert openrouter_reasoning.main() == 0
    assert target.read_text(encoding="utf-8") == "existing"


def test_catalog_rows_publish_the_reasoning_object() -> None:
    from trusted_router.catalog import MODELS, model_to_openrouter_shape

    rows = [model_to_openrouter_shape(model) for model in MODELS.values()]
    with_reasoning = [row for row in rows if "reasoning" in row]
    assert with_reasoning, "no catalog model publishes reasoning metadata"
    for row in with_reasoning:
        assert "reasoning" in row["supported_parameters"], row["id"]
        assert set(row["reasoning"]["supported_efforts"]) <= ALL, row["id"]
