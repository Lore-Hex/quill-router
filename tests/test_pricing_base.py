"""Tests for scripts/pricing/base.py — validation, AST whitelist, sandbox.

The self-heal flow itself is covered in test_pricing_self_heal.py with
mocked LLM responses. This file focuses on the standalone primitives.
"""

from __future__ import annotations

import httpx
import pytest

from scripts.pricing import base as pricing_base
from scripts.pricing.base import (
    MAX_PRICE_MICRO_PER_M,
    ModelPrice,
    _coerce_to_model_prices,
    _has_sensitive_headers,
    _safe_log_url,
    apply_required_model_price_aliases,
    ast_whitelist_check,
    guard_manifest_prune,
    normalize_parser_input,
    reconcile_manifest_tombstones,
    safe_exception_summary,
    sandbox_run_parser,
    validate,
)


def test_tombstone_reconciliation_preserves_operator_hold_across_relist() -> None:
    held = {
        "id": "provider/held",
        "routable": False,
        "routable_reason": "production-entitlement-required",
        "old_metadata": True,
    }
    live = {
        "id": "provider/held",
        "routable": True,
        "new_metadata": True,
    }

    present = reconcile_manifest_tombstones(
        [held],
        {"provider/held": live},
        priced_ids={"provider/held"},
        source="api",
    )[0]
    assert present["new_metadata"] is True
    assert present["routable"] is False
    assert present["routable_reason"] == "production-entitlement-required"

    first_miss = reconcile_manifest_tombstones(
        [present],
        {},
        priced_ids=set(),
        source="api",
        missing_date="2026-08-23",
    )[0]
    second_miss = reconcile_manifest_tombstones(
        [first_miss],
        {},
        priced_ids=set(),
        source="api",
        missing_date="2026-08-24",
    )[0]
    assert second_miss["routable_reason"] == "production-entitlement-required"

    relisted = reconcile_manifest_tombstones(
        [second_miss],
        {"provider/held": live},
        priced_ids={"provider/held"},
        source="api",
    )[0]
    assert relisted["routable"] is False
    assert relisted["routable_reason"] == "production-entitlement-required"
    assert "missing_since" not in relisted


def test_tombstone_reconciliation_preserves_new_unpriced_operator_hold() -> None:
    held = {
        "id": "provider/free-trial",
        "routable": False,
        "routable_reason": "zero-price-unbillable",
        "unresolved_since": "2026-08-25",
    }

    result = reconcile_manifest_tombstones(
        [],
        {"provider/free-trial": held},
        priced_ids=set(),
        source="api",
        missing_date="2026-08-26",
    )

    assert result == [
        {
            "id": "provider/free-trial",
            "routable": False,
            "routable_reason": "zero-price-unbillable",
        }
    ]


def test_authenticated_provider_headers_disable_redirects_by_default() -> None:
    assert _has_sensitive_headers({"Authorization": "Bearer secret"}) is True
    assert _has_sensitive_headers({"x-api-key": "secret"}) is True
    assert _has_sensitive_headers({"x-goog-api-key": "secret"}) is True
    assert _has_sensitive_headers({"Accept": "application/json"}) is False


def test_provider_log_url_drops_query_and_fragment() -> None:
    assert (
        _safe_log_url("https://provider.example/v1/models?key=secret#fragment")
        == "https://provider.example/v1/models"
    )
    assert (
        _safe_log_url("https://user:token@provider.example/v1/models")
        == "https://provider.example/v1/models"
    )


def test_provider_exception_summary_drops_signed_query_parameters() -> None:
    summary = safe_exception_summary(
        RuntimeError(
            "fetch failed at https://provider.example/v1/models?signature=secret-token"
        )
    )

    assert summary == "RuntimeError"
    assert "signature" not in summary
    assert "secret-token" not in summary

    request = httpx.Request(
        "GET", "https://user:token@provider.example/v1/models?signature=secret-token"
    )
    response = httpx.Response(401, request=request)
    http_error = httpx.HTTPStatusError("invalid api key secret-token", request=request, response=response)
    assert safe_exception_summary(http_error) == (
        "HTTPStatusError status=401 url=https://provider.example/v1/models"
    )


def test_provider_exception_summary_keeps_only_safe_diagnostic_categories() -> None:
    assert safe_exception_summary(
        RuntimeError("provider: no priced chat models discovered")
    ) == "RuntimeError category=no_supported_models"
    assert safe_exception_summary(
        RuntimeError("PROVIDER_API_KEY is required for discovery")
    ) == "RuntimeError category=missing_credentials"

    request = httpx.Request(
        "GET", "https://provider.example/v1/models?signature=secret-token"
    )
    response = httpx.Response(401, request=request)
    cause = httpx.HTTPStatusError(
        "invalid api key secret-token", request=request, response=response
    )
    wrapped = RuntimeError("provider discovery failed")
    wrapped.__cause__ = cause
    assert safe_exception_summary(wrapped) == (
        "RuntimeError cause=HTTPStatusError status=401 "
        "url=https://provider.example/v1/models"
    )


def test_fetch_json_never_redirects_authenticated_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, bool] = {}

    class FakeResponse:
        is_redirect = False
        headers: dict[str, str] = {}

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, list[object]]:
            return {"models": []}

    class FakeClient:
        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    def fake_client(*, follow_redirects: bool = True) -> FakeClient:
        seen["follow_redirects"] = follow_redirects
        return FakeClient()

    monkeypatch.setattr(pricing_base, "_provider_client", fake_client)
    monkeypatch.setattr(
        pricing_base,
        "_get_with_retries",
        lambda _client, _url, _headers: FakeResponse(),
    )

    assert pricing_base.fetch_json(
        "https://provider.example/v1/models",
        extra_headers={"Authorization": "Bearer secret"},
        follow_redirects=True,
    ) == {"models": []}
    assert seen["follow_redirects"] is False


def test_signed_query_disables_redirects_without_auth_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, bool] = {}

    class FakeResponse:
        is_redirect = False
        headers: dict[str, str] = {}

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, list[object]]:
            return {"models": []}

    class FakeClient:
        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    def fake_client(*, follow_redirects: bool = True) -> FakeClient:
        seen["follow_redirects"] = follow_redirects
        return FakeClient()

    monkeypatch.setattr(pricing_base, "_provider_client", fake_client)
    monkeypatch.setattr(
        pricing_base,
        "_get_with_retries",
        lambda _client, _url, _headers: FakeResponse(),
    )

    pricing_base.fetch_json(
        "https://provider.example/v1/models?signature=sensitive",
        follow_redirects=True,
    )
    assert seen["follow_redirects"] is False


def test_explicit_redirect_disable_is_honored_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_urls: list[str] = []
    response = httpx.Response(
        301,
        headers={"location": "https://other.example/models"},
        request=httpx.Request("GET", "https://provider.example/models"),
    )

    class FakeClient:
        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(pricing_base, "_provider_client", lambda **_kwargs: FakeClient())

    def fake_get(_client: object, url: str, _headers: object) -> httpx.Response:
        seen_urls.append(url)
        return response

    monkeypatch.setattr(pricing_base, "_get_with_retries", fake_get)

    with pytest.raises(httpx.HTTPStatusError):
        pricing_base.fetch_json(
            "https://provider.example/models",
            follow_redirects=False,
        )
    assert seen_urls == ["https://provider.example/models"]


def test_redirects_never_downgrade_to_plaintext(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_urls: list[str] = []
    response = httpx.Response(
        301,
        headers={"location": "http://provider.example/models"},
        request=httpx.Request("GET", "https://provider.example/models"),
    )

    class FakeClient:
        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(pricing_base, "_provider_client", lambda **_kwargs: FakeClient())
    monkeypatch.setattr(
        pricing_base,
        "_get_with_retries",
        lambda _client, url, _headers: seen_urls.append(url) or response,
    )

    with pytest.raises(httpx.HTTPStatusError):
        pricing_base.fetch_json("https://provider.example/models")
    assert seen_urls == ["https://provider.example/models"]


def test_authenticated_html_follows_only_same_origin_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_urls: list[str] = []
    responses = [
        httpx.Response(
            301,
            headers={"location": "/canonical/models"},
            request=httpx.Request("GET", "https://provider.example/models"),
        ),
        httpx.Response(
            200,
            text="pricing",
            request=httpx.Request("GET", "https://provider.example/canonical/models"),
        ),
    ]

    class FakeClient:
        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(pricing_base, "_provider_client", lambda **_kwargs: FakeClient())

    def fake_get(_client: object, url: str, _headers: object) -> httpx.Response:
        seen_urls.append(url)
        return responses.pop(0)

    monkeypatch.setattr(pricing_base, "_get_with_retries", fake_get)

    assert pricing_base.fetch_html(
        "https://provider.example/models",
        extra_headers={"Authorization": "Bearer sensitive"},
    ) == "pricing"
    assert seen_urls == [
        "https://provider.example/models",
        "https://provider.example/canonical/models",
    ]


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


def test_required_price_aliases_expand_only_required_approved_ids() -> None:
    source = ModelPrice(
        130_000,
        280_000,
        prompt_cached_micro_per_m=28_000,
    )
    prices, applied = apply_required_model_price_aliases(
        {"deepseek/deepseek-v4-flash": source},
        frozenset({"deepseek/deepseek-v4-flash-0731"}),
        {
            "deepseek/deepseek-v4-flash-0731": "deepseek/deepseek-v4-flash",
            "deepseek/deepseek-v4-flash-0801": "deepseek/deepseek-v4-flash",
        },
    )

    dated = prices["deepseek/deepseek-v4-flash-0731"]
    assert dated == source
    assert dated is not source
    assert "deepseek/deepseek-v4-flash-0801" not in prices
    assert applied == [
        "deepseek/deepseek-v4-flash-0731 <- deepseek/deepseek-v4-flash"
    ]


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
