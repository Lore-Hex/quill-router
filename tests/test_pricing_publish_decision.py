from __future__ import annotations

import io
import shlex
from pathlib import Path

import pytest
import yaml

from scripts.pricing.publish_decision import analyze_repository, main, publish_decision

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "path",
    [
        "src/trusted_router/data/openrouter_snapshot.json",
        "src/trusted_router/data/speech_pricing.json",
        "src/trusted_router/data/provider_models/provider.json",
        "src/trusted_router/data/provider_models/nested/provider.json",
        "src/trusted_router/data/model_changes.jsonl",
        "src/trusted_router/data/model_changes_state.json",
        "src/trusted_router/static/og/providers/provider.png",
        "scripts/pricing/parsers/provider.py",
        "scripts/pricing/providers/provider.py",
        "scripts/pricing/refresh.py",
        "scripts/pricing/publish_decision.py",
        "scripts/ingest_openrouter_catalog.py",
        "scripts/check_price_spike.py",
        "scripts/update_model_changes.py",
        "src/trusted_router/provider_lifecycle.py",
        "src/trusted_router/provider_manifest_policy.py",
        ".github/workflows/refresh-prices.yml",
        "src/trusted_router/data/provider_models",
        "src/trusted_router/static/og/providers",
        "scripts/pricing",
    ],
)
def test_refresh_inputs_and_outputs_require_recomputation(path: str) -> None:
    assert publish_decision([path]) == "recompute"
    assert publish_decision(["tests/test_other.py", path, "docs/other.md"]) == "recompute"


@pytest.mark.parametrize(
    "paths",
    [
        [],
        ["scripts/security/check_repo_security_coverage.py"],
        ["docs/pricing.md"],
        ["scripts/security/check_repo_security_coverage.py", "docs/pricing.md"],
    ],
)
def test_unrelated_changes_allow_rebase(paths: list[str]) -> None:
    assert publish_decision(paths) == "rebase"


def test_unrelated_module_is_provably_outside_generation_closure() -> None:
    path = "scripts/security/check_repo_security_coverage.py"
    assert (ROOT / path).is_file()
    dependencies = analyze_repository(ROOT)
    assert not dependencies.contains(path)
    assert publish_decision([path]) == "rebase"


@pytest.mark.parametrize(
    ("stdin", "expected"),
    [
        ("", "rebase"),
        ("docs/a b.md\0docs/multiline\nname.md\0", "rebase"),
        ("tests/x.py\0scripts/pricing/providers/new\nprovider.py\0", "recompute"),
    ],
)
def test_cli_reads_nul_delimited_paths(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], stdin: str, expected: str
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    main()
    assert capsys.readouterr().out == f"{expected}\n"


def test_every_staged_refresh_path_requires_recomputation() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/refresh-prices.yml").read_text())
    step = next(
        step for step in workflow["jobs"]["refresh"]["steps"]
        if step.get("name") == "Commit, verify, and push if changed"
    )
    staged_paths = shlex.split(step["run"].split("git add ", 1)[1].split("\n\n", 1)[0])
    # shlex preserves a token for each escaped newline; only paths matter.
    staged_paths = [path.strip() for path in staged_paths if path.strip()]
    assert len(staged_paths) == 7
    for path in staged_paths:
        probe = path + "example.json" if path.endswith("/") else path
        assert publish_decision([probe]) == "recompute", path
