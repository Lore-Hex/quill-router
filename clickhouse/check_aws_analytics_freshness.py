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

from clickhouse.check_fleet_analytics_freshness import (
    DEFAULT_STATUS_URL,
    evaluate,
    fetch_status,
)
from clickhouse.check_fleet_analytics_freshness import (
    main as _fleet_main,
)

__all__ = ["DEFAULT_STATUS_URL", "evaluate", "fetch_status", "main"]

AWS_CLOUD = "aws"


def main(argv: list[str] | None = None) -> int:
    """Run the fleet check restricted to AWS, preserving this CLI's old shape.

    The one translation: this entrypoint's ``--status-url`` took a bare URL,
    while the fleet checker's takes ``CLOUD=URL``. A bare URL is rewritten to
    ``aws=<url>`` so an operator's muscle memory keeps working; anything that
    already names a cloud is passed through.
    """
    args = list(argv or [])
    if "--cloud" in args:
        raise SystemExit(
            "check_aws_analytics_freshness is the AWS-only alias; to select "
            "clouds use `python3 -m clickhouse.check_fleet_analytics_freshness "
            "--cloud <name>`"
        )
    translated: list[str] = []
    expect_url = False
    for arg in args:
        if expect_url:
            translated.append(arg if "=" in arg else f"{AWS_CLOUD}={arg}")
            expect_url = False
            continue
        if arg == "--status-url":
            expect_url = True
        elif arg.startswith("--status-url="):
            value = arg.split("=", 1)[1]
            translated.append("--status-url")
            translated.append(value if value.startswith(f"{AWS_CLOUD}=") else f"{AWS_CLOUD}={value}")
            continue
        translated.append(arg)
    return _fleet_main(["--cloud", AWS_CLOUD, *translated])


if __name__ == "__main__":
    raise SystemExit(main())
