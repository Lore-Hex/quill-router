"""The control plane starts whichever provider delists everything.

The catalog is rebuilt hourly from provider feeds without a human in the loop.
Code that runs at import and indexes one provider's model -- as the DeepSeek V4
Pro release installer and the Archimedes proxy did until 2026-09-28 -- stops
the whole control plane from starting the day that model is delisted. Here each
provider in turn has every manifest row tombstoned (as the refresh does on a
second miss) and its endpoints dropped from the OpenRouter snapshot, and
trusted_router.main must still import in a fresh process.

The refresh also holds a listed route its provider refuses to serve
(scripts/pricing/unserved_route_holds.py), on any routable row. The row stays
listed, so no delisting above puts the catalog in that state. The second test
holds every row that step can hold, a slice of each provider's rows at a time,
with the step itself, and the control plane must start on each. Which release
tests fail under a hold is scripts/tombstone_sweep.py's `holds` mode, as
`sweep` is for a delisting.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts import tombstone_sweep_holds
from trusted_router import catalog_ingest

PROVIDERS = sorted(path.stem for path in catalog_ingest._PROVIDER_MODELS_DIR.glob("*.json"))


def _delist(provider: str, root: Path) -> tuple[Path, Path]:
    manifests = root / "provider_models"
    shutil.copytree(catalog_ingest._PROVIDER_MODELS_DIR, manifests)
    manifest = manifests / f"{provider}.json"
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    for row in raw.get("models", []):
        if isinstance(row, dict) and row.get("routable") is not False:
            row.update(routable=False, routable_reason="delisted-upstream")
    manifest.write_text(json.dumps(raw), encoding="utf-8")
    snapshot = json.loads(catalog_ingest._INGEST_PATH.read_text(encoding="utf-8"))
    kept = []
    for model in snapshot.get("models", []):
        endpoints = model.get("endpoints") or []
        remaining = [e for e in endpoints if e.get("tr_provider_slug") != provider]
        if endpoints and not remaining:
            continue
        model["endpoints"] = remaining
        kept.append(model)
    snapshot["models"] = kept
    snapshot_path = root / "openrouter_snapshot.json"
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    return manifests, snapshot_path


def _start(manifests: Path, snapshot: Path) -> subprocess.CompletedProcess[str]:
    """Import the control plane in a fresh process on these data files. It
    prints the unserved-route holds it finds live there, then "started"."""
    environ = dict(os.environ)
    environ["TR_TEST_PROVIDER_MODELS_DIR"] = str(manifests)
    environ["TR_TEST_SNAPSHOT_PATH"] = str(snapshot)
    return subprocess.run(  # noqa: S603 - fixed Python regression script
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                """
                import json
                import os
                from pathlib import Path
                from trusted_router import catalog_ingest
                catalog_ingest._PROVIDER_MODELS_DIR = Path(os.environ["TR_TEST_PROVIDER_MODELS_DIR"])
                catalog_ingest._INGEST_PATH = Path(os.environ["TR_TEST_SNAPSHOT_PATH"])
                import trusted_router.main  # noqa: F401
                from trusted_router.unserved_route_hold_plan import applied_unserved_route_holds
                held = applied_unserved_route_holds(manifest_dir=catalog_ingest._PROVIDER_MODELS_DIR)
                print(json.dumps(sorted(held)))
                print("started")
                """
            ),
        ],
        env=environ,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


@pytest.mark.parametrize("provider", PROVIDERS)
def test_the_control_plane_starts_when_a_provider_delists_everything(
    provider: str, tmp_path: Path
) -> None:
    manifests, snapshot = _delist(provider, tmp_path)
    result = _start(manifests, snapshot)
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().endswith("started")


def test_the_control_plane_starts_with_any_row_held_as_unserved(tmp_path: Path) -> None:
    # One process per slice, not per row or provider: a start costs seconds,
    # and the slices are few (two, three for an odd count of rows, more only
    # where the step already holds rows). Planned here and not at collection:
    # a hold expiring between two xdist workers' collections would give them
    # different tests.
    # One clock for the plan and every hold: a committed hold that has run
    # out, and that the next refresh has not lifted yet, is a row to hold in
    # both or in neither.
    now = datetime.now(UTC)
    plan = tombstone_sweep_holds.plan(catalog_ingest._PROVIDER_MODELS_DIR, now=now)
    # Some row is routable; none can be held only once every provider is at
    # the step's limit, as inside a `holds` sweep's own slice.
    assert plan["slices"] or plan["never_held"]
    for index, routes in enumerate(plan["slices"]):
        manifests = tmp_path / f"slice{index}"
        shutil.copytree(catalog_ingest._PROVIDER_MODELS_DIR, manifests)
        held = tombstone_sweep_holds.hold(routes, manifests, now=now)["held"]
        result = _start(manifests, catalog_ingest._INGEST_PATH)
        assert result.returncode == 0, f"slice {index}, holding {held}: {result.stderr[-2000:]}"
        live, started = result.stdout.strip().splitlines()[-2:]
        assert started == "started"
        # The process started on these holds: the control plane's own reader
        # finds each one live in the manifests it loaded.
        assert {tuple(route) for route in json.loads(live)} >= set(held), index
