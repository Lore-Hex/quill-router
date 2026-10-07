# ruff: noqa: F811 - imported pytest fixture
import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.test_async_settle_handler import env, prepare, row_for  # noqa: F401
from tests.test_async_settle_oracle import endpoint_from_candidate
from tests.test_settle_outbox_drain import _client, _typed_credit
from trusted_router.catalog_data import Model
from trusted_router.routes.internal import gateway
from trusted_router.services import async_settle_shadow as module
from trusted_router.services.async_settle_shadow import Runtime


@pytest.mark.parametrize("opted", [False, True])
@pytest.mark.parametrize("pause", ["copy", "counter"])
def test_evidence_worker_counter_lock_does_not_hold_money(env, monkeypatch, opted, pause):
    body, auth, key = prepare(env)
    store, db, rt, cfg = env
    repair = json.loads(row_for(env, body).settle_body)
    cfg.async_settle_enabled = cfg.async_settle_protection = False
    cfg._async_settle_shadow_workspace_ids = frozenset({"ws-v1"}) if opted else frozenset()
    endpoints = {
        c["endpoint_id"]: endpoint_from_candidate(c) for c in body["billing_snapshot"]["candidates"]
    }
    for e in endpoints.values():
        monkeypatch.setitem(
            gateway.MODELS,
            e.model_id,
            Model(
                id=e.model_id,
                name="review",
                provider=e.provider,
                context_length=1000000,
                prepaid_available=True,
            ),
        )
    monkeypatch.setattr(gateway, "endpoint_for_id", endpoints.get)
    client = _client(cfg)
    shadow = Runtime(cfg, rt)
    client.app.state.async_settle_shadow = shadow
    shadow.counters._day()
    held = threading.Event()
    release = threading.Event()
    capture = threading.Event()
    entered = threading.Event()
    sent = threading.Event()
    finished = threading.Event()
    deepcopy = copy.deepcopy

    def stalled_copy(value, *args, **kwargs):
        if (
            isinstance(value, dict)
            and "comparison_attempts" in value
            and threading.current_thread().name.startswith("settle-shadow")
        ):
            held.set()
            release.wait()
        return deepcopy(value, *args, **kwargs)

    if pause == "copy":
        monkeypatch.setattr(copy, "deepcopy", stalled_copy)
    else:
        add = shadow.counters.add
        def stalled_add(day, target, key, count=1):
            if key == "sequence":
                held.set()
                release.wait()
            return add(day, target, key, count)
        monkeypatch.setattr(shadow.counters, "add", stalled_add)
    # Start the same deadline at the authorization-read seam in both legs;
    # TestClient thread/startup scheduling is outside settlement timing.
    lookup = type(store).get_gateway_authorization
    def authorization_read(self, *args, **kwargs):
        result = lookup(self, *args, **kwargs)
        entered.set()
        return result
    monkeypatch.setattr(type(store), 'get_gateway_authorization', authorization_read)
    original = module.capture_authorization

    def capturing(auth):
        capture.set()
        return original(auth)

    monkeypatch.setattr(module, "capture_authorization", capturing)

    class Watch:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            async def observe(message):
                if message["type"] == "http.response.start":
                    sent.set()
                if message["type"] == "http.response.body" and not message.get("more_body", False):
                    finished.set()
                await send(message)

            await self.app(scope, receive, observe)

    client.app.add_middleware(Watch)
    # The real evidence worker runs the exact counter-snapshot operation used by flush().
    try:
        snapshot = shadow.executor.submit(shadow.counters.snapshot)
        assert held.wait(5)
        with ThreadPoolExecutor(max_workers=1) as pool:
            try:
                future = pool.submit(client.post, "/v1/internal/gateway/settle", json=repair)
                assert entered.wait(5)
                responded = finished.wait(0.25) and sent.is_set()
                booked = _typed_credit(db, "ws-v1")["total_usage"]
            finally:
                # Release before the request pool's context manager joins it.
                release.set()
            assert future.result(timeout=5).status_code == 200
        detached = snapshot.result(timeout=5)[0][1]
    finally:
        # Covers failure/inversion of held.wait(), before the request pool exists.
        release.set()
        shadow.executor.shutdown()
    assert capture.is_set() is opted
    assert booked == 2, "evidence worker held real booking"
    assert responded, "evidence counter snapshot blocked the real money path for 250ms"
    # The paused serializer owns its old snapshot, not live request counters.
    assert sum(row['observed_attempts'] for row in detached['counts']) == 0
    if pause == 'counter' and opted:
        latest = shadow.counters.snapshot()[0][1]
        assert sum(row['count'] for row in latest['drops']) == 1
        assert latest['first_gap_at_us'] is not None


def test_nonblocking_counter_mailbox_is_bounded_and_records_drops():
    from trusted_router.async_settle_shadow_evidence import Counters
    counters = Counters('us-central1', 'a'*40, clock=lambda: 1791244801)
    observations = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        for _ in range(130):
            with counters.lock:
                def request():
                    with counters.request_access(1791244801) as acquired:
                        observations.append(acquired)
                pool.submit(request).result(timeout=1)
    assert observations == [False] * 130
    assert len(counters.mailbox) == 128
    body = counters.snapshot()[0][1]
    assert body['counter_overflow'] and body['first_gap_at_us'] is not None
    assert sum(row['count'] for row in body['drops']) == 128


def test_contended_release_keeps_request_nonblocking_and_closes_writer():
    from trusted_router.async_settle_shadow_evidence import Counters
    counters = Counters('us-central1', 'a'*40, clock=lambda: 1791244801)
    counters.retain(1791244801)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with counters.lock:
            pool.submit(counters.release_request, 1791244801).result(timeout=1)
    body = counters.snapshot(closed=True)[0][1]
    assert not counters.active and body['closed']
    assert not body['drops'] and body['first_gap_at_us'] is None
