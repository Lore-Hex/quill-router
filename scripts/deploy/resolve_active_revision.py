#!/usr/bin/env python3
"""Resolve the sole serving revision from a Cloud Run service document."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from typing import Any


def resolve_active_revision(service: Mapping[str, Any]) -> str:
    """Return the exact revision receiving 100% of untagged service traffic."""
    status = service.get("status")
    if not isinstance(status, Mapping):
        raise ValueError("service status is not an object")
    raw_traffic = status.get("traffic", [])
    if not isinstance(raw_traffic, list):
        raise ValueError("service status traffic is not a list")

    active: list[tuple[Mapping[str, Any], int]] = []
    for item in raw_traffic:
        if not isinstance(item, Mapping):
            raise ValueError("service status traffic contains a non-object entry")
        try:
            percent = int(item.get("percent") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("service status traffic has an invalid percent") from exc
        if percent > 0:
            active.append((item, percent))

    if len(active) != 1 or active[0][1] != 100:
        raise ValueError("expected exactly one 100%-traffic revision")

    revision = active[0][0].get("revisionName")
    if not isinstance(revision, str) or not revision:
        raise ValueError("100%-traffic entry has no revisionName")
    return revision


def main() -> int:
    try:
        service = json.load(sys.stdin)
        if not isinstance(service, Mapping):
            raise ValueError("Cloud Run service document is not an object")
        print(resolve_active_revision(service))
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"cannot resolve active Cloud Run revision: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
