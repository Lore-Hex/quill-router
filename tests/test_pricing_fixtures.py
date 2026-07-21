"""Run each provider parser against its captured HTML fixture.

Fixtures under `tests/fixtures/pricing/<slug>.html` are real pages
fetched on 2026-05-08 with a Linux/Chrome UA. The parsers self-test
here so a regression on the parser breaks CI even before the hourly
refresh workflow runs the parser against the live page.

When a parser is rewritten by the self-heal LLM in production, the
fixture stays the same — these tests verify the *initial* parser
implementation. Re-capture the fixtures (run `curl` against the
provider URL) to update the floor for future regressions.

Production feeds use provider-owned APIs, Markdown, or server-rendered HTML.
The shared normalizer is exercised here so fixtures follow the same path as
the hourly refresh.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from scripts.pricing import refresh
from scripts.pricing.base import ast_whitelist_check, normalize_parser_input

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "pricing"

PARSER_CASES: list[tuple[str, set[str], tuple[float, float]]] = [
    (
        "anthropic",
        {
            "anthropic/claude-opus-4.7",
            "anthropic/claude-sonnet-4.6",
            "anthropic/claude-haiku-4.5",
        },
        (0.5, 100.0),
    ),
    ("cerebras", {"openai/gpt-oss-120b", "z-ai/glm-4.7"}, (0.05, 5.0)),
    ("cohere", {"cohere/embed-v4.0"}, (0.0, 100.0)),
    (
        "gemini",
        {
            "google/gemini-2.5-flash",
            "google/gemini-2.5-pro",
            "google/gemini-3.5-flash",
            "google/gemini-3.6-flash",
        },
        (0.05, 20.0),
    ),
    (
        "mistral",
        {"mistralai/mistral-medium-3-5", "mistralai/devstral-medium"},
        (0.05, 10.0),
    ),
    (
        "deepseek",
        {"deepseek/deepseek-v4-flash", "deepseek/deepseek-v4-pro"},
        (0.05, 5.0),
    ),
    (
        "openai",
        {"openai/gpt-5.5", "openai/gpt-5.4", "openai/gpt-5.4-mini"},
        (0.05, 300.0),
    ),
    ("kimi", {"moonshotai/kimi-k2.6"}, (0.05, 10.0)),
    ("zai", {"z-ai/glm-4.6", "z-ai/glm-4.5"}, (0.0, 15.0)),
    (
        "fireworks",
        {
            "moonshotai/kimi-k2.6",
            "deepseek/deepseek-v4-pro",
            "z-ai/glm-5.2",
            "openai/gpt-oss-120b",
        },
        (0.05, 10.0),
    ),
    ("grok", {"x-ai/grok-4.3"}, (0.05, 5.0)),
    ("makora", {"deepseek/deepseek-v4-flash"}, (0.05, 10.0)),
    ("minimax", {"minimax/minimax-m3"}, (0.05, 5.0)),
    ("morph", {"z-ai/glm-5.2"}, (0.01, 20.0)),
    ("novita", {"deepseek/deepseek-v4-flash"}, (0.0, 20.0)),
    ("phala", {"qwen/qwen3.5-27b"}, (0.0, 5.0)),
    (
        "siliconflow",
        {"deepseek/deepseek-v4-flash", "qwen/qwen3-vl-32b-instruct"},
        (0.0, 20.0),
    ),
    ("streamlake", {"kwaipilot/kat-coder-pro-v2.5"}, (0.01, 20.0)),
    ("thinkingmachines", {"thinkingmachines/inkling"}, (0.5, 20.0)),
    ("venice", {"z-ai/glm-4.6"}, (0.0, 10.0)),
    ("voyage", {"voyage/voyage-4-large"}, (0.0, 100.0)),
    ("xiaomi", {"xiaomi/mimo-v2.5-pro"}, (0.001, 5.0)),
]


def test_every_pricing_parser_has_a_fixture_and_contract_case() -> None:
    parser_dir = Path(__file__).parents[1] / "scripts" / "pricing" / "parsers"
    parser_slugs = {path.stem for path in parser_dir.glob("*.py") if path.stem != "__init__"}
    case_slugs = {slug for slug, _models, _band in PARSER_CASES}

    assert case_slugs == parser_slugs
    assert not [
        slug for slug in sorted(parser_slugs) if not (FIXTURE_DIR / f"{slug}.html").exists()
    ]
    assert refresh._SELF_HEALING_PARSER_SLUGS == parser_slugs - {
        "cerebras",
        "makora",
        "phala",
    }


def test_provider_pricing_sources_do_not_depend_on_reader_proxies() -> None:
    provider_dir = Path(__file__).parents[1] / "scripts" / "pricing" / "providers"

    offenders = [
        path.name
        for path in provider_dir.glob("*.py")
        if "r.jina.ai" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []


@pytest.mark.parametrize("slug", [slug for slug, _models, _band in PARSER_CASES])
def test_every_pricing_parser_passes_the_self_heal_ast_policy(slug: str) -> None:
    parser_path = Path(__file__).parents[1] / "scripts" / "pricing" / "parsers" / f"{slug}.py"

    assert ast_whitelist_check(parser_path.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("slug", [slug for slug, _models, _band in PARSER_CASES])
def test_parser_result_is_stable_through_production_normalization(slug: str) -> None:
    fixture = (FIXTURE_DIR / f"{slug}.html").read_text(encoding="utf-8")
    module = importlib.import_module(f"scripts.pricing.parsers.{slug}")

    assert module.parse(normalize_parser_input(fixture)) == module.parse(fixture)


# Per-provider expectations. `expected_models` is a STRICT subset that
# the parser MUST return; extra models are allowed (provider added a
# new SKU). Price ranges are loose ($-bands) so a normal up-or-down
# move on the live page doesn't break tests; a price out of band
# means the parser misread the column.
@pytest.mark.parametrize(
    "slug,expected_models,price_band",
    PARSER_CASES,
)
def test_parser_extracts_expected_models_within_price_band(
    slug: str,
    expected_models: set[str],
    price_band: tuple[float, float],
) -> None:
    fixture = FIXTURE_DIR / f"{slug}.html"
    assert fixture.exists(), f"missing fixture: {fixture}"
    html = fixture.read_text(encoding="utf-8")
    module = importlib.import_module(f"scripts.pricing.parsers.{slug}")
    result = module.parse(normalize_parser_input(html))

    assert isinstance(result, dict), f"{slug}: parse() must return dict"
    assert result, f"{slug}: parse() returned empty dict"

    missing = expected_models - set(result.keys())
    assert not missing, f"{slug}: parser missed expected models: {missing}"

    lo, hi = price_band
    for or_id, row in result.items():
        # Each row is either flat (prompt_micro_per_m + completion_micro_per_m)
        # or tiered (tiers=[{max_prompt_tokens, prompt_micro_per_m, ...}, ...]).
        # Verify every tier's prices land in the band.
        tiers = _row_tiers(row)
        for tier_idx, (prompt_d, completion_d) in enumerate(tiers):
            assert lo <= prompt_d <= hi, (
                f"{slug}: {or_id} tiers[{tier_idx}].prompt ${prompt_d} outside ${lo}-${hi} band"
            )
            assert lo <= completion_d <= hi, (
                f"{slug}: {or_id} tiers[{tier_idx}].completion ${completion_d} "
                f"outside ${lo}-${hi} band"
            )


def _row_tiers(row: dict) -> list[tuple[float, float]]:
    """Return a list of (prompt_dollars_per_M, completion_dollars_per_M)
    for every tier in the row. Handles both flat and tiered shapes."""
    if "tiers" in row:
        return [
            (
                t["prompt_micro_per_m"] / 1_000_000,
                t["completion_micro_per_m"] / 1_000_000,
            )
            for t in row["tiers"]
        ]
    return [
        (
            row["prompt_micro_per_m"] / 1_000_000,
            row["completion_micro_per_m"] / 1_000_000,
        )
    ]


def test_novita_fixture_keeps_qwen_235b_prices_in_true_microdollars() -> None:
    """Novita's `/models` feed reports prices 100x smaller than its public
    pricing table. The parser source of truth must preserve true
    microdollars/M so the catalog does not collapse these rows to the
    global $0.01/M floor.
    """
    fixture = FIXTURE_DIR / "novita.html"
    html = fixture.read_text(encoding="utf-8")
    module = importlib.import_module("scripts.pricing.parsers.novita")
    result = module.parse(html)

    assert result["qwen/qwen3-235b-a22b-instruct-2507"] == {
        "prompt_micro_per_m": 90_000,
        "completion_micro_per_m": 580_000,
    }
    assert result["qwen/qwen3-235b-a22b-fp8"] == {
        "prompt_micro_per_m": 200_000,
        "completion_micro_per_m": 800_000,
    }


def test_novita_parser_keeps_hy3_free_row() -> None:
    module = importlib.import_module("scripts.pricing.parsers.novita")
    result = module.parse(
        """
Hunyuan

--

Model Name\tContext\tInput\tOutput\tActions
Hy3\t262,144\t
Free
\t
Free
\tMore
"""
    )

    assert result["tencent/hy3"] == {
        "prompt_micro_per_m": 0,
        "completion_micro_per_m": 0,
    }


def test_novita_parser_tracks_kimi_k3_launch_pricing() -> None:
    module = importlib.import_module("scripts.pricing.parsers.novita")
    result = module.parse(
        """
MoonshotAI

--

Model Name\tContext\tInput\tOutput\tActions
Kimi K3\t1,048,576\t$3 /Mt· Cache Read $0.3 /Mt\t$15 /Mt\tMore
"""
    )

    assert result["moonshotai/kimi-k3"] == {
        "prompt_micro_per_m": 3_000_000,
        "completion_micro_per_m": 15_000_000,
        "prompt_cached_micro_per_m": 300_000,
    }


def test_novita_parser_reads_row_local_prices_from_next_catalog() -> None:
    module = importlib.import_module("scripts.pricing.parsers.novita")
    catalog = [
        {
            "type": "Chat",
            "id": "deepseek/deepseek-v4-flash",
            "displayName": "Deepseek V4 Flash",
            "status": 1,
            "infos": {
                "inputPricing": "$$0.14/Mt",
                "outputPricing": "$$0.28/Mt",
                "cacheReadPricing": "$$0.028/Mt",
            },
        },
        {
            "type": "Chat",
            "id": "moonshotai/kimi-k3",
            "displayName": "Kimi K3",
            "status": 1,
            "infos": {
                "inputPricing": "$$3/Mt",
                "outputPricing": "$$15/Mt",
                "cacheReadPricing": "$$0.3/Mt",
            },
        },
        {
            "type": "Chat",
            "id": "qwen/qwen4-next",
            "displayName": "Qwen4 Next",
            "status": 1,
            "input_pricing": {"pricePerM": 1250},
            "output_pricing": {"pricePerM": 8750},
        },
    ]
    flight = f'11:["$",{{"initialFullLLMModels":{json.dumps(catalog)}}}]\n'
    push = f"self.__next_f.push({json.dumps([1, flight])})"
    result = module.parse(
        normalize_parser_input(
            "<html><body>"
            f"<script>{push}</script>"
            "<table><tr><td>Deepseek V4 Flash</td><td>1M</td>"
            "<td>$0.14 /Mt</td><td>$0.28 /Mt</td></tr>"
            "<tr><td>Kimi K3</td><td>1M</td>"
            "<td>$3 /Mt</td><td>$15 /Mt</td></tr></table>"
            "</body></html>"
        )
    )

    assert result["deepseek/deepseek-v4-flash"] == {
        "prompt_micro_per_m": 140_000,
        "completion_micro_per_m": 280_000,
        "prompt_cached_micro_per_m": 28_000,
    }
    assert result["moonshotai/kimi-k3"] == {
        "prompt_micro_per_m": 3_000_000,
        "completion_micro_per_m": 15_000_000,
        "prompt_cached_micro_per_m": 300_000,
    }
    assert result["qwen/qwen4-next"] == {
        "prompt_micro_per_m": 125_000,
        "completion_micro_per_m": 875_000,
    }


def test_gemini_parser_tracks_gemini_36_flash_standard_pricing() -> None:
    module = importlib.import_module("scripts.pricing.parsers.gemini")
    result = module.parse((FIXTURE_DIR / "gemini.html").read_text(encoding="utf-8"))

    assert result["google/gemini-3.6-flash"] == {
        "prompt_micro_per_m": 1_500_000,
        "completion_micro_per_m": 7_500_000,
        "prompt_cached_micro_per_m": 150_000,
    }


def test_gemini_parser_normalizes_future_stable_release_heading() -> None:
    module = importlib.import_module("scripts.pricing.parsers.gemini")
    result = module.parse(
        """
## Gemini 3.7 Flash
|  | Free Tier | Paid Tier, per 1M tokens in USD |
| --- | --- | --- |
| Input price | Free of charge | $0.40 |
| Output price | Free of charge | $2.50 |
| Context caching price | Not available | $0.04 |
"""
    )

    assert result["google/gemini-3.7-flash"] == {
        "prompt_micro_per_m": 400_000,
        "completion_micro_per_m": 2_500_000,
        "prompt_cached_micro_per_m": 40_000,
    }


def test_gemini_parser_reads_official_server_rendered_html() -> None:
    module = importlib.import_module("scripts.pricing.parsers.gemini")
    result = module.parse(
        """
<html><body>
  <h2 id="gemini-3.7-flash">Gemini 3.7 Flash</h2>
  <section><h3>Standard</h3><table class="pricing-table"><tbody>
    <tr><td>Input price</td><td>Free of charge</td><td>$0.40</td></tr>
    <tr><td>Output price</td><td>Free of charge</td><td>$2.50</td></tr>
    <tr><td>Context caching price</td><td>Free of charge</td><td>$0.04<br>$1 / hour</td></tr>
  </tbody></table></section>
  <section><h3>Batch</h3><table><tbody>
    <tr><td>Input price</td><td>Not available</td><td>$0.20</td></tr>
    <tr><td>Output price</td><td>Not available</td><td>$1.25</td></tr>
  </tbody></table></section>
</body></html>
"""
    )

    assert result["google/gemini-3.7-flash"] == {
        "prompt_micro_per_m": 400_000,
        "completion_micro_per_m": 2_500_000,
        "prompt_cached_micro_per_m": 40_000,
    }


@pytest.mark.parametrize(
    "slug",
    [slug for slug, _models, _band in PARSER_CASES],
)
def test_parser_returns_well_shaped_dict(slug: str) -> None:
    """Schema check: every value is a dict with exactly the two int keys."""
    fixture = FIXTURE_DIR / f"{slug}.html"
    html = fixture.read_text(encoding="utf-8")
    module = importlib.import_module(f"scripts.pricing.parsers.{slug}")
    result = module.parse(html)
    for or_id, row in result.items():
        assert isinstance(or_id, str) and or_id, f"{slug}: bad model id: {or_id!r}"
        assert isinstance(row, dict), f"{slug}: row must be dict for {or_id}"
        if "tiers" in row:
            # Tiered shape: list of tier dicts with max_prompt_tokens +
            # prompt_micro_per_m + completion_micro_per_m. Last tier
            # MUST have max_prompt_tokens=None (uncapped fallback).
            tiers = row["tiers"]
            assert isinstance(tiers, list) and tiers, (
                f"{slug}: {or_id} tiers must be non-empty list"
            )
            _required = {"max_prompt_tokens", "prompt_micro_per_m", "completion_micro_per_m"}
            _allowed = _required | {"prompt_cached_micro_per_m"}
            for idx, tier in enumerate(tiers):
                assert isinstance(tier, dict)
                assert _required <= set(tier.keys()) <= _allowed, (
                    f"{slug}: {or_id} tiers[{idx}] has unexpected keys: {tier.keys()}"
                )
                threshold = tier["max_prompt_tokens"]
                assert threshold is None or isinstance(threshold, int), (
                    f"{slug}: {or_id} tiers[{idx}].max_prompt_tokens must be int or None"
                )
                assert isinstance(tier["prompt_micro_per_m"], int)
                assert isinstance(tier["completion_micro_per_m"], int)
                if "prompt_cached_micro_per_m" in tier:
                    cached = tier["prompt_cached_micro_per_m"]
                    assert cached is None or isinstance(cached, int)
            assert tiers[-1]["max_prompt_tokens"] is None, (
                f"{slug}: {or_id} last tier must have max_prompt_tokens=None (uncapped)"
            )
        else:
            _required = {"prompt_micro_per_m", "completion_micro_per_m"}
            _allowed = _required | {"prompt_cached_micro_per_m"}
            assert _required <= set(row.keys()) <= _allowed, (
                f"{slug}: {or_id} row has unexpected keys: {row.keys()}"
            )
            assert isinstance(row["prompt_micro_per_m"], int)
            assert isinstance(row["completion_micro_per_m"], int)
            assert row["prompt_micro_per_m"] >= 0
            assert row["completion_micro_per_m"] >= 0
            if "prompt_cached_micro_per_m" in row:
                cached = row["prompt_cached_micro_per_m"]
                assert cached is None or isinstance(cached, int)
                if isinstance(cached, int):
                    assert cached >= 0


def test_grok_parser_extracts_grok_45_current_pricing_row() -> None:
    from scripts.pricing.parsers import grok

    result = grok.parse(
        """
| Model | Context | Input | Cached input | Output |
| --- | --- | --- | --- | --- |
| [grok-4.5](https://docs.x.ai/developers/models/grok-4.5) | 500k | $2.00 | $0.50 | $6.00 |
"""
    )

    assert result["x-ai/grok-4.5"] == {
        "prompt_micro_per_m": 2_000_000,
        "completion_micro_per_m": 6_000_000,
        "prompt_cached_micro_per_m": 500_000,
    }


def test_grok_parser_extracts_current_long_context_tiers_and_future_names() -> None:
    from scripts.pricing.parsers import grok

    result = grok.parse(
        """
| grok-6 (< 200k prompt tokens) | 1M | $1.00 | $0.10 | $3.00 |
| grok-6 (≥ 200k prompt tokens) | 1M | $2.00 | $0.20 | $6.00 |
"""
    )

    assert result["x-ai/grok-6"]["tiers"] == [
        {
            "max_prompt_tokens": 200_000,
            "prompt_micro_per_m": 1_000_000,
            "completion_micro_per_m": 3_000_000,
            "prompt_cached_micro_per_m": 100_000,
        },
        {
            "max_prompt_tokens": None,
            "prompt_micro_per_m": 2_000_000,
            "completion_micro_per_m": 6_000_000,
            "prompt_cached_micro_per_m": 200_000,
        },
    ]


def test_siliconflow_parser_reads_server_rendered_framer_card() -> None:
    from scripts.pricing.parsers import siliconflow

    result = siliconflow.parse(
        """
<div data-framer-name="LLM - Desktop">
  <span>GLM-6</span><span>1049K</span>
  <span>$</span><span>1.25</span><span>$</span><span>0.20</span>
  <span>$</span><span>4.50</span><span>Details</span>
</div>
"""
    )

    assert result["z-ai/glm-6"] == {
        "prompt_micro_per_m": 1_250_000,
        "completion_micro_per_m": 4_500_000,
        "prompt_cached_micro_per_m": 200_000,
    }
