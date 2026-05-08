"""End-to-end test of the self-heal flow with mocked TR API.

We exercise `fetch_provider` with a stubbed `fetch_html`, a parser that
intentionally fails validation on the supplied HTML, and a mocked TR
chat-completions endpoint that returns a rewritten parser. The
expectation is that fetch_provider:
  1. Tries the broken parser, gets validation errors.
  2. Calls the (mocked) TR API.
  3. Runs ast_whitelist_check on the rewritten source (passes).
  4. Runs sandbox_run_parser on the rewritten source (passes).
  5. Validates the sandbox output (passes).
  6. Persists the rewritten parser to disk.
  7. Returns ProviderPricingResult(source="self_healed").
"""
from __future__ import annotations

import importlib
import os
import textwrap
from pathlib import Path

import pytest

from scripts.pricing import base as pricing_base


@pytest.fixture
def tmp_parser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a fake parser file in a tmpdir and redirect base.PARSERS_DIR
    to it. The new exec-based parser loader reads from disk every call,
    so no sys.modules shimming is needed."""
    parsers_dir = tmp_path / "parsers"
    parsers_dir.mkdir()
    monkeypatch.setattr(pricing_base, "PARSERS_DIR", parsers_dir)

    initial_src = textwrap.dedent(
        """
        def parse(html: str) -> dict:
            # Intentionally returns nothing — triggers validation failure.
            return {}
        """
    ).strip() + "\n"

    parser_file = parsers_dir / "testslug.py"
    parser_file.write_text(initial_src, encoding="utf-8")
    return parser_file


def _stub_fetch_html(monkeypatch: pytest.MonkeyPatch, html: str) -> None:
    monkeypatch.setattr(pricing_base, "fetch_html", lambda url: html)


def _stub_self_heal(monkeypatch: pytest.MonkeyPatch, returned_src: str) -> None:
    monkeypatch.setattr(
        pricing_base,
        "self_heal_parser",
        lambda *, slug, current_src, html, errors: returned_src,
    )


_VALID_REWRITE = textwrap.dedent(
    """
    def parse(html: str) -> dict:
        return {
            "test/model": {
                "prompt_micro_per_m": 1_000_000,
                "completion_micro_per_m": 2_000_000,
            }
        }
    """
).strip() + "\n"


def test_self_heal_happy_path_persists_new_parser(
    tmp_parser: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_fetch_html(monkeypatch, "<html>doesn't matter</html>")
    _stub_self_heal(monkeypatch, _VALID_REWRITE)

    result = pricing_base.fetch_provider(
        slug="testslug",
        url="https://example.com/pricing",
        expected_models=["test/model"],
    )

    assert result.source == "self_healed"
    assert "test/model" in result.prices
    assert result.prices["test/model"].prompt_micro_per_m == 1_000_000
    # New source written to disk.
    assert "test/model" in tmp_parser.read_text(encoding="utf-8")
    # Diff field populated.
    assert result.heal_diff and "test/model" in result.heal_diff


def test_self_heal_rejects_subprocess_import(
    tmp_parser: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_fetch_html(monkeypatch, "<html></html>")
    bad = textwrap.dedent(
        """
        import subprocess

        def parse(html: str) -> dict:
            return {"test/model": {"prompt_micro_per_m": 1, "completion_micro_per_m": 1}}
        """
    ).strip() + "\n"
    _stub_self_heal(monkeypatch, bad)

    with pytest.raises(RuntimeError, match="AST whitelist"):
        pricing_base.fetch_provider(
            slug="testslug",
            url="https://example.com/pricing",
            expected_models=["test/model"],
        )

    # On-disk file unchanged.
    assert "subprocess" not in tmp_parser.read_text(encoding="utf-8")


def test_self_heal_rejects_urllib_import(
    tmp_parser: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_fetch_html(monkeypatch, "<html></html>")
    bad = textwrap.dedent(
        """
        import urllib.request

        def parse(html: str) -> dict:
            return {"test/model": {"prompt_micro_per_m": 1, "completion_micro_per_m": 1}}
        """
    ).strip() + "\n"
    _stub_self_heal(monkeypatch, bad)

    with pytest.raises(RuntimeError, match="AST whitelist"):
        pricing_base.fetch_provider(
            slug="testslug",
            url="https://example.com/pricing",
            expected_models=["test/model"],
        )
    assert "urllib" not in tmp_parser.read_text(encoding="utf-8")


def test_self_heal_rejects_dunder_escape_hatch(
    tmp_parser: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_fetch_html(monkeypatch, "<html></html>")
    bad = textwrap.dedent(
        """
        def parse(html: str) -> dict:
            mod = ().__class__.__bases__[0].__subclasses__()
            return {"test/model": {"prompt_micro_per_m": 1, "completion_micro_per_m": 1}}
        """
    ).strip() + "\n"
    _stub_self_heal(monkeypatch, bad)

    with pytest.raises(RuntimeError, match="AST whitelist"):
        pricing_base.fetch_provider(
            slug="testslug",
            url="https://example.com/pricing",
            expected_models=["test/model"],
        )


def test_self_heal_rejects_output_outside_plausibility_range(
    tmp_parser: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_fetch_html(monkeypatch, "<html></html>")
    # Returns a price 10x above the plausibility ceiling.
    bad = textwrap.dedent(
        f"""
        def parse(html: str) -> dict:
            return {{
                "test/model": {{
                    "prompt_micro_per_m": {pricing_base.MAX_PRICE_MICRO_PER_M * 10},
                    "completion_micro_per_m": 1,
                }}
            }}
        """
    ).strip() + "\n"
    _stub_self_heal(monkeypatch, bad)

    with pytest.raises(RuntimeError, match="validation"):
        pricing_base.fetch_provider(
            slug="testslug",
            url="https://example.com/pricing",
            expected_models=["test/model"],
        )

    # On-disk parser stays at the original (broken-but-safe) version.
    assert "MAX_PRICE_MICRO_PER_M" not in tmp_parser.read_text(encoding="utf-8")


def test_self_heal_rejects_missing_parse_function(
    tmp_parser: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_fetch_html(monkeypatch, "<html></html>")
    bad = textwrap.dedent(
        """
        def not_parse(html: str) -> dict:
            return {}
        """
    ).strip() + "\n"
    _stub_self_heal(monkeypatch, bad)

    with pytest.raises(RuntimeError, match="AST whitelist"):
        pricing_base.fetch_provider(
            slug="testslug",
            url="https://example.com/pricing",
            expected_models=["test/model"],
        )


def test_self_heal_rejects_extra_method_signature(
    tmp_parser: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_fetch_html(monkeypatch, "<html></html>")
    bad = textwrap.dedent(
        """
        def parse(html, extra):
            return {}
        """
    ).strip() + "\n"
    _stub_self_heal(monkeypatch, bad)

    with pytest.raises(RuntimeError, match="AST whitelist"):
        pricing_base.fetch_provider(
            slug="testslug",
            url="https://example.com/pricing",
            expected_models=["test/model"],
        )
