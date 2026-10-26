from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException

from trusted_router.config import Settings
from trusted_router.errors import api_error
from trusted_router.sentry_config import before_send, reset_sentry_floodgate_for_tests


@pytest.fixture(autouse=True)
def reset_budget():
    reset_sentry_floodgate_for_tests(settings=Settings(
        environment="test", sentry_floodgate_max_events_per_fingerprint=1,
    ))
    yield
    reset_sentry_floodgate_for_tests()


def capture(
    milliseconds: int, *, message: str = "Workspace billing is paused",
    transaction: str = "gateway.authorize", explicit: list[str] | None = None,
    source: str = "router",
) -> dict[str, Any] | None:
    exc = api_error(503, message, "service_unavailable", source=source)
    exc.detail["data"] = {"timing": {"total_ms": milliseconds}, "request_id": str(milliseconds)}
    event: dict[str, Any] = {
        "exception": {"values": [{"type": "HTTPException", "value": str(exc.detail)}]},
        "transaction": transaction,
        "transaction_info": {"source": "component"},
    }
    if explicit:
        event["fingerprint"] = explicit
    return before_send(event, {"exc_info": (HTTPException, exc, None)})


def test_request_timings_do_not_bypass_duplicate_limit() -> None:
    assert capture(12) is not None
    assert capture(23) is None
    assert capture(89) is None


def test_distinct_errors_and_routes_still_reach_sentry() -> None:
    first = capture(12)
    other_error = capture(23, message="Settlement durability is temporarily unavailable")
    other_route = capture(89, transaction="gateway.settle")
    assert first and other_error and other_route
    assert len({tuple(e["fingerprint"]) for e in (first, other_error, other_route)}) == 3


def test_preserves_explicit_fingerprint() -> None:
    event = capture(12, explicit=["billing-invariant", "negative-balance"])
    assert event is not None
    assert event["fingerprint"] == ["billing-invariant", "negative-balance"]


def test_provider_exception_grouping_is_unchanged() -> None:
    event = capture(12, source="provider")
    assert event is not None
    assert "fingerprint" not in event


def test_fingerprint_never_exports_raw_error_message() -> None:
    message = "sensitive opaque message for grouping only"
    event = capture(12, message=message)
    assert event is not None
    assert message not in str(event["fingerprint"])


def test_unknown_http_exception_shape_is_unchanged() -> None:
    exc = HTTPException(503, "plain exception")
    event = before_send({"message": "plain exception"}, {"exc_info": (HTTPException, exc, None)})
    assert event is not None
    assert "fingerprint" not in event
