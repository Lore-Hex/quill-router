"""Effective-dated provider model retirements and announced price changes.

Live provider catalog refreshes remain the normal source of truth. This module
handles provider announcements with a precise future cutover so routing and
billing do not depend on an hourly refresh landing at exactly the right second.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

PHALA_JULY_2026_EFFECTIVE_AT = datetime(2026, 7, 29, 18, 0, tzinfo=UTC)
TOGETHER_MINIMAX_M27_RETIREMENT_AT = datetime(2026, 7, 27, 0, 0, tzinfo=UTC)
FRIENDLI_QWEN3_235B_RETIREMENT_AT = datetime(2026, 8, 5, 0, 0, tzinfo=UTC)


@dataclass(frozen=True)
class ProviderPrice:
    prompt_microdollars_per_million_tokens: int
    completion_microdollars_per_million_tokens: int


@dataclass(frozen=True)
class _Retirement:
    provider: str
    model_ids: frozenset[str]
    upstream_ids: frozenset[str]
    effective_at: datetime


_RETIREMENTS = (
    # Friendli announced that Qwen3-235B-A22B-Instruct-2507 retires from its
    # serverless Model API at 2026-08-05 00:00 UTC. Dedicated endpoints are
    # unaffected; TrustedRouter uses Friendli's serverless endpoint.
    _Retirement(
        provider="friendli",
        model_ids=frozenset({"qwen/qwen3-235b-a22b-2507"}),
        upstream_ids=frozenset({"Qwen/Qwen3-235B-A22B-Instruct-2507"}),
        effective_at=FRIENDLI_QWEN3_235B_RETIREMENT_AT,
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


def _utc_now() -> datetime:
    return datetime.now(UTC)


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
    if provider_slug != "phala" or model_id != "qwen/qwen-2.5-7b-instruct":
        return None
    if _effective_time(at) < PHALA_JULY_2026_EFFECTIVE_AT:
        return ProviderPrice(40_000, 100_000)
    return ProviderPrice(100_000, 200_000)
