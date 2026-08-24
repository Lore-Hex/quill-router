from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts.deploy.resolve_active_revision import resolve_active_revision

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/deploy/resolve_active_revision.py"


def _service(*traffic: dict[str, Any]) -> dict[str, Any]:
    return {"status": {"traffic": list(traffic)}}


def test_resolves_serving_revision_after_tag_only_entry() -> None:
    service = _service(
        {
            "revisionName": "trusted-router-older-tagged",
            "tag": "rq-lease-canary",
        },
        {"revisionName": "trusted-router-current", "percent": 100},
    )

    assert resolve_active_revision(service) == "trusted-router-current"


@pytest.mark.parametrize(
    "traffic",
    [
        (),
        (
            {"revisionName": "trusted-router-a", "percent": 50},
            {"revisionName": "trusted-router-b", "percent": 50},
        ),
        (
            {"revisionName": "trusted-router-a", "percent": 100},
            {"revisionName": "trusted-router-b", "percent": 100},
        ),
        ({"revisionName": "trusted-router-a", "percent": "invalid"},),
        ({"percent": 100},),
        ({"revisionName": "trusted-router-a", "percent": -1},),
    ],
)
def test_rejects_ambiguous_or_invalid_traffic(
    traffic: tuple[dict[str, Any], ...],
) -> None:
    with pytest.raises(ValueError):
        resolve_active_revision(_service(*traffic))


@pytest.mark.parametrize(
    "service",
    [
        {},
        {"status": None},
        {"status": {"traffic": {}}},
        {"status": {"traffic": ["not-an-object"]}},
    ],
)
def test_rejects_malformed_service_documents(service: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        resolve_active_revision(service)


def test_cli_fails_closed_on_split_traffic() -> None:
    result = subprocess.run(  # noqa: S603 - fixed interpreter and repo-owned script
        [sys.executable, str(SCRIPT)],
        input=json.dumps(
            _service(
                {"revisionName": "trusted-router-a", "percent": 90},
                {"revisionName": "trusted-router-b", "percent": 10},
            )
        ),
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 1
    assert "expected exactly one 100%-traffic revision" in result.stderr
    assert result.stdout == ""
