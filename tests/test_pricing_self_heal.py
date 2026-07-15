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

import textwrap
from pathlib import Path

import pytest

from scripts.pricing import base as pricing_base
from scripts.pricing import refresh


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
    monkeypatch.setattr(
        pricing_base, "fetch_html", lambda url, extra_headers=None: html
    )


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


@pytest.mark.parametrize(
    "fixture_result",
    [
        "return {}",
        "return {'test/model': {'prompt_micro_per_m': 9_000_000, "
        "'completion_micro_per_m': 2_000_000}}",
    ],
    ids=["removes-known-row", "changes-known-price"],
)
def test_self_heal_rejects_captured_fixture_regression(
    tmp_parser: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixture_result: str,
) -> None:
    original = textwrap.dedent(
        """
        def parse(html: str) -> dict:
            if "captured-layout" not in html:
                return {}
            return {
                "test/model": {
                    "prompt_micro_per_m": 1_000_000,
                    "completion_micro_per_m": 2_000_000,
                }
            }
        """
    ).strip() + "\n"
    tmp_parser.write_text(original, encoding="utf-8")
    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()
    (fixture_dir / "testslug.html").write_text(
        "captured-layout", encoding="utf-8"
    )
    monkeypatch.setattr(pricing_base, "PRICING_FIXTURES_DIR", fixture_dir)
    _stub_fetch_html(monkeypatch, "live-layout")
    candidate = textwrap.dedent(
        f"""
        def parse(html: str) -> dict:
            if "live-layout" in html:
                return {{
                    "test/model": {{
                        "prompt_micro_per_m": 1_000_000,
                        "completion_micro_per_m": 2_000_000,
                    }}
                }}
            {fixture_result}
        """
    ).strip() + "\n"
    _stub_self_heal(monkeypatch, candidate)

    with pytest.raises(RuntimeError, match="fixture regression"):
        pricing_base.fetch_provider(
            slug="testslug",
            url="https://example.com/pricing",
            expected_models=["test/model"],
        )

    assert tmp_parser.read_text(encoding="utf-8") == original


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


# ------------------------------------------------------------------
# ID cross-check tests (refresh._cross_check_ids)
# ------------------------------------------------------------------


def _fake_or_snapshot(model_endpoints: list[tuple[str, str]]) -> dict:
    """Build a minimal OR-snapshot-shaped dict from (model_id, slug) tuples."""
    by_model: dict[str, list[str]] = {}
    for model_id, slug in model_endpoints:
        by_model.setdefault(model_id, []).append(slug)
    models = []
    for model_id, slugs in by_model.items():
        models.append(
            {
                "id": model_id,
                "endpoints": [{"tr_provider_slug": s} for s in slugs],
            }
        )
    return {"models": models}


def test_cross_check_ids_flags_or_models_missing_from_parser() -> None:
    or_snapshot = _fake_or_snapshot(
        [
            ("anthropic/claude-opus-4.7", "anthropic"),
            ("anthropic/claude-haiku-4.5", "anthropic"),
            ("anthropic/claude-sonnet-4.6", "anthropic"),
        ]
    )
    results = {
        "anthropic": pricing_base.ProviderPricingResult(
            slug="anthropic",
            prices={
                "anthropic/claude-opus-4.7": pricing_base.ModelPrice(1, 1),
            },
            source="deterministic",
        ),
    }
    notes = refresh._cross_check_ids(results, or_snapshot)
    joined = "\n".join(notes)
    assert "OR knows" in joined
    assert "claude-haiku-4.5" in joined
    assert "claude-sonnet-4.6" in joined


def test_cross_check_ids_flags_parser_models_or_does_not_list() -> None:
    or_snapshot = _fake_or_snapshot(
        [("anthropic/claude-opus-4.7", "anthropic")]
    )
    results = {
        "anthropic": pricing_base.ProviderPricingResult(
            slug="anthropic",
            prices={
                "anthropic/claude-opus-4.7": pricing_base.ModelPrice(1, 1),
                "anthropic/claude-experimental-vapor": pricing_base.ModelPrice(1, 1),
            },
            source="deterministic",
        ),
    }
    notes = refresh._cross_check_ids(results, or_snapshot)
    joined = "\n".join(notes)
    assert "parser found" in joined
    assert "claude-experimental-vapor" in joined


def test_cross_check_ids_no_notes_on_perfect_match() -> None:
    or_snapshot = _fake_or_snapshot(
        [
            ("anthropic/claude-opus-4.7", "anthropic"),
            ("anthropic/claude-haiku-4.5", "anthropic"),
        ]
    )
    results = {
        "anthropic": pricing_base.ProviderPricingResult(
            slug="anthropic",
            prices={
                "anthropic/claude-opus-4.7": pricing_base.ModelPrice(1, 1),
                "anthropic/claude-haiku-4.5": pricing_base.ModelPrice(1, 1),
            },
            source="deterministic",
        ),
    }
    notes = refresh._cross_check_ids(results, or_snapshot)
    # All notes for slugs with no provider data should also be empty —
    # the function only emits notes when there's an actual mismatch.
    anthropic_notes = [n for n in notes if n.startswith("anthropic:")]
    assert anthropic_notes == []


def test_failed_provider_fallbacks_are_not_fatal_when_snapshot_has_prices() -> None:
    results = {
        "together": pricing_base.ProviderPricingResult(
            slug="together",
            prices={
                "meta-llama/llama-3.3-70b-instruct": pricing_base.ModelPrice(
                    1_040_000,
                    1_040_000,
                )
            },
            source="api",
        )
    }
    failures = [
        ("cerebras", "self-heal 502"),
        ("mistral", "self-heal 502"),
        ("venice", "self-heal 502"),
        ("novita", "self-heal 502"),
    ]
    snapshot = {
        "models": [
            {
                "id": "provider/model",
                "endpoints": [
                    {
                        "tr_provider_slug": slug,
                        "pricing": {
                            "prompt": "0.0000001",
                            "completion": "0.0000002",
                        },
                    }
                    for slug, _err in failures
                ],
            }
        ]
    }

    unrecovered = refresh._apply_stale_fallbacks(results, failures, snapshot)

    assert unrecovered == []
    assert results["together"].source == "api"
    assert {slug for slug, _err in failures}.issubset(results)
    assert {results[slug].source for slug, _err in failures} == {"stale_snapshot"}


def test_failed_provider_without_snapshot_price_still_counts_unrecovered() -> None:
    results: dict[str, pricing_base.ProviderPricingResult] = {}
    failures = [
        ("provider-with-snapshot", "temporary 502"),
        ("new-provider", "temporary 502"),
    ]
    snapshot = {
        "models": [
            {
                "id": "provider/model",
                "endpoints": [
                    {
                        "tr_provider_slug": "provider-with-snapshot",
                        "pricing": {
                            "prompt": "0.0000001",
                            "completion": "0.0000002",
                        },
                    }
                ],
            }
        ]
    }

    unrecovered = refresh._apply_stale_fallbacks(results, failures, snapshot)

    assert unrecovered == [("new-provider", "temporary 502")]
    assert results["provider-with-snapshot"].source == "stale_snapshot"
    assert "new-provider" not in results


def test_stale_snapshot_never_rewrites_provider_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[pricing_base.ProviderPricingResult] = []

    class FakeProvider:
        @staticmethod
        def write_provider_manifest(
            result: pricing_base.ProviderPricingResult,
        ) -> list[str]:
            calls.append(result)
            return ["manifest rewritten"]

    monkeypatch.setattr(refresh, "_import_provider", lambda _slug: FakeProvider)
    stale = pricing_base.ProviderPricingResult(
        slug="fireworks",
        prices={"provider/model": pricing_base.ModelPrice(1, 1)},
        source="stale_snapshot",
    )

    assert refresh._write_provider_manifests({"fireworks": stale}) == []
    assert calls == []
