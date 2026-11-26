#!/usr/bin/env python3
"""Print TrustedRouter's first-party acquisition funnel by creative."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from trusted_router.ai_referrals import ai_referral_platform  # noqa: E402
from trusted_router.marketing_funnel import (  # noqa: E402
    FUNNEL_EVENTS,
    aggregate_cohort_funnel_rows,
    build_axiom_cohort_query,
    parse_axiom_json_lines,
    parse_cloud_logging_acquisition_events,
    recover_creative_attribution,
    render_markdown,
    render_measurement_markdown,
    summarize_measurement,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Query metadata-only acquisition events and report engaged visits, "
            "signups, activated API users, purchases, and retention by UTM creative."
        )
    )
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--dataset", default="trusted-router-logs")
    parser.add_argument("--source")
    parser.add_argument("--ai-only", action="store_true", help="Report AI-referred acquisition cohorts, not citation counts.")
    parser.add_argument("--campaign")
    parser.add_argument("--creative")
    parser.add_argument("--experiment-id")
    parser.add_argument("--experiment-cell-id")
    parser.add_argument(
        "--cohort-lag-days",
        type=int,
        default=7,
        help=(
            "Exclude the newest acquisition days from rate comparisons while still "
            "observing their later conversion events. Default: 7."
        ),
    )
    parser.add_argument(
        "--landing",
        dest="landing_path",
        help="Limit the report to one exact landing path, such as /openrouter-alternative.",
    )
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument(
        "--public-engagements",
        choices=("auto", "required", "off"),
        default="auto",
        help=(
            "Merge landing engagements and recover creative IDs from metadata-only Cloud Logging. "
            "'required' fails if they cannot be read."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not 1 <= args.days <= 365:
        raise SystemExit("--days must be between 1 and 365")
    if not 0 <= args.cohort_lag_days <= 90:
        raise SystemExit("--cohort-lag-days must be between 0 and 90")
    axiom = shutil.which("axiom")
    if axiom is None:
        raise SystemExit("Axiom CLI is required. Install it and run `axiom auth login`.")
    observed_through = dt.datetime.now(dt.UTC)
    cohort_end = observed_through - dt.timedelta(days=args.cohort_lag_days)
    cohort_start = cohort_end - dt.timedelta(days=args.days)
    cohort_records = fetch_axiom_cohort_records(
        axiom, args.dataset, start_at=cohort_start, end_at=observed_through,
    )
    cloud_records, engagement_error = _cloud_acquisition_events(
        args,
        start_at=cohort_start,
        end_at=observed_through,
    )
    cloud_engagements = [
        row for row in cloud_records if row["event"] == "acquisition.landing_engaged"
    ]
    cohort_records.extend(cloud_engagements)
    cohort_records, recovery = recover_creative_attribution(cohort_records, cloud_records)
    rows = aggregate_cohort_funnel_rows(
        cohort_records,
        cohort_start=cohort_start,
        cohort_end=cohort_end,
        observed_through=observed_through,
        source=args.source,
        campaign=args.campaign,
        creative=args.creative,
        landing_path=args.landing_path,
        experiment_id=args.experiment_id,
        experiment_cell_id=args.experiment_cell_id,
    )
    if getattr(args, "ai_only", False):
        rows = [row for row in rows if ai_referral_platform(row.source)]
    summary = summarize_measurement(rows, source=args.source)
    if args.format == "json":
        print(
            json.dumps(
                {
                    "measurement": summary.as_dict(),
                    "traffic_scope": "ai_referral_cohorts" if getattr(args, "ai_only", False) else "all_matching_cohorts",
                    "citation_counts_available": False,
                    "cohort": {
                        "start_at": cohort_start.isoformat(),
                        "end_at": cohort_end.isoformat(),
                        "observed_through": observed_through.isoformat(),
                        "conversion_lag_days": args.cohort_lag_days,
                    },
                    "google_ads_spend": {"status": "unavailable", "reason": "native_spend_not_collected"},
                    "public_engagements": {
                        "status": "available" if engagement_error is None else "unavailable",
                        "events": len(cloud_engagements),
                        "reason": engagement_error,
                    },
                    "creative_attribution": {**recovery, "evidence_error": engagement_error},
                    "funnel": [row.as_dict() for row in rows],
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        if getattr(args, "ai_only", False):
            print("AI referral cohorts by landing page. Observed referrals and conversions, not citation counts.\n")
        print(
            f"Creative attribution: recovered {recovery['recovered_records']} records; "
            f"{recovery['unresolved_records']} remain unattributed."
        )
        print(render_measurement_markdown(summary), end="")
        print(render_markdown(rows), end="")
    return 0


def fetch_axiom_cohort_records(
    axiom: str, dataset: str, *, start_at: dt.datetime, end_at: dt.datetime,
) -> list[dict[str, object]]:
    """Split capped CLI results into disjoint windows; never return partial data."""
    pending = [(start_at, end_at)]
    records: list[dict[str, object]] = []
    queries = 0
    while pending:
        start, end = pending.pop()
        queries += 1
        if queries > 511:
            raise SystemExit("Axiom report exceeds query budget; shorten the window")
        query = build_axiom_cohort_query(dataset, start_at=start, end_at=end) + " | limit 1000"
        completed = subprocess.run(  # noqa: S603 - caller resolves the CLI executable.
            [axiom, "query", query, "--start-time", start.isoformat(), "--end-time",
             end.isoformat(), "--format", "json", "--no-spinner"],
            check=False, capture_output=True, text=True, timeout=120,
        )
        if completed.returncode != 0:
            raise SystemExit(completed.stderr.strip() or "Axiom query failed")
        # The CLI emits [] for an empty interval, but JSONL for populated ones.
        batch = [] if completed.stdout.strip() == "[]" else parse_axiom_json_lines(completed.stdout)
        if len(batch) >= 1000:
            if end - start <= dt.timedelta(seconds=1):
                raise SystemExit("Axiom query remains truncated at a one-second window")
            midpoint = start + (end - start) / 2
            pending.extend([(midpoint, end), (start, midpoint)])
            continue
        records.extend(batch)
        if len(records) > 100_000:
            raise SystemExit("Axiom report exceeds row budget; shorten the window")
    return records


def _cloud_acquisition_events(
    args: argparse.Namespace,
    *,
    start_at: dt.datetime,
    end_at: dt.datetime,
) -> tuple[list[dict[str, object]], str | None]:
    if args.public_engagements == "off":
        return [], "cloud_logging_disabled"
    gcloud = shutil.which("gcloud")
    if gcloud is None:
        if args.public_engagements == "required":
            raise SystemExit("gcloud CLI is required for acquisition telemetry")
        return [], "gcloud_not_available"
    start = start_at.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")
    end = end_at.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")
    event_filter = " OR ".join(f'"{event}"' for event in FUNNEL_EVENTS)
    log_filter = (
        'resource.type="cloud_run_revision" '
        'AND resource.labels.service_name=("trusted-router-public" OR "trusted-router") '
        f'AND jsonPayload.event=({event_filter}) '
        f'AND timestamp>="{start}" AND timestamp<="{end}"'
    )
    fields = (
        "event", "anonymous_fingerprint", "utm_source", "utm_medium", "utm_campaign",
        "utm_content", "creative_id", "landing_path", "experiment_id", "experiment_cell_id",
        "has_gclid", "has_gbraid", "has_wbraid", "google_ads_click_persisted",
    )
    projection = "json(timestamp," + ",".join(f"jsonPayload.{name}" for name in fields) + ")"
    completed = subprocess.run(  # noqa: S603 - executable is resolved by shutil.which.
        [
            gcloud,
            "logging",
            "read",
            log_filter,
            "--project",
            "quill-cloud-proxy",
            f"--format={projection}",
            "--limit=100000",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        if args.public_engagements == "required":
            detail = completed.stderr.strip() or "Cloud Logging query failed"
            raise SystemExit(detail)
        return [], "cloud_logging_fetch_failed"
    try:
        rows = parse_cloud_logging_acquisition_events(completed.stdout)
        if len(rows) >= 100000:
            raise ValueError("Cloud Logging acquisition query reached its row limit; shorten the window")
        return rows, None
    except ValueError as exc:
        if args.public_engagements == "required":
            raise SystemExit(str(exc)) from exc
        return [], "cloud_logging_parse_failed"


if __name__ == "__main__":
    raise SystemExit(main())
