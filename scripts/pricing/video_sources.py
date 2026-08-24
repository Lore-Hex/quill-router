"""Verify fixed-cost video billing against provider-owned pricing pages."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

VIDEO_PRICE_PROVIDER_SLUGS = frozenset({"kling", "ltx", "runway"})

LTX_PRICING_URL = "https://docs.ltx.io/pricing.md"
RUNWAY_PRICING_URL = "https://docs.dev.runwayml.com/guides/pricing.md"
KLING_CREDITS_POLICY_URL = "https://kling.ai/docs/point-policy"
KLING_VIDEO_GUIDE_URL = "https://app.klingai.com/cn/quickstart/klingai-video-3-model-user-guide"

_LTX_EXPECTED_MICRODOLLARS_PER_SECOND = {
    "ltx-2-3-fast": {
        "1920x1080": 60_000,
        "2560x1440": 120_000,
        "3840x2160": 240_000,
    },
    "ltx-2-3-pro": {
        "1920x1080": 80_000,
        "2560x1440": 160_000,
        "3840x2160": 320_000,
    },
}
_RUNWAY_GEN45_CREDITS_PER_SECOND = 12
_RUNWAY_MICRODOLLARS_PER_CREDIT = 10_000
_KLING_CREDITS_PER_USD = 66
_KLING_VIDEO3_BASE_CREDITS_PER_SECOND = frozenset({6, 8, 9, 12})

_LTX_ROW_RE = re.compile(
    r"^\|\s*(?:\*\*(ltx-2-3-(?:fast|pro))\*\*)?\s*\|\s*`([^`]+)`"
    r"(?:\s*/\s*`[^`]+`)?\s*\|\s*\\?\$([0-9]+(?:\.[0-9]+)?)\s*\|"
)


@dataclass(frozen=True)
class VideoPriceAudit:
    warnings: tuple[str, ...] = ()
    info: tuple[str, ...] = ()
    hard_failures: tuple[str, ...] = ()


def _microdollars(value: str) -> int:
    return int(Decimal(value) * 1_000_000)


def parse_ltx_rates(document: str) -> dict[str, dict[str, int]]:
    rates: dict[str, dict[str, int]] = {}
    current_model = ""
    # The same model name appears again for audio, editing, and upscale with
    # different rates. TrustedRouter currently exposes only text/image video
    # generation, so stop before those unrelated billing tables.
    video_generation = document.split("## Audio-to-Video", 1)[0]
    for raw_line in video_generation.splitlines():
        match = _LTX_ROW_RE.match(raw_line.strip())
        if not match:
            continue
        if match.group(1):
            current_model = match.group(1)
        if not current_model:
            continue
        resolution = match.group(2).lower()
        rate = _microdollars(match.group(3))
        existing = rates.setdefault(current_model, {}).get(resolution)
        if existing is not None and existing != rate:
            raise ValueError(
                f"conflicting {current_model} {resolution} rates: {existing} and {rate}"
            )
        rates[current_model][resolution] = rate
    return rates


def parse_runway_gen45_rate(document: str) -> int:
    credit_price = re.search(
        r"Credits can be purchased\s+for\s+\$([0-9]+(?:\.[0-9]+)?)\s+per credit",
        document,
        re.IGNORECASE,
    )
    model_rate = re.search(
        r"`gen4\.5`\s*\|\s*([0-9]+)\s+credits per second",
        document,
        re.IGNORECASE,
    )
    if not credit_price or not model_rate:
        raise ValueError("could not parse Gen-4.5 credit price")
    return int(model_rate.group(1)) * _microdollars(credit_price.group(1))


def parse_kling_credit_policy(document: str) -> int:
    match = re.search(
        r"Standard Pricing:\s*\$1\s*USD\s*=\s*([0-9]+)\s*Credits",
        document,
        re.IGNORECASE,
    )
    if not match:
        raise ValueError("could not parse Kling standard credit price")
    return int(match.group(1))


def parse_kling_video3_rates(document: str) -> frozenset[int]:
    return frozenset(
        int(value) for value in re.findall(r"([0-9]+)\s*Credits/s", document, re.IGNORECASE)
    )


def _provider_failure(slug: str, message: str, *, hard: bool) -> VideoPriceAudit:
    warning = f"{slug}: official video price verification {message}"
    return VideoPriceAudit(
        warnings=(warning,),
        hard_failures=(warning,) if hard else (),
    )


def _audit_ltx(fetch_text: Callable[[str], str]) -> VideoPriceAudit:
    try:
        actual = parse_ltx_rates(fetch_text(LTX_PRICING_URL))
    except Exception as exc:  # noqa: BLE001 - network/parser errors are audit results
        return _provider_failure("ltx", f"unavailable ({type(exc).__name__}: {exc})", hard=False)
    if actual != _LTX_EXPECTED_MICRODOLLARS_PER_SECOND:
        return _provider_failure(
            "ltx",
            "does not match the production LTX-2.3 per-second contract",
            hard=True,
        )
    return VideoPriceAudit(info=("ltx: official fixed-cost video prices match production ✓",))


def _audit_runway(fetch_text: Callable[[str], str]) -> VideoPriceAudit:
    try:
        actual = parse_runway_gen45_rate(fetch_text(RUNWAY_PRICING_URL))
    except Exception as exc:  # noqa: BLE001 - network/parser errors are audit results
        return _provider_failure("runway", f"unavailable ({type(exc).__name__}: {exc})", hard=False)
    expected = _RUNWAY_GEN45_CREDITS_PER_SECOND * _RUNWAY_MICRODOLLARS_PER_CREDIT
    if actual != expected:
        return _provider_failure(
            "runway",
            f"changed Gen-4.5 from the production rate of {expected} microdollars/second",
            hard=True,
        )
    return VideoPriceAudit(info=("runway: official fixed-cost video price matches production ✓",))


def _audit_kling(fetch_text: Callable[[str], str]) -> VideoPriceAudit:
    try:
        credits_per_usd = parse_kling_credit_policy(fetch_text(KLING_CREDITS_POLICY_URL))
        rates = parse_kling_video3_rates(fetch_text(KLING_VIDEO_GUIDE_URL))
    except Exception as exc:  # noqa: BLE001 - network/parser errors are audit results
        return _provider_failure("kling", f"unavailable ({type(exc).__name__}: {exc})", hard=False)
    if credits_per_usd != _KLING_CREDITS_PER_USD or not (
        _KLING_VIDEO3_BASE_CREDITS_PER_SECOND <= rates
    ):
        return _provider_failure(
            "kling",
            "does not match the production Video 3.0 credit contract",
            hard=True,
        )
    return VideoPriceAudit(info=("kling: official fixed-cost video prices match production ✓",))


def audit_video_price_sources(fetch_text: Callable[[str], str]) -> VideoPriceAudit:
    warnings: list[str] = []
    info: list[str] = []
    hard_failures: list[str] = []
    for audit_provider in (_audit_kling, _audit_ltx, _audit_runway):
        result = audit_provider(fetch_text)
        warnings.extend(result.warnings)
        info.extend(result.info)
        hard_failures.extend(result.hard_failures)
    return VideoPriceAudit(tuple(warnings), tuple(info), tuple(hard_failures))
