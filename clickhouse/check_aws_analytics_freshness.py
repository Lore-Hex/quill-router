"""AWS-EU-only entrypoint. The real check is the FLEET check.

This module used to hold the whole thing, pointed at one hardcoded URL. That
was the outage repeating itself one layer up: the AWS-EU drain went fifteen
days undetected because every signal for it came from the missing drain, and a
monitor that reads one cloud is green for the same reason -- it is green about
the cloud somebody happened to point it at, and GCP was healthy the whole time.

So the logic moved to :mod:`clickhouse.check_fleet_analytics_freshness`, which
iterates
:data:`trusted_router.operational_analytics_fleet.ANALYTICS_FRESHNESS_FLEET`
and fails if ANY cloud is missing the section, unavailable, stale, or over the
drain-lag bound.

What is left here is an alias, kept because it is worth being able to ask about
one cloud during an incident:

    python3 -m clickhouse.check_aws_analytics_freshness

is exactly

    python3 -m clickhouse.check_fleet_analytics_freshness --cloud aws

:func:`evaluate` is re-exported unchanged; it is a pure function over one
cloud's payload and several tests read it directly.
"""

from __future__ import annotations

import argparse

from clickhouse.check_fleet_analytics_freshness import (
    ANALYTICS_FRESHNESS_FLEET,
    DEFAULT_STATUS_URL,
    evaluate,
    fetch_status,
)
from clickhouse.check_fleet_analytics_freshness import (
    main as _fleet_main,
)

__all__ = ["DEFAULT_STATUS_URL", "evaluate", "fetch_status", "main"]

AWS_CLOUD = "aws"


def _read_argv(args: list[str]) -> tuple[argparse.Namespace, list[str]]:
    """Read this alias's two options THE WAY ARGPARSE WILL, plus everything else.

    Spelling out the guard by hand is what kept getting this wrong. `--cloud`
    and `--cloud=gcp` were covered, and `--clo=gcp` was not: argparse accepts
    unambiguous prefixes, so an abbreviation walked straight past a check that
    compared strings and was then APPENDED after this function's own
    `--cloud aws`, silently widening an AWS-only entrypoint into a two-cloud
    one whose name says otherwise.

    So the reading is delegated to argparse itself, on a throwaway parser
    holding exactly the two options this alias cares about. Whatever argparse
    would bind downstream, it binds here -- every spelling, including ones
    nobody thought of -- and unrecognised arguments pass through untouched to
    the real parser, which owns their meaning and their error messages.
    """
    probe = argparse.ArgumentParser(add_help=False)
    probe.add_argument("--cloud", action="append", default=None)
    probe.add_argument("--status-url", action="append", default=None)
    return probe.parse_known_args(args)


def main(argv: list[str] | None = None) -> int:
    """Run the fleet check restricted to AWS, preserving this CLI's old shape.

    The one translation: this entrypoint's ``--status-url`` took a bare URL,
    while the fleet checker's takes ``CLOUD=URL``. A bare URL is rewritten to
    ``aws=<url>`` so an operator's muscle memory keeps working; anything that
    already names a known cloud is passed through.
    """
    selected, rest = _read_argv(list(argv or []))
    if selected.cloud:
        raise SystemExit(
            "check_aws_analytics_freshness is the AWS-only alias; to select "
            "clouds use `python3 -m clickhouse.check_fleet_analytics_freshness "
            "--cloud <name>`"
        )

    known_clouds = {entry.cloud for entry in ANALYTICS_FRESHNESS_FLEET}
    translated: list[str] = []
    for raw in selected.status_url or []:
        head, sep, _ = raw.partition("=")
        # `CLOUD=URL` only when the head really names a cloud. A bare URL that
        # happens to carry a query string (`...?probe=1`) contains an `=` too,
        # and treating that as a cloud name produced a baffling
        # "--status-url names unknown cloud(s)" instead of checking AWS.
        translated += ["--status-url", raw if sep and head in known_clouds else f"{AWS_CLOUD}={raw}"]
    return _fleet_main(["--cloud", AWS_CLOUD, *translated, *rest])


if __name__ == "__main__":
    raise SystemExit(main())
