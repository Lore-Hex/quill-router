"""The hourly refresh's unserved-route hold, for scripts/tombstone_sweep.py.

The sweep itself is plain python3; this part runs under the worktree's own
interpreter because it calls the refresh's step. A hold is never typed out
here: rows are held by scripts.pricing.unserved_route_holds
.apply_unserved_route_holds, so each carries exactly what that step writes
today, and which rows may be held together is whatever that step's planner
accepts (never a provider's last routable row, never more than half of its
rows counting the ones it already holds).

One limit is lifted, and only when a run asks for more rows than it allows:
the step holds at most MAX_ROUTES rows in total, which would make a sweep of
every routable row one release-suite run per 25 rows. A run within that limit
goes through the step untouched.

Called with `plan` or `hold` and a JSON request on stdin ({"manifest_dir": ...,
and for a hold "routes": [[provider, model], ...]}); the answer is the last
line of stdout, as JSON.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.pricing import unserved_route_holds as step
from trusted_router import unserved_route_hold_plan as planner
from trusted_router.provider_manifest_policy import (
    UNSERVED_ROUTE_HELD_AT_FIELD,
    UNSERVED_ROUTE_HOLD_REASON,
)

Route = tuple[str, str]


@contextmanager
def _total_route_cap_lifted() -> Iterator[None]:
    cap = planner.MAX_ROUTES
    planner.MAX_ROUTES = sys.maxsize
    try:
        yield
    finally:
        planner.MAX_ROUTES = cap


def _manifests(manifest_dir: Path) -> list[tuple[str, list[Any]]]:
    manifests = []
    for path in sorted(manifest_dir.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and isinstance(raw.get("models"), list):
            manifests.append((path.stem, raw["models"]))
    return manifests


def _held(manifest_dir: Path) -> set[Route]:
    """Every row carrying the hold as the step writes it."""
    return {
        (provider, row["id"])
        for provider, rows in _manifests(manifest_dir)
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("id"), str)
        and row.get("routable") is False
        and row.get("routable_reason") == UNSERVED_ROUTE_HOLD_REASON
        and row.get(UNSERVED_ROUTE_HELD_AT_FIELD)
    }


def _most_held_at_once(provider: str, rows: list[Any], ids: list[str]) -> int:
    """How many of the provider's routable rows the step's planner will hold in
    one run. The planner is asked; its rule is not repeated here."""
    for count in range(len(ids) - 1, 0, -1):
        evidence = {(provider, model_id) for model_id in ids[:count]}
        planned, _notes = planner.plan_unserved_route_holds(
            evidence, [(provider, rows)], manifest_stems=[provider],
        )
        if len(planned) == count:
            return count
    return 0


def plan(manifest_dir: Path, *, now: datetime | None = None) -> dict[str, Any]:
    """Slices of routable rows that between them hold every row the step can.

    Each provider's routable rows are cut, in manifest order, into the fewest
    runs its planner accepts; slice k is every provider's k-th cut. A provider
    the step can hold nothing of (its last routable row) is listed apart. The
    rows are judged as the step sees them, after it lifts its expired holds.
    """
    now = now or datetime.now(UTC)
    slices: list[list[Route]] = []
    never: list[Route] = []
    with _total_route_cap_lifted():
        for provider, rows in _manifests(manifest_dir):
            planner.lift_expired_unserved_holds(rows, now=now)
            ids = list(dict.fromkeys(
                row["id"] for row in rows
                if isinstance(row, dict) and isinstance(row.get("id"), str)
                and row.get("routable") is not False
            ))
            size = _most_held_at_once(provider, rows, ids)
            if not size:
                never.extend((provider, model_id) for model_id in ids)
                continue
            for index, start in enumerate(range(0, len(ids), size)):
                if index == len(slices):
                    slices.append([])
                slices[index].extend((provider, model_id) for model_id in ids[start:start + size])
    return {"slices": slices, "never_held": never}


def hold(routes: list[Route], manifest_dir: Path, *, now: datetime | None = None) -> dict[str, Any]:
    """Hold these routes with the refresh's own step, or exit: a route the step
    will not hold was never tested, and must not be reported as tested."""
    wanted = {(provider, model_id) for provider, model_id in routes}
    before = _held(manifest_dir)
    if wanted & before:
        raise SystemExit(f"already held before this run: {sorted(wanted & before)}")
    notes = step.apply_unserved_route_holds(wanted, manifest_dir=manifest_dir, now=now)
    lifted = False
    if wanted and not wanted & _held(manifest_dir):
        # Nothing held: more rows than the step's total. Hold them anyway (see
        # the module docstring); every other limit still applies.
        with _total_route_cap_lifted():
            notes = step.apply_unserved_route_holds(wanted, manifest_dir=manifest_dir, now=now)
        lifted = True
    missing = wanted - _held(manifest_dir)
    if missing:
        raise SystemExit(f"the refresh's step would not hold {sorted(missing)}: {notes}")
    return {"held": sorted(wanted), "total_route_cap_lifted": lifted, "notes": notes}


def main() -> None:
    request = json.load(sys.stdin)
    manifest_dir = Path(request["manifest_dir"])
    if sys.argv[1:] == ["plan"]:
        result = plan(manifest_dir)
    elif sys.argv[1:] == ["hold"]:
        result = hold([(provider, model_id) for provider, model_id in request["routes"]], manifest_dir)
    else:
        raise SystemExit(__doc__)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
