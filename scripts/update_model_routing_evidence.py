"""Publish reviewed per-model/workload evidence; failed reads keep the prior file."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile

import httpx

from trusted_router.model_ranking import EVIDENCE_PATH, stabilize, validate_evidence

SOURCE = "https://trustedrouter.com/routing-evidence.json"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", help="Saved aggregate evidence; never raw customer events")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--min-age-hours", type=int, default=0)
    args = parser.parse_args()
    if args.min_age_hours < 0:
        parser.error("--min-age-hours must be nonnegative")
    now = datetime.now(UTC)
    previous = json.loads(EVIDENCE_PATH.read_text()) if EVIDENCE_PATH.exists() else None
    if previous is not None:
        generated, _ = validate_evidence(previous)
        if args.write and timedelta(0) <= now - generated < timedelta(hours=args.min_age_hours):
            print("Model routing evidence is current; no refresh due")
            return
    if args.json:
        payload = json.loads(Path(args.json).read_text())
    else:
        response = httpx.get(SOURCE, timeout=30.0, follow_redirects=False)
        response.raise_for_status()
        payload = response.json()
    generated, rows = validate_evidence(payload)
    if not rows:
        raise ValueError("empty model routing evidence")
    if not timedelta(0) <= now - generated <= timedelta(hours=12):
        raise ValueError("model routing evidence is stale or future-dated")
    if previous is not None and generated <= validate_evidence(previous)[0]:
        raise ValueError("model routing evidence did not advance")
    payload = stabilize(payload, previous)
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if not args.write:
        print(encoded)
        return
    temporary = None
    try:
        with NamedTemporaryFile("w", dir=EVIDENCE_PATH.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write(encoded)
        temporary.replace(EVIDENCE_PATH)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print(f"updated {EVIDENCE_PATH}: {len(payload['routes'])} model/host/workloads")


if __name__ == "__main__":
    main()
