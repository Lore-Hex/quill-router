#!/usr/bin/env python3
"""Refresh routing._THROUGHPUT_RANK from the public leaderboard.

The leaderboard is the already-public, metadata-only source of truth for p50
provider throughput. This script intentionally uses the rendered public page so
the committed rank reflects what users can inspect themselves.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Final

import httpx

LEADERBOARD_URL: Final = "https://trustedrouter.com/leaderboard"
ROUTING_PATH: Final = Path("src/trusted_router/routing.py")
MIN_SAMPLES: Final = 25
MIN_UPTIME: Final = 0.95
SECONDARY_START: Final = 20
SECONDARY_PROVIDERS: Final = (
    "cerebras",
    "mistral",
    "openai",
    "google-vertex",
    "google-ai-studio",
    "together",
    "zai",
    "anthropic",
    "tinfoil",
    "venice",
    "grok",
    "lightning",
    "nebius",
    "friendli",
    "novita",
    "phala",
    "gmi",
    "parasail",
    "wafer",
    "xiaomi",
)


@dataclasses.dataclass(frozen=True)
class ProviderLeaderboardRow:
    provider: str
    throughput_tokens_per_second: float | None
    uptime: float
    samples: int
    p50_ttft_ms: int | None


class _LeaderboardTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._in_table = False
        self._in_row = False
        self._in_cell = False
        self._current_table: list[list[str]] = []
        self._current_row: list[str] = []
        self._current_cell: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = dict(attrs)
        if tag == "table" and "leaderboard-table" in (attr_map.get("class") or ""):
            self._in_table = True
            self._current_table = []
        elif self._in_table and tag == "tr":
            self._in_row = True
            self._current_row = []
        elif self._in_table and tag in {"td", "th"}:
            self._in_cell = True
            self._current_cell = []

    def handle_endtag(self, tag: str) -> None:
        if self._in_table and tag in {"td", "th"} and self._in_cell:
            self._current_row.append(" ".join("".join(self._current_cell).split()))
            self._in_cell = False
        elif self._in_table and tag == "tr" and self._in_row:
            if self._current_row:
                self._current_table.append(self._current_row)
            self._in_row = False
        elif tag == "table" and self._in_table:
            self.tables.append(self._current_table)
            self._in_table = False

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._current_cell.append(data)


def parse_provider_rows(html: str) -> list[ProviderLeaderboardRow]:
    parser = _LeaderboardTableParser()
    parser.feed(html)
    if not parser.tables:
        raise ValueError("leaderboard provider table was not found")
    provider_table = parser.tables[0]
    if not provider_table or "Provider" not in provider_table[0]:
        raise ValueError("first leaderboard table is not the provider table")
    rows: list[ProviderLeaderboardRow] = []
    for row in provider_table[1:]:
        if len(row) < 9:
            continue
        rows.append(
            ProviderLeaderboardRow(
                provider=row[1].strip().lower(),
                throughput_tokens_per_second=_parse_throughput(row[4]),
                uptime=_parse_percent(row[5]),
                samples=_parse_int(row[8]),
                p50_ttft_ms=_parse_milliseconds(row[3]),
            )
        )
    return rows


def measured_rank(
    rows: list[ProviderLeaderboardRow],
    *,
    min_samples: int = MIN_SAMPLES,
    min_uptime: float = MIN_UPTIME,
) -> list[str]:
    eligible = [
        row
        for row in rows
        if row.throughput_tokens_per_second is not None
        and row.throughput_tokens_per_second > 0
        and row.samples >= min_samples
        and row.uptime >= min_uptime
    ]
    eligible.sort(
        key=lambda row: (
            -float(row.throughput_tokens_per_second or 0),
            row.p50_ttft_ms if row.p50_ttft_ms is not None else 1_000_000,
            row.provider,
        )
    )
    return [row.provider for row in eligible]


def build_rank_block(measured: list[str], *, generated_date: dt.date | None = None) -> str:
    generated = generated_date or dt.datetime.now(dt.UTC).date()
    ranks: dict[str, int] = {}
    for provider in measured:
        ranks.setdefault(provider, len(ranks))
    for provider in SECONDARY_PROVIDERS:
        if provider not in ranks:
            ranks[provider] = SECONDARY_START + len([p for p in ranks if ranks[p] >= SECONDARY_START])
    ranks["trustedrouter"] = 99

    lines = [
        "# Throughput-first routing rank. Lower values are tried first for",
        '# `provider.sort = "throughput"` and `:nitro`.',
        "#",
        f"# Generated from the public /leaderboard provider table on {generated.isoformat()} with:",
        "#   python scripts/update_provider_throughput_rank.py --write",
        "# The generator admits only providers with enough samples, >=95% measured uptime,",
        "# and positive p50 output tokens/second. Providers without reliable token/s data",
        "# keep conservative secondary ranks so they do not beat measured fast routes.",
        "_THROUGHPUT_RANK = {",
    ]
    for provider, rank in sorted(ranks.items(), key=lambda item: item[1]):
        if rank == SECONDARY_START:
            lines.extend(
                [
                    "    # Current leaderboard rows do not expose enough usable token/s for these",
                    "    # providers. Keep strong prior ordering below the measured set until the",
                    "    # synthetic probes emit stable longer completions for every provider.",
                ]
            )
        lines.append(f'    "{provider}": {rank},')
    lines.append("}")
    return "\n".join(lines)


def replace_rank_block(source: str, new_block: str) -> str:
    pattern = re.compile(
        r"# Throughput-first routing rank\..*?_THROUGHPUT_RANK = \{\n(?:.*?\n)\}",
        re.DOTALL,
    )
    next_source, count = pattern.subn(new_block, source, count=1)
    if count != 1:
        raise ValueError("_THROUGHPUT_RANK block not found")
    return next_source


def _parse_throughput(value: str) -> float | None:
    match = re.search(r"(\d+(?:\.\d+)?)\s*tok/s", value)
    return float(match.group(1)) if match else None


def _parse_percent(value: str) -> float:
    return float(value.replace("%", "").strip()) / 100


def _parse_int(value: str) -> int:
    return int(value.replace(",", "").strip())


def _parse_milliseconds(value: str) -> int | None:
    match = re.search(r"(\d+)\s*ms", value)
    return int(match.group(1)) if match else None


def _read_html(args: argparse.Namespace) -> str:
    if args.html:
        return Path(args.html).read_text()
    response = httpx.get(args.url, timeout=30.0, follow_redirects=True)
    response.raise_for_status()
    return response.text


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=LEADERBOARD_URL)
    parser.add_argument("--html", help="Read leaderboard HTML from a local file")
    parser.add_argument("--write", action="store_true", help="Update src/trusted_router/routing.py")
    args = parser.parse_args()

    rows = parse_provider_rows(_read_html(args))
    measured = measured_rank(rows)
    block = build_rank_block(measured)
    if not args.write:
        print(block)
        return
    source = ROUTING_PATH.read_text()
    ROUTING_PATH.write_text(replace_rank_block(source, block))
    print(f"updated {ROUTING_PATH} with {len(measured)} measured throughput providers")


if __name__ == "__main__":
    main()
