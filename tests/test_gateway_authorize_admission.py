from __future__ import annotations

import asyncio
import threading

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from trusted_router.config import Settings
from trusted_router.routes.internal import gateway
from trusted_router.schemas import GatewayAuthorizeRequest
from trusted_router.services.keyed_admission import KeyedConcurrencyAdmission


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/", "headers": []})


def _body(subject: str) -> GatewayAuthorizeRequest:
    return GatewayAuthorizeRequest(
        api_key_lookup_hash=subject,
        model="anthropic/claude-haiku-4.5",
        estimated_input_tokens=1,
        max_output_tokens=1,
    )


def test_keyed_admission_isolates_subjects_and_releases_capacity() -> None:
    admission = KeyedConcurrencyAdmission(max_subjects=2)

    assert admission.try_acquire("hot", limit=2)
    assert admission.try_acquire("hot", limit=2)
    assert not admission.try_acquire("hot", limit=2)
    assert admission.try_acquire("other", limit=1)
    assert not admission.try_acquire("third", limit=1)

    admission.release("hot")
    assert admission.try_acquire("hot", limit=2)
    admission.release("hot")
    admission.release("hot")
    assert admission.count("hot") == 0
    assert admission.try_acquire("third", limit=1)


def test_authorize_rejects_same_key_saturation_without_queueing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject = "lookup-admission-regression"
    started = threading.Event()
    release = threading.Event()

    def blocking_authorize(*_args: object) -> dict[str, object]:
        started.set()
        assert release.wait(timeout=5)
        return {"data": {"authorization_id": "gwa-admitted"}}

    monkeypatch.setattr(gateway, "_authorize_gateway_sync", blocking_authorize)
    settings = Settings(
        environment="test",
        gateway_authorize_max_in_flight_per_key=1,
    )

    async def scenario() -> None:
        first = asyncio.create_task(gateway.authorize_gateway(_request(), _body(subject), settings))
        assert await asyncio.to_thread(started.wait, 5)

        with pytest.raises(HTTPException) as raised:
            await gateway.authorize_gateway(_request(), _body(subject), settings)
        assert raised.value.status_code == 429
        assert raised.value.headers == {"Retry-After": "1"}
        assert raised.value.detail["error"]["type"] == "rate_limited"

        # Another key retains capacity while the hot key is blocked.
        other = asyncio.create_task(
            gateway.authorize_gateway(_request(), _body(f"{subject}-other"), settings)
        )
        release.set()
        assert (await first)["data"]["authorization_id"] == "gwa-admitted"
        assert (await other)["data"]["authorization_id"] == "gwa-admitted"

        # The finally block returns capacity after completion.
        assert gateway._AUTHORIZE_ADMISSION.count(subject) == 0
        again = await gateway.authorize_gateway(_request(), _body(subject), settings)
        assert again["data"]["authorization_id"] == "gwa-admitted"

    asyncio.run(scenario())


def test_gateway_authorize_admission_limit_must_be_positive() -> None:
    with pytest.raises(
        ValueError,
        match="TR_GATEWAY_AUTHORIZE_MAX_IN_FLIGHT_PER_KEY must be positive",
    ):
        Settings(environment="test", gateway_authorize_max_in_flight_per_key=0)
