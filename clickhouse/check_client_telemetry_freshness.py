"""Check, from outside the node, that client-observed telemetry is still flowing.

The pipeline SDK -> POST /v1/client-events -> operational outbox -> ClickHouse
ingester -> rollup timer -> public snapshot -> /status.json has five moving
parts and a dark pipeline looks exactly like "no failures". The in-process
watch (synthetic/client_watch.py) pages when it can see staleness; this job
is the check that survives the watch itself being down: it reads the PUBLIC
/status.json the way a customer would and fails when

  * the `client_observed` section is missing or `available` is false
    (no_data / stale),
  * the snapshot the section was built from is older than --max-age-seconds,
  * the canary (the synthetic monitor's own batch, posted every pass) has not
    been seen within --max-canary-age-seconds, or fewer than --min-canary-24h
    canaries landed in the last 24 h.

Pure function `evaluate(payload, ...)` returns the problems; `main()` fetches
and reports, writing them to --problems-file for the workflow's issue body.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import urllib.request
from typing import Any

DEFAULT_STATUS_URL = "https://trustedrouter.com/status.json"


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


# The canary began posting at 2026-08-17 04:49 UTC (the first synthetic pass
# after the beacon flag went live). A 24 h count gate cannot pass before this
# instant; it is a start-up fact, not a threshold to tune, and it expires on
# its own.
CANARY_COUNT_GATE_FROM = dt.datetime(2026, 8, 18, 5, 0, tzinfo=dt.UTC)


def evaluate(
    payload: dict[str, Any],
    *,
    now: dt.datetime,
    max_age_seconds: int,
    max_canary_age_seconds: int,
    min_canary_24h: int,
    canary_count_from: dt.datetime = CANARY_COUNT_GATE_FROM,
) -> list[str]:
    """Return human-readable problems; an empty list means fresh."""
    section = payload.get("client_observed")
    if not isinstance(section, dict):
        return ["/status.json has no client_observed section"]
    if not section.get("available"):
        # The status page says no_data (no snapshot at all) or stale (snapshot
        # older than 15 min); either way the pipeline is not observably alive.
        return [f"client_observed unavailable: reason={section.get('reason')!r}"]
    problems: list[str] = []
    generated_at = _parse_time(section.get("generated_at"))
    if generated_at is None:
        problems.append("client_observed.generated_at missing or unparseable")
    else:
        age = int((now - generated_at).total_seconds())
        if age > max_age_seconds:
            problems.append(f"client_reliability snapshot is {age}s old (> {max_age_seconds}s)")
    canary = section.get("canary") if isinstance(section.get("canary"), dict) else {}
    canary_age = _int(canary.get("last_seen_age_seconds"))
    if canary_age is None:
        problems.append("canary never seen (last_seen_age_seconds is null)")
    elif canary_age > max_canary_age_seconds:
        problems.append(f"canary last seen {canary_age}s ago (> {max_canary_age_seconds}s)")
    canary_24h = _int(canary.get("last_24h_count")) or 0
    # Ramp-up: the 24 h count cannot be met until the canary has been posting
    # for 24 h (it started 2026-08-17 04:49 UTC, the first pass after
    # TR_CLIENT_EVENTS_ENABLED went live). Liveness is still enforced above by
    # last_seen_age_seconds, so an outage during the ramp-up is still caught;
    # only the count is deferred, and the guard expires by itself.
    if now >= canary_count_from and canary_24h < min_canary_24h:
        problems.append(f"only {canary_24h} canary batches in the last 24h (< {min_canary_24h})")
    return problems


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--status-url", default=DEFAULT_STATUS_URL)
    parser.add_argument("--max-age-seconds", type=int, default=3_600)
    parser.add_argument("--max-canary-age-seconds", type=int, default=3_600)
    parser.add_argument("--min-canary-24h", type=int, default=200)
    parser.add_argument("--problems-file", default=None)
    return parser.parse_args(argv)


def fetch_status(url: str) -> dict[str, Any]:
    request = urllib.request.Request(  # noqa: S310 - https URL from argv/default only
        url, headers={"user-agent": "trustedrouter-client-telemetry-freshness/1"}
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
        max_age_seconds=args.max_age_seconds,
        max_canary_age_seconds=args.max_canary_age_seconds,
        min_canary_24h=args.min_canary_24h,
    )
    section = payload.get("client_observed") if isinstance(payload, dict) else None
    state = section.get("state") if isinstance(section, dict) else None
    if args.problems_file:
        with open(args.problems_file, "w", encoding="utf-8") as handle:
            handle.write("\n".join(problems) + ("\n" if problems else ""))
    if problems:
        print(f"client telemetry freshness: FAIL state={state}", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"client telemetry freshness: OK state={state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
