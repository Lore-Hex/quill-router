"""Parse xAI's official text-model pricing table."""

from __future__ import annotations

import re

_NAME_TO_OR_ID = {
    "grok-4.20-multi-agent-0309": "x-ai/grok-4.20-multi-agent",
    "grok-4.20-0309-reasoning": "x-ai/grok-4.20-reasoning",
    "grok-4.20-0309-non-reasoning": "x-ai/grok-4.20",
    "grok-4-1-fast-reasoning": "x-ai/grok-4-1-fast-reasoning",
    "grok-4-1-fast-non-reasoning": "x-ai/grok-4-1-fast",
}
_DOLLAR_RE = re.compile(r"\$([\d.]+)")
_MD_LINK_RE = re.compile(r"^\s*\**\s*\[([^\]]+)\]\([^)]+\)\s*\**\s*$")
_CONTEXT_QUALIFIER_RE = re.compile(
    r"\s*\((?P<operator><|&lt;|≥|>=|&gt;=)\s*200k\s+prompt\s+tokens\)\s*$",
    re.I,
)


def _strip_link(cell: str) -> str:
    match = _MD_LINK_RE.match(cell)
    return match.group(1).strip() if match else cell.strip().strip("*").strip()


def _to_micro(text: str) -> int | None:
    if not text or text.strip() == "-":
        return None
    match = _DOLLAR_RE.search(text)
    if not match:
        return None
    try:
        return int(round(float(match.group(1)) * 1_000_000))
    except (TypeError, ValueError):
        return None


def _canonical_id(native_name: str) -> str | None:
    if not native_name.casefold().startswith("grok-"):
        return None
    return _NAME_TO_OR_ID.get(native_name) or f"x-ai/{native_name.casefold()}"


def parse(markdown: str) -> dict:
    grouped: dict[str, dict[str, dict[str, int]]] = {}
    for line in markdown.replace(r"\$", "$").splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 5:
            continue
        decorated_name = _strip_link(cells[0])
        qualifier = _CONTEXT_QUALIFIER_RE.search(decorated_name)
        native_name = _CONTEXT_QUALIFIER_RE.sub("", decorated_name).strip()
        model_id = _canonical_id(native_name)
        if model_id is None:
            continue
        prompt = _to_micro(cells[2])
        cached = _to_micro(cells[3])
        completion = _to_micro(cells[4])
        # Compatibility with the older Input | Output | Cached layout.
        if completion is None or (cached is not None and completion < cached):
            cached, completion = completion, cached
        if prompt is None or completion is None:
            continue
        row = {
            "prompt_micro_per_m": prompt,
            "completion_micro_per_m": completion,
        }
        if cached is not None:
            row["prompt_cached_micro_per_m"] = cached
        operator = qualifier.group("operator") if qualifier else ""
        tier = "long" if operator in {"≥", ">=", "&gt;="} else "short"
        grouped.setdefault(model_id, {}).setdefault(tier, row)

    output: dict = {}
    for model_id, rows in grouped.items():
        if "short" in rows and "long" in rows:
            output[model_id] = {
                "tiers": [
                    {"max_prompt_tokens": 200_000, **rows["short"]},
                    {"max_prompt_tokens": None, **rows["long"]},
                ]
            }
        else:
            output[model_id] = rows.get("short") or rows["long"]
    return output
