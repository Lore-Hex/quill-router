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
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import httpx  # noqa: E402

from trusted_router.google_ads_reporting import (  # noqa: E402
    GoogleAdsAccessTokenProvider,
    GoogleAdsReportingClient,
    GoogleAdsReportingConfig,
    GoogleAdsReportingError,
    GoogleAdsSpendReport,
    google_ads_reporting_window,
)
from trusted_router.marketing_funnel import (  # noqa: E402
    aggregate_cohort_funnel_rows,
    build_axiom_cohort_query,
    parse_axiom_json_lines,
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
        "--google-ads-spend",
        choices=("auto", "required", "off"),
        default="auto",
        help=(
            "Pull aggregate spend from Google Ads when credentials are configured. "
            "'required' fails instead of withholding CAC/ROAS."
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
    spend, spend_error, cohort_start, cohort_end = _google_ads_spend(args)
    query = build_axiom_cohort_query(args.dataset)
    completed = subprocess.run(  # noqa: S603 - executable is resolved by shutil.which.
        [
            axiom,
            "query",
            query,
            "--start-time",
            cohort_start.isoformat().replace("+00:00", "Z"),
            "--format",
            "json",
            "--no-spinner",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "Axiom query failed"
        raise SystemExit(detail)
    observed_through = dt.datetime.now(dt.UTC)
    rows = aggregate_cohort_funnel_rows(
        parse_axiom_json_lines(completed.stdout),
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
    summary = summarize_measurement(
        rows,
        source=args.source,
        spend=spend,
        spend_error=spend_error,
    )
    if args.format == "json":
        print(
            json.dumps(
                {
                    "measurement": summary.as_dict(),
                    "cohort": {
                        "start_at": cohort_start.isoformat(),
                        "end_at": cohort_end.isoformat(),
                        "observed_through": observed_through.isoformat(),
                        "conversion_lag_days": args.cohort_lag_days,
                    },
                    "google_ads_spend": (
                        spend.as_dict()
                        if spend
                        else {"status": "unavailable", "reason": spend_error}
                    ),
                    "funnel": [row.as_dict() for row in rows],
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(render_measurement_markdown(summary), end="")
        print(render_markdown(rows), end="")
    return 0


def _google_ads_spend(
    args: argparse.Namespace,
) -> tuple[GoogleAdsSpendReport | None, str | None, dt.datetime, dt.datetime]:
    fallback_end = dt.datetime.now(dt.UTC) - dt.timedelta(days=args.cohort_lag_days)
    fallback_start = fallback_end - dt.timedelta(days=args.days)
    if args.google_ads_spend == "off" or (args.source or "").casefold() != "google":
        return None, "native_spend_disabled", fallback_start, fallback_end
    try:
        config = GoogleAdsReportingConfig.from_environment()
    except ValueError as exc:
        if args.google_ads_spend == "required":
            raise SystemExit(str(exc)) from exc
        return None, "native_spend_not_configured", fallback_start, fallback_end
    start_date, end_date, start_at = google_ads_reporting_window(
        days=args.days,
        time_zone=config.time_zone,
        lag_days=args.cohort_lag_days,
    )
    zone = ZoneInfo(config.time_zone)
    end_at = dt.datetime.combine(
        end_date + dt.timedelta(days=1),
        dt.time.min,
        tzinfo=zone,
    ).astimezone(dt.UTC)
    try:
        with httpx.Client(timeout=httpx.Timeout(30.0)) as client:
            spend = GoogleAdsReportingClient(
                config=config,
                client=client,
                token_provider=GoogleAdsAccessTokenProvider(),
            ).fetch_spend(start_date=start_date, end_date=end_date)
            filtered_spend = spend.filtered(
                campaign=args.campaign,
                creative=args.creative,
                experiment_id=args.experiment_id,
                experiment_cell_id=args.experiment_cell_id,
            )
            if any(
                value is not None
                for value in (
                    args.campaign,
                    args.creative,
                    args.experiment_id,
                    args.experiment_cell_id,
                )
            ):
                spend = filtered_spend
    except (GoogleAdsReportingError, OSError) as exc:
        if args.google_ads_spend == "required":
            raise SystemExit(str(exc)) from exc
        return None, "native_spend_fetch_failed", start_at, end_at
    return spend, None, start_at, end_at


if __name__ == "__main__":
    raise SystemExit(main())
