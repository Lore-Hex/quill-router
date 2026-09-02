#!/usr/bin/env python3
"""Export controlled Google Search message cells for Ads Editor/API import."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from urllib.parse import urlencode

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from trusted_router.marketing_experiments import (  # noqa: E402
    GOOGLE_SEARCH_CELLS,
    GOOGLE_SEARCH_EXPERIMENT_ID,
    GOOGLE_SEARCH_WAVE_COUNT,
    GoogleSearchExperimentCell,
    google_search_wave,
)

_AD_HEADLINES = {
    "migrate": "Switch Routers In One Line",
    "privacy": "Verify Your LLM Gateway",
    "uptime": "Automatic LLM Failover",
    "models": "550+ Models. One API.",
    "price": "No AI Router Subscription",
    "speed": "Route To Faster AI Models",
    "nolog": "Prompt Content Stays Private",
    "open": "Open Source LLM Gateway",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wave", type=int)
    parser.add_argument("--format", choices=("csv", "json"), default="csv")
    return parser.parse_args()


def cell_record(cell: GoogleSearchExperimentCell, *, wave: int) -> dict[str, object]:
    query = urlencode(
        {
            "utm_source": "google",
            "utm_medium": "paid_search",
            "utm_campaign": GOOGLE_SEARCH_EXPERIMENT_ID,
            "utm_content": cell.cell_id,
            "tr_exp": GOOGLE_SEARCH_EXPERIMENT_ID,
            "tr_cell": cell.cell_id,
        }
    )
    description = f"{cell.promise.detail} {cell.call_to_action.label}."
    if len(description) > 90:
        description = cell.promise.detail[:89].rstrip(" .") + "."
    return {
        "experiment_id": GOOGLE_SEARCH_EXPERIMENT_ID,
        "wave": wave,
        "cell_id": cell.cell_id,
        "audience": cell.audience.code,
        "promise": cell.promise.code,
        "proof": cell.proof.code,
        "cta": cell.call_to_action.code,
        "headline_1": _AD_HEADLINES[cell.promise.code],
        "headline_2": "TrustedRouter AI API",
        "headline_3": "Keep Your OpenAI SDK",
        "description_1": description,
        "description_2": "Privacy with proof. Public prices. Automatic provider fallback.",
        "final_url": (
            "https://trustedrouter.com/openrouter-alternative/test/"
            f"{cell.cell_id}?{query}"
        ),
    }


def main() -> int:
    args = parse_args()
    if args.wave is None:
        cells = GOOGLE_SEARCH_CELLS
        wave_by_cell = {
            cell.cell_id: wave
            for wave in range(GOOGLE_SEARCH_WAVE_COUNT)
            for cell in google_search_wave(wave)
        }
        records = [
            cell_record(cell, wave=wave_by_cell[cell.cell_id])
            for cell in cells
        ]
    else:
        if not 0 <= args.wave < GOOGLE_SEARCH_WAVE_COUNT:
            raise SystemExit(f"--wave must be between 0 and {GOOGLE_SEARCH_WAVE_COUNT - 1}")
        records = [cell_record(cell, wave=args.wave) for cell in google_search_wave(args.wave)]
    if args.format == "json":
        print(json.dumps(records, indent=2, sort_keys=True))
        return 0
    writer = csv.DictWriter(sys.stdout, fieldnames=list(records[0]))
    writer.writeheader()
    writer.writerows(records)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
