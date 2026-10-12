from __future__ import annotations

import shlex
from pathlib import Path

import pytest
import yaml

from scripts.pricing.publish_decision import (
    PUBLICATION_STEP,
    ROOT,
    WORKFLOW,
    ImportClosure,
    Uncertain,
    analyze_repository,
    publish_decision,
    shell_entry_points,
    workflow_steps,
)


def test_workflow_reader_matches_yaml_and_discovers_all_entry_points() -> None:
    source = (ROOT / WORKFLOW).read_text()
    actual = workflow_steps(source)
    expected = yaml.safe_load(source)["jobs"]["refresh"]["steps"]
    end = next(i for i, step in enumerate(expected) if step.get("name") == PUBLICATION_STEP)
    assert len(actual) == end + 1
    for parsed, original in zip(actual, expected[:end + 1], strict=True):
        assert parsed.get("run", "").rstrip() == original.get("run", "").rstrip()
    dependencies = analyze_repository(ROOT)
    assert dependencies.entry_points == {
        "scripts.pricing.speech", "scripts.pricing.refresh",
        "scripts/check_price_spike.py", "scripts/generate_provider_og.py",
        "scripts.check_price_coverage", "scripts/update_model_changes.py",
        "scripts/pricing/publish_decision.py",
    }
    for path in (
        "scripts/ingest_openrouter_catalog.py",  # Bare script import via sys.path.
        "scripts/model_change_snapshot.py",  # Launched by subprocess, not imported.
        "src/trusted_router/model_aliases.py",
        "src/trusted_router/pricing.py", "src/trusted_router/pricing_approvals.py",
        "src/trusted_router/provider_og.py", "src/trusted_router/catalog.py",
    ):
        assert path in dependencies.files, path
    assert "scripts/pricing/providers" in dependencies.directories
    assert dependencies.contains("scripts/pricing/providers/not_created_yet.py")


@pytest.mark.parametrize("command", [
    "python scripts/check_price_spike.py",
    "uv run python scripts/check_price_spike.py --summary | tee /tmp/report",
    "if true; then python scripts/check_price_spike.py; fi",
])
def test_script_entry_points(command: str) -> None:
    # Unknown conditions fail closed too; do not assume a branch is unreachable.
    if "true" in command:
        with pytest.raises(Uncertain):
            shell_entry_points(command)
    else:
        assert shell_entry_points(command) == {"scripts/check_price_spike.py"}


@pytest.mark.parametrize("command", [
    "bash scripts/generator.sh", "uv run python -c 'print(1)'", "uv run unknown", "uv run",
    "echo ok; ./new-generator", "echo ok && python -c pass", "source scripts/input.sh",
    "echo $(python scripts/input.py)",
    "KEY=$(gcloud secrets versions access latest --secret=x; ./unknown)",
    "KEY=$(gcloud secrets versions access latest --secret=x && ./unknown)",
    "echo `python scripts/input.py`", "cat <(python scripts/input.py)",
    "python <<'PY'\nprint(1)\nPY", 'python "${ENTRY_POINT}"',
])
def test_unknown_shell_commands_fail_closed(command: str) -> None:
    with pytest.raises(Uncertain):
        shell_entry_points(command)


@pytest.mark.parametrize(("before", "after"), [
    ("run: uv sync --frozen", "run: >\n          uv sync --frozen"),
    ("run: uv sync --frozen", "run: &commands uv sync --frozen"),
    ("run: uv sync --frozen", "shell: pwsh\n        run: uv sync --frozen"),
    ("run: uv sync --frozen", "working-directory: other\n        run: uv sync --frozen"),
])
def test_unsupported_workflow_shapes_fail_closed(before: str, after: str) -> None:
    with pytest.raises(Uncertain):
        workflow_steps((ROOT / WORKFLOW).read_text().replace(before, after))


def test_modified_publication_commands_fail_closed(tmp_path: Path) -> None:
    target = tmp_path / WORKFLOW
    target.parent.mkdir(parents=True)
    target.write_text((ROOT / WORKFLOW).read_text().replace(
        'git config user.name "trustedrouter-pricing-bot"',
        'git config user.name "trustedrouter-pricing-bot"\n          ./unknown-generator',
    ))
    with pytest.raises(Uncertain, match="publication shell changed"):
        analyze_repository(tmp_path)


def write_sources(root: Path, sources: dict[str, str]) -> None:
    for path, source in sources.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source)


def test_new_workflow_script_and_staged_python_are_derived(tmp_path: Path) -> None:
    publication = workflow_steps((ROOT / WORKFLOW).read_text())[-1]["run"].rstrip() + "\n"
    write_sources(tmp_path, {
        "scripts/pricing/publish_decision.py": "",
        "scripts/future_generator.py": "from trusted_router import future_dependency\n",
        "src/trusted_router/__init__.py": "",
        "src/trusted_router/future_dependency.py": "",
        "src/trusted_router/unrelated.py": "",
        "scripts/pricing/parsers/generated.py": "",
    })
    # The outputs come from the existing publication step, not a second path list.
    staged = publication.replace("\\\n", "").split("git add ", 1)[1].splitlines()[0]
    for path in shlex.split(staged):
        target = tmp_path / path
        if path.endswith("/"):
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch()
    target = tmp_path / WORKFLOW
    target.parent.mkdir(parents=True)
    target.write_text(
        "jobs:\n  refresh:\n    steps:\n"
        "      - run: uv run python scripts/future_generator.py\n"
        "      - run: uv run pytest -q\n"
        f"      - name: {PUBLICATION_STEP}\n        run: |\n"
        + "".join("          " + line + "\n" for line in publication.splitlines())
    )
    dependencies = analyze_repository(tmp_path)
    assert "scripts/future_generator.py" in dependencies.entry_points
    assert dependencies.contains("src/trusted_router/future_dependency.py")
    assert dependencies.contains("scripts/pricing/parsers/generated.py")
    assert not dependencies.contains("src/trusted_router/unrelated.py")


def test_import_closure_follows_relative_conditional_and_dynamic_imports(tmp_path: Path) -> None:
    write_sources(tmp_path, {
        "scripts/__init__.py": "",
        "scripts/generate.py": '''
import trusted_router.catalog
from sibling import snapshot
import importlib
def deferred():
    from trusted_router import pricing
importlib.import_module(f"scripts.providers.{slug}")
''',
        "scripts/sibling.py": "from trusted_router.policy import snapshot\n",
        "scripts/providers/__init__.py": "",
        "scripts/providers/one.py": "from trusted_router.policy import snapshot\n",
        "scripts/providers/nested/two.py": "from trusted_router import pricing\n",
        "src/trusted_router/__init__.py": "from . import policy\n",
        "src/trusted_router/catalog.py": "from . import pricing\n",
        "src/trusted_router/pricing.py": "from .catalog import value\n",
        "src/trusted_router/policy.py": "",
        "src/trusted_router/unrelated.py": "",
    })
    closure = ImportClosure(tmp_path)
    closure.add_file("scripts/generate.py")
    closure.follow()
    assert closure.files == {WORKFLOW} | {
        p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*.py")
        if p.name != "unrelated.py"
    }
    assert closure.directories == {"scripts/providers"}


@pytest.mark.parametrize("source", [
    "import trusted_router.missing", "from .missing import value",
    "import importlib; importlib.import_module(module_name)", "invalid python syntax",
    'import importlib; importlib.import_module(".relative", __package__)',
    'import importlib; importlib.import_module(f"")',
])
def test_unresolved_or_broken_dependencies_fail_closed(tmp_path: Path, source: str) -> None:
    write_sources(tmp_path, {"scripts/generate.py": source})
    closure = ImportClosure(tmp_path)
    closure.add_file("scripts/generate.py")
    with pytest.raises((Uncertain, SyntaxError)):
        closure.follow()


@pytest.mark.parametrize("paths", [
    ["src/trusted_router/data/provider_models_backup/provider.json"],
    ["src/trusted_router/static/og/providers_backup/provider.png"],
    ["scripts/unknown.sh"], ["docs/guide.py"], ["docs/config.json"], ["README.md"],
    [".github/workflows/other.yml"], ["scripts/conftest.py"], ["conftest.py"],
    ["src/trusted_router/added_or_deleted.py"], ["/docs/guide.md"], ["docs/../uv.lock"],
    # --no-renames supplies both sides: neither a delete nor a rename hides a dependency.
    ["src/trusted_router/model_aliases.py"],
    ["src/trusted_router/model_aliases.py", "docs/moved.md"],
    ["docs/moved.md", "src/trusted_router/model_aliases.py"],
])
def test_default_deny_and_rename_inputs(paths: list[str]) -> None:
    assert publish_decision(paths) == "recompute"
