"""Per-model reasoning-effort metadata in OpenRouter's `reasoning` shape.

OpenRouter publishes, on every reasoning model in `GET /v1/models`::

    "reasoning": {"mandatory": bool, "default_enabled": bool,
                  "supported_efforts": ["none", "low", ...], "default_effort": "high"}

Clients use it to offer exactly the thinking levels a model takes (pi builds its
thinking-level map from it). TrustedRouter serves the same model ids, so the
model-native part is the same fact: which efforts the model accepts. That comes
from OpenRouter's published metadata, captured hourly into
`data/openrouter_reasoning.json` by `scripts/pricing/openrouter_reasoning.py`.

What TrustedRouter adds is its own gateway. Most routes forward an effort to the
upstream unchanged, but some map it, reject it, ignore it, or substitute their
own default, and an effort the gateway silently collapses (Claude `max` on a
budget-only route turns thinking OFF) must not be advertised. So each model
publishes OpenRouter's efforts intersected with what the gateway honors on EVERY
one of its routes, because a request can fail over to any of them:

- a route that cannot turn reasoning off makes the model mandatory;
- a route that ignores reasoning controls entirely means nothing is published;
- the default is the gateway's own where every route agrees on it, else null.

`gateway_effort_limit` mirrors enclave-go/internal/llm: anthropic_reasoning.go
and adapter.go (Claude on the Anthropic wire, incl. azure.go and Vertex),
vertex_gemini.go, byok.go (AI Studio defaults and >= 3.7 rewrite),
ai_studio_gemini.go (native image route), reasoning_control.go (Kimi K3, Tencent),
privatemode.go. A route not listed there forwards efforts verbatim and adds no
limit: the upstream's accepted set, which OpenRouter's metadata records, is the
answer. Not representable per route, and so not reflected here: Kimi K2.5/K2.6
force thinking off when tools are present, and OpenAI's Responses route rejects
an enabled-only reasoning object (an explicit effort is still honored).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

EFFORTS: tuple[str, ...] = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
_ALL = frozenset(EFFORTS)
# The route leaves the default to the upstream, which is what OpenRouter reports.
KEEP_SOURCE_DEFAULT = "<source>"

_DATA_PATH = Path(__file__).parent / "data" / "openrouter_reasoning.json"

# anthropic_reasoning.go: substrings over the lower-cased upstream id with "." -> "-".
_ANTHROPIC_ADAPTIVE = (
    "claude-opus-4-6", "claude-opus-4-7", "claude-opus-4-8", "claude-opus-5",
    "claude-sonnet-4-6", "claude-sonnet-5", "claude-fable-5",
)
_ANTHROPIC_MANUAL = "claude-opus-4-5"
_TENCENT_ALWAYS_THINKING = frozenset({
    "glm-5.3", "glm-5.3-flash", "glm-5.3-flashx", "kimi-k2.7-code", "kimi-k2.7-code-highspeed",
    "kimi-k3", "kimi-k2.8-preview",
})


@dataclass(frozen=True)
class EffortLimit:
    """What one route honors.

    `controllable` False means the route ignores reasoning controls entirely.
    `default` is the effort the gateway sends when the caller sends none, or
    KEEP_SOURCE_DEFAULT when it leaves that to the upstream.
    """

    efforts: frozenset[str]
    can_disable: bool
    controllable: bool = True
    default: str = KEEP_SOURCE_DEFAULT


_IGNORED = EffortLimit(efforts=frozenset(), can_disable=False, controllable=False)


def _model_name(upstream_id: str | None) -> str:
    # byok.go directModelID drops an author prefix ("google/gemini-2.5-flash").
    model = (upstream_id or "").strip().lower()
    return model.rsplit("/", 1)[-1]


def _gemini_version_at_least(model: str, major: int, minor: int) -> bool:
    # gemini_version.go geminiVersionAtLeast: "gemini-3-flash" is 3.0.
    match = re.match(r"(\d+)(?:\.(\d+))?", model.removeprefix("gemini-"))
    if not match:
        return False
    found_major, found_minor = int(match.group(1)), int(match.group(2) or 0)
    return found_major > major or (found_major == major and found_minor >= minor)


def _gemini_flash_default(model: str) -> str:
    # gemini_version.go geminiFlashDefaultThinkingLevel.
    return "low" if _gemini_version_at_least(model, 3, 7) else "minimal"


def _claude_on_anthropic_wire(provider: str, model: str) -> bool:
    if provider == "anthropic":
        return True
    # azure.go azureUsesAnthropicWire; multi.go routes a claude-* Vertex upstream to gcp.go.
    if provider == "azure":
        return model.startswith("claude-")
    return provider == "google-vertex" and "claude" in model


def gateway_effort_limit(provider: str, upstream_id: str | None) -> EffortLimit | None:
    """The gateway's own limit on one route, or None where it forwards efforts verbatim."""
    model = _model_name(upstream_id)
    if _claude_on_anthropic_wire(provider, model):
        claude = model.replace(".", "-")
        if any(prefix in claude for prefix in _ANTHROPIC_ADAPTIVE):
            # output_config.effort verbatim with adaptive thinking; "minimal" is not an
            # Anthropic effort.
            return EffortLimit(efforts=_ALL - {"minimal"}, can_disable=True)
        if _ANTHROPIC_MANUAL in claude:
            # Effort plus a thinking budget for low/medium/high/xhigh; "max" sets the
            # effort but no budget, so thinking is off.
            return EffortLimit(efforts=frozenset({"none", "low", "medium", "high", "xhigh"}), can_disable=True)
        # Budget-only (adapter.go anthropicEffortBudget): xhigh is high's budget, max and
        # minimal turn thinking off.
        return EffortLimit(efforts=frozenset({"none", "low", "medium", "high"}), can_disable=True)
    gemini_route = provider in {"google-vertex", "google-ai-studio", "gemini"} and "gemini" in model
    if gemini_route and "image" in model:
        # vertexGeminiImageModel / googleAIStudioNeedsNativeImage: no thinking config is sent.
        return _IGNORED
    if provider == "google-vertex" and gemini_route:
        flash = "flash" in model
        if model.startswith("gemini-2.5"):
            # Budgets: none 0, low 1024, medium 8192, high 24576; minimal is low's budget,
            # xhigh/max are unmapped. Flash defaults to budget 0.
            return EffortLimit(
                efforts=frozenset({"none", "low", "medium", "high"}),
                can_disable=True,
                default="none" if flash else KEEP_SOURCE_DEFAULT,
            )
        # Gemini 3+: a thinking level; "none" becomes "low", so it cannot be turned off.
        efforts = {"low", "medium", "high"}
        if "pro" not in model and not _gemini_version_at_least(model, 3, 7):
            efforts.add("minimal")
        return EffortLimit(
            efforts=frozenset(efforts),
            can_disable=False,
            default=_gemini_flash_default(model) if flash else KEEP_SOURCE_DEFAULT,
        )
    if provider in {"google-ai-studio", "gemini"} and gemini_route:
        # byok.go googleAIStudioDefaultReasoningEffort: flash models get a default.
        default = KEEP_SOURCE_DEFAULT
        if "flash" in model:
            if model.startswith("gemini-2.5"):
                default = "none"
            elif model.startswith("gemini-3") or _gemini_version_at_least(model, 3, 0):
                default = _gemini_flash_default(model)
        if _gemini_version_at_least(model, 3, 7):
            # none/off/minimal are rewritten to "low" before the request leaves.
            return EffortLimit(efforts=_ALL - {"none", "minimal"}, can_disable=False, default=default)
        if default != KEEP_SOURCE_DEFAULT:
            return EffortLimit(efforts=_ALL, can_disable=True, default=default)
        return None
    if provider == "kimi" and model == "kimi-k3":
        return EffortLimit(efforts=frozenset({"low", "high", "max"}), can_disable=False)
    if provider == "tencent" and model in _TENCENT_ALWAYS_THINKING:
        return EffortLimit(efforts=_ALL, can_disable=False)
    if provider == "privatemode":
        if model == "deepseek-ocr-2":
            return _IGNORED
        efforts = {"low", "high"}
        if model == "gpt-oss-120b":
            efforts.add("medium")
        if model.startswith("glm-"):
            efforts.add("max")
        return EffortLimit(efforts=frozenset(efforts), can_disable=False)
    return None


@cache
def _openrouter_reasoning() -> dict[str, dict[str, Any]]:
    try:
        document = json.loads(_DATA_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    models = document.get("models") if isinstance(document, dict) else None
    return models if isinstance(models, dict) else {}


def openrouter_reasoning(model_id: str) -> dict[str, Any] | None:
    """OpenRouter's published reasoning metadata for a model id, if it publishes any."""
    return _openrouter_reasoning().get(model_id)


def reasoning_metadata(
    model_id: str,
    supported_parameters: Iterable[str],
    routes: Iterable[tuple[str, str | None]],
) -> dict[str, Any] | None:
    """The `reasoning` object for one catalog model, or None when it publishes none.

    `routes` are the model's (provider slug, upstream id) pairs. None unless the
    model takes the `reasoning` parameter, OpenRouter publishes metadata for it,
    and every route honors reasoning controls.
    """
    if "reasoning" not in set(supported_parameters):
        return None
    source = openrouter_reasoning(model_id)
    if source is None:
        return None
    efforts = [effort for effort in source.get("supported_efforts", []) if effort in _ALL]
    mandatory = bool(source.get("mandatory", False))
    source_default = source.get("default_effort")
    defaults: set[str | None] = set()
    for provider, upstream_id in routes:
        limit = gateway_effort_limit(provider, upstream_id)
        if limit is None:
            defaults.add(source_default)
            continue
        if not limit.controllable:
            return None
        efforts = [effort for effort in efforts if effort in limit.efforts]
        if not limit.can_disable:
            mandatory = True
        defaults.add(source_default if limit.default == KEEP_SOURCE_DEFAULT else limit.default)
    if mandatory:
        efforts = [effort for effort in efforts if effort != "none"]
    default_effort = next(iter(defaults)) if len(defaults) == 1 else None
    if default_effort not in efforts:
        default_effort = None
    if defaults <= {source_default}:
        # Every route leaves the default to the upstream: OpenRouter's answer stands.
        default_enabled = bool(source.get("default_enabled", False))
    else:
        # A gateway default is in play: reasoning is on by default iff it is not "none".
        default_enabled = default_effort is not None and default_effort != "none"
    return {
        "mandatory": mandatory,
        "default_enabled": True if mandatory else default_enabled,
        "supported_efforts": efforts,
        "default_effort": default_effort,
    }
