"""Run each provider parser against its captured HTML fixture.

Fixtures under `tests/fixtures/pricing/<slug>.html` are real pages
fetched on 2026-05-08 with a Linux/Chrome UA. The parsers self-test
here so a regression on the parser breaks CI even before the hourly
refresh workflow runs the parser against the live page.

When a parser is rewritten by the self-heal LLM in production, the
fixture stays the same — these tests verify the *initial* parser
implementation. Re-capture the fixtures (run `curl` against the
provider URL) to update the floor for future regressions.

For providers whose page is JS-rendered (kimi, zai, openai, gemini),
the parser is a hardcoded constants table; these tests verify the
table is non-empty and well-shaped.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "pricing"


# Per-provider expectations. `expected_models` is a STRICT subset that
# the parser MUST return; extra models are allowed (provider added a
# new SKU). Price ranges are loose ($-bands) so a normal up-or-down
# move on the live page doesn't break tests; a price out of band
# means the parser misread the column.
@pytest.mark.parametrize(
    "slug,expected_models,price_band",
    [
        (
            "anthropic",
            {
                "anthropic/claude-opus-4.7",
                "anthropic/claude-sonnet-4.6",
                "anthropic/claude-haiku-4.5",
            },
            (0.5, 100.0),  # $0.50/M to $100/M
        ),
        (
            "cerebras",
            {
                "openai/gpt-oss-120b",
                "z-ai/glm-4.7",
            },
            (0.05, 5.00),
        ),
        (
            "gemini",
            {
                "google/gemini-2.5-flash",
                "google/gemini-2.5-pro",
                "google/gemini-3.5-flash",
            },
            (0.05, 20.00),
        ),
        (
            "mistral",
            {
                "mistralai/mistral-medium-3-5",
                "mistralai/devstral-medium",
            },
            (0.05, 10.00),
        ),
        (
            "deepseek",
            {
                "deepseek/deepseek-v4-flash",
                "deepseek/deepseek-v4-pro",
            },
            (0.05, 5.00),
        ),
        (
            "openai",
            {
                "openai/gpt-5.5",
                "openai/gpt-5.4",
                "openai/gpt-5.4-mini",
            },
            (0.05, 300.0),  # gpt-5.4-pro completion is $180/M
        ),
        (
            "kimi",
            {"moonshotai/kimi-k2.6"},
            (0.05, 10.0),
        ),
        (
            "zai",
            {"z-ai/glm-4.6", "z-ai/glm-4.5"},
            (0.0, 15.0),  # GLM-4.5-X completion = $8.9/M; GLM-4.5-Flash is $0
        ),
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
        (
            "grok",
            {"x-ai/grok-4.3"},
            (0.05, 5.0),
        ),
        (
            "novita",
            {"deepseek/deepseek-v4-flash"},
            (0.0, 20.0),  # Long tail of open-weight models with very low rates
        ),
        (
            "phala",
            {"qwen/qwen3.5-27b"},
            (0.0, 5.0),  # Embeddings can be free; chat models cap ~$2-3/M
        ),
        (
            "siliconflow",
            {"deepseek/deepseek-v4-flash", "qwen/qwen3-vl-32b-instruct"},
            (0.0, 20.0),
        ),
        (
            "venice",
            {"z-ai/glm-4.6"},
            (0.0, 10.0),
        ),
    ],
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
    result = module.parse(html)

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
                f"{slug}: {or_id} tiers[{tier_idx}].prompt ${prompt_d} "
                f"outside ${lo}-${hi} band"
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


@pytest.mark.parametrize(
    "slug",
    ["anthropic", "cerebras", "gemini", "mistral", "deepseek", "openai", "kimi", "zai"],
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
                    f"{slug}: {or_id} tiers[{idx}].max_prompt_tokens "
                    f"must be int or None"
                )
                assert isinstance(tier["prompt_micro_per_m"], int)
                assert isinstance(tier["completion_micro_per_m"], int)
                if "prompt_cached_micro_per_m" in tier:
                    cached = tier["prompt_cached_micro_per_m"]
                    assert cached is None or isinstance(cached, int)
            assert tiers[-1]["max_prompt_tokens"] is None, (
                f"{slug}: {or_id} last tier must have "
                "max_prompt_tokens=None (uncapped)"
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
    }
