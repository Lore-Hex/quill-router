from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from scripts.pricing.base import ModelPrice, ProviderPricingResult
from scripts.pricing.parsers.thinkingmachines import _SAMPLER_IDS, _SERVERLESS_IDS, parse
from scripts.pricing.providers import thinkingmachines
from tests.pinned_manifests import THINKINGMACHINES_GLM_5_3, serve_manifest_rows
from tests.provider_holds import row_on_unserved_route_hold
from trusted_router import provider_lifecycle
from trusted_router.catalog import MODEL_ENDPOINTS, MODELS, PROVIDERS

SERVERLESS = [
    {"tinker_id": "thinkingmachines/Inkling-Small:peft:262144:sampling-nvfp4",
     "input": "$0.30", "cached_input": "$0.06", "output": "$1.20"},
    {"tinker_id": "thinkingmachines/Inkling:peft:262144:sampling-nvfp4",
     "input": "$1.00", "cached_input": "$0.17", "output": "$4.05"},
]
# These feed-parsing tests price first-gen Inkling, which Tinker retires on
# 2026-12-14 (see test_thinkingmachines_december_2026_lifecycle.py for the
# refresh past that cutoff), so they read the feeds just before it.
_BEFORE_INKLING_RETIREMENT = (
    provider_lifecycle.THINKINGMACHINES_DECEMBER_14_RETIREMENT_AT - timedelta(days=1)
)


def _before_inkling_retirement(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _BEFORE_INKLING_RETIREMENT)


SAMPLER = [
    {"tinker_id": "zai-org/GLM-5.3:peft:262144", "prefill": "$4.86",
     "cached_prefill": "$0.972", "sample": "$12.15", "train": "$14.58",
     "active_params": 40000000000, "original_sample": "$99.99"},
    {"tinker_id": "thinkingmachines/Inkling-2-Small:peft:262144", "prefill": "$0.30",
     "cached_prefill": "$0.03", "sample": "$1.00", "train": "$3.00"},
    {"tinker_id": "thinkingmachines/Inkling-2-Mini:peft:262144", "prefill": "$0.15",
     "cached_prefill": "$0.015", "sample": "$0.50", "train": "$1.50"},
]


def test_fetch_uses_named_json_prices_not_html_column_positions(monkeypatch):
    feeds = {thinkingmachines.SERVERLESS_URL: SERVERLESS, thinkingmachines.SAMPLER_URL: SAMPLER}
    monkeypatch.setattr(thinkingmachines, "fetch_json", feeds.__getitem__)
    _before_inkling_retirement(monkeypatch)
    result = thinkingmachines.fetch()
    assert result.source == "api"
    assert result.prices["z-ai/glm-5.3"] == ModelPrice(4_860_000, 12_150_000, prompt_cached_micro_per_m=972_000)
    assert result.prices["thinkingmachines/inkling"] == ModelPrice(1_000_000, 4_050_000, prompt_cached_micro_per_m=170_000)
    assert result.prices["thinkingmachines/inkling-2-small"] == ModelPrice(300_000, 1_000_000, prompt_cached_micro_per_m=30_000)
    assert result.prices["thinkingmachines/inkling-2-mini"] == ModelPrice(150_000, 500_000, prompt_cached_micro_per_m=15_000)
    assert set(result.prices) == set(thinkingmachines.EXPECTED_MODELS)


@pytest.mark.parametrize("bad", [None, "$NaN", "$-1", "4.86", "$0", "$1.1234567"])
def test_json_rejects_missing_or_invalid_prices(bad):
    row = {**SAMPLER[0], "prefill": bad}
    with pytest.raises(RuntimeError, match="thinkingmachines"):
        thinkingmachines._json_prices([row], {row["tinker_id"]: "z-ai/glm-5.3"}, ("prefill", "cached_prefill", "sample"))


def test_json_rejects_conflicts_missing_models_and_excessive_cache():
    ids = {SAMPLER[0]["tinker_id"]: "z-ai/glm-5.3"}
    for rows in [[], {}, [SAMPLER[0], {**SAMPLER[0], "sample": "$99"}], [{**SAMPLER[0], "cached_prefill": "$5"}]]:
        with pytest.raises(RuntimeError, match="thinkingmachines"):
            thinkingmachines._json_prices(rows, ids, ("prefill", "cached_prefill", "sample"))


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "pricing"


def _live_feeds() -> dict[str, object]:
    # Captured 2026-11-25 from the official machine-readable feeds:
    # serverless.json in full (Inkling and Inkling-Small only), models.json
    # trimmed to the Inkling 2 Mini and Inkling 2 Small rows and the GLM-5.3
    # sampler row TR prices.
    return {
        thinkingmachines.SERVERLESS_URL: json.loads(
            (FIXTURE_DIR / "thinkingmachines_serverless_2026-11-25.json").read_text(encoding="utf-8")
        ),
        thinkingmachines.SAMPLER_URL: json.loads(
            (FIXTURE_DIR / "thinkingmachines_models_2026-11-25.json").read_text(encoding="utf-8")
        ),
    }


def test_fetch_prices_inkling_2_models_from_their_exact_sampler_tinker_ids(monkeypatch):
    # By 2026-11-25 serverless.json listed only Inkling and Inkling-Small, so
    # Inkling 2 Small is priced from models.json's row for the exact ID its
    # route sends, at the same published rates. Inkling 2 Mini is priced from
    # its paid 256K row.
    monkeypatch.setattr(thinkingmachines, "fetch_json", _live_feeds().__getitem__)
    _before_inkling_retirement(monkeypatch)
    result = thinkingmachines.fetch()

    assert result.prices == {
        "thinkingmachines/inkling-2-mini": ModelPrice(150_000, 500_000, prompt_cached_micro_per_m=15_000),
        "thinkingmachines/inkling-2-small": ModelPrice(300_000, 1_000_000, prompt_cached_micro_per_m=30_000),
        "thinkingmachines/inkling-small": ModelPrice(300_000, 1_200_000, prompt_cached_micro_per_m=60_000),
        "thinkingmachines/inkling": ModelPrice(1_000_000, 4_050_000, prompt_cached_micro_per_m=170_000),
        "z-ai/glm-5.3": ModelPrice(3_780_000, 12_150_000, prompt_cached_micro_per_m=756_000),
    }
    assert set(result.prices) == set(thinkingmachines.EXPECTED_MODELS)


INKLING_2_MINI_FREE_ID = "thinkingmachines/Inkling-2-Mini:peft:65536:free"


def test_free_inkling_2_mini_tier_is_never_priced_or_routed(monkeypatch):
    # Tinker's free row ($0.00, 64K, rate-limited, needs a positive balance)
    # is not a route TR can sell: it maps to no catalog model, so it can
    # neither price Inkling 2 Mini nor stand in for the missing paid row.
    feeds = _live_feeds()
    free = [row for row in feeds[thinkingmachines.SAMPLER_URL] if row["tinker_id"] == INKLING_2_MINI_FREE_ID]
    assert free and free[0]["sample"] == "$0.00"
    assert INKLING_2_MINI_FREE_ID not in {**_SERVERLESS_IDS, **_SAMPLER_IDS}
    rows = json.loads(thinkingmachines.MANIFEST_PATH.read_text(encoding="utf-8"))["models"]
    assert all(":free" not in row["upstream_id"] for row in rows)

    feeds[thinkingmachines.SAMPLER_URL] = [
        row for row in feeds[thinkingmachines.SAMPLER_URL]
        if row["tinker_id"] != "thinkingmachines/Inkling-2-Mini:peft:262144"
    ]
    monkeypatch.setattr(thinkingmachines, "fetch_json", feeds.__getitem__)
    with pytest.raises(RuntimeError, match=r"missing exact model prices: \['thinkingmachines/inkling-2-mini'\]"):
        thinkingmachines.fetch()


@pytest.mark.parametrize("tinker_id", [
    "thinkingmachines/Inkling-2-Small:peft:262144",
    "thinkingmachines/Inkling-2-Mini:peft:262144",
])
def test_inkling_2_routes_price_only_from_their_exact_ids(monkeypatch, tinker_id):
    # The gate stays exact: a feed that only carries a retired
    # ":sampling-nvfp4" spelling of the ID, or only the 64K base row, leaves
    # the route unpriced and fails the provider closed.
    model_id = _SAMPLER_IDS[tinker_id]
    for replacement in (f"{tinker_id}:sampling-nvfp4", tinker_id.removesuffix(":peft:262144")):
        feeds = _live_feeds()
        feeds[thinkingmachines.SAMPLER_URL] = [
            {**row, "tinker_id": replacement} if row["tinker_id"] == tinker_id else row
            for row in feeds[thinkingmachines.SAMPLER_URL]
            if row["tinker_id"] != replacement
        ]
        monkeypatch.setattr(thinkingmachines, "fetch_json", feeds.__getitem__)

        with pytest.raises(RuntimeError, match=rf"missing exact model prices: \['{model_id}'\]"):
            thinkingmachines.fetch()


def _with_extra_fields(feeds: dict[str, object], url: str, tinker_id: str, extra: dict) -> dict[str, object]:
    feeds[url] = [{**row, **extra} if row["tinker_id"] == tinker_id else row for row in feeds[url]]
    return feeds


INKLING_SMALL_SERVERLESS_ID = "thinkingmachines/Inkling-Small:peft:262144:sampling-nvfp4"
TIER = {"min_prompt_tokens": 128_000, "input": "$0.60", "cached_input": "$0.06", "output": "$2.00"}


@pytest.mark.parametrize("extra", [
    {"unit": "USD per 1000 tokens"},
    # Neither feed states a unit today, so even a per-million spelling is a
    # format change to review, not a value to guess at.
    {"unit": "USD per 1M tokens"},
    {"currency": "EUR"},
    {"tiers": [TIER]},
    {"ranges": [{"max_prompt_tokens": 128_000}]},
    {"max_prompt_tokens": 128_000},
])
def test_serverless_row_with_a_stated_unit_tier_or_condition_is_refused(monkeypatch, extra):
    feeds = _with_extra_fields(
        _live_feeds(), thinkingmachines.SERVERLESS_URL, INKLING_SMALL_SERVERLESS_ID, extra
    )
    monkeypatch.setattr(thinkingmachines, "fetch_json", feeds.__getitem__)

    with pytest.raises(RuntimeError, match=r"unreviewed pricing field\(s\) \['"):
        thinkingmachines.fetch()


@pytest.mark.parametrize("extra", [
    {"unit": "USD per 1000 tokens"},
    {"tiers": [{"min_prompt_tokens": 128_000, "prefill": "$7.56", "sample": "$24.30"}]},
    {"condition": "prompts up to 128K tokens"},
])
def test_sampler_row_with_a_stated_unit_tier_or_condition_is_refused(monkeypatch, extra):
    feeds = _with_extra_fields(_live_feeds(), thinkingmachines.SAMPLER_URL, "zai-org/GLM-5.3:peft:262144", extra)
    monkeypatch.setattr(thinkingmachines, "fetch_json", feeds.__getitem__)

    with pytest.raises(RuntimeError, match=r"unreviewed pricing field\(s\) \['"):
        thinkingmachines.fetch()


def test_documented_discount_fields_still_price_at_the_current_rate(monkeypatch):
    # models.json documents original_* fields and a note for temporarily
    # discounted models (eight live rows carry them); they stay readable.
    discount = {
        "note": "Limited-time 50% discount", "original_prefill": "$7.56",
        "original_cached_prefill": "$1.512", "original_sample": "$24.30", "original_train": "$29.16",
    }
    feeds = _with_extra_fields(_live_feeds(), thinkingmachines.SAMPLER_URL, "zai-org/GLM-5.3:peft:262144", discount)
    monkeypatch.setattr(thinkingmachines, "fetch_json", feeds.__getitem__)

    assert thinkingmachines.fetch().prices["z-ai/glm-5.3"] == ModelPrice(
        3_780_000, 12_150_000, prompt_cached_micro_per_m=756_000
    )


GLM_SAMPLER_ID = "zai-org/GLM-5.3:peft:262144"


@pytest.mark.parametrize("bad", [
    {"name": ""},
    {"name": {"en": "Inkling 2 Small"}},
    {"url": "http://huggingface.co/thinkingmachines/Inkling-2-Small"},
    {"url": "huggingface.co/thinkingmachines/Inkling-2-Small"},
    {"url": ["https://huggingface.co/thinkingmachines/Inkling-2-Small"]},
    {"url": "https://:443"},
    {"url": "https://user@"},
    {"url": "https:// "},
    {"url": "https://bad host/model"},
    {"url": "https://huggingface.co:abc/model"},
    {"url": "https://huggingface.co:65536/model"},
    {"context": {"unit": "tokens", "value": 1_048_576}},
    {"context": "1M tokens"},
    {"context": "1048576"},
    {"context": ["1M"]},
    {"input": {"amount": "$0.30", "unit": "per 1K tokens"}},
    {"cached_input": ["$0.03"]},
])
def test_serverless_row_with_a_malformed_documented_field_is_refused(monkeypatch, bad):
    feeds = _with_extra_fields(_live_feeds(), thinkingmachines.SERVERLESS_URL, INKLING_SMALL_SERVERLESS_ID, bad)
    monkeypatch.setattr(thinkingmachines, "fetch_json", feeds.__getitem__)

    with pytest.raises(RuntimeError, match=r"thinkingmachines: malformed pricing field\(s\) \['"):
        thinkingmachines.fetch()


def test_documented_empty_url_is_still_accepted(monkeypatch):
    # "The url field is an empty string when no model-card link is provided."
    feeds = _with_extra_fields(_live_feeds(), thinkingmachines.SERVERLESS_URL, INKLING_SMALL_SERVERLESS_ID, {"url": ""})
    monkeypatch.setattr(thinkingmachines, "fetch_json", feeds.__getitem__)
    assert thinkingmachines.fetch().prices["thinkingmachines/inkling-small"] == ModelPrice(
        300_000, 1_200_000, prompt_cached_micro_per_m=60_000
    )


@pytest.mark.parametrize("bad", [
    {"train": {"amount": "$14.58"}},
    {"params": "753B"},
    {"active_params": True},
    {"params": 0},
    {"size": ["Large"]},
    {"original_sample": ["$24.30"]},
    {"note": {"text": "Limited-time 50% discount", "applies_to": "prompts up to 128K tokens"}},
])
def test_sampler_row_with_a_malformed_documented_field_is_refused(monkeypatch, bad):
    feeds = _with_extra_fields(_live_feeds(), thinkingmachines.SAMPLER_URL, GLM_SAMPLER_ID, bad)
    monkeypatch.setattr(thinkingmachines, "fetch_json", feeds.__getitem__)

    with pytest.raises(RuntimeError, match=r"thinkingmachines: malformed pricing field\(s\) \['"):
        thinkingmachines.fetch()


@pytest.mark.parametrize("tinker_id", [
    "Inkling 2 Small",
    "thinkingmachines/Inkling-2-Small:peft:big",
    "thinkingmachines/Inkling-2-Small:sampling-nvfp4:peft:262144",
    "/Inkling-2-Small",
])
def test_priced_row_with_an_id_outside_the_tinker_grammar_is_refused(tinker_id):
    row = {"tinker_id": tinker_id, "input": "$0.30", "cached_input": "$0.03", "output": "$1.00"}
    with pytest.raises(RuntimeError, match=r"malformed pricing field\(s\) \['tinker_id'\]"):
        thinkingmachines._json_prices(
            [row], {tinker_id: "thinkingmachines/inkling-2-small"}, ("input", "cached_input", "output")
        )


# The note on every discounted row in the live models.json (2026-11-19).
LIVE_DISCOUNT_NOTES = {
    "nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-BF16": "Limited-time 50% discount",
    "nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-BF16:peft:262144": "Limited-time 50% discount",
    "nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16": "Limited-time 50% discount",
    "nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16:peft:262144": "Limited-time 50% discount",
    "nvidia/NVIDIA-Nemotron-3-Ultra-550B-A55B-BF16": "Limited-time 50% discount",
    "nvidia/NVIDIA-Nemotron-3-Ultra-550B-A55B-BF16:peft:262144": "Limited-time 50% discount",
    "nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-BF16": "Limited-time 50% discount",
    "nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-BF16:peft:262144": "Limited-time 50% discount",
}
ORIGINAL_PRICES = {
    "original_prefill": "$7.56", "original_cached_prefill": "$1.512",
    "original_sample": "$24.30", "original_train": "$29.16",
}


@pytest.mark.parametrize("note", sorted(set(LIVE_DISCOUNT_NOTES.values())))
def test_every_live_discount_note_is_accepted_with_its_original_prices(monkeypatch, note):
    assert len(LIVE_DISCOUNT_NOTES) == 8
    feeds = _with_extra_fields(
        _live_feeds(), thinkingmachines.SAMPLER_URL, GLM_SAMPLER_ID, {"note": note, **ORIGINAL_PRICES}
    )
    monkeypatch.setattr(thinkingmachines, "fetch_json", feeds.__getitem__)
    assert thinkingmachines.fetch().prices["z-ai/glm-5.3"] == ModelPrice(
        3_780_000, 12_150_000, prompt_cached_micro_per_m=756_000
    )


@pytest.mark.parametrize("note", [
    "Limited-time 50% discount for prompts up to 128K tokens",
    "Limited-time discount",
    "50% off",
    "Prices double above 128K context",
    "",
])
def test_any_other_note_is_refused(monkeypatch, note):
    feeds = _with_extra_fields(
        _live_feeds(), thinkingmachines.SAMPLER_URL, GLM_SAMPLER_ID, {"note": note, **ORIGINAL_PRICES}
    )
    monkeypatch.setattr(thinkingmachines, "fetch_json", feeds.__getitem__)
    with pytest.raises(RuntimeError, match=r"malformed pricing field\(s\) \['note'\]"):
        thinkingmachines.fetch()


@pytest.mark.parametrize("originals", [
    {},
    {"original_sample": "$24.30"},
    {key: value for key, value in ORIGINAL_PRICES.items() if key != "original_train"},
])
def test_discount_note_without_all_original_prices_is_refused(monkeypatch, originals):
    feeds = _with_extra_fields(
        _live_feeds(), thinkingmachines.SAMPLER_URL, GLM_SAMPLER_ID,
        {"note": "Limited-time 50% discount", **originals},
    )
    monkeypatch.setattr(thinkingmachines, "fetch_json", feeds.__getitem__)
    with pytest.raises(RuntimeError, match="note without original prices"):
        thinkingmachines.fetch()


def test_committed_manifest_upstream_ids_are_the_exact_priced_ids() -> None:
    # A refresh writes prices onto manifest rows by catalog id. Each priced
    # row's upstream_id must be the exact Tinker ID the price was read for,
    # or a route would send one ID while billing another's verified price.
    rows = json.loads(thinkingmachines.MANIFEST_PATH.read_text(encoding="utf-8"))["models"]
    priced_ids = {**_SERVERLESS_IDS, **_SAMPLER_IDS}

    assert {row["id"]: row["upstream_id"] for row in rows} == {
        model_id: native_id for native_id, model_id in priced_ids.items()
    }
    assert sorted(priced_ids.values()) == sorted(thinkingmachines.EXPECTED_MODELS)


def _pricing_html(*, active: str = "old") -> str:
    return f"""
    <div id="pricing-toggle">
      <button class="{"active" if active == "old" else ""}" data-mode="old">Old</button>
      <button class="{"active" if active == "new" else ""}" data-mode="new">New</button>
    </div>
    <table><tbody id="model-tbody"><tr>
      <td>Inkling (256K)</td>
      <td class="tinker-id">thinkingmachines/Inkling:peft:262144</td>
      <td>Hybrid</td><td>MoE</td><td>Large</td><td>256K</td>
      <td class="price">
        <span class="price-old">$3.74 <span class="price-cached">$0.748 (cached)</span></span>
        <span class="price-new">$4.00 <span class="price-cached">$0.800 (cached)</span></span>
      </td>
      <td class="price"><span class="price-old">$9.36</span><span class="price-new">$10.00</span></td>
    </tr></tbody></table>
    """


def _serverless_pricing_html() -> str:
    return """
    <table><tbody id="model-tbody">
      <tr>
        <td>GLM-5.3 (256K)</td>
        <td class="tinker-id">zai-org/GLM-5.3:peft:262144</td>
        <td>256K</td><td>Large</td><td>MoE</td><td>Reasoning</td>
        <td class="price">$4.86<span class="price-cached">$0.972 (cached)</span></td>
        <td class="price">$12.15</td>
        <td class="price">$14.58</td>
      </tr>
    </tbody></table>
    <table><tbody id="serverless-tbody">
      <tr>
        <td>Inkling-Small</td>
        <td class="tinker-id">thinkingmachines/Inkling-Small:peft:262144:sampling-nvfp4</td>
        <td>256K</td>
        <td class="price">$0.30<span class="price-cached">$0.06 (cached)</span></td>
        <td class="price">$1.20</td>
      </tr>
      <tr>
        <td>Inkling</td>
        <td class="tinker-id">thinkingmachines/Inkling:peft:262144:sampling-nvfp4</td>
        <td>256K</td>
        <td class="price">$1.00<span class="price-cached">$0.17 (cached)</span></td>
        <td class="price">$4.05</td>
      </tr>
    </tbody></table>
    """


def test_parser_merges_serverless_and_selected_sampler_models() -> None:
    assert parse(_serverless_pricing_html()) == {
        "thinkingmachines/inkling-small": {
            "prompt_micro_per_m": 300_000,
            "completion_micro_per_m": 1_200_000,
            "prompt_cached_micro_per_m": 60_000,
        },
        "thinkingmachines/inkling": {
            "prompt_micro_per_m": 1_000_000,
            "completion_micro_per_m": 4_050_000,
            "prompt_cached_micro_per_m": 170_000,
        },
        "z-ai/glm-5.3": {
            "prompt_micro_per_m": 4_860_000,
            "completion_micro_per_m": 12_150_000,
            "prompt_cached_micro_per_m": 972_000,
        },
    }


def test_parser_uses_currently_active_pricing_version() -> None:
    assert parse(_pricing_html()) == {
        "thinkingmachines/inkling": {
            "prompt_micro_per_m": 3_740_000,
            "completion_micro_per_m": 9_360_000,
            "prompt_cached_micro_per_m": 748_000,
        }
    }
    assert parse(_pricing_html(active="new")) == {
        "thinkingmachines/inkling": {
            "prompt_micro_per_m": 4_000_000,
            "completion_micro_per_m": 10_000_000,
            "prompt_cached_micro_per_m": 800_000,
        }
    }


def test_parser_reads_current_direct_price_cells() -> None:
    html = """
    <table><tbody id="model-tbody"><tr>
      <td>Inkling (256K)</td>
      <td class="tinker-id">thinkingmachines/Inkling:peft:262144</td>
      <td>Hybrid</td><td>MoE</td><td>Large</td><td>256K</td>
      <td class="price">$3.74<span class="price-cached">$0.748 (cached)</span></td>
      <td class="price">$9.36</td>
    </tr></tbody></table>
    """

    assert parse(html) == {
        "thinkingmachines/inkling": {
            "prompt_micro_per_m": 3_740_000,
            "completion_micro_per_m": 9_360_000,
            "prompt_cached_micro_per_m": 748_000,
        }
    }


def test_manifest_writer_updates_integer_rates(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    manifest = tmp_path / "thinkingmachines.json"
    manifest.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "id": "thinkingmachines/inkling",
                        "input_token_price_per_m": 1,
                        "output_token_price_per_m": 1,
                    },
                    {
                        "id": "thinkingmachines/inkling-small",
                        "input_token_price_per_m": 1,
                        "output_token_price_per_m": 1,
                    },
                    {
                        "id": "thinkingmachines/inkling-2-small",
                        "input_token_price_per_m": 1,
                        "output_token_price_per_m": 1,
                    },
                    {
                        "id": "thinkingmachines/inkling-2-mini",
                        "input_token_price_per_m": 1,
                        "output_token_price_per_m": 1,
                    },
                    {
                        "id": "z-ai/glm-5.3",
                        "input_token_price_per_m": 1,
                        "output_token_price_per_m": 1,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(thinkingmachines, "MANIFEST_PATH", manifest)
    result = ProviderPricingResult(
        slug="thinkingmachines",
        prices={
            "thinkingmachines/inkling": ModelPrice(
                prompt_micro_per_m=1_000_000,
                completion_micro_per_m=4_050_000,
                prompt_cached_micro_per_m=170_000,
            ),
            "thinkingmachines/inkling-small": ModelPrice(
                prompt_micro_per_m=300_000,
                completion_micro_per_m=1_200_000,
                prompt_cached_micro_per_m=60_000,
            ),
            "thinkingmachines/inkling-2-small": ModelPrice(
                prompt_micro_per_m=300_000,
                completion_micro_per_m=1_000_000,
                prompt_cached_micro_per_m=30_000,
            ),
            "thinkingmachines/inkling-2-mini": ModelPrice(
                prompt_micro_per_m=150_000,
                completion_micro_per_m=500_000,
                prompt_cached_micro_per_m=15_000,
            ),
            "z-ai/glm-5.3": ModelPrice(
                prompt_micro_per_m=4_860_000,
                completion_micro_per_m=12_150_000,
                prompt_cached_micro_per_m=972_000,
            ),
        },
        source="deterministic",
    )

    thinkingmachines.write_provider_manifest(result)

    rows = {
        row["id"]: row
        for row in json.loads(manifest.read_text(encoding="utf-8"))["models"]
    }
    assert rows["thinkingmachines/inkling"]["input_token_price_per_m"] == 1_000_000
    assert rows["thinkingmachines/inkling"]["output_token_price_per_m"] == 4_050_000
    assert rows["thinkingmachines/inkling"]["cached_input_token_price_per_m"] == 170_000
    assert rows["thinkingmachines/inkling-small"]["input_token_price_per_m"] == 300_000
    assert rows["thinkingmachines/inkling-small"]["output_token_price_per_m"] == 1_200_000
    assert (
        rows["thinkingmachines/inkling-small"]["cached_input_token_price_per_m"]
        == 60_000
    )
    assert rows["thinkingmachines/inkling-2-small"]["input_token_price_per_m"] == 300_000
    assert rows["thinkingmachines/inkling-2-small"]["output_token_price_per_m"] == 1_000_000
    assert rows["thinkingmachines/inkling-2-small"]["cached_input_token_price_per_m"] == 30_000
    assert rows["thinkingmachines/inkling-2-mini"]["input_token_price_per_m"] == 150_000
    assert rows["thinkingmachines/inkling-2-mini"]["output_token_price_per_m"] == 500_000
    assert rows["thinkingmachines/inkling-2-mini"]["cached_input_token_price_per_m"] == 15_000
    assert rows["z-ai/glm-5.3"]["input_token_price_per_m"] == 4_860_000
    assert rows["z-ai/glm-5.3"]["output_token_price_per_m"] == 12_150_000
    assert rows["z-ai/glm-5.3"]["cached_input_token_price_per_m"] == 972_000


@pytest.mark.provider_health
def test_tinker_still_serves_glm_5_3() -> None:
    # Live provider state: provider-catalog-health.yml reports it hourly, and
    # the price refresh does not wait on it.
    assert "z-ai/glm-5.3@thinkingmachines/prepaid" in MODEL_ENDPOINTS


def test_tinker_glm_5_3_sampler_route_is_published_with_verified_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    # The route built from its pinned manifest row: how the catalog publishes
    # the verified sampler contract holds whatever Thinking Machines lists today.
    serve_manifest_rows(monkeypatch, tmp_path, "thinkingmachines", [THINKINGMACHINES_GLM_5_3])
    model = MODELS["z-ai/glm-5.3"]
    endpoint = MODEL_ENDPOINTS["z-ai/glm-5.3@thinkingmachines/prepaid"]

    assert model.context_length >= 262_144
    assert endpoint.provider == "thinkingmachines"
    assert endpoint.usage_type == "Credits"
    assert endpoint.upstream_id == "zai-org/GLM-5.3:peft:262144"
    assert endpoint.prompt_price_microdollars_per_million_tokens == 5_127_300
    assert endpoint.completion_price_microdollars_per_million_tokens == 12_818_250


INKLING_2_SMALL_ROW = {
    "id": "thinkingmachines/inkling-2-small",
    "upstream_id": "thinkingmachines/Inkling-2-Small:peft:262144",
    "display_name": "Thinking Machines Inkling 2 Small 256K",
    "context_length": 262144,
    "input_modalities": ["text", "image"],
    "output_modalities": ["text"],
    "endpoints": ["chat/completions"],
    "input_token_price_per_m": 300000,
    "output_token_price_per_m": 1000000,
    "cached_input_token_price_per_m": 30000,
}


def test_inkling_2_small_would_publish_with_an_open_weights_badge(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    # Once the access hold lifts, Tinker's route publishes as text+image at its
    # 256K window. Its weights are public on Hugging Face (Apache-2.0, since
    # 2026-11-12), so the model carries the open-weights badge like its
    # siblings. Other providers may serve the same model at a longer window
    # (Nebius and Cloudflare list it at 1M), so the model's window is at least
    # Tinker's.
    from trusted_router.catalog import endpoints_for_model, model_open_weights

    serve_manifest_rows(monkeypatch, tmp_path, "thinkingmachines", [INKLING_2_SMALL_ROW])
    model = MODELS["thinkingmachines/inkling-2-small"]
    assert model.context_length >= 262_144
    assert model.input_modalities == ("text", "image")
    assert model_open_weights(model)
    assert model_open_weights(MODELS["thinkingmachines/inkling-small"])
    assert {
        (endpoint.upstream_id, endpoint.effective_context_length(model))
        for endpoint in endpoints_for_model(model.id)
        if endpoint.provider == "thinkingmachines"
    } == {("thinkingmachines/Inkling-2-Small:peft:262144", 262_144)}


def test_tinker_inkling_2_routes_are_held_out_of_routing_and_probes() -> None:
    # QUILL-ROUTER-FD: Tinker rejects TR's key for Inkling 2 Small, so
    # customer requests failed. Inkling 2 Mini starts under the same hold
    # because TR's key has never been shown to serve it. Both stay out of
    # routing until the access canary passes; the siblings keep serving.
    from trusted_router.catalog import endpoints_for_model
    from trusted_router.synthetic.probes import rotation_candidates

    rows = {
        row["id"]: row
        for row in json.loads(thinkingmachines.MANIFEST_PATH.read_text(encoding="utf-8"))["models"]
    }
    for model_id, reason in thinkingmachines.OPERATOR_HOLDS.items():
        assert rows[model_id]["routable"] is False
        assert rows[model_id]["routable_reason"] == reason == "provider-access-unavailable"
        # The hold is on TR's Tinker access, not on the model: other
        # providers' routes for it are admitted on their own merits.
        assert not any(e.provider == "thinkingmachines" for e in endpoints_for_model(model_id))
    assert thinkingmachines.OPERATOR_HOLDS.keys() == {
        "thinkingmachines/inkling-2-mini",
        "thinkingmachines/inkling-2-small",
    }

    probed = rotation_candidates().get("thinkingmachines", [])
    assert "thinkingmachines/inkling-2-small" not in probed
    assert "thinkingmachines/inkling-2-mini" not in probed
    for sibling in ("thinkingmachines/inkling", "thinkingmachines/inkling-small"):
        # No operator hold on a sibling. The refresh's own unserved-route hold
        # is a different, self-lifting decision.
        held_unserved = row_on_unserved_route_hold(rows[sibling])
        assert "routable" not in rows[sibling] or held_unserved
        if provider_lifecycle.provider_model_retired("thinkingmachines", sibling):
            # First-gen Inkling retires on 2026-12-14; it is neither probed nor routed.
            assert sibling not in probed
            assert not any(e.provider == "thinkingmachines" for e in endpoints_for_model(sibling))
            continue
        if held_unserved:
            # While that hold lasts the sibling is not probed and has no route
            # on TrustedRouter's Tinker access.
            assert sibling not in probed
            assert not any(
                e.provider == "thinkingmachines" and e.usage_type == "Credits"
                for e in endpoints_for_model(sibling)
            )
            continue
        assert sibling in probed
        assert any(e.provider == "thinkingmachines" for e in endpoints_for_model(sibling))


NEBIUS_INKLING_2_SMALL_ROW = {
    "id": "thinkingmachines/inkling-2-small",
    "upstream_id": "thinkingmachines/Inkling-2-Small",
    "display_name": "Inkling-2-Small",
    "context_length": 1048576,
    "max_output_tokens": 65536,
    "input_token_price_per_m": 300000,
    "output_token_price_per_m": 1000000,
    "model_type": "chat",
    "input_modalities": ["text", "image"],
    "output_modalities": ["text"],
    "endpoints": ["chat/completions"],
    "status": 1,
}
CLOUDFLARE_INKLING_2_SMALL_ROW = {
    "id": "thinkingmachines/inkling-2-small",
    "upstream_id": "@cf/thinkingmachines/inkling-2-small",
    "display_name": "@cf/thinkingmachines/inkling-2-small",
    "context_length": 1048576,
    "model_type": "chat",
    "input_modalities": ["text"],
    "output_modalities": ["text"],
    "endpoints": ["chat/completions"],
    "status": 1,
    "input_token_price_per_m": 300000,
    "output_token_price_per_m": 1000000,
    "cached_input_token_price_per_m": 100000,
}


# DeepInfra began serving Inkling 2 Small on 2026-11-14 at 1M context
# (api.deepinfra.com/models/list: $0.30/$1.00 per M, cached input at 0.1x).
DEEPINFRA_INKLING_2_SMALL_ROW = {
    "id": "thinkingmachines/inkling-2-small",
    "upstream_id": "thinkingmachines/Inkling-2-Small",
    "display_name": "thinkingmachines/Inkling-2-Small",
    "context_length": 1048576,
    "max_output_tokens": 1048576,
    "model_type": "chat",
    "input_modalities": ["text"],
    "output_modalities": ["text"],
    "endpoints": ["chat/completions"],
    "status": 1,
    "input_token_price_per_m": 300000,
    "output_token_price_per_m": 1000000,
    "cached_input_token_price_per_m": 30000,
}


def test_third_party_inkling_2_small_routes_serve_while_tinker_is_held_and_join_liberty_4_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    # Thinking Machines published Inkling 2 Small's weights on Hugging Face on
    # 2026-11-12 (Apache-2.0, ungated), and Nebius and Cloudflare Workers AI
    # began serving it at 1M context. Their routes publish like any provider
    # route: the Tinker access hold belongs to TR's Tinker key, not to the
    # model. The same day Joseph gave it the open-weights badge and put it on
    # a new Liberty 4.1 panel; every other Liberty id keeps its members.
    from trusted_router.catalog import (
        endpoints_for_model,
        meta_candidate_models,
        model_open_weights,
    )

    model = MODELS["thinkingmachines/inkling-2-small"]
    # Judge the pinned third-party routes, not whichever providers the live
    # manifests list today: drop the live third-party routes for the model.
    # The Tinker route stays, so its hold is still checked on live data.
    for endpoint_id, endpoint in list(MODEL_ENDPOINTS.items()):
        if endpoint.model_id == model.id and endpoint.provider != "thinkingmachines":
            monkeypatch.delitem(MODEL_ENDPOINTS, endpoint_id)
    serve_manifest_rows(monkeypatch, tmp_path, "nebius", [NEBIUS_INKLING_2_SMALL_ROW])
    serve_manifest_rows(monkeypatch, tmp_path, "cloudflare-workers-ai", [CLOUDFLARE_INKLING_2_SMALL_ROW])

    def current_routes() -> set[tuple[str, str, int | None]]:
        return {
            (endpoint.provider, endpoint.upstream_id, endpoint.effective_context_length(model))
            for endpoint in endpoints_for_model(model.id)
        }

    routes = current_routes()

    assert ("nebius", "thinkingmachines/Inkling-2-Small", 1_048_576) in routes
    assert ("cloudflare-workers-ai", "@cf/thinkingmachines/inkling-2-small", 1_048_576) in routes
    assert not any(provider == "thinkingmachines" for provider, _, _ in routes)
    assert model_open_weights(model)
    liberty = [model_id for model_id in MODELS if "liberty" in model_id.lower()]
    assert "trustedrouter/liberty-4.1" in liberty
    for liberty_id in liberty:
        members = {candidate.id for candidate in meta_candidate_models(liberty_id)}
        assert (model.id in members) == (liberty_id == "trustedrouter/liberty-4.1")
    liberty_4_1 = MODELS["trustedrouter/liberty-4.1"]
    assert [candidate.id for candidate in meta_candidate_models(liberty_4_1.id)] == [
        "nvidia/nemotron-3-ultra-550b-a55b",
        "reflection/beam-1.0",
        "meta-models/muse-spark-open",
        "thinkingmachines/inkling-2-small",
    ]
    # Liberty 4.1 is open weights with Inkling 2 Small on its panel, and it has
    # a US-headquartered 1M route for it, which is what the gateway pins.
    assert model_open_weights(liberty_4_1)
    assert ("cloudflare-workers-ai", "@cf/thinkingmachines/inkling-2-small", 1_048_576) in routes
    assert PROVIDERS["cloudflare-workers-ai"].provider_headquarters_country == "US"
    # Only pinned third-party routes are served here: no DeepInfra route
    # until its row is served, and then it joins as another US 1M route.
    assert not any(provider == "deepinfra" for provider, _, _ in routes)
    serve_manifest_rows(monkeypatch, tmp_path, "deepinfra", [DEEPINFRA_INKLING_2_SMALL_ROW])
    routes = current_routes()
    assert ("deepinfra", "thinkingmachines/Inkling-2-Small", 1_048_576) in routes
    assert PROVIDERS["deepinfra"].provider_headquarters_country == "US"
    assert not any(provider == "thinkingmachines" for provider, _, _ in routes)


# Tinker's paid 256K sampler row for Inkling 2 Mini (models.json, 2026-11-25):
# $0.15 prefill, $0.015 cached prefill, $0.50 sample per 1M tokens.
INKLING_2_MINI_ROW = {
    "id": "thinkingmachines/inkling-2-mini",
    "upstream_id": "thinkingmachines/Inkling-2-Mini:peft:262144",
    "display_name": "Thinking Machines Inkling 2 Mini 256K",
    "context_length": 262144,
    "input_modalities": ["text", "image"],
    "output_modalities": ["text"],
    "endpoints": ["chat/completions"],
    "input_token_price_per_m": 150000,
    "output_token_price_per_m": 500000,
    "cached_input_token_price_per_m": 15000,
}
# DeepInfra lists Inkling 2 Mini at 1M context (api.deepinfra.com/models/list,
# 2026-11-25: $0.075 in / $0.30 out per M, cached input at 0.2x).
DEEPINFRA_INKLING_2_MINI_ROW = {
    "id": "thinkingmachines/inkling-2-mini",
    "upstream_id": "thinkingmachines/Inkling-2-Mini",
    "display_name": "thinkingmachines/Inkling-2-Mini",
    "context_length": 1048576,
    "max_output_tokens": 1048576,
    "model_type": "chat",
    "input_modalities": ["text", "image"],
    "output_modalities": ["text"],
    "endpoints": ["chat/completions"],
    "status": 1,
    "input_token_price_per_m": 75000,
    "output_token_price_per_m": 300000,
    "cached_input_token_price_per_m": 15000,
}
# io.net lists it at 256K (api.intelligence.io.solutions/api/v1/models,
# 2026-11-25: $0.10 in / $0.40 out / $0.02 cached per M).
IO_NET_INKLING_2_MINI_ROW = {
    "id": "thinkingmachines/inkling-2-mini",
    "upstream_id": "thinkingmachines/inkling-2-mini",
    "display_name": "Thinking Machines: Inkling 2 Mini",
    "title": "thinkingmachines/inkling-2-mini",
    "model_type": "chat",
    "context_length": 262144,
    "max_output_tokens": 65536,
    "input_modalities": ["text", "image"],
    "output_modalities": ["text"],
    "endpoints": ["chat/completions"],
    "status": 1,
    "routable": True,
    "input_token_price_per_m": 100000,
    "output_token_price_per_m": 400000,
    "cached_input_token_price_per_m": 20000,
}


def test_inkling_2_mini_would_publish_from_tinker_with_an_open_weights_badge(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    # Once the access hold lifts, Tinker's paid route publishes as text+image
    # at its 256K window and its paid sample price. The weights are public on
    # Hugging Face (thinkingmachines/Inkling-2-Mini: public, ungated,
    # Apache-2.0), so the model carries the open-weights badge.
    from trusted_router.catalog import endpoints_for_model, model_open_weights

    serve_manifest_rows(monkeypatch, tmp_path, "thinkingmachines", [INKLING_2_MINI_ROW])
    model = MODELS["thinkingmachines/inkling-2-mini"]
    assert model.context_length >= 262_144
    assert model.input_modalities == ("text", "image")
    assert model_open_weights(model)
    tinker = [e for e in endpoints_for_model(model.id) if e.provider == "thinkingmachines"]
    assert {(e.upstream_id, e.effective_context_length(model)) for e in tinker} == {
        ("thinkingmachines/Inkling-2-Mini:peft:262144", 262_144)
    }
    prepaid = MODEL_ENDPOINTS["thinkingmachines/inkling-2-mini@thinkingmachines/prepaid"]
    assert prepaid.usage_type == "Credits"
    assert prepaid.prompt_price_microdollars_per_million_tokens > 0
    assert ":free" not in prepaid.upstream_id


def test_third_party_inkling_2_mini_routes_serve_while_tinker_is_held_and_join_no_combo(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    # DeepInfra and io.net list Inkling 2 Mini publicly with prices; their
    # routes publish on their own merits while Tinker's stays held. Nothing
    # puts it in a combo: no TrustedRouter meta model has it as a candidate.
    from trusted_router.catalog import META_MODEL_IDS, endpoints_for_model, meta_candidate_models

    model_id = "thinkingmachines/inkling-2-mini"
    for endpoint_id, endpoint in list(MODEL_ENDPOINTS.items()):
        if endpoint.model_id == model_id and endpoint.provider != "thinkingmachines":
            monkeypatch.delitem(MODEL_ENDPOINTS, endpoint_id)
    serve_manifest_rows(monkeypatch, tmp_path, "deepinfra", [DEEPINFRA_INKLING_2_MINI_ROW])
    serve_manifest_rows(monkeypatch, tmp_path, "io-net", [IO_NET_INKLING_2_MINI_ROW])
    model = MODELS[model_id]

    routes = {
        (endpoint.provider, endpoint.upstream_id, endpoint.effective_context_length(model))
        for endpoint in endpoints_for_model(model_id)
    }
    assert ("deepinfra", "thinkingmachines/Inkling-2-Mini", 1_048_576) in routes
    assert ("io-net", "thinkingmachines/inkling-2-mini", 262_144) in routes
    assert not any(provider == "thinkingmachines" for provider, _, _ in routes)
    assert META_MODEL_IDS
    for meta_id in META_MODEL_IDS:
        assert model_id not in {candidate.id for candidate in meta_candidate_models(meta_id)}


def _held_manifest(tmp_path, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN202
    manifest = tmp_path / "thinkingmachines.json"
    held = {**INKLING_2_SMALL_ROW, "routable": False, "routable_reason": "provider-access-unavailable"}
    held_mini = {**INKLING_2_MINI_ROW, "routable": False, "routable_reason": "provider-access-unavailable"}
    sibling = {
        "id": "thinkingmachines/inkling-small",
        "upstream_id": "thinkingmachines/Inkling-Small:peft:262144:sampling-nvfp4",
        "input_token_price_per_m": 1,
        "output_token_price_per_m": 1,
    }
    others = [
        {"id": model_id, "upstream_id": model_id, "input_token_price_per_m": 1, "output_token_price_per_m": 1}
        for model_id in ("thinkingmachines/inkling", "z-ai/glm-5.3")
    ]
    manifest.write_text(json.dumps({"models": [held_mini, held, sibling, *others]}), encoding="utf-8")
    monkeypatch.setattr(thinkingmachines, "MANIFEST_PATH", manifest)
    feeds = {thinkingmachines.SERVERLESS_URL: SERVERLESS, thinkingmachines.SAMPLER_URL: SAMPLER}
    monkeypatch.setattr(thinkingmachines, "fetch_json", feeds.__getitem__)
    for name in thinkingmachines.API_KEY_ENVS:
        monkeypatch.delenv(name, raising=False)
    return manifest


def _refresh_rows(manifest) -> dict[str, dict]:  # noqa: ANN001
    thinkingmachines.write_provider_manifest(thinkingmachines.fetch())
    return {row["id"]: row for row in json.loads(manifest.read_text(encoding="utf-8"))["models"]}


def test_access_hold_survives_refreshes_without_a_tinker_key(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _held_manifest(tmp_path, monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(thinkingmachines, "probe_openai_chat", lambda **kw: calls.append(kw["model"]) or True)

    rows = _refresh_rows(manifest)

    assert calls == []
    held = rows["thinkingmachines/inkling-2-small"]
    assert (held["routable"], held["routable_reason"]) == (False, "provider-access-unavailable")
    assert held["output_token_price_per_m"] == 1_000_000
    held_mini = rows["thinkingmachines/inkling-2-mini"]
    assert (held_mini["routable"], held_mini["routable_reason"]) == (False, "provider-access-unavailable")
    assert held_mini["output_token_price_per_m"] == 500_000
    assert "routable" not in rows["thinkingmachines/inkling-small"]


@pytest.mark.parametrize("healthy", [False, True])
def test_access_canary_rechecks_only_held_routes_and_lifts_on_success(
    tmp_path, monkeypatch: pytest.MonkeyPatch, healthy: bool,
) -> None:
    manifest = _held_manifest(tmp_path, monkeypatch)
    monkeypatch.setenv("THINKING_MACHINES_API_KEY", "test-key")
    calls: list[dict] = []
    monkeypatch.setattr(thinkingmachines, "probe_openai_chat", lambda **kw: calls.append(kw) or healthy)

    rows = _refresh_rows(manifest)

    assert [(c["base_url"], c["model"]) for c in calls] == [
        (thinkingmachines.BASE_URL, "thinkingmachines/Inkling-2-Mini:peft:262144"),
        (thinkingmachines.BASE_URL, "thinkingmachines/Inkling-2-Small:peft:262144"),
    ]
    for model_id in ("thinkingmachines/inkling-2-mini", "thinkingmachines/inkling-2-small"):
        held = rows[model_id]
        if healthy:
            assert "routable" not in held and "routable_reason" not in held
        else:
            assert (held["routable"], held["routable_reason"]) == (False, "provider-access-unavailable")
    assert "routable" not in rows["thinkingmachines/inkling-small"]

    # Once lifted, later refreshes neither probe nor re-hold the route.
    calls.clear()
    rows = _refresh_rows(manifest)
    assert bool(calls) is not healthy
    assert ("routable" not in rows["thinkingmachines/inkling-2-small"]) is healthy
    assert ("routable" not in rows["thinkingmachines/inkling-2-mini"]) is healthy
