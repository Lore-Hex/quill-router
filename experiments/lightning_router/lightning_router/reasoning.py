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
             setup: bool = False, setup_default: str | None = None) -> None:
    for model_id in ids:
        if model_id in PROFILES:
            raise ValueError(f"Duplicate reasoning review: {model_id}")
        PROFILES[model_id] = {
            "status": "reviewed", "field": field, "values": list(values),
            "default": default, "source": source, "reviewed_at": REVIEWED_AT,
            "note": note, "setup_efforts": list(values) if setup else [],
            "setup_default": (setup_default or default) if setup else None,
        }


register(
    ("deepseek/deepseek-v4.1-flash", "deepseek/deepseek-flash",
     "deepseek/deepseek-v4-pro", "deepseek/deepseek-v4-pro-0813"),
    field="reasoning_effort", values=("low", "high", "max"), default="high",
    source=DEEPSEEK, setup=True,
    note="Native DeepSeek defaults to high with thinking on. Medium maps to high, not a separate level. Hosted providers can differ.",
)

values: tuple[str, ...]
ids: tuple[str, ...]
for slug, values, default in (
    ("gpt-5", ("minimal", "low", "medium", "high"), "medium"),
    ("gpt-5-mini", ("minimal", "low", "medium", "high"), "medium"),
    ("gpt-5-nano", ("minimal", "low", "medium", "high"), "medium"),
    ("gpt-5.1", ("none", "low", "medium", "high"), "none"),
    ("gpt-5.2", ("none", "low", "medium", "high", "xhigh"), "none"),
    ("gpt-5.4-mini", ("none", "low", "medium", "high", "xhigh"), "none"),
    ("gpt-5.5", ("none", "low", "medium", "high", "xhigh"), "medium"),
    ("gpt-5.6-sol", ("none", "low", "medium", "high", "xhigh", "max"), "medium"),
    ("gpt-5.6-luna", ("none", "low", "medium", "high", "xhigh", "max"), "medium"),
    ("gpt-5.6-terra", ("none", "low", "medium", "high", "xhigh", "max"), "medium"),
    ("gpt-oss-120b", ("low", "medium", "high"), "medium"),
    ("gpt-oss-20b", ("low", "medium", "high"), "medium"),
    ("o1", ("low", "medium", "high"), "medium"),
    ("o3", ("low", "medium", "high"), "medium"),
    ("o3-mini", ("low", "medium", "high"), "medium"),
    ("o4-mini", ("low", "medium", "high"), "medium"),
):
    register((f"openai/{slug}",), field="reasoning_effort", values=values, default=default,
             source=f"https://developers.openai.com/api/docs/models/{slug}", setup=True,
             note="The config sets this effort explicitly. Other hosting providers may impose different limits.")

register(("openai/gpt-6-astra",), field="reasoning_effort",
         values=("low", "medium", "high", "xhigh", "max"), default=None,
         source="https://developers.openai.com/api/docs/models/gpt-6-astra", setup=True, setup_default="medium",
         note="The config explicitly selects medium. The API model page lists low/medium/high/xhigh/max; it does not list none or minimal.")

for ids, values in (
    (("anthropic/claude-opus-4.5",), ("low", "medium", "high")),
    (("anthropic/claude-opus-4.6", "anthropic/claude-sonnet-4.6"), ("low", "medium", "high", "max")),
    (("anthropic/claude-opus-4.7", "anthropic/claude-opus-4.8", "anthropic/claude-opus-5",
      "anthropic/claude-sonnet-5", "anthropic/claude-fable-5", "anthropic/claude-fable-5.1"),
     ("low", "medium", "high", "xhigh", "max")),
):
    register(ids, field="output_config.effort", values=values, default="high", source=ANTHROPIC, setup=True,
             note="The config sends reasoning_effort. TR maps it to native output_config.effort after route selection, with adaptive thinking on supported Claude models. Opus 4.5 retains its separate thinking-token budget.")

register(("anthropic/claude-haiku-4.5", "anthropic/claude-sonnet-4.5"),
         field="thinking.budget_tokens", values=(), default=None,
         source="https://platform.claude.com/docs/en/build-with-claude/extended-thinking",
         note="These models use a thinking-token budget, not named native effort levels. No budget is forced by this config.")

for ids, values, default, selected in (
    (("google/gemini-2.5-flash", "google/gemini-2.5-flash-lite"), ("none", "minimal", "low", "medium", "high"), "dynamic", "none"),
    (("google/gemini-2.5-pro",), ("minimal", "low", "medium", "high"), "dynamic", "medium"),
    (("google/gemini-3-flash-preview", "google/gemini-3.1-flash-lite-preview", "google/gemini-3.1-flash-lite"), ("minimal", "low", "medium", "high"), "high", "minimal"),
    (("google/gemini-3.1-pro-preview",), ("low", "medium", "high"), "high", "high"),
    (("google/gemini-3.5-flash", "google/gemini-3.6-flash"), ("minimal", "low", "medium", "high"), "medium", "minimal"),
    (("google/gemini-3.5-flash-lite",), ("minimal", "low", "medium", "high"), "minimal", "minimal"),
    (("google/gemini-3.7-flash", "google/gemini-3.8-flash"), ("low", "medium", "high"), "medium", "low"),
):
    register(ids, field="reasoning_effort (Google compatibility API)", values=values,
             default=default, source=GEMINI, setup=True, setup_default=selected,
             note="The config selects an explicit effort. Vertex maps 2.5 low/minimal to 1024 thinking tokens, medium to 8192, and high to 24576; 3.x uses native thinkingLevel. Flash configs keep TR's fast default, which can differ from Google's native default.")

register(("minimax/minimax-m3", "MiniMaxAI/MiniMax-M3"), field="thinking.type",
         values=("disabled", "adaptive"), default="adaptive", source=MINIMAX,
         note="M3 has an on/off thinking control, not low/medium/high effort. No generic effort is sent by this config.")
register(("minimax/minimax-m2", "minimax/minimax-m2.1", "minimax/minimax-m2.5", "minimax/minimax-m2.7"),
         field="Always-on thinking", values=(), default="on", source=MINIMAX,
         note="M2.x thinking cannot be disabled. No named effort levels are documented; reasoning_split only changes output formatting.")
register(("moonshotai/kimi-k2.5", "moonshotai/kimi-k2.6"), field="thinking.type",
         values=("disabled", "enabled"), default="enabled",
         source="https://platform.kimi.ai/docs/guide/use-thinking-models",
         note="K2.5/K2.6 have a thinking switch, not named effort levels. Native thinking defaults on; TR's direct Kimi route currently disables it when tools are included. No effort override is generated.")
register(("moonshotai/kimi-k2.7-code",), field="Always-on thinking", values=(), default="on",
         source="https://platform.kimi.ai/docs/guide/use-thinking-models",
         note="K2.7 Code always thinks and does not support reasoning_effort. No low/medium/high setting is generated.")
register(("moonshotai/kimi-k3",), field="reasoning_effort",
         values=("low", "high", "max"), default="max",
         source="https://platform.kimi.ai/docs/guide/use-reasoning-effort", setup=True,
         note="K3 always reasons and defaults to max. TR sends top-level reasoning_effort without the incompatible K2.x thinking field. Hosted providers may support fewer levels.")
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

register(("qwen/qwen3.8-max", "qwen/qwen3.8-flash"), field="reasoning_effort",
         values=("low", "medium", "xhigh"), default="xhigh", setup=True,
         source="https://docs.qwencloud.com/api-reference/chat/openai-chat",
         note="QwenCloud documents low/medium/xhigh for these models. High maps to xhigh, not a separate level. Other hosts can expose a different control; confirm the selected provider before relying on an effort setting.")
register(("mistralai/mistral-medium-3-5",), field="reasoning_effort",
         values=("none", "high"), default=None, setup=True, setup_default="high",
         source="https://docs.mistral.ai/studio/conversations/reasoning",
         note="Mistral documents none/high, not low/medium/high. This config selects high for coding; none omits the thinking chunk but does not promise zero internal reasoning.")
register(("google/gemma-4-12b-it", "google/gemma-4-26b-a4b-it", "google/gemma-4-31b-it"),
         field="enable_thinking (provider-specific)", values=("false", "true"), default=None,
         source="https://ai.google.dev/gemma/docs/capabilities/thinking",
         note="Gemma 4 exposes a thinking switch in its chat template, not named effort levels. Hosted APIs differ, so these snippets do not invent a generic effort control.")

# Reviewed catalog spellings, not a fuzzy rule for arbitrary future models.
ALIASES = {
    "deepseek/deepseek-v4-1-flash": "deepseek/deepseek-v4.1-flash",
    "moonshotai/kimi-k2-5": "moonshotai/kimi-k2.5",
    "moonshotai/kimi-k2-6": "moonshotai/kimi-k2.6",
    "moonshotai/Kimi-K2.6": "moonshotai/kimi-k2.6",
    "moonshotai/kimi-k2-7-code": "moonshotai/kimi-k2.7-code",
    "moonshotai/Kimi-K2.7-Code": "moonshotai/kimi-k2.7-code",
    "minimax/minimax-m25": "minimax/minimax-m2.5",
    "minimax/minimax-m27": "minimax/minimax-m2.7",
    "minimax/minimax-m2.1-highspeed": "minimax/minimax-m2.1",
    "minimax/minimax-m2.5-highspeed": "minimax/minimax-m2.5",
    "minimax/minimax-m2.7-highspeed": "minimax/minimax-m2.7",
    "qwen/qwen-3-8-max": "qwen/qwen3.8-max",
    "qwen/qwen-3-8-flash": "qwen/qwen3.8-flash",
}
for alias, canonical in ALIASES.items():
    PROFILES[alias] = deepcopy(PROFILES[canonical])


def reasoning_profile(item: dict[str, Any]) -> dict[str, Any]:
    profile = PROFILES.get(item["id"])
    if profile is not None:
        return deepcopy(profile)
    return {
        "status": "unverified", "field": None, "values": [], "default": None,
        "source": None, "reviewed_at": None, "setup_efforts": [], "setup_default": None,
        "note": "Model-specific controls and defaults have not been verified. No effort override is generated; this does not mean the model cannot reason.",
    }
