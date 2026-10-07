"""tokens.css is generated from the brand kit and never hand-edited; keep the two in step."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_builder():
    spec = importlib.util.spec_from_file_location(
        "build_tokens_css", ROOT / "scripts" / "build_tokens_css.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_committed_tokens_css_matches_the_brand_kit() -> None:
    builder = _load_builder()
    committed = (ROOT / "src/trusted_router/static/tokens.css").read_text()
    assert committed == builder.build(), (
        "static/tokens.css differs from the kit; run scripts/build_tokens_css.py"
    )
