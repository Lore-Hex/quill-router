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
upstream unchanged, but some map it or reject it, and an effort that the gateway
silently collapses (Claude `max` on a budget-only route turns thinking OFF) must
not be advertised. So each model publishes OpenRouter's efforts intersected with
what the gateway honors on EVERY one of its routes, because a request can fail
over to any of them. A route that cannot turn reasoning off makes it mandatory.

`gateway_effort_limit` mirrors enclave-go/internal/llm (anthropic_reasoning.go,
vertex_gemini.go, byok.go googleAIStudioCompatibleReasoningEffort,
reasoning_control.go validateKimiReasoningEffort, privatemode.go). A route not
listed there forwards efforts verbatim, so it adds no limit: the upstream's
accepted set, which OpenRouter's metadata records, is the answer.
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

_DATA_PATH = Path(__file__).parent / "data" / "openrouter_reasoning.json"

# anthropic_reasoning.go: substrings over the lower-cased upstream id with "." -> "-".
_ANTHROPIC_ADAPTIVE = (
    "claude-opus-4-6", "claude-opus-4-7", "claude-opus-4-8", "claude-opus-5",
    "claude-sonnet-4-6", "claude-sonnet-5", "claude-fable-5",
)
_ANTHROPIC_MANUAL = "claude-opus-4-5"
# Routes that build an Anthropic Messages body from a chat request (anthropic.go,
# aws.go, gcp.go for a claude-* upstream on google-vertex).
_ANTHROPIC_HOSTS = frozenset({"anthropic", "amazon-bedrock"})


@dataclass(frozen=True)
class EffortLimit:
    """What one route honors: the accepted efforts and whether it can turn reasoning off."""

    efforts: frozenset[str]
    can_disable: bool


def _gemini_version_at_least(upstream_id: str, major: int, minor: int) -> bool:
    # gemini_version.go geminiVersionAtLeast: "gemini-3-flash" is 3.0.
    model = upstream_id.strip().lower().removeprefix("google/").removeprefix("gemini-")
    match = re.match(r"(\d+)(?:\.(\d+))?", model)
    if not match:
        return False
    found_major, found_minor = int(match.group(1)), int(match.group(2) or 0)
    return found_major > major or (found_major == major and found_minor >= minor)


def gateway_effort_limit(provider: str, upstream_id: str | None) -> EffortLimit | None:
    """The gateway's own limit on one route, or None where it forwards efforts verbatim."""
    upstream = (upstream_id or "").lower()
    if provider in _ANTHROPIC_HOSTS or (provider == "google-vertex" and "claude" in upstream):
        model = upstream.replace(".", "-")
        if any(prefix in model for prefix in _ANTHROPIC_ADAPTIVE) or _ANTHROPIC_MANUAL in model:
            # output_config.effort is sent verbatim with thinking enabled or disabled.
            # "minimal" is not an Anthropic effort; the API rejects it.
            return EffortLimit(efforts=_ALL - {"minimal"}, can_disable=True)
        # Budget-only Claude (adapter.go anthropicEffortBudget): low/medium/high map to
        # 1024/4096/8192; xhigh is the same budget as high; max and minimal turn thinking off.
        return EffortLimit(efforts=frozenset({"none", "low", "medium", "high"}), can_disable=True)
    if provider == "google-vertex" and "gemini" in upstream:
        if upstream.startswith("gemini-2.5"):
            # Budgets: none 0, low 1024, medium 8192, high 24576; minimal is low's budget,
            # xhigh/max are unmapped.
            return EffortLimit(efforts=frozenset({"none", "low", "medium", "high"}), can_disable=True)
        # Gemini 3+: a thinking level; "none" becomes "low", so it cannot be turned off.
        efforts = {"low", "medium", "high"}
        if "pro" not in upstream and not _gemini_version_at_least(upstream, 3, 7):
            efforts.add("minimal")
        return EffortLimit(efforts=frozenset(efforts), can_disable=False)
    if provider in {"google-ai-studio", "gemini"} and _gemini_version_at_least(upstream, 3, 7):
        # none/off/minimal are rewritten to "low" before the request leaves.
        return EffortLimit(efforts=_ALL - {"none", "minimal"}, can_disable=False)
    if provider == "kimi" and upstream == "kimi-k3":
        return EffortLimit(efforts=frozenset({"low", "high", "max"}), can_disable=False)
    if provider == "privatemode":
        efforts = {"low", "high"}
        if upstream == "gpt-oss-120b":
            efforts.add("medium")
        if upstream.startswith("glm-"):
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
    model takes the `reasoning` parameter and OpenRouter publishes metadata for it.
    """
    if "reasoning" not in set(supported_parameters):
        return None
    source = openrouter_reasoning(model_id)
    if source is None:
        return None
    efforts = [effort for effort in source.get("supported_efforts", []) if effort in _ALL]
    mandatory = bool(source.get("mandatory", False))
    for provider, upstream_id in routes:
        limit = gateway_effort_limit(provider, upstream_id)
        if limit is None:
            continue
        efforts = [effort for effort in efforts if effort in limit.efforts]
        if not limit.can_disable:
            mandatory = True
    if mandatory:
        efforts = [effort for effort in efforts if effort != "none"]
    default_effort = source.get("default_effort")
    return {
        "mandatory": mandatory,
        "default_enabled": True if mandatory else bool(source.get("default_enabled", False)),
        "supported_efforts": efforts,
        "default_effort": default_effort if default_effort in efforts else None,
    }
