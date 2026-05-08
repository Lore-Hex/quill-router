from __future__ import annotations

import argparse
import datetime as dt
from collections.abc import Sequence

from trusted_router.config import get_settings
from trusted_router.storage_gcp_synthetic_index import synthetic_probe_samples
from trusted_router.storage_gcp_synthetic_rollups import write_synthetic_rollups


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Backfill synthetic status rollups from raw samples.")
    parser.add_argument("--date", action="append", dest="dates", help="UTC date YYYY-MM-DD.")
    parser.add_argument("--days", type=int, default=3, help="Recent UTC days to backfill when --date is omitted.")
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
        for index, sample in enumerate(samples, 1):
            write_synthetic_rollups(table, "m", sample)
            if index % 5_000 == 0:
                print(f"{date}: backfilled {index}/{len(samples)}", flush=True)
        print(f"{date}: complete", flush=True)


def _recent_dates(days: int) -> list[str]:
    today = dt.datetime.now(dt.UTC).date()
    return [
        (today - dt.timedelta(days=offset)).isoformat()
        for offset in reversed(range(max(days, 1)))
    ]


if __name__ == "__main__":
    main()
