from __future__ import annotations

import importlib
import shutil
from pathlib import Path

import pytest

decision = importlib.import_module("scripts.pricing.publish_decision")
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Copy sources only; analysis must never execute generators or read credentials.
    for directory in ("scripts", "src/trusted_router"):
        for source in (ROOT / directory).rglob("*.py"):
            target = tmp_path / source.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    workflow = tmp_path / decision.WORKFLOW
    workflow.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / decision.WORKFLOW, workflow)
    dependencies = decision.analyze_repository(ROOT)
    for path in dependencies.files:
        target = tmp_path / path
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch()
    for directory in dependencies.directories:
        (tmp_path / directory).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(decision, "ROOT", tmp_path)
    return tmp_path


def test_split_path_subprocess_target_requires_recomputation(repository: Path) -> None:
    source = repository / "scripts/update_model_changes.py"
    source.write_text(source.read_text().replace(
        'ROOT / "scripts/model_change_snapshot.py"',
        'ROOT / "scripts" / "model_change_snapshot.py"',
    ))
    assert decision.publish_decision(["scripts/model_change_snapshot.py"]) == "recompute"


def test_assigned_import_callable_requires_recomputation(repository: Path) -> None:
    target = "scripts/security/check_repo_security_coverage.py"
    assert not decision.analyze_repository(repository).contains(target)
    source = repository / "scripts/update_model_changes.py"
    source.write_text(source.read_text() + '\nimport importlib\n'
                      'loader = importlib.import_module\n'
                      'loader("scripts.security.check_repo_security_coverage")\n')
    assert decision.publish_decision([target]) == "recompute"


@pytest.mark.parametrize("source", [
    'import importlib\nloader = importlib.import_module\nloader(name)',
    'from importlib import import_module as loader\nloader("scripts.target")',
    'loader = __import__\nloader("scripts.target")',
    'import importlib.util\nimportlib.util.spec_from_file_location("name", filename)',
    'import importlib.util\nimportlib.util.module_from_spec(spec)',
    'import runpy\nrunpy.run_path(filename)',
    'import subprocess\nsubprocess.run(command)',
    'import subprocess\nrunner = subprocess.run\nrunner(command)',
    'import os\nos.system(command)', 'import os\nos.execv(executable, args)',
    'import os\nos.spawnv(mode, executable, args)', 'import os\nos.popen(command)',
    'exec(source)', 'eval(source)', 'compile(source, filename, "exec")',
    'import sys\ncommand = [sys.executable, target]',
    'path = "scripts/" + name + ".py"',
    'from pathlib import Path\npath = Path(root) / "scripts" / "target.py"',
    'path = "scripts/{}.py".format(name)',
    'from pathlib import Path\npath = Path("scripts") / filename',
    'from pathlib import Path\npath = Path(f"{directory}/target.py")',
    'from pathlib import Path\npath = Path(directory).joinpath("target.py")',
    'from subprocess import *\nrun(command)',
    'import os\nrunner = os\nrunner.system(command)',
    'import builtins\nloader = builtins.__import__\nloader("scripts.target")',
    'import importlib as loaders\nloader = loaders.import_module\nloader("scripts.target")',
    'import importlib\nimportlib.import_module = replacement',
    'import importlib\ndef f(importlib):\n    importlib.import_module("scripts.target")',
    'import importlib\nimport other as importlib\nimportlib.import_module("scripts.target")',
    'def f(__import__):\n    __import__("scripts.target")',
    'import importlib\nloader = getattr(importlib, "import_module")',
    'import subprocess\nrunner = subprocess.TimeoutExpired',
    'import subprocess, sys\nsubprocess.run([sys.executable, "scripts/target.py"], **options)',
    'import subprocess, sys\nsubprocess.run([sys.executable, "scripts/target.py"], cwd=directory)',
    'import importlib\nimportlib.import_module.__call__("scripts.target")',
    '__import__.__call__("scripts.target")',
    'exec.__call__(source)',
    'import importlib.util\nloader = importlib.util.spec_from_file_location.__call__',
    'import importlib\nloader = importlib.__getattribute__("import_module")',
    'import builtins\nloader = builtins.__dict__["exec"]',
])
def test_unresolved_execution_or_callable_alias_fails_closed(tmp_path: Path, source: str) -> None:
    entry = tmp_path / "scripts/entry.py"
    entry.parent.mkdir()
    entry.write_text(source)
    (entry.parent / "target.py").write_text("")
    closure = decision.ImportClosure(tmp_path)
    closure.add_file("scripts/entry.py")
    with pytest.raises(decision.Uncertain):
        closure.follow()


def test_today_has_a_definite_rebase_for_unrelated_python() -> None:
    target = "scripts/security/check_repo_security_coverage.py"
    dependencies = decision.analyze_repository(ROOT)  # Must not raise Uncertain.
    assert (ROOT / target).is_file()
    assert not dependencies.contains(target)
    assert decision.publish_decision([target]) == "rebase"


def test_self_heal_execution_adapter_fails_closed_after_code_changes(repository: Path) -> None:
    source = repository / "scripts/pricing/base.py"
    source.write_text(source.read_text().replace(
        "runner = _SANDBOX_RUNNER_TEMPLATE.format(parser_source=parser_source)",
        "runner = unknown_source",
    ))
    with pytest.raises(decision.Uncertain, match="scripts/pricing/base.py"):
        decision.analyze_repository(repository)
    assert decision.publish_decision(["docs/notes.md"]) == "recompute"


def test_exec_of_a_literal_repository_file_is_resolved(tmp_path: Path) -> None:
    directory = tmp_path / "scripts"
    directory.mkdir()
    (directory / "target.py").write_text("from scripts import child\n")
    (directory / "child.py").write_text("")
    (directory / "entry.py").write_text(
        'from pathlib import Path\n'
        'path = Path("scripts") / "target.py"\n'
        'source = path.read_text()\n'
        'exec(compile(source, str(path), "exec"), {})\n'
    )
    closure = decision.ImportClosure(tmp_path)
    closure.add_file("scripts/entry.py")
    closure.follow()
    assert {"scripts/target.py", "scripts/child.py"} <= closure.files


def test_fstring_path_includes_the_known_package(tmp_path: Path) -> None:
    directory = tmp_path / "scripts"
    directory.mkdir()
    (directory / "target.py").write_text("")
    (directory / "entry.py").write_text(
        'from pathlib import Path\n'
        'PACKAGE = Path("scripts")\n'
        'path = f"{PACKAGE}/{name}.py"\n'
    )
    closure = decision.ImportClosure(tmp_path)
    closure.add_file("scripts/entry.py")
    closure.follow()
    assert "scripts" in closure.directories
    assert "scripts/target.py" in closure.files


@pytest.mark.parametrize("expression", [
    '"scripts/target.py"', 'Path("scripts") / "target.py"',
    'ROOT / "scripts" / "target.py"',
])
def test_literal_python_subprocess_paths_are_resolved(tmp_path: Path, expression: str) -> None:
    directory = tmp_path / "scripts"
    directory.mkdir()
    (directory / "target.py").write_text("")
    (directory / "entry.py").write_text(
        "import subprocess, sys\nfrom pathlib import Path\n"
        "ROOT = Path(__file__).resolve().parents[1]\n"
        f"subprocess.run([sys.executable, str({expression})])\n"
    )
    closure = decision.ImportClosure(tmp_path)
    closure.add_file("scripts/entry.py")
    closure.follow()
    assert "scripts/target.py" in closure.files
