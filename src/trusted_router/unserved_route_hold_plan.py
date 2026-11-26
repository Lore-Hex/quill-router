"""Which evidenced routes the unserved-route hold will actually hold.

One planner for both sides of the hold. The hourly price refresh
(scripts/pricing/unserved_route_holds.py) uses it to decide what to write into
the provider manifests. The route-health pass uses it on the deployed
manifests to decide which routes' pages a pending hold replaces, so it never
replaces a page with a hold that the caps would refuse.

Repeat holds escalate. A route that qualifies again within REQUALIFY_WINDOW of
its previous hold lifting is held twice as long as last time (24h, 48h, 96h,
then MAX_HOLD_HOURS), and the route-health alert asks a human to consider a
permanent operator hold. The hold count and the lift time stay on the manifest
row so they survive refreshes; a route that stays out of the hold for the
whole window starts over at 24h.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from trusted_router.provider_lifecycle import provider_model_retired
from trusted_router.provider_manifest_policy import (
    UNSERVED_ROUTE_HELD_AT_FIELD,
    UNSERVED_ROUTE_HOLD_COUNT_FIELD,
    UNSERVED_ROUTE_HOLD_REASON,
    UNSERVED_ROUTE_LIFTED_AT_FIELD,
    provider_manifest_row_price_is_valid,
)

PACKAGED_MANIFEST_DIR = Path(__file__).parent / "data" / "provider_models"
MAX_ROUTES = 25
MAX_PROVIDER_FRACTION = 0.5
HOLD_HOURS = 24
# A route that re-qualifies within this long after its hold lifted is the same
# problem again, so its next hold doubles, up to MAX_HOLD_HOURS. The window
# must cover a lift reaching production (a refresh plus a snapshot rollout,
# see route_health.ROUTE_HOLD_APPLY_DEADLINE) plus the six-hour outage rule
# and its re-hold; a test pins that.
REQUALIFY_WINDOW = timedelta(hours=24)
MAX_HOLD_HOURS = 7 * 24
# Hold stamps and evidence timestamps tolerate this much clock skew.
MAX_CLOCK_SKEW = timedelta(minutes=5)

Route = tuple[str, str]


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def _hold_count(row: Any) -> int:
    """The row's hold count; 1 for a missing or malformed one."""
    count = row.get(UNSERVED_ROUTE_HOLD_COUNT_FIELD) if isinstance(row, dict) else None
    return count if isinstance(count, int) and not isinstance(count, bool) and count >= 1 else 1


def unserved_hold_hours(count: int) -> int:
    """Length of the count-th consecutive hold: 24h doubling, capped."""
    if count <= 1:
        return HOLD_HOURS
    return min(HOLD_HOURS << min(count - 1, 16), MAX_HOLD_HOURS)


def next_unserved_hold_count(row: Any, *, now: datetime) -> int:
    """The count a hold placed on this row now would carry.

    One more than the last hold when that hold lifted within REQUALIFY_WINDOW;
    otherwise 1. A missing, malformed or future-dated lift stamp starts over,
    so a bad stamp can only shorten a hold, never lengthen it.
    """
    if not isinstance(row, dict) or UNSERVED_ROUTE_HOLD_COUNT_FIELD not in row:
        return 1
    lifted_at = _parse_time(row.get(UNSERVED_ROUTE_LIFTED_AT_FIELD))
    if lifted_at is None or not -MAX_CLOCK_SKEW <= now - lifted_at <= REQUALIFY_WINDOW:
        return 1
    return _hold_count(row) + 1


def _forget_stale_hold_history(row: dict[str, Any], now: datetime) -> None:
    """Clear the repeat-hold record of a row that stayed out of the hold for
    the whole window (or whose record cannot be read)."""
    if UNSERVED_ROUTE_HOLD_COUNT_FIELD not in row and UNSERVED_ROUTE_LIFTED_AT_FIELD not in row:
        return
    if next_unserved_hold_count(row, now=now) == 1:
        row.pop(UNSERVED_ROUTE_HOLD_COUNT_FIELD, None)
        row.pop(UNSERVED_ROUTE_LIFTED_AT_FIELD, None)


def lift_expired_unserved_holds(rows: Iterable[Any], *, now: datetime) -> list[str]:
    """Lift every expired hold in place; return the lifted model ids.

    A lifted row keeps its hold count and records when it lifted, so a quick
    re-qualification escalates the next hold. The record of a row that is not
    held is cleared once the re-qualify window has passed.
    """
    lifted = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("routable_reason") != UNSERVED_ROUTE_HOLD_REASON:
            _forget_stale_hold_history(row, now)
            continue
        # A missing, malformed or future-dated stamp cannot prove the hold is
        # young; lift it rather than risk a hold that never expires.
        if _hold_is_live(row, now):
            continue
        row.pop(UNSERVED_ROUTE_HELD_AT_FIELD, None)
        row[UNSERVED_ROUTE_HOLD_COUNT_FIELD] = _hold_count(row)
        row[UNSERVED_ROUTE_LIFTED_AT_FIELD] = _iso(now)
        # While held, discovery could not record its own state (the hold
        # outranks it). Never lift into a delisted or unpriced routable row:
        # one such row expires the provider's whole manifest.
        # Judge the price as the row would be published once lifted: some
        # validators accept fields only on a row that is not routable.
        as_lifted = {key: value for key, value in row.items() if key not in {"routable", "routable_reason"}}
        if row.get("missing_since"):
            row["routable"] = False
            row["routable_reason"] = "delisted-upstream"
        elif not provider_manifest_row_price_is_valid(as_lifted):
            row["routable"] = False
            row["routable_reason"] = "price-unavailable"
        else:
            row.pop("routable", None)
            row.pop("routable_reason", None)
        lifted.append(str(row.get("id")))
    return lifted


def _routable(row: dict[str, Any]) -> bool:
    return row.get("routable") is not False


def _live(stem: str, row: dict[str, Any], now: datetime) -> bool:
    """A row the catalog serves at ``now``.

    A scheduled retirement (provider_lifecycle) stops a route without touching
    its manifest row, which stays marked routable. Such a row is not a route
    this step can hold, and it must not count as one the provider still
    serves: a provider whose other rows have all retired would otherwise have
    its last live route held, and the half-of-a-provider limit would be
    measured against rows that serve nothing.
    """
    upstream = row.get("upstream_id")
    return _routable(row) and not provider_model_retired(
        stem, row["id"], upstream if isinstance(upstream, str) else None, at=now,
    )


def live_route_ids(stem: str, rows: Iterable[Any], *, now: datetime) -> list[str]:
    """The provider's routes at ``now``, in manifest order: the rows the
    planner may hold and counts as served."""
    return list(dict.fromkeys(
        row["id"] for row in rows
        if isinstance(row, dict) and isinstance(row.get("id"), str) and _live(stem, row, now)
    ))


def _held(row: Any) -> bool:
    return isinstance(row, dict) and row.get("routable_reason") == UNSERVED_ROUTE_HOLD_REASON


def plan_unserved_route_holds(
    evidence: set[Route] | None,
    manifests: Sequence[tuple[str, list[Any]]],
    *,
    manifest_stems: Iterable[str],
    extra_held: int = 0,
    now: datetime | None = None,
) -> tuple[list[tuple[int, dict[str, Any]]], list[str]]:
    """Return ([(manifest index, row to hold)], notes).

    ``manifests`` are (provider manifest stem, model rows) pairs the caller may
    change; ``manifest_stems`` names every manifest that exists, including
    ones the caller leaves untouched; ``extra_held`` counts holds in those.
    ``now`` is the time the rows are judged at: a row past its scheduled
    retirement is not a route (see _live).
    """
    now = now or datetime.now(UTC)
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
            if isinstance(row, dict) and isinstance(row.get("id"), str) and _live(stem, row, now)
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


def _hold_is_live(row: Any, now: datetime) -> bool:
    if not _held(row):
        return False
    held_at = _parse_time(row.get(UNSERVED_ROUTE_HELD_AT_FIELD))
    hours = unserved_hold_hours(_hold_count(row))
    return held_at is not None and -MAX_CLOCK_SKEW <= now - held_at < timedelta(hours=hours)


def _manifest_rows(manifest_dir: Path | None) -> list[tuple[str, list[Any]]]:
    manifests: list[tuple[str, list[Any]]] = []
    for path in sorted((manifest_dir or PACKAGED_MANIFEST_DIR).glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(raw, dict) and isinstance(raw.get("models"), list):
            manifests.append((path.stem, raw["models"]))
    return manifests


def applied_unserved_route_holds(
    *, manifest_dir: Path | None = None, now: datetime | None = None,
) -> set[Route]:
    """The routes the deployed manifests hold now: an unserved-route hold
    whose stamp has not expired (the same test lift_expired_unserved_holds
    applies, so an expired hold is not counted as live)."""
    now = now or datetime.now(UTC)
    return {
        (stem, row["id"])
        for stem, rows in _manifest_rows(manifest_dir)
        for row in rows
        if _hold_is_live(row, now) and isinstance(row.get("id"), str)
    }


def pending_unserved_route_holds(
    evidence: set[Route], *, manifest_dir: Path | None = None, now: datetime | None = None,
) -> set[Route]:
    """The evidenced routes the next refresh would hold, judged on copies of
    the deployed manifests after the same expiry lift the refresh applies
    first. Quarantine state is not known here."""
    return set(pending_unserved_route_hold_counts(evidence, manifest_dir=manifest_dir, now=now))


def pending_unserved_route_hold_counts(
    evidence: set[Route], *, manifest_dir: Path | None = None, now: datetime | None = None,
) -> dict[Route, int]:
    """pending_unserved_route_holds, with the hold count each would carry
    (above 1 for a repeat hold, see next_unserved_hold_count)."""
    now = now or datetime.now(UTC)
    manifests: list[tuple[str, list[Any]]] = []
    for stem, raw_rows in _manifest_rows(manifest_dir):
        rows = copy.deepcopy(raw_rows)
        lift_expired_unserved_holds(rows, now=now)
        manifests.append((stem, rows))
    planned, _notes = plan_unserved_route_holds(
        evidence, manifests, manifest_stems=[stem for stem, _ in manifests], now=now,
    )
    return {
        (manifests[index][0], str(row["id"])): next_unserved_hold_count(row, now=now)
        for index, row in planned
    }
