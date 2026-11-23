"""Hold listed provider routes that the provider refuses to serve.

A provider can keep a model in its /models list while answering every request
for it with "model not found" or "not available". Discovery sees the listing,
so the route stays routable, and customer requests try it first and fail over.
The per-provider refresh canaries cannot catch this: they probe only new routes
and routes already held by a failed canary (``models_requiring_canary``), and
several providers have no inference credential in the refresh at all.

The control plane already observes the failure: configured-route probes
classify the responses as ``unsupported_route``. ``/route-health/unserved.json``
publishes the routes whose newest probes form a long model-not-served streak,
or have failed every probe for six hours (a sustained per-route outage, such
as Chutes answering 502 for one model while serving the rest), see
``trusted_router.synthetic.route_health.evaluate_unserved_routes``. This
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
import importlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from trusted_router.provider_manifest_policy import (
    UNSERVED_ROUTE_HELD_AT_FIELD,
    UNSERVED_ROUTE_HOLD_REASON,
)
from trusted_router.unserved_route_hold_plan import (
    HOLD_HOURS,
    MAX_CLOCK_SKEW,
    MAX_PROVIDER_FRACTION,
    MAX_ROUTES,
    lift_expired_unserved_holds,
    plan_unserved_route_holds,
)

SOURCE = "https://trustedrouter.com/route-health/unserved.json"
MANIFEST_DIR = Path(__file__).resolve().parents[2] / "src/trusted_router/data/provider_models"
HELD_AT_FIELD = UNSERVED_ROUTE_HELD_AT_FIELD
MAX_EVIDENCE_AGE = timedelta(hours=2)
__all__ = [
    "HOLD_HOURS", "MAX_CLOCK_SKEW", "MAX_PROVIDER_FRACTION", "MAX_ROUTES",
    "apply_unserved_route_holds", "parse_evidence",
]

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
    frozen: frozenset[str] = frozenset(),
) -> list[str]:
    """Lift expired holds, then hold the evidenced routes. Returns notes.

    ``frozen`` names manifests (by file stem) this step must leave byte for
    byte: a quarantined provider's committed manifest (scripts/pricing/
    quarantine.py). Lifting an expired hold there would re-enable a route
    the quarantine is keeping exactly as published.
    """
    now = now or datetime.now(UTC)
    notes: list[str] = []
    manifest_stems = [path.stem for path in manifest_dir.glob("*.json")]
    manifests: list[tuple[Path, dict[str, Any], bool]] = []
    frozen_held = 0
    for path in sorted(manifest_dir.glob("*.json")):
        raw = _load(path)
        if path.stem in frozen:
            notes.append(f"{path.stem}: quarantined; manifest left as published")
            # Its holds still count against the global route cap.
            frozen_held += sum(
                1 for row in (raw or {}).get("models", [])
                if isinstance(row, dict) and row.get("routable_reason") == UNSERVED_ROUTE_HOLD_REASON
            )
            continue
        if raw is None:
            continue
        lifted = lift_expired_unserved_holds(raw["models"], now=now)
        if lifted:
            notes.append(f"{path.stem}: lifted expired unserved-route hold on {', '.join(sorted(lifted))}")
        manifests.append((path, raw, bool(lifted)))

    planned, plan_notes = plan_unserved_route_holds(
        evidence,
        [(path.stem, raw["models"]) for path, raw, _ in manifests],
        manifest_stems=manifest_stems,
        extra_held=frozen_held,
    )
    notes.extend(plan_notes)

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


def quarantined_manifests() -> frozenset[str]:
    """Manifest stems of the providers quarantined for this run."""
    from scripts.pricing.quarantine import quarantined_providers

    stems: set[str] = set()
    for slug in quarantined_providers():
        try:
            module = importlib.import_module(f"scripts.pricing.providers.{slug}")
        except ImportError:
            stems.add(slug)
            continue
        manifest = getattr(module, "MANIFEST_PATH", None)
        stems.add(Path(manifest).stem if manifest is not None else slug)
    return frozenset(stems)


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
    frozen = quarantined_manifests()
    for note in apply_unserved_route_holds(evidence, manifest_dir=args.manifest_dir, now=now, frozen=frozen):
        print(note)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
