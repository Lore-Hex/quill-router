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

UNCHECKED IS A THIRD OUTCOME, AND IT IS PRINTED
    Two registry states are neither pass nor fail: a cloud with no public
    status page (``reason=``), and a cloud that legitimately runs no
    operational-analytics outbox (``expects_outbox=False`` -- Azure today).
    Failing those every day would be crying wolf about something nobody can
    fix, and the repo has already learned what that costs: see
    ``CANARY_COUNT_GATE_FROM`` in
    :mod:`clickhouse.check_client_telemetry_freshness`.  Silently skipping them
    would be the outage itself -- a cloud that renders as covered and was never
    measured.  So they are reported as EXPLICITLY UNCHECKED, on stdout, on
    every run, in the problems file, and counted in the summary line.  A cloud
    declared outbox-free that starts publishing a real lag is a FAILURE, not an
    unchecked: that is the day it needs watching.

Pure function :func:`evaluate` returns one cloud's problems;
:func:`evaluate_fleet` returns a :class:`FleetResult` of problems AND
unchecked notes; :func:`main` fetches every registry entry and reports,
writing both to ``--problems-file`` for the workflow's issue body.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import urllib.request
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from trusted_router.operational_analytics_fleet import (
    ANALYTICS_FRESHNESS_FLEET,
    FleetAnalyticsEndpoint,
    registry_defects,
)
from trusted_router.operational_analytics_freshness import (
    ANALYTICS_STATUS_KEY,
    AVAILABLE_FIELD,
    BACKEND_FIELD,
    DEFAULT_MAX_DRAIN_LAG_SECONDS,
    DRAIN_LAG_FIELD,
    GENERATED_AT_FIELD,
    OUTBOX_DEPTH_FIELD,
    REASON_FIELD,
    REASON_NO_DATA,
    REASON_NOT_CONFIGURED,
    REASON_UNREACHABLE,
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


#: What each published reason MEANS to an operator, and what to do about it.
#: One shared sentence for all three was worse than nothing: it told the reader
#: that a `not_configured` cloud "could not read the outbox", which is the
#: opposite of the truth -- there is no outbox, so nothing tried to read one,
#: and an operator sent to check the database would find a healthy database.
_UNAVAILABLE_EXPLANATION: dict[str, str] = {
    REASON_UNREACHABLE: (
        "the control plane could not read the outbox -- the database is "
        "unreachable, erroring, or slower than the read's own timeout. The "
        "drain may well be fine; what is broken is the plane's DB access."
    ),
    REASON_NO_DATA: (
        "the control plane read the outbox and got nothing it could use. This "
        "is not an empty outbox (that publishes available with lag 0.0); it is "
        "a read that returned no usable answer."
    ),
    REASON_NOT_CONFIGURED: (
        "this deployment has NO operational-analytics outbox at all "
        "(TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED is off, or its backend has "
        "no outbox). Nothing is being enqueued, so nothing is being drained: "
        "the pipeline is ABSENT, not behind, and the database is not the "
        "problem. Either turn that flag on for a cloud that should run it, or "
        "set expects_outbox=False for it in ANALYTICS_FRESHNESS_FLEET so this "
        "job reports it as explicitly unchecked instead of failing daily."
    ),
}


def unavailable_reason(payload: dict[str, Any]) -> str | None:
    """The reason a payload's section gives, or ``None`` if it is not unavailable.

    Shared with :func:`evaluate_fleet` so that "is this the declared-absent
    case?" and "what do we say about it?" cannot answer differently.
    """
    section = payload.get(ANALYTICS_STATUS_KEY)
    if not isinstance(section, dict) or section.get(AVAILABLE_FIELD):
        return None
    reason = section.get(REASON_FIELD)
    return reason if isinstance(reason, str) else ""


def evaluate(
    payload: dict[str, Any],
    *,
    now: dt.datetime,
    max_drain_lag_seconds: float = DEFAULT_MAX_DRAIN_LAG_SECONDS,
    max_age_seconds: int = 3_600,
    max_outbox_depth: int | None = None,
    expected_backend: str | None = None,
    expects_outbox: bool = True,
) -> list[str]:
    """Return human-readable problems; an empty list means the drain is healthy.

    ``expects_outbox=False`` inverts one branch and one only: a
    ``not_configured`` section becomes the EXPECTED answer (the caller reports
    it as explicitly unchecked), and an AVAILABLE section becomes a failure,
    because a cloud the registry says has no pipeline just grew one and nobody
    is watching it.
    """
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
        reason = section.get(REASON_FIELD)
        if not expects_outbox and reason == REASON_NOT_CONFIGURED:
            # The registry declared this absence; the caller reports it as
            # explicitly unchecked rather than as health or as a failure.
            return []
        explanation = _UNAVAILABLE_EXPLANATION.get(
            reason if isinstance(reason, str) else "",
            "the control plane published a reason this checker does not know; "
            "the publisher narrows reasons to a fixed set, so an unknown value "
            "means the two have drifted",
        )
        return [f"{ANALYTICS_STATUS_KEY} unavailable: reason={reason!r} -- {explanation}"]

    problems: list[str] = []

    if not expects_outbox:
        problems.append(
            "publishes a live drain lag, but ANALYTICS_FRESHNESS_FLEET says this "
            "cloud runs no operational-analytics outbox (expects_outbox=False). "
            "The pipeline was turned on and nothing is watching it -- which is "
            "the whole shape of the AWS-EU outage. Set expects_outbox=True so "
            "this cloud's drain lag is actually checked."
        )

    # Which cloud actually answered. Two registry entries pointed at one
    # control plane would otherwise both look checked; the plane can only
    # answer for its own storage backend, so a mismatch is proof the URL is
    # aimed at somebody else's deployment. (It cannot separate aws from azure,
    # which are both Postgres -- registry_defects rejects duplicate URLs
    # offline for exactly that gap.)
    backend = section.get(BACKEND_FIELD)
    if expected_backend is not None and backend != expected_backend:
        problems.append(
            f"answered by {BACKEND_FIELD}={backend!r}, but this cloud runs "
            f"{expected_backend!r}. The registry's status_url is pointed at another "
            "cloud's control plane, so this run measured a cloud it already measured "
            "and never measured this one."
        )

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


@dataclass(frozen=True)
class FleetResult:
    """A fleet run's outcome, with UNCHECKED as a first-class third value.

    Returning a bare ``list[str]`` of problems made "unchecked" express itself
    either as a failure or as nothing at all, and both are wrong: the first
    cries wolf daily about a cloud nobody can fix, the second is the outage --
    a cloud that renders as covered and was never read.  A caller has to
    destructure this to get the problems, so it cannot avoid seeing the
    unchecked list on its way past.
    """

    problems: list[str] = field(default_factory=list)
    unchecked: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def evaluate_fleet(
    payloads: dict[str, dict[str, Any] | None],
    *,
    now: dt.datetime,
    registry: Sequence[FleetAnalyticsEndpoint] = ANALYTICS_FRESHNESS_FLEET,
    selected: Iterable[str] | None = None,
    deployed: Iterable[str] | None = None,
    max_drain_lag_seconds: float = DEFAULT_MAX_DRAIN_LAG_SECONDS,
    max_age_seconds: int = 3_600,
    max_outbox_depth: int | None = None,
) -> FleetResult:
    """Problems and unchecked notes, each prefixed with the cloud it is about.

    ``payloads`` maps cloud name to the fetched ``/status.json`` body, or to
    ``None`` when the fetch itself failed.  Every registry entry must appear:
    a cloud absent from ``payloads`` is reported rather than skipped, because
    "nobody fetched it" and "it is healthy" must not render the same.

    ``registry`` is the registry that gets VALIDATED, not merely the one that
    gets fetched -- an earlier revision validated the module-level table no
    matter what it was handed, so a caller passing a synthetic or edited
    registry had it silently unexamined while the failure message spoke about a
    table that was not in play.

    ``selected`` narrows which entries are FETCHED (the ``--cloud`` debugging
    slice) and deliberately does not narrow what is validated: a run restricted
    to one cloud must still notice that a fourth cloud has no entry, or the
    slice becomes a way to make the coverage check disappear.
    """
    problems: list[str] = []
    unchecked: list[str] = []
    for defect in registry_defects(deployed, registry=registry):
        problems.append(f"registry: {defect}")

    wanted = None if selected is None else set(selected)
    for entry in registry:
        if wanted is not None and entry.cloud not in wanted:
            unchecked.append(
                f"{entry.cloud}: NOT CHECKED -- excluded by --cloud. The scheduled "
                "job never passes --cloud; this run is a manual slice."
            )
            continue
        if not entry.checkable:
            unchecked.append(
                f"{entry.cloud}: NOT CHECKED -- no public status page "
                f"(reason={entry.reason!r}). Its drain lag is not observable from "
                "CI; check it by hand, or give the deployment a public "
                "control-plane status page and delete the reason."
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
        if not entry.expects_outbox and unavailable_reason(payload) == REASON_NOT_CONFIGURED:
            # The registry declared this cloud runs no pipeline and the plane
            # agrees. Reported every run so "we do not watch Azure" stays a
            # visible fact rather than an absence in a list. `evaluate` returns
            # no problems for this combination; the note is the whole answer.
            unchecked.append(
                f"{entry.cloud}: NOT CHECKED -- runs no operational-analytics outbox "
                "(expects_outbox=False, and the plane confirms not_configured). "
                "There is no drain here to be missing. This line becomes a FAILURE "
                "the day it publishes a real lag."
            )
            continue
        for problem in evaluate(
            payload,
            now=now,
            max_drain_lag_seconds=max_drain_lag_seconds,
            max_age_seconds=max_age_seconds,
            max_outbox_depth=max_outbox_depth,
            expected_backend=entry.expected_backend,
            expects_outbox=entry.expects_outbox,
        ):
            problems.append(f"{entry.cloud}: {problem}")
    return FleetResult(problems=problems, unchecked=unchecked)


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
    """The WHOLE registry with any --status-url overrides applied.

    Never filtered by ``--cloud``. Filtering here is what would let a run
    restricted to one cloud stop validating the coverage of the others;
    :func:`evaluate_fleet` takes the selection separately and applies it only
    to fetching.
    """
    overrides: dict[str, str] = {}
    for raw in args.status_url or []:
        cloud, _, url = str(raw).partition("=")
        if not cloud or not url:
            raise SystemExit(f"--status-url expects CLOUD=URL, got {raw!r}")
        overrides[cloud.strip()] = url.strip()
    unknown = set(overrides) - {entry.cloud for entry in ANALYTICS_FRESHNESS_FLEET}
    if unknown:
        raise SystemExit(f"--status-url names unknown cloud(s): {sorted(unknown)}")
    return tuple(
        FleetAnalyticsEndpoint(
            cloud=entry.cloud,
            status_url=overrides.get(entry.cloud, entry.status_url),
            reason=None if entry.cloud in overrides else entry.reason,
            expected_backend=entry.expected_backend,
            expects_outbox=entry.expects_outbox,
            note=entry.note,
        )
        for entry in ANALYTICS_FRESHNESS_FLEET
    )


def _selected_clouds(args: argparse.Namespace) -> set[str] | None:
    selected = set(args.cloud or []) or None
    if selected:
        unknown_clouds = selected - {entry.cloud for entry in ANALYTICS_FRESHNESS_FLEET}
        if unknown_clouds:
            raise SystemExit(f"--cloud names unknown cloud(s): {sorted(unknown_clouds)}")
    return selected


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    registry = _resolved_registry(args)
    selected = _selected_clouds(args)

    payloads: dict[str, dict[str, Any] | None] = {}
    lags: dict[str, Any] = {}
    for entry in registry:
        if selected is not None and entry.cloud not in selected:
            continue
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

    result = evaluate_fleet(
        payloads,
        now=dt.datetime.now(dt.UTC),
        registry=registry,
        selected=selected,
        max_drain_lag_seconds=args.max_drain_lag_seconds,
        max_age_seconds=args.max_age_seconds,
        max_outbox_depth=args.max_outbox_depth,
    )

    summary = " ".join(f"{cloud}={lags.get(cloud)}" for cloud in sorted(lags)) or "no clouds read"
    if result.unchecked:
        summary = f"{summary} unchecked={len(result.unchecked)}"
    if args.problems_file:
        # The unchecked lines go in the file too. They are what the issue body
        # needs to say "these clouds were NOT part of this answer"; an issue
        # that lists only failures reads as though everything else passed.
        lines = [*result.problems, *(f"(unchecked) {note}" for note in result.unchecked)]
        with open(args.problems_file, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + ("\n" if lines else ""))
    # Printed before the verdict, and on both paths: an unchecked cloud is a
    # standing fact about this job's coverage, not an artifact of a bad run.
    for note in result.unchecked:
        print(f"  ~ {note}")
    if result.problems:
        print(f"fleet analytics freshness: FAIL drain_lag_seconds {summary}", file=sys.stderr)
        for problem in result.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"fleet analytics freshness: OK drain_lag_seconds {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
