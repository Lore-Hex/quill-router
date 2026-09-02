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
