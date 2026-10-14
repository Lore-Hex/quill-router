#!/usr/bin/env python3
"""Refresh routing evidence from the ClickHouse-backed public seven-day leaderboard.

Does not alter public performance ranks. Run with --write and review the diff.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile
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
    parser.add_argument("--min-age-hours", type=int, default=0,
                        help="Skip a scheduled write while validated evidence is younger than this")
    args = parser.parse_args()
    if args.min_age_hours < 0:
        parser.error("--min-age-hours must be nonnegative")
    if args.write and args.min_age_hours and EVIDENCE_PATH.exists():
        generated, _ = validate_snapshot(json.loads(EVIDENCE_PATH.read_text()))
        age = datetime.now(UTC) - generated
        if timedelta(0) <= age < timedelta(hours=args.min_age_hours):
            print("Routing evidence is current; no refresh due")
            return
    if args.html:
        html = Path(args.html).read_text()
    else:
        response = httpx.get(LEADERBOARD_URL, timeout=30.0, follow_redirects=True)
        response.raise_for_status()
        html = response.text
    payload = parse_snapshot(html)
    encoded = json.dumps(payload, indent=2) + "\n"
    if args.write:
        temporary = None
        try:
            with NamedTemporaryFile("w", dir=EVIDENCE_PATH.parent, delete=False) as output:
                temporary = Path(output.name)
                output.write(encoded)
            temporary.replace(EVIDENCE_PATH)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        print(f"updated {EVIDENCE_PATH}: {len(payload['providers'])} providers")
    else:
        print(encoded)


if __name__ == "__main__":
    main()
