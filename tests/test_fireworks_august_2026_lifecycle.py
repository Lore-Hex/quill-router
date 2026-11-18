from __future__ import annotations

from datetime import UTC, datetime, timedelta

from trusted_router import provider_lifecycle

_AUGUST_CUTOFF = datetime(2026, 8, 27, 0, 0, tzinfo=UTC)
_QWEN_CUTOFF = datetime(2026, 9, 4, 0, 0, tzinfo=UTC)
_AUGUST_RETIRING = {
    "minimax/minimax-m2.7": "accounts/fireworks/models/minimax-m2p7",
    "openai/gpt-oss-20b": "accounts/fireworks/models/gpt-oss-20b",
    "moonshotai/kimi-k2.6-fast": "accounts/fireworks/routers/kimi-k2p6-turbo",
    "moonshotai/kimi-k2.7-code-fast": "accounts/fireworks/routers/kimi-k2p7-code-fast",
    "deepseek/deepseek-v4-pro": "accounts/fireworks/models/deepseek-v4-pro",
}
_AUGUST_REPLACEMENTS = {
    "minimax/minimax-m3": "accounts/fireworks/models/minimax-m3",
    "nvidia/nemotron-3.5-lightning": (
        "accounts/fireworks/models/nemotron-lightning-3p5-30b-a3b"
    ),
    "z-ai/glm-5.2": "accounts/fireworks/models/glm-5p2",
    "deepseek/deepseek-v4-pro-0813": "accounts/fireworks/models/deepseek-v4-pro-0813",
}
_STANDARD_KIMI = {
    "moonshotai/kimi-k2.6": "accounts/fireworks/models/kimi-k2p6",
    "moonshotai/kimi-k2.7-code": "accounts/fireworks/models/kimi-k2p7-code",
}


def test_fireworks_august_retirement_spares_standard_kimi_and_replacements() -> None:
    # The cutoff itself, scope and the refresh and catalog paths are checked
    # for every retirement in tests/test_provider_retirements_generic.py.
    for model_id, upstream_id in {**_STANDARD_KIMI, **_AUGUST_REPLACEMENTS}.items():
        assert not provider_lifecycle.provider_model_retired(
            "fireworks",
            model_id,
            upstream_id,
            at=_AUGUST_CUTOFF,
        )


def test_fireworks_qwen_extension_uses_its_own_cutoff() -> None:
    old_model = "qwen/qwen3.7-plus"
    old_upstream = "accounts/fireworks/models/qwen3p7-plus"

    assert not provider_lifecycle.provider_model_retired(
        "fireworks",
        old_model,
        old_upstream,
        at=_QWEN_CUTOFF - timedelta(microseconds=1),
    )
    assert provider_lifecycle.provider_model_retired(
        "fireworks",
        old_model,
        old_upstream,
        at=_QWEN_CUTOFF,
    )
    assert not provider_lifecycle.provider_model_retired(
        "fireworks",
        "qwen/qwen3.8-max",
        "accounts/fireworks/models/qwen3p8-max",
        at=_QWEN_CUTOFF,
    )
