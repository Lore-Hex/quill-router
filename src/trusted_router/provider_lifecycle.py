"""Effective-dated provider model retirements and announced price changes.

Live provider catalog refreshes remain the normal source of truth. This module
handles provider announcements with a precise future cutover so routing and
billing do not depend on an hourly refresh landing at exactly the right second.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime

# Test-only clock override, e.g. TR_LIFECYCLE_CLOCK_OVERRIDE=2027-01-01T00:00:00Z.
# Every effective-dated cutover below is a scheduled change of behaviour: a
# test that fixtures a soon-to-retire id passes right up to the announced
# minute and then turns main red with no code change (Wafer, 2026-08-17 00:00
# UTC, CI run 31980690855). CI runs the suite a second time with this clock
# pinned past the latest cutover so that class of test fails on the pull
# request that introduces it. The override is honoured ONLY under pytest or
# with TR_ENVIRONMENT=test; anywhere else it is ignored so a stray variable
# can never move routing or billing off the real clock.
LIFECYCLE_CLOCK_OVERRIDE_ENV = "TR_LIFECYCLE_CLOCK_OVERRIDE"

PHALA_JULY_2026_EFFECTIVE_AT = datetime(2026, 7, 29, 18, 0, tzinfo=UTC)
TOGETHER_MINIMAX_M27_RETIREMENT_AT = datetime(2026, 7, 27, 0, 0, tzinfo=UTC)
BASETEN_JULY_2026_RETIREMENT_AT = datetime(2026, 7, 25, 0, 0, tzinfo=UTC)
TINFOIL_KIMI_K26_RETIREMENT_AT = datetime(2026, 8, 3, 0, 0, tzinfo=UTC)
PARASAIL_AUGUST_2026_RETIREMENT_AT = datetime(2026, 8, 4, 0, 0, tzinfo=UTC)
FRIENDLI_QWEN3_235B_RETIREMENT_AT = datetime(2026, 8, 5, 0, 0, tzinfo=UTC)
FRIENDLI_K_EXAONE_236B_RETIREMENT_AT = datetime(2026, 8, 20, 0, 0, tzinfo=UTC)
CRUSOE_NEMOTRON_3_ULTRA_RETIREMENT_AT = datetime(2026, 7, 28, 18, 0, tzinfo=UTC)
WAFER_AUGUST_2026_RETIREMENT_AT = datetime(2026, 8, 17, 0, 0, tzinfo=UTC)
DEEPINFRA_TERMINUS_RETIREMENT_AT = datetime(2026, 8, 17, 0, 0, tzinfo=UTC)
DEEPINFRA_QWEN3_235B_THINKING_RETIREMENT_AT = datetime(
    2026, 8, 24, 0, 0, tzinfo=UTC
)
NEBIUS_AUGUST_2026_RETIREMENT_AT = datetime(2026, 8, 31, 0, 0, tzinfo=UTC)
CEREBRAS_GEMMA4_SHARED_RETIREMENT_AT = datetime(2026, 9, 3, 0, 0, tzinfo=UTC)
NOVITA_LING_30_TINY_RETIREMENT_AT = datetime(2026, 8, 13, 15, 0, tzinfo=UTC)
ALIBABA_OCTOBER_2026_RETIREMENT_AT = datetime(2026, 10, 9, 16, 0, tzinfo=UTC)
DEEPSEEK_V4_PRICING_EFFECTIVE_AT = datetime(2026, 8, 16, 16, 0, tzinfo=UTC)
FIREWORKS_DSV4_FLASH_0731_PRICING_EFFECTIVE_AT = datetime(
    2026, 8, 22, 12, 0, tzinfo=UTC
)

# DeepSeek prices direct V4 traffic at twice the off-peak rate during these
# half-open UTC windows. Keep them as seconds since midnight so boundary
# behavior is independent of locale, daylight-saving time, and date.
DEEPSEEK_V4_PEAK_WINDOWS_UTC = (
    (1 * 60 * 60, 4 * 60 * 60),
    (6 * 60 * 60, 10 * 60 * 60),
)


@dataclass(frozen=True)
class ProviderPrice:
    prompt_microdollars_per_million_tokens: int
    completion_microdollars_per_million_tokens: int
    prompt_cached_microdollars_per_million_tokens: int | None = None


_DEEPSEEK_V4_FLASH_MODEL_IDS = frozenset(
    {
        "deepseek/deepseek-v4-flash",
        # This TrustedRouter alias resolves to the same first-party
        # `deepseek-v4-flash` upstream id and therefore the same bill.
        "deepseek/deepseek-v4-flash-0731",
    }
)
_DEEPSEEK_V4_PRICES = {
    "flash": {
        "legacy": ProviderPrice(140_000, 280_000, 2_800),
        "off_peak": ProviderPrice(220_000, 660_000, 7_000),
        "peak": ProviderPrice(440_000, 1_320_000, 14_000),
    },
    "pro": {
        "legacy": ProviderPrice(435_000, 870_000, 3_625),
        "off_peak": ProviderPrice(660_000, 1_980_000, 22_000),
        "peak": ProviderPrice(1_320_000, 3_960_000, 44_000),
    },
}

_FIREWORKS_DSV4_FLASH_0731_MODEL_ID = "deepseek/deepseek-v4-flash-0731"
_FIREWORKS_DSV4_FLASH_0731_PRICES = {
    "legacy": ProviderPrice(140_000, 280_000, 28_000),
    "new": ProviderPrice(220_000, 660_000, 7_000),
}


@dataclass(frozen=True)
class _Retirement:
    provider: str
    model_ids: frozenset[str]
    upstream_ids: frozenset[str]
    effective_at: datetime


_RETIREMENTS = (
    # Cerebras announced that Qwen 3.8 27B replaces Gemma 4 31B on its
    # Shared Tier on 2026-09-03. The notice did not specify a time zone, so
    # retire the shared route conservatively at 00:00 UTC. Gemma remains
    # available through separately provisioned Cerebras dedicated endpoints;
    # TrustedRouter's prepaid Cerebras route uses the Shared Tier.
    _Retirement(
        provider="cerebras",
        model_ids=frozenset(
            {
                "google/gemma-4-31b-it",
                "cerebras/gemma-4-31b",
            }
        ),
        upstream_ids=frozenset({"gemma-4-31b"}),
        effective_at=CEREBRAS_GEMMA4_SHARED_RETIREMENT_AT,
    ),
    # Nebius announced that these Token Factory Serverless routes retire on
    # 2026-08-31. The notice did not specify a time zone, so use 00:00 UTC as
    # the conservative cutover. Keep this provider-scoped: equivalent model
    # checkpoints remain routable through other providers that still serve
    # them. The announcement did not name replacements.
    _Retirement(
        provider="nebius",
        model_ids=frozenset(
            {
                "nvidia/nemotron-3-ultra-550b-a55b",
                "Qwen/Qwen3-32B",
                "NousResearch/Hermes-4-70B",
                "meta-llama/Llama-3.3-70B-Instruct",
                "Qwen/Qwen2.5-VL-72B-Instruct",
                "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
                "MiniMaxAI/MiniMax-M2.5",
                "nvidia/Nemotron-3-Nano-Omni",
                "deepseek/deepseek-v4-flash",
                "nvidia/Cosmos3-Super-Reasoner",
                "Qwen/Qwen3-Next-80B-A3B-Thinking",
                "nvidia/Llama-3_1-Nemotron-Ultra-253B-v1",
            }
        ),
        upstream_ids=frozenset(
            {
                "nvidia/Nemotron-3-Ultra-550b-a55b",
                "Qwen/Qwen3-32B",
                "NousResearch/Hermes-4-70B",
                "meta-llama/Llama-3.3-70B-Instruct",
                "Qwen/Qwen2.5-VL-72B-Instruct",
                "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
                "MiniMaxAI/MiniMax-M2.5",
                "nvidia/Nemotron-3-Nano-Omni",
                "deepseek-ai/DeepSeek-V4-Flash",
                "nvidia/Cosmos3-Super-Reasoner",
                "Qwen/Qwen3-Next-80B-A3B-Thinking",
                "nvidia/Llama-3_1-Nemotron-Ultra-253B-v1",
            }
        ),
        effective_at=NEBIUS_AUGUST_2026_RETIREMENT_AT,
    ),
    # Alibaba Cloud Model Studio is consolidating its previously announced
    # retirements at 2026-10-10 00:00 UTC+08. The original DeepSeek V4 Flash
    # preview aliases retire at the same instant in favor of the dated 0731 GA
    # routes. Keep this provider-scoped: the same open-weight models remain
    # routable through other healthy providers. Alibaba explicitly canceled
    # retirement for qwen-vl-ocr, qwen-mt-image, and the named
    # ASR/live-translation routes, so none appear here.
    _Retirement(
        provider="alibaba",
        model_ids=frozenset(
            {
                "deepseek/deepseek-r1",
                "deepseek/deepseek-r1-0528",
                "deepseek/deepseek-r1-distill-qwen-7b",
                "deepseek/deepseek-r1-distill-qwen-14b",
                "deepseek/deepseek-r1-distill-qwen-32b",
                "deepseek/deepseek-v3",
                "deepseek/deepseek-v3.1",
                "deepseek/deepseek-v3.2",
                "deepseek/deepseek-v3.2-exp",
                "deepseek/deepseek-v4-flash",
                "deepseek/deepseek-v4-flash-us",
                "minimax/minimax-m2.1",
                "moonshotai/kimi-k2-instruct",
                "moonshotai/kimi-k2-thinking",
                "qwen/qwen3-8b",
                "qwen/qwen3-14b",
                "qwen/qwen3-30b-a3b",
                "qwen/qwen3-30b-a3b-instruct-2507",
                "qwen/qwen3-30b-a3b-thinking-2507",
                "qwen/qwen3-32b",
                "qwen/qwen3-235b-a22b",
                "qwen/qwen3-235b-a22b-instruct-2507",
                "qwen/qwen3-235b-a22b-thinking-2507",
                "qwen/qwen3-coder-30b-a3b-instruct",
                "qwen/qwen3-coder-480b-a35b-instruct",
                "qwen/qwen3-coder-next",
                "qwen/qwen3-coder-plus",
                "qwen/qwen3-coder-plus-2025-07-22",
                "qwen/qwen3-coder-plus-2025-09-23",
                "qwen/qwen3-max",
                "qwen/qwen3-max-2025-09-23",
                "qwen/qwen3-max-2026-01-23",
                "qwen/qwen3-max-preview",
                "qwen/qwen3-next-80b-a3b-instruct",
                "qwen/qwen3-next-80b-a3b-thinking",
                "qwen/qwen3-vl-8b-instruct",
                "qwen/qwen3-vl-8b-thinking",
                "qwen/qwen3-vl-30b-a3b-instruct",
                "qwen/qwen3-vl-30b-a3b-thinking",
                "qwen/qwen3-vl-32b-instruct",
                "qwen/qwen3-vl-32b-thinking",
                "qwen/qwen3-vl-235b-a22b-instruct",
                "qwen/qwen3-vl-235b-a22b-thinking",
                "qwen/qwen3-vl-flash",
                "qwen/qwen3-vl-flash-2025-10-15",
                "qwen/qwen3-vl-flash-2026-01-22",
                "qwen/qwen3.6-max-preview",
                "qwen/qwen-mt-turbo",
                "z-ai/glm-4.6",
                "z-ai/glm-4.7",
            }
        ),
        upstream_ids=frozenset(
            {
                "deepseek-r1",
                "deepseek-r1-0528",
                "deepseek-r1-distill-qwen-7b",
                "deepseek-r1-distill-qwen-14b",
                "deepseek-r1-distill-qwen-32b",
                "deepseek-v3",
                "deepseek-v3.1",
                "deepseek-v3.2",
                "deepseek-v3.2-exp",
                "deepseek-v4-flash",
                "deepseek-v4-flash-us",
                "MiniMax-M2.1",
                "Moonshot-Kimi-K2-Instruct",
                "kimi-k2-thinking",
                "qwen3-8b",
                "qwen3-14b",
                "qwen3-30b-a3b",
                "qwen3-30b-a3b-instruct-2507",
                "qwen3-30b-a3b-thinking-2507",
                "qwen3-32b",
                "qwen3-235b-a22b",
                "qwen3-235b-a22b-instruct-2507",
                "qwen3-235b-a22b-thinking-2507",
                "qwen3-coder-30b-a3b-instruct",
                "qwen3-coder-480b-a35b-instruct",
                "qwen3-coder-next",
                "qwen3-coder-plus",
                "qwen3-coder-plus-2025-07-22",
                "qwen3-coder-plus-2025-09-23",
                "qwen3-max",
                "qwen3-max-2025-09-23",
                "qwen3-max-2026-01-23",
                "qwen3-max-preview",
                "qwen3-next-80b-a3b-instruct",
                "qwen3-next-80b-a3b-thinking",
                "qwen3-vl-8b-instruct",
                "qwen3-vl-8b-thinking",
                "qwen3-vl-30b-a3b-instruct",
                "qwen3-vl-30b-a3b-thinking",
                "qwen3-vl-32b-instruct",
                "qwen3-vl-32b-thinking",
                "qwen3-vl-235b-a22b-instruct",
                "qwen3-vl-235b-a22b-thinking",
                "qwen3-vl-flash",
                "qwen3-vl-flash-2025-10-15",
                "qwen3-vl-flash-2026-01-22",
                "qwen3.6-max-preview",
                "qwen-mt-turbo",
                "glm-4.6",
                "glm-4.7",
            }
        ),
        effective_at=ALIBABA_OCTOBER_2026_RETIREMENT_AT,
    ),
    # Novita's time-limited free-trial Ling 3.0 Tiny route retired at the
    # provider's exact announced UTC cutover. There is no replacement model.
    _Retirement(
        provider="novita",
        model_ids=frozenset({"inclusionai/ling-3.0-tiny"}),
        upstream_ids=frozenset({"inclusionai/ling-3.0-tiny"}),
        effective_at=NOVITA_LING_30_TINY_RETIREMENT_AT,
    ),
    # DeepInfra announced that its DeepSeek V3.1 Terminus route retires on
    # 2026-08-17 and that subsequent requests will be redirected to DeepSeek
    # V4 Flash 0731. TrustedRouter must not silently substitute a different
    # model, so retire only DeepInfra's endpoint at the conservative 00:00 UTC
    # boundary. Terminus routes on other providers remain eligible.
    _Retirement(
        provider="deepinfra",
        model_ids=frozenset({"deepseek/deepseek-v3.1-terminus"}),
        upstream_ids=frozenset({"deepseek-ai/DeepSeek-V3.1-Terminus"}),
        effective_at=DEEPINFRA_TERMINUS_RETIREMENT_AT,
    ),
    # DeepInfra announced that Qwen3-235B-A22B-Thinking-2507 retires on
    # 2026-08-24 and that requests will subsequently redirect to
    # Qwen3.6-35B-A3B. Do not accept that silent model substitution: retire
    # only DeepInfra's old route while preserving the requested checkpoint on
    # other providers. The notice did not specify a time zone, so use 00:00 UTC
    # conservatively. The replacement remains independently routable.
    _Retirement(
        provider="deepinfra",
        model_ids=frozenset({"qwen/qwen3-235b-a22b-thinking-2507"}),
        upstream_ids=frozenset({"Qwen/Qwen3-235B-A22B-Thinking-2507"}),
        effective_at=DEEPINFRA_QWEN3_235B_THINKING_RETIREMENT_AT,
    ),
    # Wafer announced that GLM 5.1, GLM 5.2 Fast, and Kimi K3 Fast retire on
    # 2026-08-17. Standard GLM 5.2 replaces both GLM routes, while Kimi K3
    # Standard replaces Kimi K3 Fast.
    # The notice did not specify a time zone, so use 00:00 UTC as the
    # conservative cutover. Other providers serving these models are
    # unaffected.
    _Retirement(
        provider="wafer",
        model_ids=frozenset(
            {
                "z-ai/glm-5.1",
                "z-ai/glm-5.2-fast",
                "moonshotai/kimi-k3-fast",
            }
        ),
        upstream_ids=frozenset(
            {
                "GLM-5.1",
                "GLM-5.2-Fast",
                "glm5.2-fast",
                "kimi-k3-fast",
            }
        ),
        effective_at=WAFER_AUGUST_2026_RETIREMENT_AT,
    ),
    # Crusoe announced that Nemotron 3 Ultra retires from its Serverless
    # offering at 2026-07-28 11:00 PT, which is 2026-07-28 18:00 UTC.
    # Other providers serving Nemotron 3 Ultra are unaffected.
    _Retirement(
        provider="crusoe",
        model_ids=frozenset({"nvidia/nemotron-3-ultra-550b"}),
        upstream_ids=frozenset({"nvidia/NVIDIA-Nemotron-3-Ultra-550B"}),
        effective_at=CRUSOE_NEMOTRON_3_ULTRA_RETIREMENT_AT,
    ),
    # Baseten announced that these Model API routes become inactive at
    # 2026-07-24 17:00 PT, which is 2026-07-25 00:00 UTC. Dedicated
    # deployments and other providers serving the same checkpoints are
    # unaffected.
    _Retirement(
        provider="baseten",
        model_ids=frozenset(
            {
                "z-ai/glm-5",
                "z-ai/glm-5.1",
                "moonshotai/kimi-k2.5",
                "nvidia/nemotron-120b-a12b",
            }
        ),
        upstream_ids=frozenset(
            {
                "zai-org/GLM-5",
                "zai-org/GLM-5.1",
                "moonshotai/Kimi-K2.5",
                "nvidia/Nemotron-120B-A12B",
            }
        ),
        effective_at=BASETEN_JULY_2026_RETIREMENT_AT,
    ),
    # Tinfoil announced that Kimi K2.6 retires on 2026-08-03 and that Kimi K3
    # is still being prepared. The notice did not specify a time zone, so use
    # 00:00 UTC as the conservative cutover. Other K2.6 providers are
    # unaffected, and Tinfoil K3 must not be advertised until its live model
    # feed publishes the route.
    _Retirement(
        provider="tinfoil",
        model_ids=frozenset({"moonshotai/kimi-k2.6"}),
        upstream_ids=frozenset({"kimi-k2-6"}),
        effective_at=TINFOIL_KIMI_K26_RETIREMENT_AT,
    ),
    # Parasail announced that these three serverless routes retire on
    # 2026-08-04. The notice did not specify a time zone, so use 00:00 UTC as
    # the conservative cutover. Other providers serving the same checkpoints
    # are unaffected.
    _Retirement(
        provider="parasail",
        model_ids=frozenset(
            {
                "z-ai/glm-5",
                "minimax/minimax-m2.5",
                "qwen/qwen3-235b-a22b-2507",
            }
        ),
        upstream_ids=frozenset(
            {
                "zai-org/GLM-5",
                "zai-org/GLM-5-FP8",
                "parasail-glm-5",
                "MiniMaxAI/MiniMax-M2.5",
                "parasail-minimax-m25",
                "Qwen/Qwen3-235B-A22B-Instruct-2507",
                "parasail-qwen3-235b-a22b-instruct-2507",
            }
        ),
        effective_at=PARASAIL_AUGUST_2026_RETIREMENT_AT,
    ),
    # Friendli announced that Qwen3-235B-A22B-Instruct-2507 retires from its
    # serverless Model API at 2026-08-05 00:00 UTC. Dedicated endpoints are
    # unaffected; TrustedRouter uses Friendli's serverless endpoint.
    _Retirement(
        provider="friendli",
        model_ids=frozenset({"qwen/qwen3-235b-a22b-2507"}),
        upstream_ids=frozenset({"Qwen/Qwen3-235B-A22B-Instruct-2507"}),
        effective_at=FRIENDLI_QWEN3_235B_RETIREMENT_AT,
    ),
    # Friendli announced that K-EXAONE-236B-A23B retires from its serverless
    # Model API at 2026-08-20 00:00 UTC. Dedicated endpoints are unaffected;
    # TrustedRouter uses Friendli's serverless endpoint.
    _Retirement(
        provider="friendli",
        model_ids=frozenset({"lgai-exaone/k-exaone-236b-a23b"}),
        upstream_ids=frozenset({"LGAI-EXAONE/K-EXAONE-236B-A23B"}),
        effective_at=FRIENDLI_K_EXAONE_236B_RETIREMENT_AT,
    ),
    # Together announced that its serverless MiniMax M2.7 route retires on
    # 2026-07-27 and named MiniMax M3 as the replacement. The announcement did
    # not include a time zone, so use 00:00 UTC as the conservative cutover.
    _Retirement(
        provider="together",
        model_ids=frozenset({"minimax/minimax-m2.7"}),
        upstream_ids=frozenset({"MiniMaxAI/MiniMax-M2.7"}),
        effective_at=TOGETHER_MINIMAX_M27_RETIREMENT_AT,
    ),
    _Retirement(
        provider="phala",
        model_ids=frozenset(
            {
                "z-ai/glm-4.7",
                "qwen/qwen3-30b-a3b-instruct-2507",
            }
        ),
        upstream_ids=frozenset(
            {
                "phala/glm-4.7",
                "phala/qwen3-30b-a3b-instruct-2507",
            }
        ),
        effective_at=PHALA_JULY_2026_EFFECTIVE_AT,
    ),
)


def _clock_override_permitted() -> bool:
    return (
        os.environ.get("TR_ENVIRONMENT", "").lower() == "test"
        or "pytest" in sys.modules
        or bool(os.environ.get("PYTEST_CURRENT_TEST"))
    )


def _utc_now() -> datetime:
    override = os.environ.get(LIFECYCLE_CLOCK_OVERRIDE_ENV)
    if override and _clock_override_permitted():
        # A malformed override raises rather than falling back to the wall
        # clock: a run that silently used the real clock would report the
        # very time-bomb coverage it failed to provide.
        return _effective_time(override)
    return datetime.now(UTC)


def latest_scheduled_cutover() -> datetime:
    """The latest effective-dated change this module schedules.

    CI pins the lifecycle clock one day past this so tests that only pass
    before a scheduled cutover fail on the pull request, not at midnight.
    """
    return max(
        DEEPSEEK_V4_PRICING_EFFECTIVE_AT,
        FIREWORKS_DSV4_FLASH_0731_PRICING_EFFECTIVE_AT,
        PHALA_JULY_2026_EFFECTIVE_AT,
        *(retirement.effective_at for retirement in _RETIREMENTS),
    )


def _effective_time(at: datetime | str | None) -> datetime:
    if at is None:
        return _utc_now()
    if isinstance(at, str):
        parsed = datetime.fromisoformat(at.replace("Z", "+00:00"))
    else:
        parsed = at
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def provider_model_retired(
    provider_slug: str,
    model_id: str,
    upstream_id: str | None = None,
    *,
    at: datetime | str | None = None,
) -> bool:
    effective_at = _effective_time(at)
    for retirement in _RETIREMENTS:
        if retirement.provider != provider_slug or effective_at < retirement.effective_at:
            continue
        if model_id in retirement.model_ids:
            return True
        if upstream_id is not None and upstream_id in retirement.upstream_ids:
            return True
    return False


def _deepseek_v4_family(provider_slug: str, model_id: str) -> str | None:
    if provider_slug != "deepseek":
        return None
    if model_id in _DEEPSEEK_V4_FLASH_MODEL_IDS:
        return "flash"
    if model_id == "deepseek/deepseek-v4-pro":
        return "pro"
    return None


def _deepseek_v4_period(effective_at: datetime) -> str:
    if effective_at < DEEPSEEK_V4_PRICING_EFFECTIVE_AT:
        return "legacy"
    seconds_since_midnight = (
        effective_at.hour * 60 * 60 + effective_at.minute * 60 + effective_at.second
    )
    if any(
        start <= seconds_since_midnight < end
        for start, end in DEEPSEEK_V4_PEAK_WINDOWS_UTC
    ):
        return "peak"
    return "off_peak"


def provider_pricing_schedule(
    provider_slug: str,
    model_id: str,
    *,
    at: datetime | str | None = None,
) -> dict[str, object] | None:
    """Public timing metadata for a provider's variable token pricing."""
    effective_at = _effective_time(at)

    if (
        provider_slug == "fireworks"
        and model_id == _FIREWORKS_DSV4_FLASH_0731_MODEL_ID
    ):
        return {
            "kind": "fixed_cutover",
            "timezone": "UTC",
            "effective_at": (
                FIREWORKS_DSV4_FLASH_0731_PRICING_EFFECTIVE_AT.isoformat().replace(
                    "+00:00", "Z"
                )
            ),
            "current_period": (
                "legacy"
                if effective_at < FIREWORKS_DSV4_FLASH_0731_PRICING_EFFECTIVE_AT
                else "new"
            ),
            "rate_locked_at": "authorization",
        }

    if _deepseek_v4_family(provider_slug, model_id) is None:
        return None

    def clock(seconds: int) -> str:
        return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}"

    return {
        "kind": "time_of_day",
        "timezone": "UTC",
        "effective_at": DEEPSEEK_V4_PRICING_EFFECTIVE_AT.isoformat().replace("+00:00", "Z"),
        "current_period": _deepseek_v4_period(effective_at),
        "peak_multiplier": 2,
        "peak_windows": [
            {"start": clock(start), "end": clock(end)}
            for start, end in DEEPSEEK_V4_PEAK_WINDOWS_UTC
        ],
        # Authorization time, not settlement time, selects the period so a
        # long stream cannot change price midway through the request.
        "rate_locked_at": "authorization",
    }


def provider_price_microdollars(
    provider_slug: str,
    model_id: str,
    *,
    at: datetime | str | None = None,
) -> ProviderPrice | None:
    """Return an announced provider cost override, before or after cutover.

    Pinning both sides prevents a provider API from publishing the new price
    early and makes the exact advertised transition deterministic.
    """
    effective_at = _effective_time(at)
    family = _deepseek_v4_family(provider_slug, model_id)
    if family is not None:
        return _DEEPSEEK_V4_PRICES[family][_deepseek_v4_period(effective_at)]

    if (
        provider_slug == "fireworks"
        and model_id == _FIREWORKS_DSV4_FLASH_0731_MODEL_ID
    ):
        period = (
            "legacy"
            if effective_at < FIREWORKS_DSV4_FLASH_0731_PRICING_EFFECTIVE_AT
            else "new"
        )
        return _FIREWORKS_DSV4_FLASH_0731_PRICES[period]

    if provider_slug == "phala" and model_id == "qwen/qwen-2.5-7b-instruct":
        if effective_at < PHALA_JULY_2026_EFFECTIVE_AT:
            return ProviderPrice(40_000, 100_000)
        return ProviderPrice(100_000, 200_000)
    return None
