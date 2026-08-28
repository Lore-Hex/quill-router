"""Normalize provider catalog capability metadata into OpenRouter fields."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

# Keep common OpenRouter parameters stable for clients that compare catalogs or
# generate deterministic documentation. Provider-specific passthrough fields
# are retained after this known prefix in lexical order.
_PARAMETER_ORDER = (
    "tools",
    "tool_choice",
    "parallel_tool_calls",
    "max_tokens",
    "max_completion_tokens",
    "temperature",
    "top_p",
    "top_k",
    "min_p",
    "reasoning",
    "reasoning_effort",
    "include_reasoning",
    "structured_outputs",
    "response_format",
    "stop",
    "frequency_penalty",
    "presence_penalty",
    "repetition_penalty",
    "seed",
    "logit_bias",
    "logprobs",
    "top_logprobs",
    "stream_options",
    "prediction",
    "verbosity",
    "web_search_options",
    "dimensions",
    "encoding_format",
)
_PARAMETER_RANK = {name: index for index, name in enumerate(_PARAMETER_ORDER)}

_FEATURE_PARAMETERS: dict[str, tuple[str, ...]] = {
    "function-calling": ("tools",),
    "tools": ("tools",),
    "tool-choice": ("tool_choice",),
    "parallel-tool-calls": ("parallel_tool_calls",),
    "reasoning": ("reasoning", "include_reasoning"),
    "reasoning-effort": ("reasoning", "reasoning_effort", "include_reasoning"),
    "reasoning_effort": ("reasoning", "reasoning_effort", "include_reasoning"),
    "structured-output": ("structured_outputs",),
    "structured-outputs": ("structured_outputs",),
    "structured_outputs": ("structured_outputs",),
    "json-mode": ("response_format",),
    "json_mode": ("response_format",),
    "response-format": ("response_format",),
    "logprobs": ("logprobs",),
}


def _strings(value: object) -> Iterable[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return ()
    return (item.strip() for item in value if isinstance(item, str) and item.strip())


_PROVIDER_EXTENSION_PARAMETERS: dict[str, tuple[str, ...]] = {
    # OpenAI's priority lane is selected per request and is not described by any
    # manifest or snapshot field.
    "openai": ("service_tier",),
}


def provider_extension_parameters(provider: str) -> tuple[str, ...]:
    """Return parameters a provider accepts that no catalog manifest declares."""
    return _PROVIDER_EXTENSION_PARAMETERS.get(provider, ())


def union_supported_parameters(*groups: Iterable[str]) -> tuple[str, ...]:
    """Return a deterministic union without discarding provider extensions."""
    names = {name.strip() for group in groups for name in group if name.strip()}
    return tuple(
        sorted(
            names,
            key=lambda name: (_PARAMETER_RANK.get(name, len(_PARAMETER_RANK)), name),
        )
    )


def manifest_supported_parameters(
    raw: Mapping[str, Any],
    *,
    supports_chat: bool = True,
    supports_embeddings: bool = False,
) -> tuple[str, ...]:
    """Translate one provider/model record into OpenRouter parameter names.

    Explicit provider arrays are authoritative. Feature labels add only the
    parameter they directly prove; for example, ``function-calling`` proves
    ``tools`` but does not imply control through ``tool_choice``.
    """
    groups: list[Iterable[str]] = []
    if supports_chat:
        # The gateway normalizes response length for every chat adapter.
        groups.append(("max_tokens",))
    if supports_embeddings:
        groups.append(("dimensions", "encoding_format"))

    groups.append(_strings(raw.get("supported_parameters")))
    groups.append(_strings(raw.get("supported_sampling_parameters")))

    features = {
        feature.lower().replace(" ", "-")
        for feature in (
            *_strings(raw.get("features")),
            *_strings(raw.get("supported_features")),
        )
    }
    for feature in features:
        groups.append(_FEATURE_PARAMETERS.get(feature, ()))
    if raw.get("supports_reasoning") is True:
        groups.append(("reasoning", "include_reasoning"))

    return union_supported_parameters(*groups)
