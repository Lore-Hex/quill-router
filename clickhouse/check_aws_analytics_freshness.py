"""Check, from outside the VPC, that the AWS-EU analytics drain is still draining.

Sibling of :mod:`clickhouse.check_client_telemetry_freshness`, and the same
shape: read a PUBLIC ``/status.json`` with no credentials and fail when the
pipeline is not observably alive.  What differs is which cloud and which
signal.

The Paris ClickHouse node is private -- it listens on its VPC address and its
security group admits only the VPC CIDR -- so this job cannot ask it anything.
It reads the ``analytics`` section the control plane publishes instead, whose
rationale and field names live in
:mod:`trusted_router.operational_analytics_freshness`.  The number that matters
is ``drain_lag_seconds``: the age of the oldest row still sitting in the
DSQL outbox.  Rows leave the outbox only after ClickHouse has accepted them, so
a lag that stops falling is a drain that has stopped delivering.

This exists because the drain's own ``backlog_alarm`` cannot fire when the
drain is not running, and on 2026-08-17 that was not hypothetical: no unit had
ever been installed, 465,119 rows had accumulated since 2026-08-02, and every
in-band signal was silent because every in-band signal came from the missing
process.  This check runs somewhere else, on someone else's schedule, and asks
the database rather than the daemon.

Pure function :func:`evaluate` returns the problems; :func:`main` fetches and
reports, writing them to ``--problems-file`` for the workflow's issue body.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import urllib.request
from typing import Any

from trusted_router.operational_analytics_freshness import (
    ANALYTICS_STATUS_KEY,
    AVAILABLE_FIELD,
    DEFAULT_MAX_DRAIN_LAG_SECONDS,
    DRAIN_LAG_FIELD,
    GENERATED_AT_FIELD,
    OUTBOX_DEPTH_FIELD,
    REASON_FIELD,
)

#: The AWS-EU control plane. Not trustedrouter.com: that is the GCP deployment,
#: whose analytics run on an entirely separate cluster and would answer this
#: question about the wrong cloud.
DEFAULT_STATUS_URL = "https://gchircrcif.eu-west-3.awsapprunner.com/status.json"


def _float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def evaluate(
    payload: dict[str, Any],
    *,
    now: dt.datetime,
    max_drain_lag_seconds: float = DEFAULT_MAX_DRAIN_LAG_SECONDS,
    max_age_seconds: int = 3_600,
    max_outbox_depth: int | None = None,
) -> list[str]:
    """Return human-readable problems; an empty list means the drain is healthy."""
    section = payload.get(ANALYTICS_STATUS_KEY)
    if not isinstance(section, dict):
        # Deliberately a FAILURE and not a skip. A missing section means either
        # the control plane does not publish it yet or it stopped publishing
        # it; treating "no signal" as "no problem" is how a monitor becomes
        # decorative.
        return [
            f"/status.json has no {ANALYTICS_STATUS_KEY} section "
            "(the control plane does not publish drain lag)"
        ]
    if not section.get(AVAILABLE_FIELD):
        return [
            f"{ANALYTICS_STATUS_KEY} unavailable: reason={section.get(REASON_FIELD)!r} "
            "(the control plane could not read the outbox)"
        ]

    problems: list[str] = []

    lag = _float(section.get(DRAIN_LAG_FIELD))
    if lag is None:
        problems.append(f"{ANALYTICS_STATUS_KEY}.{DRAIN_LAG_FIELD} missing or unparseable")
    elif lag > max_drain_lag_seconds:
        problems.append(
            f"oldest undelivered outbox row is {lag:.0f}s old "
            f"(> {max_drain_lag_seconds:.0f}s): the drain is behind or stopped"
        )

    # The section's own age. A control plane that froze would otherwise serve a
    # stale-but-healthy lag forever, and the check would agree with it.
    generated_at = _parse_time(section.get(GENERATED_AT_FIELD))
    if generated_at is None:
        problems.append(f"{ANALYTICS_STATUS_KEY}.{GENERATED_AT_FIELD} missing or unparseable")
    else:
        age = int((now - generated_at).total_seconds())
        if age > max_age_seconds:
            problems.append(f"analytics section is {age}s old (> {max_age_seconds}s)")

    # Optional, and only checked when both a bound and a value are present:
    # depth is a count(*) the publisher may decline to pay for.
    depth = _int(section.get(OUTBOX_DEPTH_FIELD))
    if max_outbox_depth is not None and depth is not None and depth > max_outbox_depth:
        problems.append(f"outbox holds {depth} undelivered rows (> {max_outbox_depth})")

    return problems


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--status-url", default=DEFAULT_STATUS_URL)
    parser.add_argument(
        "--max-drain-lag-seconds", type=float, default=DEFAULT_MAX_DRAIN_LAG_SECONDS
    )
    parser.add_argument("--max-age-seconds", type=int, default=3_600)
    parser.add_argument("--max-outbox-depth", type=int, default=None)
    parser.add_argument("--problems-file", default=None)
    return parser.parse_args(argv)


def fetch_status(url: str) -> dict[str, Any]:
    request = urllib.request.Request(  # noqa: S310 - https URL from argv/default only
        url, headers={"user-agent": "trustedrouter-aws-analytics-freshness/1"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        payload = json.loads(response.read().decode("utf-8"))
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        payload = payload["data"]
    if not isinstance(payload, dict):
        raise SystemExit("status.json is not a JSON object")
    return payload


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    payload = fetch_status(args.status_url)
    problems = evaluate(
        payload,
        now=dt.datetime.now(dt.UTC),
        max_drain_lag_seconds=args.max_drain_lag_seconds,
        max_age_seconds=args.max_age_seconds,
        max_outbox_depth=args.max_outbox_depth,
    )
    section = payload.get(ANALYTICS_STATUS_KEY)
    lag = section.get(DRAIN_LAG_FIELD) if isinstance(section, dict) else None
    if args.problems_file:
        with open(args.problems_file, "w", encoding="utf-8") as handle:
            handle.write("\n".join(problems) + ("\n" if problems else ""))
    if problems:
        print(f"aws-eu analytics freshness: FAIL drain_lag_seconds={lag}", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"aws-eu analytics freshness: OK drain_lag_seconds={lag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
