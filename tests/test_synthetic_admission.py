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
    synthetic_routes._note_remediator_finished()  # noqa: SLF001
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
    deadline_armed = threading.Event()
    expire_deadline = threading.Event()
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

    async def controlled_deadline(
        run: object,
        *,
        timeout: float,
    ) -> dict[str, object]:
        assert timeout == 0.2
        task = asyncio.ensure_future(run)  # type: ignore[arg-type]
        # Let the child reach the blocked backend before exposing the explicit
        # deadline control to the request thread.
        await asyncio.sleep(0)
        assert started.is_set()
        deadline_armed.set()
        assert await asyncio.to_thread(expire_deadline.wait, 5.0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        raise TimeoutError

    monkeypatch.setattr(synthetic_routes, "_run_and_record", blocked_forever)
    monkeypatch.setattr(synthetic_routes, "_wait_for_run_deadline", controlled_deadline)
    with caplog.at_level("ERROR"), _client(synthetic_run_deadline_seconds=0.2) as client:
        try:
            first = client.post(
                "/v1/internal/synthetic/run",
                headers=_headers(),
                json={"detach": True, "rotation_count": 8},
            )
            assert first.status_code == 202
            assert deadline_armed.wait(timeout=5), "detached run did not arm its deadline"
            assert started.is_set()

            # The test controls expiration, so this request necessarily lands
            # while the first run still owns admission.
            overlap = client.post(
                "/v1/internal/synthetic/run",
                headers=_headers(),
                json={"detach": True},
            )
            assert overlap.status_code == 429

            expire_deadline.set()
            deadline = time.monotonic() + 5
            while (
                synthetic_routes._BACKGROUND_RUNS  # noqa: SLF001
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            assert not synthetic_routes._BACKGROUND_RUNS  # noqa: SLF001

            recovered = client.post(
                "/v1/internal/synthetic/run",
                headers=_headers(),
                json={"detach": True, "monitor_region": "recovered"},
            )
            assert recovered.status_code == 202
        finally:
            expire_deadline.set()

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
        with caplog.at_level("WARNING"):
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


def _wait_until(predicate: Any, *, seconds: float = 2.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    pytest.fail("condition was not reached in time")


def test_remediate_deadline_overrun_is_a_structured_202_not_a_500(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """QUILL-ROUTER-B7: an overrun answered 500 with an unhandled TimeoutError."""
    started = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    running = 0
    max_running = 0
    calls = 0

    def blocking_pass(_settings: Settings) -> list[object]:
        nonlocal running, max_running, calls
        with lock:
            calls += 1
            first = calls == 1
            running += 1
            max_running = max(max_running, running)
        try:
            if first:
                started.set()
                release.wait(5)
            return ["decision"]
        finally:
            with lock:
                running -= 1

    monkeypatch.setattr(synthetic_routes, "run_remediator_pass", blocking_pass)
    try:
        with _client(synthetic_remediator_deadline_seconds=0.2) as client, caplog.at_level(
            "WARNING"
        ):
            overrun = client.post("/v1/internal/synthetic/remediate", headers=_headers())
            assert overrun.status_code == 202
            data = overrun.json()["data"]
            assert data["deadline_exceeded"] is True
            assert data["worker"] == "continuing"
            assert data["decisions"] is None
            assert data["consecutive_overruns"] == 1
            assert data["elapsed_seconds"] >= 0.2
            assert started.is_set()

            # The still-running worker owns the slot: the next tick is told a
            # pass is in progress instead of starting a second one.
            overlap = client.post("/v1/internal/synthetic/remediate", headers=_headers())
            assert overlap.status_code == 429
            assert overlap.json()["error"]["message"] == (
                "Synthetic operation is already in progress"
            )
            assert calls == 1

            release.set()
            semaphore = synthetic_routes._OPERATION_SLOTS["remediate"]  # noqa: SLF001

            def slot_free() -> bool:
                if semaphore.acquire(blocking=False):
                    semaphore.release()
                    return True
                return False

            _wait_until(slot_free)
            synthetic_routes._OPERATION_RATE_LIMITS.reset()  # noqa: SLF001
            finished = client.post("/v1/internal/synthetic/remediate", headers=_headers())
            assert finished.status_code == 200
            assert finished.json() == {"data": {"decisions": 1}}
    finally:
        release.set()

    assert max_running == 1
    overrun_logs = [
        record
        for record in caplog.records
        if record.getMessage().startswith("synthetic.remediator_deadline_exceeded")
    ]
    # One expected overrun is a warning, never an ERROR (which reaches Sentry).
    assert [record.levelname for record in overrun_logs] == ["WARNING"]
    assert "worker=continuing consecutive=1" in overrun_logs[0].getMessage()


def test_remediator_overrun_releases_slot_when_worker_never_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def counting_pass(_settings: Settings) -> list[object]:
        nonlocal calls
        calls += 1
        return []

    monkeypatch.setattr(synthetic_routes, "run_remediator_pass", counting_pass)
    # Occupy the single remediator worker thread so the pass stays queued.
    occupy = threading.Event()
    blocker = synthetic_routes._REMEDIATOR_EXECUTOR.submit(occupy.wait, 5)  # noqa: SLF001
    settings = Settings(environment="test", synthetic_remediator_deadline_seconds=0.1)
    try:
        slot = synthetic_routes._HeldOperationSlot(  # noqa: SLF001
            synthetic_routes._admit_operation("remediate")  # noqa: SLF001
        )
        result = asyncio.run(
            synthetic_routes._run_remediator_with_deadline(settings, slot)  # noqa: SLF001
        )
    finally:
        occupy.set()
        blocker.result(timeout=5)

    assert isinstance(result, synthetic_routes.RemediatorOverrun)
    assert result.worker == "abandoned"
    # Cancelled before it started: it never runs, and the admission is free.
    time.sleep(0.05)
    assert calls == 0
    semaphore = synthetic_routes._OPERATION_SLOTS["remediate"]  # noqa: SLF001
    assert semaphore.acquire(blocking=False)
    semaphore.release()


def test_repeated_remediator_overruns_escalate_to_error(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    gate = threading.Event()

    def slow_pass(_settings: Settings) -> list[object]:
        gate.wait(0.3)
        return []

    monkeypatch.setattr(synthetic_routes, "run_remediator_pass", slow_pass)
    settings = Settings(environment="test", synthetic_remediator_deadline_seconds=0.05)
    semaphore = synthetic_routes._OPERATION_SLOTS["remediate"]  # noqa: SLF001

    def overrun_once() -> Any:
        _wait_until(lambda: semaphore.acquire(blocking=False))
        slot = synthetic_routes._HeldOperationSlot(semaphore)  # noqa: SLF001
        return asyncio.run(
            synthetic_routes._run_remediator_with_deadline(settings, slot)  # noqa: SLF001
        )

    with caplog.at_level("WARNING"):
        results = [overrun_once() for _ in range(3)]
    assert [r.consecutive for r in results] == [1, 2, 3]
    levels = [
        record.levelname
        for record in caplog.records
        if record.getMessage().startswith("synthetic.remediator_deadline_exceeded")
    ]
    assert levels == ["WARNING", "WARNING", "ERROR"]

    # A pass that finishes resets the run.
    gate.set()
    _wait_until(lambda: semaphore.acquire(blocking=False))
    slot = synthetic_routes._HeldOperationSlot(semaphore)  # noqa: SLF001
    assert asyncio.run(
        synthetic_routes._run_remediator_with_deadline(settings, slot)  # noqa: SLF001
    ) == 0
    assert synthetic_routes._note_remediator_overrun() == 1  # noqa: SLF001


def test_remediator_real_failure_still_surfaces_and_frees_the_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_pass(_settings: Settings) -> list[object]:
        raise RuntimeError("analytics schema drift")

    monkeypatch.setattr(synthetic_routes, "run_remediator_pass", broken_pass)
    with _client(raise_server_exceptions=False) as client:
        failed = client.post("/v1/internal/synthetic/remediate", headers=_headers())
        assert failed.status_code == 500

        monkeypatch.setattr(synthetic_routes, "run_remediator_pass", lambda _s: [])
        recovered = client.post("/v1/internal/synthetic/remediate", headers=_headers())
        assert recovered.status_code == 200
        assert recovered.json() == {"data": {"decisions": 0}}

    synthetic_routes._OPERATION_RATE_LIMITS.reset()  # noqa: SLF001
    monkeypatch.setattr(synthetic_routes, "run_remediator_pass", broken_pass)
    slot = synthetic_routes._HeldOperationSlot(  # noqa: SLF001
        synthetic_routes._admit_operation("remediate")  # noqa: SLF001
    )
    with pytest.raises(RuntimeError, match="schema drift"):
        asyncio.run(
            synthetic_routes._run_remediator_with_deadline(  # noqa: SLF001
                Settings(environment="test"), slot
            )
        )
