from datetime import UTC, datetime

import pytest

from scripts.pricing.manifest import models_requiring_canary
from scripts.pricing.providers import fal, nvidia_nim
from trusted_router import catalog
from trusted_router.provider_contracts import provider_model_operator_held
from trusted_router.provider_lifecycle import provider_model_retired
from trusted_router.synthetic.probes import rotation_candidates


def test_fal_promotion_with_third_resolution_keeps_existing_standard_rates() -> None:
    page = """
    Video costs $0.0325 per second at 480p, $0.052 per second at 768p,
    and $0.104 per second at 1080p. These are promotional rates.
    The discount ends October 31, after which 480p is $0.05 /second,
    768p is $0.08 /second, and 1080p is $0.16 /second.
    """
    assert fal._h3_max_standard_rates(page) == {"480p": 50_000, "768p": 80_000}


def test_fal_comma_prices_still_reject_conflicting_standard_rates() -> None:
    with pytest.raises(RuntimeError, match="missing or ambiguous"):
        fal._h3_max_standard_rates(
            "after which 480p is $0.05/second, 768p is $0.08/second, and 1080p is $0.16/second. "
            "after which 480p is $0.06/second and 768p is $0.09/second."
        )


def test_streamlake_deprecated_vl_thinking_is_not_substituted() -> None:
    model = "qwen/qwen3-vl-235b-a22b-thinking"
    native = "Qwen3-VL-235B-A22B-Thinking"
    assert provider_model_retired(
        "streamlake", model, native, at=datetime(2026, 10, 29, tzinfo=UTC),
    )
    assert not provider_model_retired("novita", model, native)
    assert not any(e.provider == "streamlake" for e in catalog.endpoints_for_model(model))
    assert model not in rotation_candidates().get("streamlake", [])


def test_pearl_unknown_gemma_is_held_without_affecting_other_providers() -> None:
    model = "google/gemma-4-31b-it"
    assert provider_model_operator_held("pearl", model, "google/gemma-4-31B-it")
    assert not provider_model_operator_held("novita", model)
    assert not provider_model_operator_held("pearl", "z-ai/glm-5.3")
    assert not any(e.provider == "pearl" for e in catalog.endpoints_for_model(model))
    assert model not in rotation_candidates().get("pearl", [])


def test_nvidia_stalled_vision_route_requires_successful_recovery_canary() -> None:
    model = "meta-llama/llama-3.2-90b-vision-instruct"
    assert not any(e.provider == "nvidia-nim" for e in catalog.endpoints_for_model(model))
    assert model in models_requiring_canary(nvidia_nim.MANIFEST_PATH, {model})
    assert not provider_model_retired("nvidia-nim", model)
