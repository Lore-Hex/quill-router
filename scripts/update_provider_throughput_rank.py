#!/usr/bin/env python3
"""Refresh routing evidence from the ClickHouse-backed public seven-day leaderboard.

Does not alter public performance ranks. Run with --write and review the diff.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from bs4 import BeautifulSoup

from trusted_router.provider_ranking import validate_snapshot

LEADERBOARD_URL = "https://trustedrouter.com/leaderboard?window=7d"
EVIDENCE_PATH = Path("src/trusted_router/data/provider_routing_evidence.json")


def parse_snapshot(html: str, *, now: datetime | None = None) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    updated = soup.select_one(".lb-muted")
    if updated is None or not updated.get_text().startswith("Last updated "):
        raise ValueError("leaderboard timestamp not found")
    rows = []
    for row in soup.select("#lb-providers [data-lb-row]"):
        if not row.get("data-completion"):
            continue
        count = row.select_one('[data-label="Effective throughput"] small')
        if count is None:
            raise ValueError("throughput sample count not found")
        rows.append({
            "provider": str(row["data-provider"]),
            "completion_rate": float(str(row["data-completion"])),
            "p50_ttft_ms": int(str(row["data-ttft"])) if row["data-ttft"] else None,
            "tokens_per_second": float(str(row["data-throughput"])) if row["data-throughput"] else None,
            "samples": int(str(row["data-samples"])),
            "throughput_samples": int(count.get_text().split()[0]),
        })
    payload = {
        "source": LEADERBOARD_URL, "window": "7d",
        "generated_at": updated.get_text().removeprefix("Last updated "),
        "providers": rows,
    }
    generated, _ranks = validate_snapshot(payload)
    if not timedelta(0) <= (now or datetime.now(UTC)) - generated <= timedelta(days=1):
        raise ValueError("leaderboard evidence is stale or future-dated")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--html", help="Read a saved seven-day leaderboard page")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    if args.html:
        html = Path(args.html).read_text()
    else:
        response = httpx.get(LEADERBOARD_URL, timeout=30.0, follow_redirects=True)
        response.raise_for_status()
        html = response.text
    payload = parse_snapshot(html)
    encoded = json.dumps(payload, indent=2) + "\n"
    if args.write:
        EVIDENCE_PATH.write_text(encoded)
        print(f"updated {EVIDENCE_PATH}: {len(payload['providers'])} providers")
    else:
        print(encoded)


if __name__ == "__main__":
    main()
