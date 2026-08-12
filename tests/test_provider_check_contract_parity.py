from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXPORTER_PATH = ROOT / "scripts/export_provider_check_contract.py"
SNAPSHOT_PATH = ROOT / "src/trusted_router/data/provider_check_contract.json"
PARITY_MESSAGE = (
    "Production contract symbols changed. Re-run "
    "scripts/export_provider_check_contract.py, review the diff, and open a sync PR to "
    "Lore-Hex/trustedrouter-provider-check. Do NOT relax this assertion."
)
EXPECTED_KEYS = {
    "prompts",
    "markers",
    "catalog_contract",
    "decision_tables",
    "failure_classification",
    "model_deadlines",
    "leaderboard",
    "extractors",
    "rotation_errors",
    "source_hashes",
    "sample_fields",
    "contract_version",
}


def _load_builder() -> Callable[[], dict[str, Any]]:
    spec = importlib.util.spec_from_file_location(
        "provider_check_contract_exporter",
        EXPORTER_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not import {EXPORTER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build_contract


def _committed_contract() -> dict[str, Any]:
    return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))


def test_production_contract_matches_snapshot() -> None:
    assert _load_builder()() == _committed_contract(), PARITY_MESSAGE


def test_contract_guards_are_nonempty_and_deterministic() -> None:
    build_contract = _load_builder()
    previous_catalog = sys.modules.pop("trusted_router.catalog", None)
    try:
        first = build_contract()
        second = build_contract()

        assert first == second
        assert set(first) == EXPECTED_KEYS
        assert all(isinstance(values, list) and values for values in first["markers"].values())
        assert len(first["decision_tables"]) >= 15
        assert len(first["failure_classification"]) >= 20
        assert len(first["extractors"]) >= 40
        assert first["rotation_errors"]["classification"]
        assert first["rotation_errors"]["excluded_from_uptime"]
        assert len(first["source_hashes"]) >= 20
        assert all(
            isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
            for value in first["source_hashes"].values()
        )
        assert hashlib.sha256(b"").hexdigest() not in first["source_hashes"].values()
        # Distinct bodies must hash distinctly. The empty-digest check above
        # only catches a normalizer that strips everything; one that collapses
        # every function to the same non-empty text would still pass it while
        # pinning nothing.
        assert len(set(first["source_hashes"].values())) == len(first["source_hashes"])
        assert first["leaderboard"]["models"]
        assert first["leaderboard"]["providers"]
        assert "trusted_router.catalog" not in sys.modules
    finally:
        if previous_catalog is not None:
            sys.modules["trusted_router.catalog"] = previous_catalog


def test_optional_public_provider_check_snapshot() -> None:
    provider_check_repo = os.environ.get("PROVIDER_CHECK_REPO_PATH")
    if not provider_check_repo:
        pytest.skip("PROVIDER_CHECK_REPO_PATH is not set")
    public_snapshot = Path(provider_check_repo) / "contract_snapshot.json"
    public_contract = json.loads(public_snapshot.read_text(encoding="utf-8"))
    assert public_contract == _committed_contract(), PARITY_MESSAGE
