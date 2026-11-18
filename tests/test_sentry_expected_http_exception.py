"""A designed retryable answer is not a Sentry error event (QUILL-ROUTER-EE, -DV, -H3).

The raise sites log the cause; the caller retries or starts over by design.
The end-to-end test runs the real Sentry SDK with the production integrations
and `before_send`, so it fails if the SDK stops handing the exception over in
the hint shape `before_send` reads.
"""

from __future__ import annotations

from typing import Any

import pytest
import sentry_sdk
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration
from sentry_sdk.transport import Transport

from trusted_router.config import Settings
from trusted_router.errors import ExpectedHTTPException, api_error, expected_api_error
from trusted_router.sentry_config import (
    SENTRY_FAILED_REQUEST_STATUS_CODES,
    before_send,
    reset_sentry_floodgate_for_tests,
)


@pytest.fixture(autouse=True)
def reset_budget():
    reset_sentry_floodgate_for_tests(settings=Settings(environment="test"))
    yield
    reset_sentry_floodgate_for_tests()


def _event(exc: HTTPException) -> dict[str, Any] | None:
    event: dict[str, Any] = {
        "exception": {"values": [{"type": "HTTPException", "value": str(exc.detail)}]},
        "transaction": "gateway.heartbeat",
        "transaction_info": {"source": "component"},
    }
    return before_send(event, {"exc_info": (type(exc), exc, None)})


def test_expected_answer_is_dropped_and_the_same_answer_from_api_error_is_kept() -> None:
    args = (503, "Stage D heartbeat owner unavailable; retry", "service_unavailable")
    expected = expected_api_error(*args, headers={"Retry-After": "1"})
    ordinary = api_error(*args, headers={"Retry-After": "1"})
    assert isinstance(expected, ExpectedHTTPException)
    # The client sees exactly the same response either way.
    assert (expected.status_code, expected.detail, expected.headers) == (
        ordinary.status_code, ordinary.detail, ordinary.headers,
    )
    assert _event(expected) is None
    assert _event(ordinary) is not None


class _Captured(Transport):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[dict[str, Any]] = []

    def capture_envelope(self, envelope: Any) -> None:
        for item in envelope.items:
            if item.type == "event":
                self.events.append(item.payload.json)


def test_the_real_sdk_drops_only_the_expected_503() -> None:
    app = FastAPI()

    @app.get("/expected")
    def expected() -> None:
        raise expected_api_error(503, "Google did not respond; please sign in again", "service_unavailable")

    @app.get("/ordinary")
    def ordinary() -> None:
        raise api_error(503, "Settlement durability is temporarily unavailable", "service_unavailable")

    transport = _Captured()
    sentry_sdk.init(
        dsn="https://public@sentry.invalid/1",
        transport=transport,
        default_integrations=False,
        integrations=[
            StarletteIntegration(
                transaction_style="endpoint",
                failed_request_status_codes=SENTRY_FAILED_REQUEST_STATUS_CODES,
            ),
            FastApiIntegration(
                transaction_style="endpoint",
                failed_request_status_codes=SENTRY_FAILED_REQUEST_STATUS_CODES,
            ),
        ],
        before_send=before_send,  # type: ignore[arg-type]
    )
    try:
        with TestClient(app) as http:
            assert http.get("/expected").status_code == 503
            assert http.get("/ordinary").status_code == 503
        sentry_sdk.flush()
    finally:
        sentry_sdk.get_client().close()
        sentry_sdk.init()  # a disabled client for the rest of the session
    values = [
        value["value"]
        for event in transport.events
        for value in event["exception"]["values"]
    ]
    assert len(transport.events) == 1, values
    assert any("Settlement durability" in value for value in values)
    assert not any("Google did not respond" in value for value in values)
