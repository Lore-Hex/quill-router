"""Second, configuration-only privacy boundary for enclave value previews."""

from __future__ import annotations

import json
import math
from typing import Any, NoReturn

# Mirror enclave-go/internal/trustedrouter/contract_value.go. No arbitrary
# strings, identifiers, prompts, tool definitions, or credential fields.
OPTIONS = frozenset("""
temperature top_p top_k top_a min_p max_tokens max_output_tokens
max_completion_tokens max_tool_calls n seed depth frequency_penalty
presence_penalty repetition_penalty logprobs top_logprobs stream store
allow_fallbacks parallel_tool_calls background usage.include
stream_options.include_usage reasoning.enabled reasoning.exclude reasoning.max_tokens
provider.allow_fallbacks provider.require_parameters provider.zdr
provider.max_price.prompt provider.max_price.completion provider.max_price.image
provider.max_price.audio provider.max_price.request
""".split())
ENUMS = {
    "prompt_cache_retention": "in_memory 24h",
    "prompt_cache_options.ttl": "in_memory 24h",
    "reasoning_effort": "none minimal low medium high xhigh max auto",
    "reasoning.effort": "none minimal low medium high xhigh max auto",
    "service_tier": "auto default flex priority scale",
    "truncation": "auto disabled",
    "provider.data_collection": "allow deny",
    "provider.min_privacy": "standard zdr confidential",
    "provider.usage": "byok prepaid credits",
    "provider.billing": "byok prepaid credits",
    "response_format.type": "text json_object json_schema",
    "text.format.type": "text json_object json_schema",
}
MARKERS = frozenset(f"[redacted:{kind}]" for kind in ("string", "number", "boolean", "object", "array"))


def _safe_value(path: str, value: Any) -> Any:
    allowed = path in OPTIONS or path in ENUMS
    if value is None:
        return None
    if isinstance(value, bool):
        return value if allowed else "[redacted:boolean]"
    if isinstance(value, (int, float)):
        return value if allowed and math.isfinite(value) and abs(value) <= 1e12 else "[redacted:number]"
    if isinstance(value, str):
        enums = ENUMS.get(path, "true false yes no on off enabled disabled auto none" if path in OPTIONS else "").split()
        return value if value in MARKERS or value in enums else "[redacted:string]"
    if isinstance(value, dict):
        prefix = path + "."
        children = {key[len(prefix):].split(".", 1)[0] for key in OPTIONS | ENUMS.keys() if key.startswith(prefix)}
        if not children:
            return "[redacted:object]"
        out = {child: _safe_value(prefix + child, value[child]) for child in sorted(children) if child in value}
        if len(out) < len(value):
            out["_redacted"] = True
        return out
    return "[redacted:array]"


def _reject_json_constant(value: str) -> NoReturn:
    raise ValueError("non-JSON numeric constant")


def safe_value_preview(path: str, preview: str | None) -> tuple[str | None, bool]:
    if not preview or len(preview) > 100:
        return None, False
    try:
        value = _safe_value(path, json.loads(preview, parse_constant=_reject_json_constant))
    except (ValueError, OverflowError, RecursionError):
        return None, False
    truncated = False
    while True:
        encoded = json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=True)
        if len(encoded) <= 100:
            return encoded, truncated
        if not isinstance(value, dict) or not value:
            return None, False
        del value[sorted(value)[-1]]
        truncated = True
