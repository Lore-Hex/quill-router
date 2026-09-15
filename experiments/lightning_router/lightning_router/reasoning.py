"""Reviewed native controls, kept separate from the chat gateway's capabilities.

Exact IDs only: a new release or a provider-specific alias needs its own review.
The catalog's reasoning_effort flag does not establish a supported value set.
"""

from copy import deepcopy
from typing import Any

REVIEWED_AT = "2026-09-15"
ANTHROPIC = "https://platform.claude.com/docs/en/build-with-claude/effort"
DEEPSEEK = "https://api-docs.deepseek.com/guides/thinking_mode/"
GEMINI = "https://ai.google.dev/gemini-api/docs/openai"
MINIMAX = "https://platform.minimax.io/docs/api-reference/text-openai-api"
PROFILES: dict[str, dict[str, Any]] = {}


def register(ids: tuple[str, ...], *, field: str, values: tuple[str, ...],
             default: str | None, source: str, note: str,
             setup: bool = False) -> None:
    for model_id in ids:
        if model_id in PROFILES:
            raise ValueError(f"Duplicate reasoning review: {model_id}")
        PROFILES[model_id] = {
            "status": "reviewed", "field": field, "values": list(values),
            "default": default, "source": source, "reviewed_at": REVIEWED_AT,
            "note": note, "setup_efforts": list(values) if setup else [],
            "setup_default": default if setup else None,
        }


register(
    ("deepseek/deepseek-v4.1-flash", "deepseek/deepseek-flash",
     "deepseek/deepseek-v4-pro", "deepseek/deepseek-v4-pro-0813"),
    field="reasoning_effort", values=("low", "high", "max"), default="high",
    source=DEEPSEEK, setup=True,
    note="Native DeepSeek defaults to high with thinking on. Medium maps to high, not a separate level. Hosted providers can differ.",
)

values: tuple[str, ...]
for slug, values, default in (
    ("gpt-5", ("minimal", "low", "medium", "high"), "medium"),
    ("gpt-5-mini", ("minimal", "low", "medium", "high"), "medium"),
    ("gpt-5-nano", ("minimal", "low", "medium", "high"), "medium"),
    ("gpt-5.1", ("none", "low", "medium", "high"), "none"),
    ("gpt-5.2", ("none", "low", "medium", "high", "xhigh"), "none"),
    ("gpt-5.5", ("none", "low", "medium", "high", "xhigh"), "medium"),
    ("gpt-5.6-sol", ("none", "low", "medium", "high", "xhigh", "max"), "medium"),
):
    register((f"openai/{slug}",), field="reasoning_effort", values=values, default=default,
             source=f"https://developers.openai.com/api/docs/models/{slug}", setup=True,
             note="The config sets this effort explicitly. Other hosting providers may impose different limits.")

for ids, values in (
    (("anthropic/claude-opus-4.5",), ("low", "medium", "high")),
    (("anthropic/claude-opus-4.6", "anthropic/claude-sonnet-4.6"), ("low", "medium", "high", "max")),
    (("anthropic/claude-opus-4.7", "anthropic/claude-opus-4.8", "anthropic/claude-opus-5",
      "anthropic/claude-sonnet-5", "anthropic/claude-fable-5", "anthropic/claude-fable-5.1"),
     ("low", "medium", "high", "xhigh", "max")),
):
    register(ids, field="output_config.effort", values=values, default="high", source=ANTHROPIC,
             note="Native effort is not mapped by TR chat completions yet. Its low/medium/high map to 1024/4096/8192 thinking tokens instead. This config omits that override; native effort defaults to high, independently of thinking mode.")

register(("anthropic/claude-haiku-4.5", "anthropic/claude-sonnet-4.5"),
         field="thinking.budget_tokens", values=(), default=None,
         source="https://platform.claude.com/docs/en/build-with-claude/extended-thinking",
         note="These models use a thinking-token budget, not named native effort levels. No budget is forced by this config.")

for ids, values, default in (
    (("google/gemini-2.5-flash", "google/gemini-2.5-flash-lite"), ("none", "minimal", "low", "medium", "high"), "dynamic"),
    (("google/gemini-2.5-pro",), ("minimal", "low", "medium", "high"), "dynamic"),
    (("google/gemini-3-flash-preview", "google/gemini-3.1-flash-lite-preview"), ("minimal", "low", "medium", "high"), "high"),
    (("google/gemini-3.1-pro-preview",), ("low", "medium", "high"), "high"),
):
    register(ids, field="reasoning_effort (Google compatibility API)", values=values,
             default=default, source=GEMINI,
             note="TR's Vertex and Google compatibility adapters do not map all levels identically. No effort is forced here. TR defaults 2.5 Flash thinking off and 3 Flash to a low/minimal level depending on the route.")

register(("minimax/minimax-m3", "MiniMaxAI/MiniMax-M3"), field="thinking.type",
         values=("disabled", "adaptive"), default="adaptive", source=MINIMAX,
         note="M3 has an on/off thinking control, not low/medium/high effort. No generic effort is sent by this config.")
register(("minimax/minimax-m2", "minimax/minimax-m2.1", "minimax/minimax-m2.5", "minimax/minimax-m2.7"),
         field="Always-on thinking", values=(), default="on", source=MINIMAX,
         note="M2.x thinking cannot be disabled. No named effort levels are documented; reasoning_split only changes output formatting.")
register(("moonshotai/kimi-k2.6",), field="thinking.type",
         values=("disabled", "enabled"), default="enabled",
         source="https://platform.kimi.ai/docs/guide/use-thinking-models",
         note="K2.6 has a thinking switch, not named effort levels. Native thinking defaults on; TR's direct Kimi route currently disables it when tools are included. No effort override is generated.")
register(("moonshotai/kimi-k2.7-code",), field="Always-on thinking", values=(), default="on",
         source="https://platform.kimi.ai/docs/guide/use-thinking-models",
         note="K2.7 Code always thinks and does not support reasoning_effort. No low/medium/high setting is generated.")
register(("moonshotai/kimi-k3",), field="reasoning_effort",
         values=("low", "high", "max"), default="max",
         source="https://platform.kimi.ai/docs/guide/use-reasoning-effort",
         note="K3 always reasons and natively defaults to max. The chat adapter's thinking-field interaction is not verified for K3, so this config does not force effort.")
register(("z-ai/glm-4.5", "z-ai/glm-4.5-air", "z-ai/glm-4.6", "z-ai/glm-4.7", "z-ai/glm-5", "z-ai/glm-5.1"),
         field="thinking.type", values=("disabled", "enabled"), default="enabled",
         source="https://docs.z.ai/guides/capabilities/thinking-mode",
         note="These releases expose a thinking switch, not low/medium/high effort. No generic effort is sent by this config.")
register(("z-ai/glm-5.2",), field="reasoning_effort", values=("high", "max"), default="max",
         source="https://docs.z.ai/api-reference/llm/chat-completion", setup=True,
         note="Native GLM-5.2 has high/max effort. Low/medium map to high, xhigh maps to max, and none/minimal skip thinking. The config lists the two distinct thinking levels.")
register(("z-ai/glm-5.3", "z-ai/glm-5.3-flash"), field="reasoning_effort",
         values=("low", "high", "max"), default="max",
         source="https://docs.z.ai/api-reference/llm/chat-completion", setup=True,
         note="Native GLM-5.3 thinking cannot be disabled. Its levels are low/high/max, not medium. Hosted provider support can differ.")


def reasoning_profile(item: dict[str, Any]) -> dict[str, Any]:
    profile = PROFILES.get(item["id"])
    if profile is not None:
        return deepcopy(profile)
    return {
        "status": "unverified", "field": None, "values": [], "default": None,
        "source": None, "reviewed_at": None, "setup_efforts": [], "setup_default": None,
        "note": "Model-specific controls and defaults have not been verified. No effort override is generated; this does not mean the model cannot reason.",
    }
