#!/usr/bin/env python3
"""Would the staged rollout accept this region's leftover drain tag?

staged_traffic.sh keeps the outgoing revision warm behind the `staged-drain`
tag and removes the tag when the drain completes. A drain that is interrupted
keeps the tag on purpose, and prepare_revision_drain then refuses the next
rollout of that region, because the tag protects a revision other than the one
now serving. That refusal is correct, but it arrives late: the primary region
has already taken the new revision by the time a secondary refuses.

This asks the same question from a service document, before anything rolls
out. It reads; it never changes a tag.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from typing import Any

from resolve_active_revision import resolve_active_revision

DRAIN_TAG = "staged-drain"


def leftover_drain_revision(service: Mapping[str, Any], tag: str = DRAIN_TAG) -> str | None:
    """Return the revision a leftover tag protects, or None when the rollout would accept the region.

    Raises ValueError when the document cannot answer: no single serving
    revision, or a tag that names more than one entry or no revision.
    """
    serving = resolve_active_revision(service)
    tagged = [entry for entry in service["status"].get("traffic", []) if entry.get("tag") == tag]
    if len(tagged) > 1:
        raise ValueError(f"tag {tag!r} resolves to more than one traffic entry")
    if not tagged:
        return None
    revision = tagged[0].get("revisionName")
    if not isinstance(revision, str) or not revision:
        raise ValueError(f"tag {tag!r} has an empty revisionName")
    # The same rule as prepare_revision_drain: a tag on the serving revision is
    # the one the rollout is about to set, so it is taken over, not refused.
    return None if revision == serving else revision


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--region", required=True)
    parser.add_argument("--service", required=True)
    parser.add_argument("--project", required=True)
    args = parser.parse_args(argv)
    try:
        service = json.load(sys.stdin)
        if not isinstance(service, Mapping):
            raise ValueError("Cloud Run service document is not an object")
        serving = resolve_active_revision(service)
        leftover = leftover_drain_revision(service)
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"::error::{args.region}: cannot tell whether a rollout would be accepted: {exc}")
        return 1
    if leftover is None:
        print(f"{args.region}: ready ({serving} serves, no leftover {DRAIN_TAG} tag)")
        return 0
    print(
        f"::error::{args.region}: {DRAIN_TAG} is still on {leftover}, but {serving} serves. "
        "An earlier rollout's drain was interrupted there, and the staged rollout refuses a region in "
        f"this state. Nothing has been rolled out by this run. Confirm {serving} is healthy at 100%, "
        "then remove the tag and rerun: "
        f"gcloud run services update-traffic {args.service} --region {args.region} "
        f"--project {args.project} --remove-tags {DRAIN_TAG}"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
