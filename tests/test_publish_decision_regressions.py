"""Inputs missed by the original hand-maintained publication path list."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

decision = importlib.import_module("scripts.pricing.publish_decision")
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "path",
    [
        "src/trusted_router/model_aliases.py",
        "scripts/check_price_coverage.py",
        "src/trusted_router/pricing.py",
        "src/trusted_router/pricing_approvals.py",
        "scripts/generate_provider_og.py",
        "src/trusted_router/provider_og.py",
        "src/trusted_router/catalog.py",
        "scripts/model_change_snapshot.py",
        "src/trusted_router/static/provider-logos/openai.png",
        "src/trusted_router/static/fonts/archivo-latin.woff2",
        "uv.lock",
        "pyproject.toml",
        "setup.cfg",
        "ruff.toml",
        "mypy.ini",
        "tests/test_pricing.py",
        ".github/workflows/ci.yml",
    ],
)
def test_previously_missed_refresh_dependencies(path: str) -> None:
    assert decision.publish_decision([path]) == "recompute"


@pytest.mark.parametrize(
    "command",
    ["bash scripts/new_generator.sh", "uv run python -c 'print(1)'", "./new-generator"],
)
def test_unclassifiable_workflow_command_requires_recomputation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    workflow = ".github/workflows/refresh-prices.yml"
    target = tmp_path / workflow
    target.parent.mkdir(parents=True)
    target.write_text(
        (ROOT / workflow).read_text().replace(
            "      - name: Commit, verify, and push if changed",
            f"      - name: New generator\n        run: {command}\n\n"
            "      - name: Commit, verify, and push if changed",
        )
    )
    monkeypatch.setattr(decision, "ROOT", tmp_path, raising=False)
    assert decision.publish_decision(["docs/review.md"]) == "recompute"
    reason = capsys.readouterr().err
    assert "unclassified workflow command" in reason or "unrecognized Python invocation" in reason
