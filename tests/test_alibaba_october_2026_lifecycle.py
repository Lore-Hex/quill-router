from __future__ import annotations

from datetime import UTC, datetime

from trusted_router import provider_lifecycle

_BEFORE_CUTOFF = datetime(2026, 10, 9, 15, 59, 59, tzinfo=UTC)
_CUTOFF = datetime(2026, 10, 9, 16, 0, tzinfo=UTC)


def test_alibaba_october_retirements_are_effective_at_announced_cutoff() -> None:
    affected = (
        ("qwen/qwen3-max", "qwen3-max"),
        ("qwen/qwen3-vl-flash", "qwen3-vl-flash"),
        ("qwen/qwen3-coder-next", "qwen3-coder-next"),
        ("qwen/qwen3-235b-a22b-instruct-2507", "qwen3-235b-a22b-instruct-2507"),
        ("qwen/qwen-mt-turbo", "qwen-mt-turbo"),
        ("deepseek/deepseek-v3.2", "deepseek-v3.2"),
        ("deepseek/deepseek-v4-flash", "deepseek-v4-flash"),
        ("deepseek/deepseek-v4-flash-us", "deepseek-v4-flash-us"),
        ("z-ai/glm-4.7", "glm-4.7"),
    )

    for model_id, upstream_id in affected:
        assert not provider_lifecycle.provider_model_retired(
            "alibaba", model_id, upstream_id, at=_BEFORE_CUTOFF
        )
        assert provider_lifecycle.provider_model_retired(
            "alibaba", model_id, upstream_id, at=_CUTOFF
        )


def test_alibaba_canceled_retirements_remain_available() -> None:
    canceled = (
        ("qwen/qwen-vl-ocr", "qwen-vl-ocr"),
        ("qwen/qwen-mt-image", "qwen-mt-image"),
        ("qwen/qwen3-asr-flash-us", "qwen3-asr-flash-us"),
        (
            "qwen/qwen3-asr-flash-2025-09-08-us",
            "qwen3-asr-flash-2025-09-08-us",
        ),
        ("qwen/qwen3-livetranslate-flash", "qwen3-livetranslate-flash"),
        (
            "qwen/qwen3-livetranslate-flash-2025-12-01",
            "qwen3-livetranslate-flash-2025-12-01",
        ),
    )

    for model_id, upstream_id in canceled:
        assert not provider_lifecycle.provider_model_retired(
            "alibaba", model_id, upstream_id, at=_CUTOFF
        )


def test_alibaba_retirement_does_not_disable_other_providers() -> None:
    assert not provider_lifecycle.provider_model_retired(
        "novita",
        "qwen/qwen3-32b",
        "qwen/qwen3-32b",
        at=_CUTOFF,
    )


def test_alibaba_deepseek_v4_flash_ga_replacements_remain_available() -> None:
    replacements = (
        ("deepseek/deepseek-v4-flash-0731", "deepseek-v4-flash-0731"),
        ("deepseek/deepseek-v4-flash-0731-us", "deepseek-v4-flash-0731-us"),
    )

    for model_id, upstream_id in replacements:
        assert not provider_lifecycle.provider_model_retired(
            "alibaba", model_id, upstream_id, at=_CUTOFF
        )


def test_alibaba_deepseek_v4_flash_retirement_is_provider_scoped() -> None:
    assert not provider_lifecycle.provider_model_retired(
        "another-provider",
        "deepseek/deepseek-v4-flash",
        "deepseek-v4-flash",
        at=_CUTOFF,
    )
