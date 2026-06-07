#!/usr/bin/env python3
"""Coverage + staleness audit for the hourly price-refresh system.

A price change can only be CAUGHT for a provider whose prices the refresh
actually re-reads each run. This reports the gaps:
  * prepaid providers (GATEWAY_PREPAID_PROVIDER_SLUGS) with NO live scraper
    in scripts/pricing/providers/ — they rely on a static manifest (which
    drifts) or have no price source at all (hand-coded catalog prices that
    never refresh, e.g. Cohere embeddings);
  * provider_models/<slug>.json manifests whose `generated_at` is older than
    --max-age-days (stale → may serve wrong prices).

Run in refresh-prices.yml as a non-failing visibility step (writes to the
Actions run summary). Pass --strict to fail CI on any gap.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROVIDERS_DIR = ROOT / "scripts" / "pricing" / "providers"
MANIFEST_DIR = ROOT / "src" / "trusted_router" / "data" / "provider_models"
DEFAULT_MAX_AGE_DAYS = 14


def _scraper_slugs() -> set[str]:
    if not PROVIDERS_DIR.is_dir():
        return set()
    return {
        p.stem
        for p in PROVIDERS_DIR.glob("*.py")
        if p.stem not in {"__init__", "base", "_base"}
    }


def _manifest_age_days(path: Path, now: dt.datetime) -> float | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    gen = raw.get("generated_at")
    if not isinstance(gen, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(gen.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return (now - parsed).total_seconds() / 86400.0


def audit(max_age_days: int, now: dt.datetime) -> tuple[list[str], list[str]]:
    """Return (warnings, info)."""
    from trusted_router.catalog import GATEWAY_PREPAID_PROVIDER_SLUGS

    scrapers = _scraper_slugs()
    warnings: list[str] = []
    info: list[str] = []

    for slug in sorted(GATEWAY_PREPAID_PROVIDER_SLUGS):
        if slug in scrapers:
            info.append(f"{slug}: live scraper ✓")
            continue
        manifest = MANIFEST_DIR / f"{slug}.json"
        if not manifest.exists():
            warnings.append(
                f"{slug}: NO price source (no scraper, no manifest) — "
                f"catalog prices are hand-coded and never refresh"
            )
            continue
        age = _manifest_age_days(manifest, now)
        if age is None:
            warnings.append(f"{slug}: no scraper; manifest has no parseable generated_at")
        elif age > max_age_days:
            warnings.append(
                f"{slug}: no scraper; manifest is {age:.0f}d stale "
                f"(> {max_age_days}d) — prices may be wrong"
            )
        else:
            info.append(f"{slug}: manifest {age:.0f}d old (within {max_age_days}d) ✓")

    return warnings, info


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-age-days", type=int, default=DEFAULT_MAX_AGE_DAYS)
    parser.add_argument("--strict", action="store_true", help="exit 1 if any gaps found")
    parser.add_argument("--now", default=None, help="ISO timestamp override (testing)")
    args = parser.parse_args(argv)

    now = dt.datetime.now(dt.UTC)
    if args.now:
        now = dt.datetime.fromisoformat(args.now.replace("Z", "+00:00"))
        if now.tzinfo is None:
            now = now.replace(tzinfo=dt.UTC)

    warnings, info = audit(args.max_age_days, now)

    print("## Price-source coverage")
    if warnings:
        print("")
        print("⚠️ Gaps (price changes for these may be MISSED — review manually):")
        for w in warnings:
            print(f"  - {w}")
    else:
        print("")
        print("All prepaid providers have a fresh price source.")
    if info:
        print("")
        print("Covered:")
        for i in info:
            print(f"  - {i}")
    return 1 if (args.strict and warnings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
