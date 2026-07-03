"""Parser for MiniMax API Token Plan pricing."""

from __future__ import annotations

import re


def _money_to_micro_per_m(value: str) -> int:
    return int(round(float(value) * 1_000_000))


def parse(html: str) -> dict[str, dict[str, object]]:
    text = re.sub(r"\s+", " ", html)
    prices: dict[str, dict[str, object]] = {}

    m3_tiers = re.findall(
        r"MiniMax-M3\s+Context\s+([^$]+?)\s+\$([0-9.]+)\$([0-9.]+)\s+\$([0-9.]+)\$([0-9.]+)\s+\$([0-9.]+)\$([0-9.]+)",
        text,
    )
    if len(m3_tiers) >= 2:
        tiers: list[dict[str, int | None]] = []
        for label, _input_list, input_discounted, _output_list, output_discounted, _cache_list, cache_discounted in m3_tiers[:2]:
            max_prompt_tokens = 512_000 if "≤ 512K" in label else None
            tiers.append(
                {
                    "max_prompt_tokens": max_prompt_tokens,
                    "prompt_micro_per_m": _money_to_micro_per_m(input_discounted),
                    "completion_micro_per_m": _money_to_micro_per_m(output_discounted),
                    "prompt_cached_micro_per_m": _money_to_micro_per_m(cache_discounted),
                }
            )
        prices["minimax/minimax-m3"] = {"tiers": tiers}

    for native, model_id in (
        ("MiniMax-M2.7", "minimax/minimax-m2.7"),
        ("MiniMax-M2.7-highspeed", "minimax/minimax-m2.7-highspeed"),
    ):
        match = re.search(
            rf"{re.escape(native)}\s+\$([0-9.]+)\s*/\s*M tokens\s+\$([0-9.]+)\s*/\s*M tokens\s+\$([0-9.]+)\s*/\s*M tokens",
            text,
        )
        if not match:
            continue
        prices[model_id] = {
            "prompt_micro_per_m": _money_to_micro_per_m(match.group(1)),
            "completion_micro_per_m": _money_to_micro_per_m(match.group(2)),
            "prompt_cached_micro_per_m": _money_to_micro_per_m(match.group(3)),
        }

    return prices
