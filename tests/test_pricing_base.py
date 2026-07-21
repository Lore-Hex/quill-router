"""Tests for scripts/pricing/base.py — validation, AST whitelist, sandbox.

The self-heal flow itself is covered in test_pricing_self_heal.py with
mocked LLM responses. This file focuses on the standalone primitives.
"""

from __future__ import annotations

from scripts.pricing.base import (
    MAX_PRICE_MICRO_PER_M,
    ModelPrice,
    _coerce_to_model_prices,
    ast_whitelist_check,
    guard_manifest_prune,
    normalize_parser_input,
    sandbox_run_parser,
    validate,
)


def test_normalize_parser_input_projects_official_html_without_scripts() -> None:
    html = r"""
    <html><body><h2>Pricing</h2>
    <table><tr><th>Model</th><th>Input</th></tr><tr><td>Model-X</td><td>\$0.20</td></tr></table>
    <script>secretNoise = 'ignore me'</script></body></html>
    """

    full = normalize_parser_input(html)
    compact = normalize_parser_input(html, include_raw_html=False)

    assert "<table>" in full
    assert "## Pricing" in full
    assert "| Model-X | $0.20 |" in compact
    assert "secretNoise" not in compact


def test_normalize_parser_input_unescapes_markdown_dollars() -> None:
    assert normalize_parser_input("| model | \\$0.20 |") == "| model | $0.20 |"

# ----------------------------------------------------------------------
# validate()
# ----------------------------------------------------------------------


def test_validate_passes_on_clean_input() -> None:
    prices = {
        "anthropic/claude-opus-4.7": ModelPrice(15_000_000, 75_000_000),
    }
    assert validate(prices, ["anthropic/claude-opus-4.7"]) == []


def test_validate_fails_on_empty_dict() -> None:
    errors = validate({}, [])
    assert any("empty" in e for e in errors)


def test_validate_warns_when_expected_model_missing(tmp_path, monkeypatch, capsys) -> None:  # noqa: ANN001
    summary_path = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_path))
    prices = {"foo/bar": ModelPrice(1_000_000, 1_000_000)}
    errors = validate(prices, ["expected/missing"])

    assert errors == []
    assert "expected/missing" in capsys.readouterr().err
    assert "expected/missing" in summary_path.read_text(encoding="utf-8")


def test_validate_fails_when_newly_discovered_required_model_is_missing() -> None:
    prices = {"foo/bar": ModelPrice(1_000_000, 1_000_000)}

    errors = validate(
        prices,
        [],
        required_models=["provider/new-model"],
    )

    assert errors == ["newly discovered models missing from parser output: ['provider/new-model']"]


def test_validate_fails_on_out_of_range_prompt_price() -> None:
    prices = {
        "x/y": ModelPrice(MAX_PRICE_MICRO_PER_M + 1, 1),
    }
    errors = validate(prices, [])
    assert any("outside" in e for e in errors)


def test_validate_fails_when_all_prices_zero() -> None:
    prices = {
        "x/y": ModelPrice(0, 0),
        "a/b": ModelPrice(0, 0),
    }
    errors = validate(prices, [])
    assert any("all prices are zero" in e for e in errors)


def test_validate_allows_one_zero_row_when_others_nonzero() -> None:
    prices = {
        "x/y": ModelPrice(0, 0),
        "a/b": ModelPrice(1_000_000, 2_000_000),
    }
    assert validate(prices, []) == []


def test_guard_manifest_prune_blocks_half_or_more_and_empty(tmp_path, monkeypatch, capsys) -> None:  # noqa: ANN001
    summary_path = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_path))
    old_rows = [{"id": "a"}, {"id": "b"}]

    assert guard_manifest_prune(old_rows, [{"id": "a"}], provider_slug="test") is old_rows
    assert guard_manifest_prune(old_rows, [], provider_slug="test") is old_rows
    stderr = capsys.readouterr().err
    assert stderr.count("mass-prune guard") == 2
    assert summary_path.read_text(encoding="utf-8").count("mass-prune guard") == 2


def test_guard_manifest_prune_allows_small_prune() -> None:
    old_rows = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    new_rows = [{"id": "a"}, {"id": "b"}]

    assert guard_manifest_prune(old_rows, new_rows) is new_rows


def test_guard_manifest_prune_keeps_delisted_rows_in_baseline() -> None:
    old_rows = [
        {
            "id": f"old-{index}",
            "routable": False,
            "routable_reason": "delisted-upstream",
        }
        for index in range(4)
    ] + [{"id": "live-a"}, {"id": "live-b"}]
    new_rows = [*old_rows[:-2], {"id": "live-a"}, {"id": "live-b", "routable": False}]

    assert guard_manifest_prune(old_rows, new_rows) is new_rows


# ----------------------------------------------------------------------
# _coerce_to_model_prices() — schema check on parser output
# ----------------------------------------------------------------------


def test_coerce_rejects_non_dict() -> None:
    out, errors = _coerce_to_model_prices("not a dict")
    assert out is None
    assert any("must return dict" in e for e in errors)


def test_coerce_rejects_non_string_model_id() -> None:
    out, errors = _coerce_to_model_prices(
        {123: {"prompt_micro_per_m": 1, "completion_micro_per_m": 1}}
    )
    assert out is None
    assert errors


def test_coerce_rejects_unexpected_chars_in_model_id() -> None:
    out, errors = _coerce_to_model_prices(
        {"foo$bar": {"prompt_micro_per_m": 1, "completion_micro_per_m": 1}}
    )
    assert out is None
    assert any("unexpected chars" in e for e in errors)


def test_coerce_rejects_bool_as_int() -> None:
    out, errors = _coerce_to_model_prices(
        {"x/y": {"prompt_micro_per_m": True, "completion_micro_per_m": 1}}
    )
    assert out is None


def test_coerce_accepts_valid_input() -> None:
    out, errors = _coerce_to_model_prices(
        {"x/y": {"prompt_micro_per_m": 100, "completion_micro_per_m": 200}}
    )
    assert errors == []
    assert out is not None
    assert out["x/y"].prompt_micro_per_m == 100
    assert out["x/y"].completion_micro_per_m == 200


# ----------------------------------------------------------------------
# ast_whitelist_check() — static gate on LLM-generated parser code
# ----------------------------------------------------------------------


_VALID_PARSER = '''
"""docstring"""
import re
from bs4 import BeautifulSoup

def parse(html: str) -> dict:
    return {"x/y": {"prompt_micro_per_m": 1, "completion_micro_per_m": 2}}
'''


def test_ast_whitelist_passes_clean_parser() -> None:
    assert ast_whitelist_check(_VALID_PARSER) == []


def test_ast_whitelist_allows_future_import_and_any_arg_name() -> None:
    """Regression for the self-heal freeze: the LLM-rewritten parsers emit
    `from __future__ import annotations` (idiomatic, present in every
    committed parser) and often name the arg `text`/`markdown` instead of
    `html`. Both used to fail the whitelist, so venice/novita/mistral could
    never self-heal and went stale hourly. `__future__` is a compile-time
    directive (no runtime import) and parse() is called positionally."""
    src = (
        "from __future__ import annotations\n"
        "import re\n\n"
        "def parse(markdown: str) -> dict:\n"
        '    return {"x/y": {"prompt_micro_per_m": 1, "completion_micro_per_m": 2}}\n'
    )
    assert ast_whitelist_check(src) == []


def test_ast_whitelist_rejects_subprocess_import() -> None:
    src = "import subprocess\n\ndef parse(html: str) -> dict:\n    return {}\n"
    errors = ast_whitelist_check(src)
    assert any("subprocess" in e for e in errors)


def test_ast_whitelist_rejects_urllib() -> None:
    src = "import urllib.request\n\ndef parse(html: str) -> dict:\n    return {}\n"
    errors = ast_whitelist_check(src)
    assert errors


def test_ast_whitelist_rejects_os() -> None:
    src = "import os\n\ndef parse(html: str) -> dict:\n    return {}\n"
    errors = ast_whitelist_check(src)
    assert errors


def test_ast_whitelist_rejects_exec_call() -> None:
    src = """
def parse(html: str) -> dict:
    exec("x = 1")
    return {}
"""
    errors = ast_whitelist_check(src)
    assert any("forbidden" in e for e in errors)


def test_ast_whitelist_rejects_open_call() -> None:
    src = """
def parse(html: str) -> dict:
    open('/etc/passwd')
    return {}
"""
    errors = ast_whitelist_check(src)
    assert errors


def test_ast_whitelist_rejects_dunder_attr_access() -> None:
    src = """
def parse(html: str) -> dict:
    return ().__class__.__bases__[0].__subclasses__()
"""
    errors = ast_whitelist_check(src)
    assert any("dunder" in e for e in errors)


def test_ast_whitelist_rejects_missing_parse_function() -> None:
    src = "def not_parse(html: str) -> dict:\n    return {}\n"
    errors = ast_whitelist_check(src)
    assert any("missing top-level function `parse`" in e for e in errors)


def test_ast_whitelist_rejects_wrong_parse_signature() -> None:
    src = "def parse(html, extra):\n    return {}\n"
    errors = ast_whitelist_check(src)
    assert any("exactly one positional arg" in e for e in errors)


def test_ast_whitelist_rejects_class_definition() -> None:
    src = """
class Foo:
    pass

def parse(html: str) -> dict:
    return {}
"""
    errors = ast_whitelist_check(src)
    assert any("class" in e for e in errors)


def test_ast_whitelist_rejects_async_function() -> None:
    src = """
async def parse(html: str) -> dict:
    return {}
"""
    errors = ast_whitelist_check(src)
    assert any("async" in e for e in errors)


def test_ast_whitelist_rejects_dynamic_getattr() -> None:
    src = """
def parse(html: str) -> dict:
    x = getattr(html, html)
    return {}
"""
    errors = ast_whitelist_check(src)
    assert any("getattr" in e for e in errors)


def test_ast_whitelist_allows_static_getattr() -> None:
    src = """
def parse(html: str) -> dict:
    x = getattr(html, "upper")
    return {"x/y": {"prompt_micro_per_m": 1, "completion_micro_per_m": 1}}
"""
    assert ast_whitelist_check(src) == []


def test_ast_whitelist_rejects_oversize_source() -> None:
    src = "def parse(html: str) -> dict:\n    return {}\n" + ("# pad\n" * 10000)
    errors = ast_whitelist_check(src)
    assert any("bytes" in e for e in errors)


# ----------------------------------------------------------------------
# sandbox_run_parser() — actually executes LLM-generated parser
# ----------------------------------------------------------------------


def test_sandbox_runs_valid_parser_and_returns_prices() -> None:
    src = """
def parse(html: str) -> dict:
    return {"x/y": {"prompt_micro_per_m": 1234, "completion_micro_per_m": 5678}}
"""
    prices, errors = sandbox_run_parser(src, "<html></html>")
    assert errors == []
    assert prices is not None
    assert prices["x/y"].prompt_micro_per_m == 1234
    assert prices["x/y"].completion_micro_per_m == 5678


def test_sandbox_runs_parser_with_future_import() -> None:
    """The wrapper must not precede compile-time future imports."""
    src = """from __future__ import annotations

def parse(markdown: str) -> dict:
    return {"x/y": {"prompt_micro_per_m": 1234, "completion_micro_per_m": 5678}}
"""
    prices, errors = sandbox_run_parser(src, "<html></html>")

    assert errors == []
    assert prices is not None
    assert prices["x/y"].prompt_micro_per_m == 1234


def test_sandbox_rejects_non_dict_return() -> None:
    src = """
def parse(html: str) -> dict:
    return "string"
"""
    prices, errors = sandbox_run_parser(src, "<html></html>")
    assert prices is None
    assert errors


def test_sandbox_propagates_runtime_errors() -> None:
    src = """
def parse(html: str) -> dict:
    raise RuntimeError("bang")
"""
    prices, errors = sandbox_run_parser(src, "<html></html>")
    assert prices is None
    assert errors


def test_sandbox_kills_infinite_loop_via_timeout() -> None:
    # 5s timeout should fire well before this test hits the pytest timeout
    src = """
def parse(html: str) -> dict:
    while True:
        pass
"""
    prices, errors = sandbox_run_parser(src, "<html></html>")
    assert prices is None
    assert any("timeout" in e for e in errors)
