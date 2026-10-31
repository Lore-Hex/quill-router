from __future__ import annotations

import pytest

from scripts.pricing.video_sources import (
    KLING_CREDITS_POLICY_URL,
    KLING_VIDEO_GUIDE_URL,
    LTX_PRICING_URL,
    RUNWAY_PRICING_URL,
    audit_video_price_sources,
    parse_kling_credit_policy,
    parse_kling_video3_rates,
    parse_ltx_rates,
    parse_runway_gen45_rate,
    parse_runway_kling4_rates,
)

LTX_DOCUMENT = r"""
## Text-to-Video

| Model | Resolution | Cost per second |
| **ltx-2-5-fast** | `1280x720` / `720x1280` | \$0.09 |
| | `1920x1080` / `1080x1920` | \$0.13 |
| **ltx-2-5-pro** | `1280x720` / `720x1280` | \$0.12 |
| | `1920x1080` / `1080x1920` | \$0.17 |
| **ltx-2-3-fast** | `1280x720` / `720x1280` | \$0.03 |
| **ltx-2-3-fast** | `1920x1080` / `1080x1920` | \$0.06 |
| | `2560x1440` / `1440x2560` | \$0.12 |
| | `3840x2160` / `2160x3840` | \$0.24 |
| **ltx-2-3-pro** | `1280x720` / `720x1280` | \$0.04 |
| | `1920x1080` / `1080x1920` | \$0.08 |
| | `2560x1440` / `1440x2560` | \$0.16 |
| | `3840x2160` / `2160x3840` | \$0.32 |

## Image-to-Video

| Model | Resolution | Cost per second |
| **ltx-2-5-fast** | `1280x720` / `720x1280` | \$0.09 |
| | `1920x1080` / `1080x1920` | \$0.13 |
| **ltx-2-3-fast** | `1280x720` / `720x1280` | \$0.03 |
| | `1920x1080` / `1080x1920` | \$0.06 |
| **ltx-2-3-pro** | `1280x720` / `720x1280` | \$0.04 |
| | `1920x1080` / `1080x1920` | \$0.08 |

## Audio-to-Video
| **ltx-2-3-pro** | `1920x1080` | \$0.10 |
"""
RUNWAY_DOCUMENT = """
Credits can be purchased
for $0.01 per credit in the developer portal.
| `gen4.5` | 12 credits per second |
| `kling_4` (720p) | Promotion: 12.56 credits per second. List price: 15.7 and 9.4 credits per second |
| `kling_4` (1080p) | Promotion: 18.8 credits per second. List price: 23.5 and 14.1 credits per second |
Charges are rounded up to the next credit.
"""
KLING_POLICY = "Standard Pricing: $1 USD = 66 Credits"
KLING_GUIDE = "12 Credits/s 9 Credits/s 8 Credits/s 6 Credits/s 2 Credits/s"


def _official_documents(url: str) -> str:
    return {
        LTX_PRICING_URL: LTX_DOCUMENT,
        RUNWAY_PRICING_URL: RUNWAY_DOCUMENT,
        KLING_CREDITS_POLICY_URL: KLING_POLICY,
        KLING_VIDEO_GUIDE_URL: KLING_GUIDE,
    }[url]


def test_video_price_parsers_return_integer_upstream_rates() -> None:
    assert parse_ltx_rates(LTX_DOCUMENT)["ltx-2-3-pro"]["3840x2160"] == 320_000
    assert parse_runway_gen45_rate(RUNWAY_DOCUMENT) == 120_000
    assert parse_kling_credit_policy(KLING_POLICY) == 66
    assert parse_kling_video3_rates(KLING_GUIDE) == frozenset({2, 6, 8, 9, 12})


def test_kling_credit_parser_accepts_ssr_markup_between_words() -> None:
    document = "Standard <strong>Pricing</strong>: $1&nbsp;USD = <span>66</span> Credits"

    assert parse_kling_credit_policy(document) == 66


def test_kling_credit_parser_rejects_conflicting_prices() -> None:
    document = "Standard Pricing: $1 USD = 66 Credits Standard Pricing: $1 USD = 72 Credits"

    with pytest.raises(ValueError, match="conflicting Kling standard credit prices"):
        parse_kling_credit_policy(document)


def test_video_price_audit_accepts_current_official_contract() -> None:
    result = audit_video_price_sources(_official_documents)

    assert result.warnings == ()
    assert result.hard_failures == ()
    assert len(result.info) == 3


def test_video_price_audit_fails_closed_on_rate_drift() -> None:
    def changed_runway(url: str) -> str:
        value = _official_documents(url)
        return value.replace("12 credits per second", "14 credits per second")

    result = audit_video_price_sources(changed_runway)

    assert result.hard_failures == result.warnings
    assert len(result.hard_failures) == 1
    assert result.hard_failures[0].startswith("runway:")


def test_video_price_audit_keeps_last_known_rate_on_source_outage() -> None:
    def ltx_unavailable(url: str) -> str:
        if url == LTX_PRICING_URL:
            raise TimeoutError("docs timeout")
        return _official_documents(url)

    result = audit_video_price_sources(ltx_unavailable)

    assert len(result.warnings) == 1
    assert result.warnings[0].startswith("ltx:")
    assert result.hard_failures == ()


def test_video_price_audit_retries_transient_kling_ssr_failure() -> None:
    attempts = 0

    def transient_kling(url: str) -> str:
        nonlocal attempts
        if url == KLING_CREDITS_POLICY_URL:
            attempts += 1
            if attempts == 1:
                return "loading"
        return _official_documents(url)

    result = audit_video_price_sources(transient_kling)

    assert attempts == 2
    assert result.warnings == ()
    assert result.hard_failures == ()


def test_runway_kling4_uses_standard_rates_not_promotion_or_video_input() -> None:
    assert parse_runway_kling4_rates(RUNWAY_DOCUMENT) == {"720p": 157_000, "1080p": 235_000}


@pytest.mark.parametrize(
    "document",
    [
        RUNWAY_DOCUMENT.replace("15.7 and 9.4", "unknown"),
        RUNWAY_DOCUMENT.replace("(1080p)", "(4K)"),
        RUNWAY_DOCUMENT.replace("Charges are rounded up to the next credit.", ""),
        RUNWAY_DOCUMENT + "\n| `kling_4` (720p) | 20 credits per second |",
    ],
)
def test_runway_kling4_rejects_ambiguous_or_incomplete_contract(document: str) -> None:
    with pytest.raises(ValueError):
        parse_runway_kling4_rates(document)


def test_runway_kling4_price_drift_blocks_publication() -> None:
    result = audit_video_price_sources(
        lambda url: _official_documents(url).replace("15.7 and 9.4", "16.7 and 9.4")
    )
    assert len(result.hard_failures) == 1
    assert "Kling 4.0" in result.hard_failures[0]


@pytest.mark.parametrize(
    ("missing", "old", "new", "changed_model"),
    [
        ("`kling_4`", "12 credits per second", "14 credits per second", "Gen-4.5"),
        ("`gen4.5`", "15.7 and 9.4", "16.7 and 9.4", "Kling 4.0"),
    ],
)
def test_runway_one_missing_model_does_not_mask_other_price_drift(
    missing: str, old: str, new: str, changed_model: str
) -> None:
    result = audit_video_price_sources(
        lambda url: _official_documents(url).replace(missing, "unknown").replace(old, new)
    )
    assert len(result.hard_failures) == 1
    assert changed_model in result.hard_failures[0]
    assert len(result.warnings) == 2
