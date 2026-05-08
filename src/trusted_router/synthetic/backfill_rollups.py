from __future__ import annotations

import argparse
import datetime as dt
from collections.abc import Sequence
from typing import Any

from trusted_router.config import get_settings
from trusted_router.storage_gcp_codec import json_body
from trusted_router.storage_gcp_synthetic_index import synthetic_probe_samples
from trusted_router.storage_models import SyntheticProbeSample, SyntheticRollup
from trusted_router.synthetic.rollups import (
    apply_sample_to_rollup,
    new_rollup_for_sample,
    sample_rollup_ids,
)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Backfill synthetic status rollups from raw samples.")
    parser.add_argument("--date", action="append", dest="dates", help="UTC date YYYY-MM-DD.")
    parser.add_argument("--days", type=int, default=14, help="Recent UTC days to backfill when --date is omitted.")
    parser.add_argument("--limit-per-day", type=int, default=35_000)
    args = parser.parse_args(argv)

    settings = get_settings()
    if not settings.gcp_project_id or not settings.bigtable_instance_id:
        raise SystemExit("TR_GCP_PROJECT_ID and TR_BIGTABLE_INSTANCE_ID are required")

    try:
        from google.cloud import bigtable
    except ImportError as exc:  # pragma: no cover - dependency exists in prod image.
        raise SystemExit("google-cloud-bigtable is required") from exc

    table = (
        bigtable.Client(project=settings.gcp_project_id, admin=True)
        .instance(settings.bigtable_instance_id)
        .table(settings.bigtable_generation_table)
    )
    dates = args.dates or _recent_dates(args.days)
    all_samples: list[SyntheticProbeSample] = []
    for date in dates:
        samples = synthetic_probe_samples(
            table,
            "m",
            date=date,
            target=None,
            probe_type=None,
            monitor_region=None,
            limit=args.limit_per_day,
        )
        print(f"{date}: read {len(samples)} raw samples", flush=True)
        all_samples.extend(samples)
    rollups = _recompute_rollups(all_samples)
    print(f"writing {len(rollups)} recomputed rollup rows from {len(all_samples)} samples", flush=True)
    for index, rollup in enumerate(rollups, 1):
        _write_rollup(table, "m", rollup)
        if index % 500 == 0:
            print(f"wrote {index}/{len(rollups)} rollups", flush=True)
    print("complete", flush=True)


def _recompute_rollups(samples: list[SyntheticProbeSample]) -> list[SyntheticRollup]:
    rollups: dict[bytes, SyntheticRollup] = {}
    seen: set[tuple[bytes, str]] = set()
    for sample in samples:
        for period, component in sample_rollup_ids(sample):
            candidate = new_rollup_for_sample(sample, period=period, component=component)
            key = _rollup_key(candidate)
            seen_key = (key, sample.id)
            if seen_key in seen:
                continue
            seen.add(seen_key)
            existing = rollups.get(key)
            if existing is None:
                rollups[key] = candidate
            else:
                apply_sample_to_rollup(existing, sample)
    return sorted(rollups.values(), key=lambda row: (row.period, row.period_start, row.component))


def _rollup_key(rollup: SyntheticRollup) -> bytes:
    parts = [
        "synthetic_rollup",
        rollup.period,
        rollup.period_start,
        rollup.component,
        rollup.target,
        rollup.probe_type,
        rollup.monitor_region,
        rollup.target_region or "-",
    ]
    return "#".join(parts).encode("utf-8")


def _write_rollup(table: Any, family: str, rollup: SyntheticRollup) -> None:
    row = table.direct_row(_rollup_key(rollup))
    row.set_cell(family, b"body", json_body(rollup).encode("utf-8"))
    row.commit()


def _recent_dates(days: int) -> list[str]:
    today = dt.datetime.now(dt.UTC).date()
    return [
        (today - dt.timedelta(days=offset)).isoformat()
        for offset in reversed(range(max(days, 1)))
    ]


if __name__ == "__main__":
    main()
