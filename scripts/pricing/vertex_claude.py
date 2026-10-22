"""Vertex Claude model identities, global tariffs, and native admission probes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
from bs4 import BeautifulSoup

from scripts.pricing.base import ModelPrice, PriceTier
from scripts.pricing.vertex_prices import _money, _table_tab

_NATIVE = re.compile(r"^claude-([a-z]+)-(\d+)(?:-(\d+))?(?:@\d{8})?$")
_DISPLAY = re.compile(r"^(?:Claude )?(Opus|Sonnet|Haiku|Fable) (\d+(?:\.\d+)?)$")


def canonical_id(native_id: str) -> str | None:
    match = _NATIVE.fullmatch(native_id)
    if match is None:
        return None
    version = match[2] + (f".{match[3]}" if match[3] else "")
    return f"anthropic/claude-{match[1]}-{version}"


def native_rows(publisher_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for source in publisher_rows:
        name = source.get("name", "")
        prefix = "publishers/anthropic/models/"
        if not isinstance(name, str) or not name.startswith(prefix):
            continue
        native = name.removeprefix(prefix)
        model_id = canonical_id(native)
        version = source.get("versionId")
        if model_id is None or not isinstance(version, str):
            continue
        if version != "default":
            if not re.fullmatch(r"\d{8}", version):
                continue
            native += f"@{version}"
        previous = rows.get(model_id)
        # Prefer the provider's default when it exists; otherwise pin the
        # newest dated version, independent of publisher pagination order.
        if previous and ("@" not in previous["upstream_id"] or (
            "@" in native and previous["upstream_id"] >= native
        )):
            continue
        rows[model_id] = {"id": model_id, "upstream_id": native}
        access = source.get("supportedActions", {}).get("requestAccess", {}).get("references", {})
        for target in access.values():
            query = parse_qs(urlparse(target.get("uri", "")).query)
            if (
                "zdrExemption" in query
                or "true" in query.get("dataSharing", [])
                or "true" in query.get("advanceAISafetyAddendum", [])
            ):
                # A successful prediction is not privacy evidence. Vertex is
                # currently advertised as ZDR; review model-specific exceptions
                # before letting them inherit that provider-wide policy.
                rows[model_id].update(routable=False, routable_reason="vertex-publisher-policy-review")
                break
    return rows


@dataclass(frozen=True)
class ClaudeTariff:
    price: ModelPrice
    context_limit: int | None = None


def parse(html: str) -> dict[str, ClaudeTariff]:
    soup = BeautifulSoup(html, "html.parser")
    tariffs: dict[str, ClaudeTariff] = {}
    directions = {"input", "output", "cache hit", "5m cache write", "1h cache write"}
    for table in soup.find_all("table"):
        if _table_tab(table, soup) != "global":
            continue
        rows = table.find_all("tr")
        if not rows:
            continue
        headers = [c.get_text(" ", strip=True).lower() for c in rows[0].find_all(["th", "td"])]
        if headers[:2] != ["model", "type"] or "200k" not in " ".join(headers):
            continue
        if len(headers) not in (4, 6):
            raise ValueError("vertex Claude: pricing columns changed")
        parts: dict[str, dict[str, list[str]]] = {}
        model_id: str | None = None
        for row in rows[1:]:
            cells = [c.get_text(" ", strip=True) for c in row.find_all(["th", "td"], recursive=False)]
            if len(cells) != len(headers):
                raise ValueError("vertex Claude: pricing row columns changed")
            if cells[0]:
                match = _DISPLAY.fullmatch(cells[0])
                model_id = f"anthropic/claude-{match[1].lower()}-{match[2]}" if match else None
            kind = cells[1].lower()
            if model_id is None or kind not in directions:
                continue
            values = cells[2:]
            previous = parts.setdefault(model_id, {}).get(kind)
            if previous is not None:
                if any(a and b and a != b for a, b in zip(previous, values, strict=True)):
                    raise ValueError(f"vertex Claude: conflicting {kind} prices for {model_id}")
                values = [a or b for a, b in zip(previous, values, strict=True)]
            parts[model_id][kind] = values
        for model_id, rates in parts.items():
            if not {"input", "output", "cache hit", "5m cache write"} <= rates.keys():
                continue
            active = [i for i in range(len(headers) - 2) if any(v[i] for v in rates.values())]
            offset = 2 if active and min(active) >= 2 else 0
            if any(i >= offset + 2 for i in active):
                raise ValueError(f"vertex Claude: ambiguous context tiers for {model_id}")
            boundary = 100_000 if offset == 2 else 200_000
            tier_values: list[tuple[int, int, int]] = []
            for index in (offset, offset + 1):
                if any(not rates[kind][index] for kind in ("input", "output", "cache hit", "5m cache write")):
                    break
                prompt, completion, cached, write = (
                    _money(rates[kind][index])
                    for kind in ("input", "output", "cache hit", "5m cache write")
                )
                # Existing accounting supports Claude's standard 5m/1h write
                # multipliers; don't admit an unrepresentable new tariff.
                if write * 4 != prompt * 5:
                    raise ValueError(f"vertex Claude: unsupported cache-write price for {model_id}")
                hour = rates.get("1h cache write")
                if hour and hour[index] and _money(hour[index]) != prompt * 2:
                    raise ValueError(f"vertex Claude: unsupported 1h cache price for {model_id}")
                tier_values.append((prompt, completion, cached))
            if not tier_values:
                continue
            low = tier_values[0]
            if len(tier_values) == 1:
                # The caller must hold routes whose advertised context exceeds
                # this fully priced range; a blank is not a repeated low rate.
                tariff = ClaudeTariff(ModelPrice(low[0], low[1], prompt_cached_micro_per_m=low[2]), boundary)
            elif low == tier_values[1]:
                tariff = ClaudeTariff(ModelPrice(low[0], low[1], prompt_cached_micro_per_m=low[2]))
            else:
                tariff = ClaudeTariff(ModelPrice(tiers=[PriceTier(boundary, *low), PriceTier(None, *tier_values[1])]))
            previous_tariff = tariffs.get(model_id)
            if previous_tariff and (
                previous_tariff.price.tiers != tariff.price.tiers
                or previous_tariff.context_limit != tariff.context_limit
            ):
                raise ValueError(f"vertex Claude: conflicting global prices for {model_id}")
            tariffs[model_id] = tariff
    if not tariffs:
        raise ValueError("vertex Claude: no complete global token prices")
    return tariffs


def probe(headers: dict[str, str], project: str, native_id: str) -> tuple[bool, int | None]:
    try:
        response = httpx.post(
            f"https://aiplatform.googleapis.com/v1/projects/{project}/locations/global/"
            f"publishers/anthropic/models/{native_id}:rawPredict",
            headers=headers,
            json={"anthropic_version": "vertex-2023-10-16", "max_tokens": 32,
                  "messages": [{"role": "user", "content": "Reply exactly PONG"}]},
            timeout=60,
        )
        if response.status_code != 200:
            return False, response.status_code
        payload = response.json()
        visible = "".join(p.get("text", "") for p in payload.get("content", []) if p.get("type") == "text")
        usage = payload.get("usage", {})
        valid_usage = all(type(usage.get(k)) is int and usage[k] >= 0 for k in ("input_tokens", "output_tokens"))
        return visible.strip() == "PONG" and valid_usage and payload.get("stop_reason") == "end_turn", 200
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        return False, None
