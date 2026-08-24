"""Property tests for rollover / failure-attribution coherence.

Two modules independently decide who owns a failed provider call:

  * services/inference_errors.py decides whether to try the next candidate
    (is_rollover_http_error over the exception provider_http_error built);
  * provider_reliability.classify_provider_failure decides who gets blamed in
    the analytics that feed the leaderboard and default routing.

They must agree. The law:

    for every upstream status s,
        classify_provider_failure(...).owner in {CUSTOMER, CONFIGURATION}
            =>  not is_rollover_http_error(provider_http_error(s))

It was false for 400 and 422. provider_http_error's if/elif chain has a default
branch mapping every unlisted status to 502/PROVIDER_ERROR, which
is_rollover_http_error treats as rollover-eligible — so a malformed request
walked the entire candidate ladder, each provider rejecting it identically,
and surfaced to the caller as a 502 blaming the provider for their own body.
Meanwhile the attribution module already called it CONFIGURATION.

The fix is deliberately narrow. "Every unlisted 4xx is deterministic" would be
wrong: 408 and 425 are transient, and 402 can mean the TrustedRouter account
for that provider is out of capacity — all cases where another provider may
genuinely succeed. Only 400 and 422 are re-classified, and the property below
pins that the transient and capacity statuses still roll over, so a later
"tidy-up" cannot widen the set and quietly disable failover.

Quantifying over the whole 100..599 range rather than a sampled list is what
makes this a coherence contract instead of another example table: the two
modules' branch structures do not line up, so the disagreement lived in a
status neither module's own tests enumerated.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from trusted_router.provider_reliability import FailureOwner, classify_provider_failure
from trusted_router.providers import ProviderError
from trusted_router.services.inference_errors import (
    is_rollover_http_error,
    provider_http_error,
)
from trusted_router.types import ErrorType

# Statuses where the next provider genuinely may succeed, so failover must stay
# enabled. Pinned explicitly because narrowing rollover is a availability risk
# in the opposite direction from the bug being fixed.
RETRYABLE_STATUSES = [402, 408, 425, 429, 500, 502, 503, 504, 529]

# Statuses the caller must own: every provider will reject these identically.
DETERMINISTIC_CLIENT_STATUSES = [400, 404, 422]


def _surfaced(status: int) -> tuple[int, str, bool]:
    exc = provider_http_error(ProviderError("acme", status, "boom"))
    error_type = exc.detail["error"]["type"]
    return exc.status_code, error_type, is_rollover_http_error(exc)


def _attribution(status: int, error_type: str) -> FailureOwner:
    return classify_provider_failure(
        status="error", error_status=status, error_type=error_type
    ).owner


# ----------------------------------------------------------------- the law ---


@given(status=st.integers(min_value=100, max_value=599))
@settings(max_examples=600)
def test_a_failure_the_customer_owns_is_never_rolled_over(status: int) -> None:
    """The two modules never disagree about who owns a failure.

    Quantified over the full range because the modules' branch structures do
    not line up: the disagreement lived in statuses neither module's own tests
    enumerated.
    """
    _, error_type, rollover = _surfaced(status)
    owner = _attribution(status, error_type)

    if owner in {FailureOwner.CUSTOMER, FailureOwner.CONFIGURATION}:
        assert not rollover, (
            f"upstream {status} is attributed to {owner.value} but is rollover-eligible: "
            f"a deterministic failure would be re-sent to every remaining candidate"
        )


@pytest.mark.parametrize("status", DETERMINISTIC_CLIENT_STATUSES)
def test_a_deterministic_client_error_stops_the_ladder(status: int) -> None:
    surfaced_status, _, rollover = _surfaced(status)
    assert not rollover
    assert 400 <= surfaced_status < 500, (
        f"upstream {status} surfaced as {surfaced_status}: a malformed request must not "
        f"be reported to the caller as a provider fault"
    )


@pytest.mark.parametrize("status", RETRYABLE_STATUSES)
def test_transient_and_capacity_failures_still_roll_over(status: int) -> None:
    """The other direction, and the one worth guarding hardest.

    Narrowing rollover is an availability risk: 408 and 425 are transient and
    402 can mean this provider's account is exhausted, so the next candidate
    may well succeed. A future widening of the deterministic set would disable
    failover for them, and this test is what stops it.
    """
    assert _surfaced(status)[2], f"upstream {status} must remain rollover-eligible"


# ------------------------------------------------------------ regressions ---


def test_the_two_statuses_that_used_to_walk_the_whole_ladder() -> None:
    for status in (400, 422):
        surfaced_status, error_type, rollover = _surfaced(status)
        assert surfaced_status == status
        assert error_type == ErrorType.BAD_REQUEST
        assert not rollover


def test_unlisted_statuses_still_default_to_a_provider_fault() -> None:
    """The fix is narrow on purpose: only 400 and 422 moved. An unrecognised
    status is still treated as the provider's problem and stays retryable."""
    for status in (418, 451, 200):
        surfaced_status, error_type, rollover = _surfaced(status)
        assert surfaced_status == 502
        assert error_type == ErrorType.PROVIDER_ERROR
        assert rollover


def test_auth_failures_keep_their_existing_shape() -> None:
    """401/403 were already non-rollover via the status gate rather than the
    type gate; pinned so the new branch above did not disturb them."""
    for status in (401, 403):
        surfaced_status, error_type, rollover = _surfaced(status)
        assert surfaced_status == 401
        assert error_type == ErrorType.PROVIDER_AUTH_ERROR
        assert not rollover


@given(status=st.integers(min_value=100, max_value=599))
@settings(max_examples=300)
def test_attribution_is_total_and_availability_blame_is_provider_owned(
    status: int,
) -> None:
    """Totality, and the invariant that only a provider-owned failure counts
    against a provider's published availability."""
    _, error_type, _ = _surfaced(status)
    attribution = classify_provider_failure(
        status="error", error_status=status, error_type=error_type
    )
    assert attribution.owner in set(FailureOwner)
    if attribution.counts_toward_provider_availability:
        assert attribution.owner in {FailureOwner.NONE, FailureOwner.PROVIDER}
