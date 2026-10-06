"""The control plane starts whichever provider delists everything.

The catalog is rebuilt hourly from provider feeds without a human in the loop.
Code that runs at import and indexes one provider's model -- as the DeepSeek V4
Pro release installer and the Archimedes proxy did until 2026-09-28 -- stops
the whole control plane from starting the day that model is delisted. Here each
provider in turn has every manifest row tombstoned (as the refresh does on a
second miss) and its endpoints dropped from the OpenRouter snapshot, and
trusted_router.main must still import in a fresh process.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

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


@pytest.mark.parametrize("provider", PROVIDERS)
def test_the_control_plane_starts_when_a_provider_delists_everything(
    provider: str, tmp_path: Path
) -> None:
    manifests, snapshot = _delist(provider, tmp_path)
    environ = dict(os.environ)
    environ["TR_TEST_PROVIDER_MODELS_DIR"] = str(manifests)
    environ["TR_TEST_SNAPSHOT_PATH"] = str(snapshot)
    result = subprocess.run(  # noqa: S603 - fixed Python regression script
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                """
                import os
                from pathlib import Path
                from trusted_router import catalog_ingest
                catalog_ingest._PROVIDER_MODELS_DIR = Path(os.environ["TR_TEST_PROVIDER_MODELS_DIR"])
                catalog_ingest._INGEST_PATH = Path(os.environ["TR_TEST_SNAPSHOT_PATH"])
                import trusted_router.main  # noqa: F401
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
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().endswith("started")
