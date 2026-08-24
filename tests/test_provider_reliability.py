from __future__ import annotations

import pytest

from trusted_router.catalog_data import ModelEndpoint
from trusted_router.provider_reliability import (
    FailureClass,
    FailureOwner,
    classify_provider_failure,
    model_deadlines,
)


def test_failure_attribution_separates_provider_capacity_from_router_faults() -> None:
    overloaded = classify_provider_failure(
        status="error",
        error_type="rate_limit_error",
        error_status=429,
    )
    router = classify_provider_failure(
        status="error",
        error_type="router_error",
        error_status=503,
    )

    assert overloaded.owner is FailureOwner.PROVIDER
    assert overloaded.failure_class is FailureClass.PROVIDER_CAPACITY
    assert overloaded.capacity_rejected is True
    assert overloaded.counts_toward_provider_availability is True
    assert router.owner is FailureOwner.TRUSTEDROUTER
    assert router.failure_class is FailureClass.ROUTER_FAULT
    assert router.counts_toward_provider_availability is False


def test_provider_account_quota_is_owned_by_trustedrouter() -> None:
    result = classify_provider_failure(
        status="error",
        error_type="rate_limit_error",
        error_status=429,
        error_message="Account quota exceeded for this project",
    )

    assert result.owner is FailureOwner.TRUSTEDROUTER
    assert result.failure_class is FailureClass.TRUSTEDROUTER_CAPACITY
    assert result.capacity_rejected is True


def test_monitor_account_unavailable_is_a_router_fault_not_a_capacity_rejection() -> None:
    # The probe never reached the provider: this type is only emitted when the
    # MONITOR's own router account is unusable (probes.py, source == "router"
    # plus an "insufficient credits"/"invalid api key" message). It is monitor
    # trouble, so it must not read as an upstream capacity rejection — a bare
    # 402 still does, and that path is asserted alongside it here so removing
    # the redundant leg cannot quietly take the real one with it.
    monitor = classify_provider_failure(
        status="error",
        error_type="monitor_account_unavailable",
        error_status=None,
    )
    upstream_payment_required = classify_provider_failure(
        status="error",
        error_type="provider_error",
        error_status=402,
    )

    assert monitor.owner is FailureOwner.TRUSTEDROUTER
    assert monitor.failure_class is FailureClass.ROUTER_FAULT
    assert monitor.counts_toward_provider_availability is False
    assert monitor.capacity_rejected is False
    assert upstream_payment_required.failure_class is FailureClass.TRUSTEDROUTER_CAPACITY
    assert upstream_payment_required.capacity_rejected is True


def test_ambiguous_error_is_not_charged_to_provider() -> None:
    result = classify_provider_failure(
        status="error",
        error_type="mystery",
        error_status=None,
    )

    assert result.owner is FailureOwner.UNKNOWN
    assert result.counts_toward_provider_availability is False


def test_model_deadlines_are_model_specific_and_bounded() -> None:
    fast = model_deadlines("cerebras/gpt-oss-120b")
    normal = model_deadlines("meta/llama-3.3-70b")
    reasoning = model_deadlines("anthropic/claude-opus-4.8")

    assert fast.first_token_seconds == 15
    assert normal.first_token_seconds == 20
    assert reasoning.first_token_seconds == 45
    assert fast.completion_seconds < reasoning.completion_seconds


def test_provider_contract_deadlines_override_generic_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trusted_router.catalog import MODEL_ENDPOINTS

    endpoint = ModelEndpoint(
        id="test/deadline-model@neurometric/prepaid",
        model_id="test/deadline-model",
        provider="neurometric",
        usage_type="Credits",
        upstream_id="test/deadline-model",
        first_token_timeout_seconds=37,
        completion_timeout_seconds=456,
        stream_idle_timeout_seconds=12,
    )
    monkeypatch.setitem(MODEL_ENDPOINTS, endpoint.id, endpoint)

    deadlines = model_deadlines("test/deadline-model", provider="neurometric")

    assert deadlines.first_token_seconds == 37
    assert deadlines.completion_seconds == 456


def test_model_deadline_rejects_invalid_default() -> None:
    try:
        model_deadlines("test/model", default_first_token_seconds=0)
    except ValueError as exc:
        assert "positive" in str(exc)
    else:
        raise AssertionError("expected invalid timeout to fail")
