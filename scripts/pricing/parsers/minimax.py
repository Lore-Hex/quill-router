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
    markdown_tabs = re.search(r"^[ \t]*>[ \t]*\[tab\]", html, flags=re.I | re.M)
    if standard_match is None and markdown_tabs:
        standard_sections = list(re.finditer(
            r"^[ \t]*>[ \t]*\[tab\][ \t]+Standard[ \t]*\r?\n(.*?)"
            r"(?=^[ \t]*>[ \t]*\[|^#{1,6}[ \t]|\Z)",
            html,
            flags=re.I | re.S | re.M,
        ))
        if len(standard_sections) == 1:
            standard_match = standard_sections[0]
    if standard_match:
        tiers: list[dict[str, int | None]] = []
        invalid_tiers = False
        for line in standard_match.group(1).splitlines():
            line = re.sub(r"^[ \t]*>[ \t]?", "", line)
            if not line.lstrip().startswith("|"):
                continue
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if not re.match(r"^\*{0,2}MiniMax-M3\*{0,2}(?=\s)", cells[0], flags=re.I):
                continue
            if len(cells) < 4:
                invalid_tiers = True
                continue
            amounts = [re.findall(r"\$([0-9.]+)", cell) for cell in cells[1:4]]
            if any(not values for values in amounts):
                invalid_tiers = True
                continue
            finite = re.search(r"(?:≤|<=)\s*512k\b", cells[0], flags=re.I)
            uncapped = re.search(r">\s*512k\b|512k\s*(?:~|-|to)\s*1m\b", cells[0], flags=re.I)
            if bool(finite) == bool(uncapped):
                invalid_tiers = True
                continue
            tiers.append(
                {
                    "max_prompt_tokens": 512_000 if finite else None,
                    "prompt_micro_per_m": _money_to_micro_per_m(amounts[0][-1]),
                    "completion_micro_per_m": _money_to_micro_per_m(amounts[1][-1]),
                    "prompt_cached_micro_per_m": _money_to_micro_per_m(amounts[2][-1]),
                }
            )
        if not invalid_tiers and len(tiers) == 2:
            tiers.sort(key=lambda tier: tier["max_prompt_tokens"] is None)
            if [tier["max_prompt_tokens"] for tier in tiers] == [512_000, None]:
                prices["minimax/minimax-m3"] = {"tiers": tiers}

    m3_tiers = re.findall(
        r"MiniMax-M3\s+Context\s+([^$]+?)\s+\$([0-9.]+)\$([0-9.]+)\s+\$([0-9.]+)\$([0-9.]+)\s+\$([0-9.]+)\$([0-9.]+)",
        text,
    )
    if len(m3_tiers) >= 2 and standard_match is None and not markdown_tabs:
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
        r"^[ \t]*\|\s*\**(?P<native>MiniMax-[A-Za-z0-9._-]+)\**\s*\|"
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
