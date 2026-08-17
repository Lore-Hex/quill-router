"""Check, from outside every VPC, that EVERY cloud's analytics drain is draining.

Sibling of :mod:`clickhouse.check_client_telemetry_freshness`, and the same
shape: read a PUBLIC ``/status.json`` with no credentials and fail when the
pipeline is not observably alive.  What differs is which signal, and how many
clouds.

Each cloud's ClickHouse node is private -- AWS-EU's listens on its VPC address
and its security group admits only the VPC CIDR -- so this job cannot ask any
of them anything.  It reads the ``analytics`` section each control plane
publishes instead, whose rationale and field names live in
:mod:`trusted_router.operational_analytics_freshness`.  The number that matters
is ``drain_lag_seconds``: the age of the oldest row still sitting in that
cloud's outbox.  Rows leave the outbox only after ClickHouse has accepted them,
so a lag that stops falling is a drain that has stopped delivering.

WHY THE FLEET AND NOT ONE CLOUD
    This started as an AWS-only check, and an AWS-only check would have been
    the outage repeating itself one layer up.  Between 2026-08-02 and
    2026-08-17 the AWS-EU drain had never been installed -- no unit, no env
    file, a node role with no ``dsql`` permission at all -- and 470,370 rows
    accumulated in silence, because the only backlog alarm is emitted BY the
    drain.  GCP was healthy throughout, so the fleet looked healthy.  A monitor
    that reads one cloud reproduces exactly that: it is green because of the
    cloud it happens to be pointed at.

    So the URL list is not an argument.  It is
    :data:`trusted_router.operational_analytics_fleet.ANALYTICS_FRESHNESS_FLEET`,
    which CI binds to the deployment list in both directions.

A cloud is a FAILURE, never a skip, when it is missing the section, publishes
it unavailable, is stale, or exceeds max drain lag.  An unreachable status page
is also a failure: this job has no way to distinguish "the control plane is
down" from "the control plane is fine and I could not be bothered", and only
one of those is safe to ignore.

Pure function :func:`evaluate` returns one cloud's problems; :func:`main`
fetches every registry entry and reports, writing the union to
``--problems-file`` for the workflow's issue body.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import urllib.request
from typing import Any

from trusted_router.operational_analytics_fleet import (
    ANALYTICS_FRESHNESS_FLEET,
    FleetAnalyticsEndpoint,
    registry_defects,
)
from trusted_router.operational_analytics_freshness import (
    ANALYTICS_STATUS_KEY,
    AVAILABLE_FIELD,
    DEFAULT_MAX_DRAIN_LAG_SECONDS,
    DRAIN_LAG_FIELD,
    GENERATED_AT_FIELD,
    OUTBOX_DEPTH_FIELD,
    REASON_FIELD,
)

#: Kept for the single-cloud alias in :mod:`clickhouse.check_aws_analytics_freshness`.
#: The AWS-EU control plane; not trustedrouter.com, which is the GCP deployment
#: and would answer this question about the wrong cloud.
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


def evaluate_fleet(
    payloads: dict[str, dict[str, Any] | None],
    *,
    now: dt.datetime,
    registry: tuple[FleetAnalyticsEndpoint, ...] = ANALYTICS_FRESHNESS_FLEET,
    max_drain_lag_seconds: float = DEFAULT_MAX_DRAIN_LAG_SECONDS,
    max_age_seconds: int = 3_600,
    max_outbox_depth: int | None = None,
) -> list[str]:
    """Problems across the whole fleet, each prefixed with the cloud it is about.

    ``payloads`` maps cloud name to the fetched ``/status.json`` body, or to
    ``None`` when the fetch itself failed.  Every registry entry must appear:
    a cloud absent from ``payloads`` is reported rather than skipped, because
    "nobody fetched it" and "it is healthy" must not render the same.
    """
    problems: list[str] = []
    for defect in registry_defects():
        problems.append(f"registry: {defect}")
    for entry in registry:
        if not entry.checkable:
            problems.append(
                f"{entry.cloud}: not checkable over HTTP (reason={entry.reason!r}). "
                "Its drain lag is not observable from CI; check it by hand or give "
                "the deployment a public control-plane status page."
            )
            continue
        if entry.cloud not in payloads:
            problems.append(
                f"{entry.cloud}: never fetched. Every registry entry must be "
                "checked on every run, or the fleet result is only about the "
                "clouds somebody remembered."
            )
            continue
        payload = payloads[entry.cloud]
        if payload is None:
            problems.append(
                f"{entry.cloud}: could not read {entry.status_url} "
                "(the control plane is unreachable or served no JSON object)"
            )
            continue
        for problem in evaluate(
            payload,
            now=now,
            max_drain_lag_seconds=max_drain_lag_seconds,
            max_age_seconds=max_age_seconds,
            max_outbox_depth=max_outbox_depth,
        ):
            problems.append(f"{entry.cloud}: {problem}")
    return problems


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument(
        "--cloud",
        action="append",
        default=None,
        help=(
            "Restrict the run to these clouds (repeatable). Debugging aid only: "
            "the default is every registry entry, on purpose."
        ),
    )
    parser.add_argument(
        "--status-url",
        action="append",
        default=None,
        metavar="CLOUD=URL",
        help="Override one cloud's status URL, e.g. aws=https://host/status.json.",
    )
    parser.add_argument(
        "--max-drain-lag-seconds", type=float, default=DEFAULT_MAX_DRAIN_LAG_SECONDS
    )
    parser.add_argument("--max-age-seconds", type=int, default=3_600)
    parser.add_argument("--max-outbox-depth", type=int, default=None)
    parser.add_argument("--problems-file", default=None)
    return parser.parse_args(argv)


def fetch_status(url: str) -> dict[str, Any]:
    request = urllib.request.Request(  # noqa: S310 - https URL from the registry/argv only
        url, headers={"user-agent": "trustedrouter-fleet-analytics-freshness/1"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        payload = json.loads(response.read().decode("utf-8"))
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        payload = payload["data"]
    if not isinstance(payload, dict):
        raise ValueError("status.json is not a JSON object")
    return payload


def _resolved_registry(args: argparse.Namespace) -> tuple[FleetAnalyticsEndpoint, ...]:
    overrides: dict[str, str] = {}
    for raw in args.status_url or []:
        cloud, _, url = str(raw).partition("=")
        if not cloud or not url:
            raise SystemExit(f"--status-url expects CLOUD=URL, got {raw!r}")
        overrides[cloud.strip()] = url.strip()
    unknown = set(overrides) - {entry.cloud for entry in ANALYTICS_FRESHNESS_FLEET}
    if unknown:
        raise SystemExit(f"--status-url names unknown cloud(s): {sorted(unknown)}")
    selected = set(args.cloud or []) or None
    if selected:
        unknown_clouds = selected - {entry.cloud for entry in ANALYTICS_FRESHNESS_FLEET}
        if unknown_clouds:
            raise SystemExit(f"--cloud names unknown cloud(s): {sorted(unknown_clouds)}")
    return tuple(
        FleetAnalyticsEndpoint(
            cloud=entry.cloud,
            status_url=overrides.get(entry.cloud, entry.status_url),
            reason=None if entry.cloud in overrides else entry.reason,
            note=entry.note,
        )
        for entry in ANALYTICS_FRESHNESS_FLEET
        if selected is None or entry.cloud in selected
    )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    registry = _resolved_registry(args)

    payloads: dict[str, dict[str, Any] | None] = {}
    lags: dict[str, Any] = {}
    for entry in registry:
        if not entry.checkable or entry.status_url is None:
            continue
        try:
            payload = fetch_status(entry.status_url)
        except Exception as exc:  # noqa: BLE001 - any failure to read is a failure
            print(f"{entry.cloud}: fetch failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            payloads[entry.cloud] = None
            continue
        payloads[entry.cloud] = payload
        section = payload.get(ANALYTICS_STATUS_KEY)
        lags[entry.cloud] = section.get(DRAIN_LAG_FIELD) if isinstance(section, dict) else None

    problems = evaluate_fleet(
        payloads,
        now=dt.datetime.now(dt.UTC),
        registry=registry,
        max_drain_lag_seconds=args.max_drain_lag_seconds,
        max_age_seconds=args.max_age_seconds,
        max_outbox_depth=args.max_outbox_depth,
    )

    summary = " ".join(f"{cloud}={lags.get(cloud)}" for cloud in sorted(lags)) or "no clouds read"
    if args.problems_file:
        with open(args.problems_file, "w", encoding="utf-8") as handle:
            handle.write("\n".join(problems) + ("\n" if problems else ""))
    if problems:
        print(f"fleet analytics freshness: FAIL drain_lag_seconds {summary}", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"fleet analytics freshness: OK drain_lag_seconds {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
