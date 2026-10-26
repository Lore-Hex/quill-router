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
from trusted_router.reasoning_metadata import (
    EFFORTS,
    KEEP_SOURCE_DEFAULT,
    gateway_effort_limit,
    reasoning_metadata,
)

ALL = set(EFFORTS)


@pytest.mark.parametrize(
    ("provider", "upstream", "efforts", "can_disable"),
    [
        # Adaptive / manual Claude: output_config.effort verbatim; minimal is not an Anthropic effort.
        ("anthropic", "claude-opus-4-8", ALL - {"minimal"}, True),
        ("anthropic", "claude-sonnet-4.6", ALL - {"minimal"}, True),
        ("azure", "claude-fable-5", ALL - {"minimal"}, True),
        ("google-vertex", "claude-opus-5", ALL - {"minimal"}, True),
        # Opus 4.5: effort plus a budget; "max" sets no budget, so thinking is off.
        ("anthropic", "claude-opus-4-5-20251101", {"none", "low", "medium", "high", "xhigh"}, True),
        # Budget-only Claude: xhigh duplicates high, max and minimal turn thinking off.
        ("anthropic", "claude-sonnet-4-20250514", {"none", "low", "medium", "high"}, True),
        # Vertex Gemini 2.5: budgets; minimal is low's budget; xhigh/max unmapped.
        ("google-vertex", "gemini-2.5-flash", {"none", "low", "medium", "high"}, True),
        # An author-prefixed upstream id is resolved like the gateway's directModelID.
        ("google-vertex", "google/gemini-2.5-flash", {"none", "low", "medium", "high"}, True),
        # Vertex Gemini 3+: levels; none becomes low, so reasoning cannot be turned off.
        ("google-vertex", "gemini-3.6-flash", {"minimal", "low", "medium", "high"}, False),
        ("google-vertex", "gemini-3-flash-preview", {"minimal", "low", "medium", "high"}, False),
        ("google-vertex", "gemini-3.1-pro-preview", {"low", "medium", "high"}, False),
        ("google-vertex", "gemini-3.8-flash", {"low", "medium", "high"}, False),
        # AI Studio Gemini >= 3.7 rewrites none/minimal to low.
        ("google-ai-studio", "gemini-3.8-flash", ALL - {"none", "minimal"}, False),
        # The legacy `gemini` slug is served by Vertex (multi.go).
        ("gemini", "gemini-3-flash-preview", {"minimal", "low", "medium", "high"}, False),
        # Kimi K3 rejects anything but low/high/max.
        ("kimi", "kimi-k3", {"low", "high", "max"}, False),
        # Tencent TokenHub: these models are always thinking.
        ("tencent", "glm-5.3", ALL, False),
        ("tencent", "kimi-k2.8-preview", ALL, False),
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
        # Public amazon-bedrock is the OpenAI-compatible Mantle client, not the Anthropic wire.
        ("amazon-bedrock", "us.anthropic.claude-sonnet-4"),
        ("azure", "gpt-5.4"),  # Azure non-Claude: OpenAI client
        ("deepinfra", "anthropic/claude-sonnet-4"),  # pass-through host: no gateway mapping
        ("google-ai-studio", "gemini-3.1-pro-preview"),  # below 3.7, not flash: verbatim, no default
        ("kimi", "kimi-k2.6"),
        ("together", "deepseek-ai/DeepSeek-V4"),
    ],
)
def test_verbatim_routes_add_no_limit(provider: str, upstream: str) -> None:
    assert gateway_effort_limit(provider, upstream) is None


@pytest.mark.parametrize(
    ("provider", "upstream"),
    [
        ("google-ai-studio", "gemini-3.1-flash-image"),  # native image route: no thinking config
        ("google-vertex", "gemini-3.1-flash-image-preview"),
        ("privatemode", "deepseek-ocr-2"),  # OCR rejects any reasoning setting
        ("engy", "qwen3.6-35b-a3b"),  # always sent enable_thinking=false
    ],
)
def test_routes_that_ignore_reasoning_controls(provider: str, upstream: str) -> None:
    limit = gateway_effort_limit(provider, upstream)
    assert limit is not None
    assert limit.controllable is False


@pytest.mark.parametrize(
    ("provider", "upstream", "default"),
    [
        ("google-vertex", "gemini-2.5-flash", "none"),  # budget 0
        ("google-vertex", "gemini-3.6-flash", "minimal"),
        ("google-vertex", "gemini-3.8-flash", "low"),
        ("google-vertex", "gemini-3.1-pro-preview", KEEP_SOURCE_DEFAULT),  # left to Google
        ("google-ai-studio", "gemini-2.5-flash", "none"),
        ("google-ai-studio", "gemini-3-flash-preview", "minimal"),
        ("google-ai-studio", "gemini-3.8-flash", "low"),
        # The AI Studio default injector only knows gemini-2.5* and gemini-3* flash ids.
        ("google-ai-studio", "gemini-4-flash", KEEP_SOURCE_DEFAULT),
    ],
)
def test_gateway_defaults_when_the_caller_sends_no_effort(provider: str, upstream: str, default: str) -> None:
    limit = gateway_effort_limit(provider, upstream)
    assert (limit.default if limit is not None else KEEP_SOURCE_DEFAULT) == default


def test_azure_custom_deployment_takes_the_anthropic_wire_with_budget_rules() -> None:
    # azure.go picks the Anthropic wire from the requested model; anthropicChatReasoningBody
    # then classifies by the deployment name, which matches no effort class.
    limit = gateway_effort_limit("azure", "production-chat", "anthropic/claude-opus-4.7")
    assert limit is not None
    assert set(limit.efforts) == {"none", "low", "medium", "high"}
    assert gateway_effort_limit("azure", "production-chat", "openai/gpt-5.4") is None


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
        # Both routes send "low" when the caller sends nothing, not OpenRouter's "medium".
        "default_effort": "low",
    }


def test_a_gateway_default_equal_to_the_source_effort_still_enables_reasoning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = {"mandatory": False, "default_enabled": False,
              "supported_efforts": ["minimal", "medium"], "default_effort": "minimal"}
    monkeypatch.setattr(rm, "openrouter_reasoning", {"google/gemini-3-flash-preview": source}.get)
    result = reasoning_metadata(
        "google/gemini-3-flash-preview", ["reasoning"], [("google-ai-studio", "gemini-3-flash-preview")]
    )
    assert result is not None
    # AI Studio injects "minimal" when the caller sends nothing: reasoning is on by default.
    assert (result["default_enabled"], result["default_effort"]) == (True, "minimal")


def test_routes_that_all_enable_by_default_keep_enabled_even_with_different_efforts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = {"mandatory": False, "default_enabled": True,
              "supported_efforts": ["minimal", "medium"], "default_effort": "medium"}
    monkeypatch.setattr(rm, "openrouter_reasoning", {"google/gemini-3-flash-preview": source}.get)
    result = reasoning_metadata(
        "google/gemini-3-flash-preview",
        ["reasoning"],
        [("google-ai-studio", "gemini-3-flash-preview"), ("atlas-cloud", "google/gemini-3-flash-preview")],
    )
    assert result is not None
    assert (result["default_enabled"], result["default_effort"]) == (True, None)


def test_mandatory_source_keeps_its_default_enabled_when_routes_keep_upstream_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = {"mandatory": True, "default_enabled": False, "supported_efforts": [], "default_effort": None}
    monkeypatch.setattr(rm, "openrouter_reasoning", {"a/m": source}.get)
    assert reasoning_metadata("a/m", ["reasoning"], [("deepinfra", "a/m")]) == source


def test_disagreeing_route_defaults_publish_no_default(published: dict[str, Any]) -> None:
    # AI Studio defaults to "low"; DeepInfra leaves it to the model (OpenRouter's "medium").
    result = reasoning_metadata(
        "google/gemini-3.8-flash",
        ["reasoning"],
        [("google-ai-studio", "gemini-3.8-flash"), ("deepinfra", "google/gemini-3.8-flash")],
    )
    assert result is not None
    assert result["default_effort"] is None


def test_a_route_that_ignores_reasoning_publishes_nothing(published: dict[str, Any]) -> None:
    assert reasoning_metadata(
        "google/gemini-3.8-flash",
        ["reasoning"],
        [("deepinfra", "x"), ("google-ai-studio", "gemini-3.8-flash-image")],
    ) is None


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
            {"id": "a/null-reasoning", "reasoning": None},
            # OpenRouter omits the optional fields on mandatory-only models.
            {"id": "a/mandatory-only", "reasoning": {"mandatory": True}},
            {"reasoning": {"mandatory": True, "supported_efforts": ["low"]}},
        ]
    }
    assert openrouter_reasoning.parse(document) == {
        "a/ok": {"mandatory": True, "default_enabled": True, "supported_efforts": ["high", "low"], "default_effort": "high"},
        "a/mandatory-only": {"mandatory": True, "default_enabled": False, "supported_efforts": [], "default_effort": None},
    }
    # One malformed reasoning object anywhere rejects the whole response.
    for bad in (
        {"mandatory": False, "supported_efforts": ["low"], "default_effort": "turbo"},
        {"mandatory": False, "supported_efforts": ["low"], "default_effort": []},
        {"supported_efforts": ["ultra"]},
        {},
    ):
        with pytest.raises(ValueError, match="malformed reasoning"):
            openrouter_reasoning.parse({"data": [*document["data"], {"id": "a/bad", "reasoning": bad}]})
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


@pytest.mark.parametrize(
    "rows",
    [
        [{"id": f"m/{i}", "reasoning": {}} for i in range(200)],  # many empty rows
        [{"id": f"m/{i}", "reasoning": {"mandatory": True, "default_effort": []}} for i in range(200)],
        # Enough valid rows to pass MIN_MODELS, plus one malformed: still rejected.
        [*({"id": f"m/{i}", "reasoning": {"mandatory": True}} for i in range(150)),
         {"id": "google/gemini-3-flash-preview", "reasoning": {}}],
    ],
)
def test_capture_keeps_the_existing_file_on_malformed_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, rows: list[dict[str, Any]]
) -> None:
    target = tmp_path / "openrouter_reasoning.json"
    target.write_text("existing", encoding="utf-8")
    monkeypatch.setattr(openrouter_reasoning, "DATA_PATH", target)

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"data": rows}

    monkeypatch.setattr(openrouter_reasoning.httpx, "get", lambda *_a, **_k: Response())
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
