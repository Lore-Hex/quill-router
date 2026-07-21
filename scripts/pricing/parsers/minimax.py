"""Parser for MiniMax API Token Plan pricing."""

from __future__ import annotations

import re


def _money_to_micro_per_m(value: str) -> int:
    return int(round(float(value) * 1_000_000))


def parse(html: str) -> dict[str, dict[str, object]]:
    html = html.replace(r"\$", "$")
    text = re.sub(r"\s+", " ", html)
    prices: dict[str, dict[str, object]] = {}

    standard_match = re.search(
        r'<Tab\s+title="Standard">(.*?)</Tab>',
        html,
        flags=re.I | re.S,
    )
    if standard_match:
        tiers: list[dict[str, int | None]] = []
        for line in standard_match.group(1).splitlines():
            if "MiniMax-M3" not in line or not line.lstrip().startswith("|"):
                continue
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) < 4:
                continue
            amounts = [re.findall(r"\$([0-9.]+)", cell) for cell in cells[1:4]]
            if any(not values for values in amounts):
                continue
            tiers.append(
                {
                    "max_prompt_tokens": 512_000 if "512k" in cells[0] and "≤" in cells[0] else None,
                    "prompt_micro_per_m": _money_to_micro_per_m(amounts[0][-1]),
                    "completion_micro_per_m": _money_to_micro_per_m(amounts[1][-1]),
                    "prompt_cached_micro_per_m": _money_to_micro_per_m(amounts[2][-1]),
                }
            )
        if len(tiers) >= 2:
            tiers.sort(key=lambda tier: tier["max_prompt_tokens"] is None)
            prices["minimax/minimax-m3"] = {"tiers": tiers[:2]}

    m3_tiers = re.findall(
        r"MiniMax-M3\s+Context\s+([^$]+?)\s+\$([0-9.]+)\$([0-9.]+)\s+\$([0-9.]+)\$([0-9.]+)\s+\$([0-9.]+)\$([0-9.]+)",
        text,
    )
    if len(m3_tiers) >= 2:
        tiers: list[dict[str, int | None]] = []
        for (
            label,
            _input_list,
            input_discounted,
            _output_list,
            output_discounted,
            _cache_list,
            cache_discounted,
        ) in m3_tiers[:2]:
            max_prompt_tokens = 512_000 if "≤ 512K" in label or "<= 512K" in label else None
            tiers.append(
                {
                    "max_prompt_tokens": max_prompt_tokens,
                    "prompt_micro_per_m": _money_to_micro_per_m(input_discounted),
                    "completion_micro_per_m": _money_to_micro_per_m(output_discounted),
                    "prompt_cached_micro_per_m": _money_to_micro_per_m(cache_discounted),
                }
            )
        prices["minimax/minimax-m3"] = {"tiers": tiers}

    markdown_row_re = re.compile(
        r"^\|\s*\**(?P<native>MiniMax-[A-Za-z0-9._-]+)\**\s*\|"
        r"(?P<input>[^|]+)\|(?P<output>[^|]+)\|(?P<cached>[^|]+)\|",
        re.IGNORECASE | re.MULTILINE,
    )
    for match in markdown_row_re.finditer(html):
        native = match.group("native")
        if native.casefold() == "minimax-m3":
            continue
        values = [
            re.findall(r"\$([0-9.]+)", match.group(field))
            for field in ("input", "output", "cached")
        ]
        if any(not item for item in values):
            continue
        prices.setdefault(
            f"minimax/{native.casefold()}",
            {
                "prompt_micro_per_m": _money_to_micro_per_m(values[0][-1]),
                "completion_micro_per_m": _money_to_micro_per_m(values[1][-1]),
                "prompt_cached_micro_per_m": _money_to_micro_per_m(values[2][-1]),
            },
        )

    simple_row_re = re.compile(
        r"\b(?P<native>MiniMax-[A-Za-z0-9._-]+)\s+"
        r"\$(?P<input>[0-9.]+)\s*/\s*M tokens\s+"
        r"\$(?P<output>[0-9.]+)\s*/\s*M tokens\s+"
        r"\$(?P<cached>[0-9.]+)\s*/\s*M tokens",
        re.IGNORECASE,
    )
    for match in simple_row_re.finditer(text):
        native = match.group("native")
        model_id = f"minimax/{native.casefold()}"
        prices.setdefault(model_id, {
            "prompt_micro_per_m": _money_to_micro_per_m(match.group("input")),
            "completion_micro_per_m": _money_to_micro_per_m(match.group("output")),
            "prompt_cached_micro_per_m": _money_to_micro_per_m(match.group("cached")),
        })

    return prices
