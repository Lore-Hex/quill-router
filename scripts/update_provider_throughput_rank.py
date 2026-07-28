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
MIN_THROUGHPUT_SAMPLES: Final = 5
MIN_THROUGHPUT_COMPLETION_RATE: Final = 0.70
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
    throughput_samples: int
    throughput_attempts: int
    throughput_completion_rate: float
    throughput_confidence: str
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
                throughput_samples=_parse_throughput_samples(row[4]),
                throughput_attempts=_parse_throughput_attempts(row[4]),
                throughput_completion_rate=_parse_throughput_completion_rate(row[4]),
                throughput_confidence=_parse_throughput_confidence(row[4]),
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
    min_throughput_samples: int = MIN_THROUGHPUT_SAMPLES,
    min_throughput_completion_rate: float = MIN_THROUGHPUT_COMPLETION_RATE,
    min_uptime: float = MIN_UPTIME,
) -> list[str]:
    eligible = [
        row
        for row in rows
        if row.throughput_tokens_per_second is not None
        and row.throughput_tokens_per_second > 0
        and row.throughput_samples >= min_throughput_samples
        and row.throughput_completion_rate >= min_throughput_completion_rate
        and row.throughput_confidence in {"medium", "high"}
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
            ranks[provider] = SECONDARY_START + len(
                [p for p in ranks if ranks[p] >= SECONDARY_START]
            )
    ranks["trustedrouter"] = 99

    lines = [
        "# Throughput-first routing rank. Lower values are tried first for",
        '# `provider.sort = "throughput"` and `:nitro`.',
        "#",
        f"# Generated from the public /leaderboard provider table on {generated.isoformat()} with:",
        "#   python scripts/update_provider_throughput_rank.py --write",
        "# The generator admits only providers with enough availability and throughput",
        "# samples, >=70% throughput completion, medium/high confidence,",
        "# >=95% pinned route success, and positive effective visible tokens/second.",
        "# Providers without reliable token/s data",
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


def _parse_throughput_samples(value: str) -> int:
    match = re.search(r"\bn=(\d+)(?:/\d+)?\b", value)
    return int(match.group(1)) if match else 0


def _parse_throughput_attempts(value: str) -> int:
    match = re.search(r"\bn=\d+/(\d+)\b", value)
    if match:
        return int(match.group(1))
    return _parse_throughput_samples(value)


def _parse_throughput_completion_rate(value: str) -> float:
    match = re.search(r"(\d+(?:\.\d+)?)%\s+complete\b", value)
    if match:
        return float(match.group(1)) / 100
    attempts = _parse_throughput_attempts(value)
    return _parse_throughput_samples(value) / attempts if attempts else 0.0


def _parse_throughput_confidence(value: str) -> str:
    match = re.search(r"\b(high|medium|low)\b", value, re.IGNORECASE)
    # Old pages had only successful n values. Treat enough old measurements as
    # medium confidence during a rolling deploy, never as high confidence.
    if match:
        return match.group(1).lower()
    return "medium" if _parse_throughput_samples(value) >= MIN_THROUGHPUT_SAMPLES else "low"


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
