"""Which evidenced routes the unserved-route hold will actually hold.

One planner for both sides of the hold. The hourly price refresh
(scripts/pricing/unserved_route_holds.py) uses it to decide what to write into
the provider manifests. The route-health pass uses it on the deployed
manifests to decide which routes' pages a pending hold replaces, so it never
replaces a page with a hold that the caps would refuse.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from trusted_router.provider_manifest_policy import UNSERVED_ROUTE_HOLD_REASON

PACKAGED_MANIFEST_DIR = Path(__file__).parent / "data" / "provider_models"
MAX_ROUTES = 25
MAX_PROVIDER_FRACTION = 0.5

Route = tuple[str, str]


def _routable(row: dict[str, Any]) -> bool:
    return row.get("routable") is not False


def _held(row: Any) -> bool:
    return isinstance(row, dict) and row.get("routable_reason") == UNSERVED_ROUTE_HOLD_REASON


def plan_unserved_route_holds(
    evidence: set[Route] | None,
    manifests: Sequence[tuple[str, list[Any]]],
    *,
    manifest_stems: Iterable[str],
    extra_held: int = 0,
) -> tuple[list[tuple[int, dict[str, Any]]], list[str]]:
    """Return ([(manifest index, row to hold)], notes).

    ``manifests`` are (provider manifest stem, model rows) pairs the caller may
    change; ``manifest_stems`` names every manifest that exists, including
    ones the caller leaves untouched; ``extra_held`` counts holds in those.
    """
    notes: list[str] = []
    by_provider: dict[str, set[str]] = {}
    if evidence is None:
        notes.append("unserved-route evidence unavailable; holding nothing")
    elif len(evidence) > MAX_ROUTES:
        notes.append(f"{len(evidence)} unserved routes exceed the {MAX_ROUTES}-route cap; holding nothing")
    else:
        for provider, model in evidence:
            by_provider.setdefault(provider, set()).add(model)
        for provider in sorted(set(by_provider) - set(manifest_stems)):
            notes.append(f"{provider}: no provider manifest; not held")

    planned: list[tuple[int, dict[str, Any]]] = []
    for index, (stem, rows) in enumerate(manifests):
        wanted = by_provider.get(stem, set())
        if not wanted:
            continue
        routable_rows = {
            row["id"]: row for row in rows
            if isinstance(row, dict) and isinstance(row.get("id"), str) and _routable(row)
        }
        targets = sorted(wanted & set(routable_rows))
        already_held = {
            row.get("id") for row in rows
            if _held(row) and isinstance(row.get("id"), str)
        }
        for model in sorted(wanted - set(routable_rows) - already_held):
            notes.append(f"{stem}: {model} is not a routable manifest row; not held")
        # Rows this step already holds count on both sides, so the cap
        # cannot ratchet a provider down one partial run at a time.
        unserved = len(targets) + len(already_held)
        candidates = len(routable_rows) + len(already_held)
        if targets and (
            unserved > MAX_PROVIDER_FRACTION * candidates
            or len(targets) >= len(routable_rows)
        ):
            notes.append(
                f"{stem}: {unserved} of {candidates} rows unserved; "
                "provider-wide problem, holding none"
            )
            targets = []
        planned.extend((index, routable_rows[model]) for model in targets)

    # Held routes stop being probed and drop out of the evidence, so the
    # route cap must count the holds already in place: otherwise it bounds
    # only each hourly run and a rolling problem accumulates without limit.
    held_total = extra_held + sum(1 for _, rows in manifests for row in rows if _held(row))
    if planned and held_total + len(planned) > MAX_ROUTES:
        notes.append(
            f"{held_total} held + {len(planned)} new unserved routes exceed the "
            f"{MAX_ROUTES}-route cap; holding nothing new"
        )
        planned = []
    return planned, notes


def pending_unserved_route_holds(
    evidence: set[Route], *, manifest_dir: Path | None = None,
) -> set[Route]:
    """The evidenced routes the next refresh would hold, judged on the
    deployed manifests. Expired holds count as still held here (the refresh
    lifts them first), which can only make this stricter than the refresh."""
    manifests: list[tuple[str, list[Any]]] = []
    for path in sorted((manifest_dir or PACKAGED_MANIFEST_DIR).glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(raw, dict) and isinstance(raw.get("models"), list):
            manifests.append((path.stem, raw["models"]))
    planned, _notes = plan_unserved_route_holds(
        evidence, manifests, manifest_stems=[stem for stem, _ in manifests],
    )
    return {(manifests[index][0], str(row["id"])) for index, row in planned}
