"""Hold listed provider routes that the provider refuses to serve.

A provider can keep a model in its /models list while answering every request
for it with "model not found" or "not available". Discovery sees the listing,
so the route stays routable, and customer requests try it first and fail over.
The per-provider refresh canaries cannot catch this: they probe only new routes
and routes already held by a failed canary (``models_requiring_canary``), and
several providers have no inference credential in the refresh at all.

The control plane already observes the failure: configured-route probes
classify the responses as ``unsupported_route``. ``/route-health/unserved.json``
publishes the routes whose newest probes form a long model-not-served streak
(see ``trusted_router.synthetic.route_health.evaluate_unserved_routes``). This
step, run after the provider refresh, applies that evidence to the committed
manifests:

* each listed route that is routable in its provider's manifest is held with
  ``routable_reason: provider-route-unserved`` and ``unserved_held_at``;
* a hold expires after ``HOLD_HOURS``. The route becomes routable again, the
  configured-route probes resume on the real serving path, and if the provider
  still refuses it the next streak holds it again. A route that recovered
  stays lifted. Expiry runs even when evidence is unavailable, because lifting
  only restores what discovery published;
* when the evidence cannot be read, is malformed or stale, nothing is held;
* a provider is never held wholesale: a run that would leave more than half
  of a provider's rows held by this step (counting earlier holds), or would
  hold its last routable row, holds none of that provider's rows; a run
  listing more than ``MAX_ROUTES`` routes holds nothing, and no new hold is
  placed while it would leave more than ``MAX_ROUTES`` rows held in total;
* an expired hold on a row discovery has since delisted or left unpriced
  becomes ``delisted-upstream`` or ``price-unavailable``, never routable.

Holds use a reason the shared writers treat as an operator hold, so discovery
carries them forward until this step lifts them.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from trusted_router.provider_manifest_policy import (
    UNSERVED_ROUTE_HOLD_REASON,
    provider_manifest_row_price_is_valid,
)

SOURCE = "https://trustedrouter.com/route-health/unserved.json"
MANIFEST_DIR = Path(__file__).resolve().parents[2] / "src/trusted_router/data/provider_models"
HELD_AT_FIELD = "unserved_held_at"
HOLD_HOURS = 24
MAX_EVIDENCE_AGE = timedelta(hours=2)
# The server stamps generated_at when it builds; tolerate runner/server skew.
MAX_CLOCK_SKEW = timedelta(minutes=5)
MAX_ROUTES = 25
MAX_PROVIDER_FRACTION = 0.5

Route = tuple[str, str]


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_evidence(payload: Any, *, now: datetime) -> set[Route]:
    """Validate the published evidence. Raises ValueError on anything doubtful."""
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("unsupported unserved-route evidence")
    generated = _parse_time(payload.get("generated_at"))
    if generated is None or not -MAX_CLOCK_SKEW <= now - generated <= MAX_EVIDENCE_AGE:
        raise ValueError("unserved-route evidence is stale or future-dated")
    rows = payload.get("routes")
    if not isinstance(rows, list):
        raise ValueError("unserved-route evidence has no routes list")
    routes: set[Route] = set()
    for row in rows:
        provider = row.get("provider") if isinstance(row, dict) else None
        model = row.get("model") if isinstance(row, dict) else None
        if not isinstance(provider, str) or not provider or "/" in provider or not isinstance(model, str) or not model:
            raise ValueError("malformed unserved-route evidence row")
        routes.add((provider, model))
    return routes


def fetch_evidence(source: str) -> set[Route]:
    response = httpx.get(source, timeout=30.0, follow_redirects=False)
    response.raise_for_status()
    # Judge freshness after the response: a cache miss builds the evidence
    # while the request is in flight.
    return parse_evidence(response.json(), now=datetime.now(UTC))


def _routable(row: dict[str, Any]) -> bool:
    return row.get("routable") is not False


def _lift_expired(rows: Iterable[Any], *, now: datetime) -> list[str]:
    lifted = []
    for row in rows:
        if not isinstance(row, dict) or row.get("routable_reason") != UNSERVED_ROUTE_HOLD_REASON:
            continue
        held_at = _parse_time(row.get(HELD_AT_FIELD))
        # A missing, malformed or future-dated stamp cannot prove the hold is
        # young; lift it rather than risk a hold that never expires.
        if held_at is not None and -MAX_CLOCK_SKEW <= now - held_at < timedelta(hours=HOLD_HOURS):
            continue
        row.pop(HELD_AT_FIELD, None)
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


def _load(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) and isinstance(raw.get("models"), list) else None


def apply_unserved_route_holds(
    evidence: set[Route] | None,
    *,
    manifest_dir: Path = MANIFEST_DIR,
    now: datetime | None = None,
) -> list[str]:
    """Lift expired holds, then hold the evidenced routes. Returns notes."""
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
        for provider in sorted(set(by_provider) - {path.stem for path in manifest_dir.glob("*.json")}):
            notes.append(f"{provider}: no provider manifest; not held")

    manifests: list[tuple[Path, dict[str, Any], bool]] = []
    for path in sorted(manifest_dir.glob("*.json")):
        raw = _load(path)
        if raw is None:
            continue
        lifted = _lift_expired(raw["models"], now=now)
        if lifted:
            notes.append(f"{path.stem}: lifted expired unserved-route hold on {', '.join(sorted(lifted))}")
        manifests.append((path, raw, bool(lifted)))

    planned: list[tuple[int, dict[str, Any]]] = []
    for index, (path, raw, _) in enumerate(manifests):
        wanted = by_provider.get(path.stem, set())
        if not wanted:
            continue
        rows = raw["models"]
        routable_rows = {
            row["id"]: row for row in rows
            if isinstance(row, dict) and isinstance(row.get("id"), str) and _routable(row)
        }
        targets = sorted(wanted & set(routable_rows))
        already_held = {
            row.get("id") for row in rows
            if isinstance(row, dict) and isinstance(row.get("id"), str)
            and row.get("routable_reason") == UNSERVED_ROUTE_HOLD_REASON
        }
        for model in sorted(wanted - set(routable_rows) - already_held):
            notes.append(f"{path.stem}: {model} is not a routable manifest row; not held")
        # Rows this step already holds count on both sides, so the cap
        # cannot ratchet a provider down one partial run at a time.
        unserved = len(targets) + len(already_held)
        candidates = len(routable_rows) + len(already_held)
        if targets and (
            unserved > MAX_PROVIDER_FRACTION * candidates
            or len(targets) >= len(routable_rows)
        ):
            notes.append(
                f"{path.stem}: {unserved} of {candidates} rows unserved; "
                "provider-wide problem, holding none"
            )
            targets = []
        planned.extend((index, routable_rows[model]) for model in targets)

    # Held routes stop being probed and drop out of the evidence, so the
    # route cap must count the holds already in place: otherwise it bounds
    # only each hourly run and a rolling problem accumulates without limit.
    held_total = sum(
        1
        for _, raw, _ in manifests
        for row in raw["models"]
        if isinstance(row, dict) and row.get("routable_reason") == UNSERVED_ROUTE_HOLD_REASON
    )
    if planned and held_total + len(planned) > MAX_ROUTES:
        notes.append(
            f"{held_total} held + {len(planned)} new unserved routes exceed the "
            f"{MAX_ROUTES}-route cap; holding nothing new"
        )
        planned = []

    changed = {index for index, (_, _, lifted) in enumerate(manifests) if lifted}
    for index, row in planned:
        row["routable"] = False
        row["routable_reason"] = UNSERVED_ROUTE_HOLD_REASON
        row[HELD_AT_FIELD] = _iso(now)
        changed.add(index)
        notes.append(f"{manifests[index][0].stem}: held {row['id']} ({UNSERVED_ROUTE_HOLD_REASON}) for {HOLD_HOURS}h")
    for index in sorted(changed):
        path, raw, _ = manifests[index]
        path.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", default=SOURCE)
    parser.add_argument("--json", help="Saved evidence payload instead of --source")
    parser.add_argument("--manifest-dir", type=Path, default=MANIFEST_DIR)
    args = parser.parse_args(argv)
    now = datetime.now(UTC)
    evidence: set[Route] | None
    try:
        if args.json:
            evidence = parse_evidence(json.loads(Path(args.json).read_text(encoding="utf-8")), now=now)
        else:
            evidence = fetch_evidence(args.source)
    except Exception as exc:  # noqa: BLE001 - any failure means no evidence, never "all healthy"
        print(f"unserved-route evidence unavailable ({type(exc).__name__})", file=sys.stderr)
        evidence = None
    for note in apply_unserved_route_holds(evidence, manifest_dir=args.manifest_dir, now=now):
        print(note)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
