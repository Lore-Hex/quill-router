from __future__ import annotations

import tomllib
from pathlib import Path


def test_markdown_negotiation_parser_is_a_runtime_dependency() -> None:
    """The production image installs project dependencies without the dev group."""
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    runtime_dependencies = pyproject["project"]["dependencies"]

    assert any(
        dependency.split(">=", 1)[0] == "beautifulsoup4"
        for dependency in runtime_dependencies
    )
