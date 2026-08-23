#!/usr/bin/env python3
"""Plan repair of failed Cloud Run revisions left in service traffic state."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from scripts.deploy.resolve_active_revision import resolve_active_revision


@dataclass(frozen=True)
class FailedRevisionRepair:
    """A safe rollback target and the failed revisions it makes removable."""

    active_revision: str
    failed_revisions: tuple[str, ...]


def _traffic_revision_names(service: Mapping[str, Any]) -> set[str]:
    names: set[str] = set()
    for parent_name in ("spec", "status"):
        parent = service.get(parent_name)
        if parent is None:
            continue
        if not isinstance(parent, Mapping):
            raise ValueError(f"service {parent_name} is not an object")
        traffic = parent.get("traffic", [])
        if not isinstance(traffic, list):
            raise ValueError(f"service {parent_name} traffic is not a list")
        for item in traffic:
            if not isinstance(item, Mapping):
                raise ValueError(f"service {parent_name} traffic contains a non-object entry")
            revision = item.get("revisionName")
            if revision is not None:
                if not isinstance(revision, str) or not revision:
                    raise ValueError(
                        f"service {parent_name} traffic has an invalid revisionName"
                    )
                names.add(revision)
    return names


def plan_failed_revision_repair(
    service: Mapping[str, Any], failed_revisions: Iterable[str]
) -> FailedRevisionRepair | None:
    """Return a rollback plan when failed revisions remain traffic-referenced.

    The sole effective 100% revision is the only safe automatic target. If the
    service is actively split, ambiguous, or malformed, resolution raises and
    the deploy remains fail-closed.
    """
    failed = {revision for revision in failed_revisions if revision}
    referenced = tuple(sorted(failed & _traffic_revision_names(service)))
    if not referenced:
        return None
    return FailedRevisionRepair(
        active_revision=resolve_active_revision(service),
        failed_revisions=referenced,
    )


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        service = json.load(sys.stdin)
        if not isinstance(service, Mapping):
            raise ValueError("Cloud Run service document is not an object")
        plan = plan_failed_revision_repair(service, args)
        if plan is not None:
            print(f"{plan.active_revision}\t{','.join(plan.failed_revisions)}")
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"cannot plan failed Cloud Run revision repair: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
