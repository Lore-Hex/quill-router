from __future__ import annotations

import asyncio
import datetime as dt
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.routes.internal import synthetic as synthetic_routes

_OBSERVER_TOKEN = "observer-admission-token"  # noqa: S105 - test token.


@pytest.fixture(autouse=True)
def _reset_operation_limits() -> Iterator[None]:
    synthetic_routes._OPERATION_RATE_LIMITS.reset()  # noqa: SLF001
    yield
    deadline = time.monotonic() + 2
    while synthetic_routes._BACKGROUND_RUNS and time.monotonic() < deadline:  # noqa: SLF001
        time.sleep(0.01)
    assert not synthetic_routes._BACKGROUND_RUNS  # noqa: SLF001
    synthetic_routes._OPERATION_RATE_LIMITS.reset()  # noqa: SLF001


def _client(
    *,
    raise_server_exceptions: bool = True,
    **settings_overrides: Any,
) -> TestClient:
    settings = Settings(
        environment="test",
        service_surface="internal",
        internal_gateway_token="billing-admission-token",  # noqa: S106
        observer_internal_token=_OBSERVER_TOKEN,
        rate_limit_internal_per_window=10_000,
        **settings_overrides,
    )
    return TestClient(
        create_app(settings, init_observability=False),
        raise_server_exceptions=raise_server_exceptions,
    )


def _headers() -> dict[str, str]:
    return {"x-trustedrouter-internal-token": _OBSERVER_TOKEN}


def test_detached_synthetic_runs_are_single_flight_and_share_scheduler_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    release = threading.Event()
    run_calls = 0
    body_calls = 0
    original_json_body = synthetic_routes.json_body

    async def tracked_json_body(request: object) -> dict[str, object]:
        nonlocal body_calls
        body_calls += 1
        return await original_json_body(request)  # type: ignore[arg-type]

    async def blocked(
        _settings: Settings,
        _body: dict[str, object],
    ) -> dict[str, object]:
        nonlocal run_calls
        run_calls += 1
        started.set()
        await asyncio.to_thread(release.wait)
        return {"data": {"recorded": 0}}

    monkeypatch.setattr(synthetic_routes, "json_body", tracked_json_body)
    monkeypatch.setattr(synthetic_routes, "_run_and_record", blocked)
    with _client() as client:
        try:
            first = client.post(
                "/v1/internal/synthetic/run",
                headers=_headers(),
                json={"detach": True, "rotation_count": 8},
            )
            assert first.status_code == 202
            assert started.wait(timeout=1)

            overlaps = [
                client.post(
                    "/v1/internal/synthetic/run",
                    headers=_headers(),
                    json={"detach": True, "rotation_count": 8},
                )
                for _ in range(20)
            ]
            scheduler_result = asyncio.run(
                synthetic_routes.run_synthetic_pass(
                    client.app.state.settings,
                    rotation_count=8,
                )
            )

            assert {response.status_code for response in overlaps} == {429}
            assert all(response.headers["retry-after"] for response in overlaps)
            assert scheduler_result == {
                "data": {"scheduled": False, "reason": "already_running"}
            }
            assert run_calls == 1
            assert body_calls == 1
        finally:
            release.set()


def test_detached_run_deadline_releases_slot_and_logs_tripwire(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    started = threading.Event()
    run_calls = 0

    async def blocked_forever(
        _settings: Settings,
        _body: dict[str, object],
    ) -> dict[str, object]:
        nonlocal run_calls
        run_calls += 1
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(synthetic_routes, "_run_and_record", blocked_forever)
    with caplog.at_level("ERROR"), _client(synthetic_run_deadline_seconds=0.2) as client:
        first = client.post(
            "/v1/internal/synthetic/run",
            headers=_headers(),
            json={"detach": True, "rotation_count": 8},
        )
        assert first.status_code == 202
        assert started.wait(timeout=1)

        overlap = client.post(
            "/v1/internal/synthetic/run",
            headers=_headers(),
            json={"detach": True},
        )
        assert overlap.status_code == 429

        deadline = time.monotonic() + 2
        while synthetic_routes._BACKGROUND_RUNS and time.monotonic() < deadline:  # noqa: SLF001
            time.sleep(0.01)
        assert not synthetic_routes._BACKGROUND_RUNS  # noqa: SLF001

        recovered = client.post(
            "/v1/internal/synthetic/run",
            headers=_headers(),
            json={"detach": True, "monitor_region": "recovered"},
        )
        assert recovered.status_code == 202

    deadline_logs = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("synthetic.run_deadline_exceeded")
    ]
    assert deadline_logs
    assert "elapsed_seconds=" in deadline_logs[0]
    assert "rotation_count" in deadline_logs[0]
    assert run_calls == 2


def test_synchronous_run_deadline_releases_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def blocked_forever(
        _settings: Settings,
        _body: dict[str, object],
    ) -> dict[str, object]:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def immediate(
        _settings: Settings,
        _body: dict[str, object],
    ) -> dict[str, object]:
        return {"data": {"recorded": 0}}

    monkeypatch.setattr(synthetic_routes, "_run_and_record", blocked_forever)
    with _client(
        synthetic_run_deadline_seconds=0.2,
        raise_server_exceptions=False,
    ) as client:
        timed_out = client.post(
            "/v1/internal/synthetic/run",
            headers=_headers(),
            json={},
        )
        assert timed_out.status_code == 500

        monkeypatch.setattr(synthetic_routes, "_run_and_record", immediate)
        recovered = client.post(
            "/v1/internal/synthetic/run",
            headers=_headers(),
            json={},
        )
        assert recovered.status_code == 200


def test_remediator_deadline_keeps_slot_until_abandoned_worker_exits(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    started = threading.Event()
    release = threading.Event()
    calls = 0
    worker_names: list[str] = []

    def first_call_blocks(_settings: Settings) -> list[object]:
        nonlocal calls
        calls += 1
        worker_names.append(threading.current_thread().name)
        if calls == 1:
            started.set()
            release.wait()
        return []

    monkeypatch.setattr(synthetic_routes, "run_remediator_pass", first_call_blocks)
    settings = Settings(
        environment="test",
        synthetic_remediator_deadline_seconds=0.2,
    )
    try:
        with caplog.at_level("ERROR"):
            assert asyncio.run(synthetic_routes._run_scheduled_remediator_pass(settings)) is None  # noqa: SLF001
        assert started.is_set()
        assert worker_names == ["synthetic-remediator_0"]

        # The request has returned, but the timed-out worker retains the slot
        # until it exits. A scheduler retry therefore cannot pile up another
        # blocked analytics reader on this process.
        assert asyncio.run(synthetic_routes._run_scheduled_remediator_pass(settings)) is None  # noqa: SLF001
        assert calls == 1
    finally:
        release.set()

    assert any(
        record.getMessage().startswith("synthetic.remediator_deadline_exceeded")
        for record in caplog.records
    )
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if asyncio.run(synthetic_routes._run_scheduled_remediator_pass(settings)) == 0:  # noqa: SLF001
            break
        time.sleep(0.02)
    else:
        pytest.fail("remediator admission was not released after the worker exited")
    assert calls == 2
    semaphore = synthetic_routes._OPERATION_SLOTS["remediate"]  # noqa: SLF001
    assert semaphore.acquire(blocking=False)
    assert not semaphore.acquire(blocking=False)
    semaphore.release()


@pytest.mark.parametrize(
    ("path", "maximum"),
    [
        ("/v1/internal/synthetic/samples", 256),
        ("/v1/internal/synthetic/benchmark", 128),
    ],
)
def test_synthetic_ingest_rejects_oversized_item_batches_before_work(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    maximum: int,
) -> None:
    calls = 0

    def forbidden(_items: object) -> None:
        nonlocal calls
        calls += 1

    monkeypatch.setattr(synthetic_routes, "_record_probe_samples", forbidden)
    monkeypatch.setattr(synthetic_routes, "_record_benchmark_samples", forbidden)

    response = _client().post(
        path,
        headers=_headers(),
        json={"samples": [{}] * (maximum + 1)},
    )

    assert response.status_code == 400
    assert f"at most {maximum} items" in response.json()["error"]["message"]
    assert calls == 0


def test_synthetic_run_has_a_small_route_specific_cadence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "trusted_router.storage_rate_limits.utcnow",
        lambda: dt.datetime(2026, 8, 26, 20, 20, 30, tzinfo=dt.UTC),
    )

    async def immediate(
        _settings: Settings,
        _body: dict[str, object],
    ) -> dict[str, object]:
        return {"data": {"recorded": 0}}

    monkeypatch.setattr(synthetic_routes, "_run_and_record", immediate)
    client = _client()

    responses = [
        client.post("/v1/internal/synthetic/run", headers=_headers(), json={})
        for _ in range(3)
    ]

    assert [response.status_code for response in responses] == [200, 200, 429]
    assert responses[-1].headers["retry-after"]
