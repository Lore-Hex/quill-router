"""The refresh-input list that decides whether a snapshot may be carried onto a newer main."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from scripts.pricing import refresh_inputs as module

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/refresh-prices.yml"


def covered(path: str, specs: list[str]) -> bool:
    return any(path == spec or (spec.endswith("/") and path.startswith(spec)) for spec in specs)


def test_every_python_command_before_publication_is_an_entry_point() -> None:
    text = WORKFLOW.read_text()
    before = text[: text.index(module.PUBLICATION_STEP)]
    commands = re.findall(r"uv run python\b[^\n|>]*", before)
    entries = {path.as_posix() for path in module.entry_points(text)}
    assert len(commands) >= 10
    for command in commands:
        target = command.split()[3:]
        name = target[1] if target[0] == "-m" else target[0]
        path = module.resolve_module(name).as_posix() if target[0] == "-m" else name
        assert path in entries, command


def test_closure_follows_imports_and_subprocess_scripts() -> None:
    specs = module.refresh_inputs()
    for path in (
        "scripts/pricing/refresh.py",
        "scripts/check_price_spike.py",
        "scripts/update_model_changes.py",
        # Run by update_model_changes.py in a subprocess, named by a literal.
        "scripts/model_change_snapshot.py",
        "src/trusted_router/__init__.py",
        "src/trusted_router/catalog.py",
        "src/trusted_router/provider_lifecycle.py",
        "src/trusted_router/provider_contracts.py",
    ):
        assert covered(path, specs), path
    # Code the refresh never loads is not an input, so its changes can be carried.
    for path in ("src/trusted_router/routes/internal/gateway.py", "fastpath/cmd", "docs/README.md"):
        assert not covered(path, specs), path


def test_every_published_path_is_a_refresh_input() -> None:
    # The carry's three-way merge cannot conflict only because main changed
    # none of the paths the refresh writes.
    workflow = yaml.safe_load(WORKFLOW.read_text())
    publication = next(step for step in workflow["jobs"]["refresh"]["steps"]
                       if step.get("id") == "publication")
    staged = re.search(r"git add \\\n(.*?)\n\n", publication["run"], re.S)
    assert staged
    paths = [line.strip().rstrip("\\").strip() for line in staged.group(1).splitlines()]
    assert len(paths) == 12
    assert "src/trusted_router/data/model_routing_evidence.json" in paths
    specs = module.refresh_inputs()
    for path in paths:
        assert covered(path.rstrip("/") + ("/x" if path.endswith("/") else ""), specs), path


def test_static_inputs_cover_dynamic_and_data_reads() -> None:
    specs = module.refresh_inputs()
    for path in (
        "scripts/pricing/providers/together.py",  # importlib.import_module by slug
        "scripts/pricing/parsers/fixture.py",  # self-healed parsers
        "scripts/pricing/refresh_inputs.py",  # this rule itself
        "src/trusted_router/data/provider_models/together.json",
        "src/trusted_router/static/provider-logos/together.png",
        "src/trusted_router/static/fonts/archivo-latin.woff2",
        "uv.lock",
        ".github/workflows/refresh-prices.yml",
    ):
        assert covered(path, specs), path


def test_cli_prints_one_pathspec_per_line(capsys: pytest.CaptureFixture[str]) -> None:
    assert module.main([]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines == module.refresh_inputs()
    assert lines[: len(module.STATIC_INPUTS)] == list(module.STATIC_INPUTS)
    assert len(lines) == len(set(lines))


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(module, "ROOT", tmp_path)
    files = {
        "scripts/__init__.py": "",
        "scripts/entry.py": "from scripts import helper\n",
        "scripts/helper.py": (
            "def run():\n"
            "    from trusted_router.pkg import lazy  # a function-level import\n"
            "    return 'scripts/child.py'\n"
        ),
        "scripts/child.py": "import trusted_router.leaf\n",
        "src/trusted_router/__init__.py": "",
        "src/trusted_router/pkg/__init__.py": "",
        "src/trusted_router/pkg/lazy.py": "from . import sibling\nfrom ..leaf import VALUE\n",
        "src/trusted_router/pkg/sibling.py": "import json\n",
        "src/trusted_router/leaf.py": "VALUE = 1\n",
        "src/trusted_router/unused.py": "",
    }
    for name, body in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(body)
    return tmp_path


def test_closure_resolves_relative_lazy_and_package_imports(tree: Path) -> None:
    found = {path.as_posix() for path in module.closure([Path("scripts/entry.py")])}
    assert found == {
        "scripts/entry.py", "scripts/__init__.py", "scripts/helper.py", "scripts/child.py",
        "src/trusted_router/__init__.py", "src/trusted_router/pkg/__init__.py",
        "src/trusted_router/pkg/lazy.py", "src/trusted_router/pkg/sibling.py",
        "src/trusted_router/leaf.py",
    }


def test_entry_points_require_a_publication_step_and_existing_targets(tree: Path) -> None:
    step = module.PUBLICATION_STEP
    with pytest.raises(ValueError, match="no publication step"):
        module.entry_points("run: uv run python scripts/entry.py\n")
    with pytest.raises(ValueError, match="runs no Python entry point"):
        module.entry_points(f"run: echo\n{step}\n")
    with pytest.raises(ValueError, match="entry module not found: scripts.missing"):
        module.entry_points(f"run: uv run python -m scripts.missing\n{step}\n")
    with pytest.raises(ValueError, match="entry script not found: scripts/missing.py"):
        module.entry_points(f"run: uv run python scripts/missing.py\n{step}\n")
    # Commands after the publication step are not refresh inputs.
    text = f"run: uv run python scripts/entry.py\n{step}\nrun: uv run python scripts/child.py\n"
    assert module.entry_points(text) == [Path("scripts/entry.py")]
