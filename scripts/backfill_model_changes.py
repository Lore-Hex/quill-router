#!/usr/bin/env python3
"""Replay first-parent catalog revisions offline. Run once to seed a new history.

Commit timestamps are observations, not assertions about deployment times.
Existing history is never overwritten. Finish with update_model_changes.py.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from scripts.update_model_changes import ROOT, read_snapshot, record
from trusted_router.model_changes import (
    HISTORY_PATH,
    STATE_PATH,
    canonical,
    diff_catalogs,
    route_state,
    timestamp,
)


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=ROOT)  # noqa: S603,S607


def main() -> None:
    import tarfile

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", default="2026-09-01T00:00:00Z")
    args = parser.parse_args()
    if HISTORY_PATH.exists() or STATE_PATH.exists():
        raise SystemExit("Backfill needs absent history/state files; it never overwrites history")
    baseline = git("rev-list", "--first-parent", "-1", f"--before={args.since}", "HEAD").decode().strip()
    revisions = [baseline, *git(
        "rev-list", "--first-parent", "--reverse", f"{baseline}..HEAD", "--",
        "src/trusted_router/catalog*.py", "src/trusted_router/provider*.py",
        "src/trusted_router/request_capabilities.py", "src/trusted_router/pricing.py",
        "src/trusted_router/image_generation.py", "src/trusted_router/money.py",
        "src/trusted_router/partner_billing.py", "src/trusted_router/polyphemus.py",
        "src/trusted_router/routing_candidates.py", "src/trusted_router/wafer_policy.py",
        "src/trusted_router/data",
    ).decode().splitlines()]
    history: list[dict] = []
    previous = None
    previous_raw = None
    previous_at = args.since
    with tempfile.TemporaryDirectory(prefix="tr-model-history-") as temp:
        source = Path(temp)
        for index, revision in enumerate(revisions):
            at = max(args.since, timestamp(datetime.fromisoformat(git("show", "-s", "--format=%cI", revision).decode().strip())))
            if previous_raw is not None:
                for cutover in previous_raw["cutovers"]:
                    if previous_at < cutover <= at:
                        following = route_state(read_snapshot(cutover, source=source)["rows"])
                        history.extend(record(e, cutover) for e in diff_catalogs(
                            previous, following, cutover, source="backfill_scheduled",
                        ))
                        previous = following
            # Top-level Python modules plus committed manifests are sufficient;
            # avoid shipping historical templates, images, credentials or git.
            archive = git("archive", revision, "src/trusted_router/*.py", "src/trusted_router/data")
            if (source / "src").exists():
                shutil.rmtree(source / "src")
            with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
                tar.extractall(source, filter="data")
            raw = read_snapshot(at, source=source)
            current = route_state(raw["rows"])
            if previous is not None:
                events = diff_catalogs(previous, current, at, source="backfill")
                for event in events:
                    event["commit"] = revision
                    history.append(record(event, at))
            previous, previous_raw, previous_at = current, raw, at
            print(f"{index + 1}/{len(revisions)} {revision[:10]} {at}: {len(history)} changes", flush=True)
    content = "".join(canonical(e) + "\n" for e in sorted(history, key=lambda e: (e["recorded_at"], e["id"]))).encode()
    HISTORY_PATH.write_bytes(content)
    STATE_PATH.write_text(canonical({"version": 1, "as_of": previous_at, "models": previous,
                                    "scheduled": [], "history_sha256": hashlib.sha256(content).hexdigest()}) + "\n")


if __name__ == "__main__":
    main()
