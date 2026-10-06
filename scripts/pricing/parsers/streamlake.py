"""StreamLake's USD/M tables, including prefix-cache and context tiers."""

from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any

from bs4 import BeautifulSoup


def model_id(label: str) -> str | None:
    native = label.strip().casefold().replace(" ", "-")
    for prefix, author in (
        ("kat-coder-", "kwaipilot"), ("deepseek-", "deepseek"),
        ("glm-", "z-ai"), ("kimi-", "moonshotai"),
        ("minimax-", "minimax"), ("mimo-", "xiaomi"), ("qwen", "qwen"),
    ):
        if native.startswith(prefix) and re.fullmatch(r"[a-z0-9._-]+", native):
            return f"{author}/{native}"
    return None


def _money(cell: str) -> int | None:
    match = re.fullmatch(r"\$([0-9]+(?:\.[0-9]+)?)", cell.strip())
    if not match:
        return None
    value = Decimal(match[1]) * 1_000_000
    if value != value.to_integral_value():
        raise ValueError("streamlake: fractional microdollar price")
    return int(value)


def _tables(html: str) -> list[list[list[str]]]:
    soup = BeautifulSoup(html, "html.parser")
    if not soup.find("table"):
        data = soup.find("script", id="__NEXT_DATA__")
        if data:
            pending = [json.loads(data.get_text())]
            fragments = []
            while pending:
                value = pending.pop()
                if isinstance(value, dict):
                    pending.extend(value.values())
                elif isinstance(value, list):
                    pending.extend(value)
                elif isinstance(value, str) and "<table" in value:
                    fragments.append(value)
            soup = BeautifulSoup("\n".join(fragments), "html.parser")
    tables = []
    for table in soup.find_all("table"):
        rows = []
        spans: dict[int, tuple[str, int]] = {}
        for tr in table.find_all("tr"):
            cells = iter(tr.find_all(["td", "th"], recursive=False))
            row: list[str] = []
            index = 0
            while True:
                if index in spans:
                    text, remaining = spans.pop(index)
                    row.append(text)
                    if remaining > 1:
                        spans[index] = (text, remaining - 1)
                else:
                    cell = next(cells, None)
                    if cell is None:
                        break
                    if cell.get("colspan", "1") != "1":
                        raise ValueError("streamlake: unsupported pricing colspan")
                    text = cell.get_text(" ", strip=True)
                    row.append(text)
                    remaining = int(str(cell.get("rowspan", "1"))) - 1
                    if remaining:
                        spans[index] = (text, remaining)
                index += 1
            rows.append(row)
        tables.append(rows)
    if not tables:
        rows = [line.strip().strip("|").split("|") for line in html.splitlines()
                if line.strip().startswith("|")]
        if rows:
            tables.append([[cell.strip() for cell in row] for row in rows])
    return tables


def parse(html: str) -> dict[str, dict[str, Any]]:
    profiles: dict[str, list[dict[str, Any]]] = {}
    for table in _tables(html):
        columns: dict[str, int] = {}
        for cells in table:
            if not cells:
                continue
            headers = [cell.casefold() for cell in cells]
            if any("input price" in h or "input length" in h for h in headers):
                columns = {}
                for i, header in enumerate(headers):
                    if header.startswith("input price"):
                        columns["prompt_micro_per_m"] = i
                    elif header.startswith("output price"):
                        columns["completion_micro_per_m"] = i
                    elif header.startswith(("prefixcache", "cached input")):
                        columns["prompt_cached_micro_per_m"] = i
                    elif header.startswith("input length"):
                        columns["length"] = i
                    elif header.startswith("cache read"):
                        columns["read"] = i
                continue
            mid = model_id(cells[0])
            if mid is None or not {"prompt_micro_per_m", "completion_micro_per_m"} <= columns.keys():
                continue
            if max(columns.values()) >= len(cells):
                raise ValueError(f"streamlake: truncated price row for {mid}")
            rates = {key: _money(cells[index]) for key, index in columns.items()
                     if key not in {"length", "read"}}
            # Thinking/non-thinking prices are not a flat rate. Do not guess.
            if rates.get("prompt_micro_per_m") is None or rates.get("completion_micro_per_m") is None:
                continue
            rates = {key: value for key, value in rates.items() if value is not None}
            if mid.startswith("kwaipilot/") and "read" in columns:
                cached = _money(cells[columns["read"]])
                if cached is not None:
                    rates["prompt_cached_micro_per_m"] = cached
            # Explicit-cache APIs require separate write accounting. Admit
            # only published automatic prefix-cache rates in this adapter.
            elif "read" in columns and _money(cells[columns["read"]]) is not None:
                if "prompt_cached_micro_per_m" not in rates:
                    continue
            limit = cells[columns["length"]].strip() if "length" in columns else "-"
            tier: dict[str, Any] = dict(rates)
            if limit not in {"-", "0-256K"}:
                match = re.fullmatch(r"(≤|>)\s*(\d+)k", limit, re.IGNORECASE)
                if not match:
                    raise ValueError(f"streamlake: unknown price tier for {mid}")
                tier["max_prompt_tokens"] = int(match[2]) * 1024 if match[1] == "≤" else None
            profiles.setdefault(mid, []).append(tier)
    output = {}
    for mid, rows in profiles.items():
        unique = []
        for row in rows:
            if row not in unique:
                unique.append(row)
        if len(unique) == 1 and "max_prompt_tokens" not in unique[0]:
            output[mid] = unique[0]
        elif (len(unique) == 2 and isinstance(unique[0].get("max_prompt_tokens"), int)
              and "max_prompt_tokens" in unique[1] and unique[1]["max_prompt_tokens"] is None):
            output[mid] = {"tiers": unique}
        else:
            raise ValueError(f"streamlake: conflicting or incomplete prices for {mid}")
    return output
