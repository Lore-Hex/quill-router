"""Shadow safety gates. The reference store never touches customer state."""
from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
import threading
import time
from pathlib import Path
from unittest.mock import Mock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.gateway_boot import BootAuthHeader, boot_auth_digest
from trusted_router.services import speculation_shadow as shadow
from trusted_router.speculation_protocol import TrustedKey, verify_descriptor, verify_grant
from trusted_router.storage_gcp_speculation_shadow import Transaction, table_name
from trusted_router.storage_models import GatewayBoot


class ReferenceStore:
    def __init__(self):
        self.rows = {}
        self.facts = {}
        self.boots = {}

    def get(self, table, key):
        return copy.deepcopy(self.rows.get((table, key)))

    read = get

    def put(self, table, key, value):
        table_name(table)
        self.rows[table, key] = copy.deepcopy(value)

    def transaction(self, operation):
        before = copy.deepcopy(self.rows)
        try:
            return operation(self)
        except Exception:
            self.rows = before
            raise

    def ready(self):
        pass

    def resolve(self, lookup, now):
        if lookup not in self.facts:
            raise shadow.ShadowMiss("key-unresolved")
        return self.facts[lookup]

    def boot(self, kid):
        return self.boots.get(kid)


def event(sequence, *, auth=None, **kw):
    return shadow.Outcome(f"e{sequence}", "p", "inc", sequence, 1990, "w", "k", "a" * 64,
                          authorization_id=auth or f"a{sequence}", status=200, reason="success", **kw)


def apply(store, e, *, now=2000, loss=False):
    store.transaction(lambda tx: shadow.project(tx, e, e.producer, e.incarnation, e.sequence, loss, now))


def key_state(store):
    return store.get("scope", shadow.identity("key", "w", "k"))


def install_recorder_fault(monkeypatch, dispatcher, fault):
    def fail(*args, **kwargs):
        raise RuntimeError("recorder fault")
    if fault == "loss-set":
        monkeypatch.setattr(dispatcher.loss, "set", fail)
    elif fault == "reason-map":
        class Reasons(dict):
            def __getitem__(self, key):
                fail()
        class Reason:
            def __bool__(self):
                return bool(Reasons()["first"])
        monkeypatch.setattr(dispatcher, "loss_reason", Reason())
    elif fault in {"logging", "counter"}:
        # Inject optional diagnostics inside the recorder; production needs
        # neither. Their failures must remain inside the recorder's guard.
        import logging
        original = shadow._record_loss
        logger = logging.getLogger("shadow-recorder-test")
        monkeypatch.setattr(logger, "warning", fail)
        counter = Mock(side_effect=RuntimeError("counter fault"))
        def recording(*args):
            if fault == "logging":
                logger.warning("observer failure")
            else:
                counter()
            original(*args)
        monkeypatch.setattr(shadow, "_record_loss", recording)


@pytest.fixture(params=[None, "loss-set", "reason-map", "logging", "counter"])
def recorder_fault(monkeypatch, request):
    # Production never clears the last-resort marker; test isolation must.
    monkeypatch.setattr(shadow, "_COVERAGE_UNKNOWN", False)
    return request.param, lambda dispatcher: install_recorder_fault(monkeypatch, dispatcher, request.param)


def test_distinct_history_replay_and_invocation_dedup():
    store = ReferenceStore()
    for seq in range(1, 25):
        apply(store, event(seq, auth="one"))
    assert len(key_state(store)["successes"]) == 1
    apply(store, event(25, auth="missed-original", replay=True))
    assert len(key_state(store)["successes"]) == 1
    apply(store, event(26, invocation_nonce="same-invocation"))
    apply(store, event(27, invocation_nonce="same-invocation"))
    assert len(key_state(store)["successes"]) == 2
    # Exactly-once event delivery also leaves the projection sequence unchanged.
    before = copy.deepcopy(store.rows)
    apply(store, event(27, invocation_nonce="same-invocation"))
    assert store.rows == before


@pytest.mark.parametrize(("status", "reason", "rate", "scope"), [
    (402, "credit_exhausted", "", "workspace"), (403, "billing_paused", "", "workspace"),
    (503, "billing_paused", "", "workspace"), (401, "key_disabled", "", "key"),
    (402, "key_limit_exceeded", "", "key"), (429, "key_window_limit_exceeded", "", "key"),
    (429, "rate_limit", "key", "key"), (429, "rate_limit", "", "workspace"),
    (400, "request_error", "", "none"), (500, "infrastructure_error", "", "key"),
])
def test_scoped_late_denial(status, reason, rate, scope):
    store = ReferenceStore()
    apply(store, dataclasses.replace(event(1), occurred_at=1000), now=1000)
    denial = dataclasses.replace(event(2), status=status, reason=reason, rate_scope=rate, occurred_at=900)
    apply(store, denial, now=2000)
    workspace = store.get("scope", shadow.identity("workspace", "w"))
    assert workspace["clean_since"] == (2000 if scope == "workspace" else 1000)
    assert key_state(store)["clean_since"] == (2000 if scope == "key" else 1000)


def test_loss_restart_unacknowledged_tail_and_late_membership():
    store = ReferenceStore()
    apply(store, dataclasses.replace(event(1), occurred_at=1000), now=1000)
    apply(store, event(2), now=2000, loss=True)
    apply(store, event(3), now=2001)
    assert store.get("producer", "p")["lost"] is True
    assert store.get("producer", "p")["clean_since"] == 2000
    shadow.project(store, None, "p", "new-inc", 2, False, 2100)
    row = store.get("producer", "p")
    assert row["clean_since"] == 2100 and row["sequence"] == 0 and row["submitted"] == 2
    shadow.project(store, None, "p", "new-inc", 2, False, 2101, "new-membership")
    assert store.get("producer", "p")["clean_since"] == 2101


def test_nonblocking_overflow_and_sticky_loss():
    store = ReferenceStore()
    dispatcher = shadow.Dispatcher(store, "p", capacity=1)
    observation = shadow.Observation(dispatcher)
    dispatcher.try_submit(observation, 200, "success", {})
    dispatcher.try_submit(observation, 200, "success", {})
    assert dispatcher.loss.is_set()
    dispatcher.pending.get_nowait()
    dispatcher.try_submit(observation, 200, "success", {})
    assert dispatcher.loss.is_set()
    dispatcher.lock.acquire()
    try:
        dispatcher.try_submit(observation, 200, "success", {})
    finally:
        dispatcher.lock.release()
    assert dispatcher.loss.is_set()


def test_submit_does_not_wait_for_worker_io():
    store = ReferenceStore()
    entered = threading.Event()
    release = threading.Event()
    def blocked():
        entered.set()
        release.wait(0.5)
    store.ready = blocked
    dispatcher = shadow.Dispatcher(store, "p")
    dispatcher.start()
    try:
        assert entered.wait(2)
        start = time.monotonic()
        dispatcher.try_submit(shadow.Observation(dispatcher), 200, "success", {})
        assert time.monotonic() - start < 0.1
    finally:
        release.set()
        dispatcher.close()


def facts():
    return dict(workspace_id="w", key_id="k", lookup_digest="a" * 64, key_eligible=True,
                shards_complete=True, tier=2, latched=False, paused=False, reconciled_through=1900,
                trust_fresh_until=2100, key_expires_at=2100, credits=20_000_000, usage=0, reserved=0)


def paid():
    return dict(version=1, complete=True, conserved=True, as_of=1990, expires_at=2100,
                accounted_credits_micro=20_000_000, nonqualifying_upper_micro=0)


def history():
    return dict(clean_since=1000, count=20, last_success_at=1990, sequence=20, window_start=1400)


@pytest.mark.parametrize("change", [dict(shards_complete=False), dict(latched=True), dict(paused=True),
    dict(tier=1), dict(usage=-1), dict(reserved=-1), dict(credits=-1), dict(reconciled_through=0), dict(reconciled_through=2001), dict(trust_fresh_until=1999), dict(key_eligible=False)])
def test_current_missing_or_stale_facts_fail_closed(change):
    with pytest.raises(shadow.ShadowMiss):
        shadow.eligibility({**facts(), **change}, paid(), history(), 2000)


def test_promotional_mixed_funds_conservation():
    assert shadow.eligibility(facts(), paid(), history(), 2000) == 20_000_000
    for nonpaid in (20_000_000, 16_000_000):
        with pytest.raises(shadow.ShadowMiss, match="paid-headroom"):
            shadow.eligibility({**facts(), "lifetime_topup": 100_000_000}, {**paid(), "nonqualifying_upper_micro": nonpaid}, history(), 2000)
    assert shadow.eligibility(facts(), {**paid(), "nonqualifying_upper_micro": 10_000_000}, history(), 2000) == 10_000_000
    for change in (dict(complete=False), dict(complete="false"), dict(conserved="false"), dict(conserved=False), dict(unresolved_recovery=True), dict(accounted_credits_micro=19_000_000)):
        with pytest.raises(shadow.ShadowMiss):
            shadow.eligibility(facts(), {**paid(), **change}, history(), 2000)
    with pytest.raises(shadow.ShadowMiss, match="paid-headroom"):
        shadow.eligibility({**facts(), "usage": 12_000_000, "reserved": 4_000_000}, paid(), history(), 2000)


def fixture_signer():
    bundle = json.loads((Path(__file__).parent / "fixtures/speculation_v1/grant-permit-tokens.json").read_text())
    keys = [TrustedKey(**k) for k in bundle["trusted_test_keys"]]
    frozen = verify_grant(bundle["shadow_grant_jws"], keys, bundle["context"], bundle["now"], shadow=True)
    private = Ed25519PrivateKey.generate()
    import base64
    public = base64.urlsafe_b64encode(private.public_key().public_bytes_raw()).rstrip(b"=").decode()
    trusted = dataclasses.replace(next(k for k in keys if k.purpose == "shadow-grant"), public_key_b64url=public)
    return shadow.ShadowSigner(private, trusted), frozen.claims, bundle["context"], bundle["now"]


def test_exact_v1_signer_round_trip_frozen_verifier_and_authority():
    signer, claims, context, now = fixture_signer()
    token = signer.sign(claims, context, now)
    verified = verify_grant(token, [signer.trusted], context, now, shadow=True)
    assert verified.claims == claims and verified.shadow
    with pytest.raises(ValueError, match="type"):
        verify_grant(token, [signer.trusted], context, now)
    with pytest.raises(ValueError, match="dry_run_cannot_dispatch"):
        verify_descriptor("", [], verified, b"", "x", "n")
    with pytest.raises(ValueError, match="start_window"):
        verify_grant(token, [signer.trusted], context, claims["exp"] - 2, shadow=True)
    with pytest.raises(shadow.ShadowMiss, match="issuer-purpose"):
        shadow.ShadowSigner(signer.private_key, dataclasses.replace(signer.trusted, purpose="grant"))
    with pytest.raises(ValueError, match="fields"):
        signer.sign({**claims, "extra": 1}, context, now)


def ready_service():
    signer, claims, _, _ = fixture_signer()
    store = ReferenceStore()
    settings = Settings(environment="test", speculative_provider_shadow_enabled=True,
        speculation_shadow_workspaces=["w"], speculation_shadow_routes=[claims["route"]["endpoint_id"]],
        speculation_shadow_producers=["p"], speculation_shadow_images=["image"],
        speculation_shadow_slots={"boot": {"slot": "slot", "region": claims["route"]["region"]}},
        speculation_shadow_image_policy_version=1, speculation_shadow_policy_expires_at=2100)
    dispatcher = shadow.Dispatcher(store, "p")
    dispatcher.health = "observing"
    for seq in range(1, 21):
        apply(store, dataclasses.replace(event(seq), occurred_at=1000 if seq == 1 else 1980+seq//2, endpoint_ids=(claims["route"]["endpoint_id"],)), now=1000 if seq == 1 else 2000)
    store.put("producer", "p", {"clean_since": 1000, "sequence": 20, "submitted": 20, "expires_at": 2100, "membership": ""})
    # Projection source times in this synthetic setup are independent of commit times.
    state = key_state(store)
    state["successes"] = [[f"a{i}", 1990] for i in range(20)]
    state["sequence"] = 20
    state["route_identity"] = [claims["route"][k] for k in ("endpoint_id", "provider", "upstream_model", "region")]
    store.put("scope", shadow.identity("key", "w", "k"), state)
    store.put("paid", "w", paid())
    route = {**claims["route"], "price_expires_at": 2100}
    store.put("route", route["endpoint_id"], {"complete": True, "expires_at": 2100, "route": route,
        "applicability": {"vendor_source": "fixture", "source_revision": "v1", "cap_certified": True, "input_certified": True, "policy_version": 1}})
    store.facts["a" * 64] = facts()
    return shadow.RefreshService(store, signer, settings, dispatcher)


def test_refresh_replay_retains_original_deadline_and_budget():
    service = ready_service()
    token = service.mint(facts(), "boot", 2000)
    before = copy.deepcopy(service.store.rows)
    assert service.mint(facts(), "boot", 2010) == token
    assert service.store.rows == before
    state = key_state(service.store)
    state["successes"][-1][1] = 2020
    service.store.put("scope", shadow.identity("key", "w", "k"), state)
    second = service.mint(facts(), "boot", 2028)
    assert second != token
    exposure = service.store.get("exposure", "fleet")
    assert exposure["ordinal"] == 2
    assert exposure["micro"] > before["exposure", "fleet"]["micro"]


def test_current_request_cannot_qualify_its_predecision():
    service = ready_service()
    state = key_state(service.store)
    state["successes"][-1][1] = 2000
    service.store.put("scope", shadow.identity("key", "w", "k"), state)
    with pytest.raises(shadow.ShadowMiss, match="history-count"):
        service.mint(facts(), "boot", 2000)
    assert service.mint(facts(), "boot", 2001)


@pytest.mark.parametrize("failure", ["lost", "expired", "tail", "missing", "membership", "queue"])
def test_coverage_blocks_grants_and_cached_grants(failure):
    service = ready_service()
    service.mint(facts(), "boot", 2000)
    coverage = service.store.get("producer", "p")
    if failure == "lost":
        coverage["lost"] = True
    if failure == "expired":
        coverage["expires_at"] = 2000
    if failure == "tail":
        coverage["submitted"] = 21
    if failure == "membership":
        coverage["membership"] = "new"
    service.store.put("producer", "p", coverage)
    if failure == "missing":
        del service.store.rows["producer", "p"]
    if failure == "queue":
        service.dispatcher.loss.set()
    with pytest.raises(shadow.ShadowMiss, match="coverage"):
        service.mint(facts(), "boot", 2000)


def test_authority_namespace_at_native_mutation_boundary():
    native = Mock()
    tx = Transaction(native, "gcp")
    for table in shadow.TABLES:
        tx.put(table, "inc:1" if table == "event" else "id", {"value": 1})
    tx.flush()
    assert len(native.insert_or_update.call_args_list) == len(shadow.TABLES)
    assert all(call.args[0].startswith("tr_speculation_shadow_") for call in native.insert_or_update.call_args_list)
    for table in ("tr_credit_balance", "credit", "risk", "authorization"):
        with pytest.raises(shadow.ShadowMiss):
            tx.put(table, "id", {})


def test_batch_binding_every_member_and_exact_body(monkeypatch):
    service = ready_service()
    private = Ed25519PrivateKey.generate()
    import base64
    def enc(b):
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    service.store.boots["boot"] = GatewayBoot(kid="boot", jwk={"kty":"OKP", "crv":"Ed25519", "x":enc(private.public_key().public_bytes_raw())},
        approved=False, verified=True, image_digest="image", attestation_kind="gcp", registered_at="2026-01-01T00:00:00Z")
    good = {k: facts()[k] for k in ("workspace_id", "key_id", "lookup_digest")}
    second = {**good, "lookup_digest": "b" * 64, "key_id": "other"}
    service.store.facts["b" * 64] = {**facts(), "lookup_digest": "b" * 64, "key_id": "actual-other"}
    items = [good, second, good]
    body = shadow.canonical({"items": items})
    path = "/internal/speculation/shadow/refresh"
    auth = BootAuthHeader("boot", enc(private.sign(boot_auth_digest("POST", path, body))))
    monkeypatch.setattr(shadow.time, "time", lambda: 2000)
    results = service.refresh(items, auth, body, "POST", path)
    assert len(results) == 2 and "grant" in results[0]
    assert results[1]["miss"] == "batch-identity-mismatch"
    another_replica = dataclasses.replace(service, lock=threading.Lock(), rates={}, active={})
    with pytest.raises(shadow.ShadowMiss, match="refresh-rate-limited"):
        another_replica.refresh(items, auth, body, "POST", path)
    service.rates.clear()
    with pytest.raises(shadow.ShadowMiss, match="boot-auth-invalid"):
        service.refresh(items, auth, body+b" ", "POST", path)
    service.rates.clear()
    service.settings.speculation_shadow_images = ["different"]
    with pytest.raises(shadow.ShadowMiss, match="boot-auth-invalid"):
        service.refresh(items, auth, body, "POST", path)


def client(settings, service=None):
    from trusted_router.auth import settings_from_request
    from trusted_router.routes.internal.speculation import register
    app = FastAPI()
    app.dependency_overrides[settings_from_request] = lambda: settings
    app.state.settings = settings
    app.state.speculation_shadow = service
    router = APIRouter()
    register(router)
    app.include_router(router)
    return TestClient(app)


def test_refresh_off_and_bounds_have_no_storage_or_signer_access():
    off = client(Settings(environment="test"), Mock())
    response = off.post("/internal/speculation/shadow/refresh", content=b"not even json")
    assert response.status_code == 503 and response.json() == {"miss": "feature-disabled"}
    service = Mock()
    on = client(Settings(environment="test", speculative_provider_shadow_enabled=True), service)
    for body in (b"x" * 32769, shadow.canonical({"items": [{}] * 65})):
        assert on.post("/internal/speculation/shadow/refresh", content=body).status_code == 413
    assert not service.mock_calls


def test_disabled_install_and_scope_are_inert(monkeypatch):
    app = Mock()
    settings = Settings(environment="test")
    shadow.install(app, settings)
    assert not app.mock_calls
    monkeypatch.setattr(shadow, "Observation", Mock(side_effect=AssertionError("off allocation")))
    with shadow.outcome_scope(settings) as observation:
        assert observation is None
    assert not shadow.Observation.mock_calls


def test_content_free_observation_and_observer_failure(monkeypatch, recorder_fault):
    dispatcher = shadow.Dispatcher(ReferenceStore(), "p")
    fault, install_fault = recorder_fault
    install_fault(dispatcher)
    observation = shadow.Observation(dispatcher, workspace_id="w", key_id="k")
    shadow.complete(observation, {"total_ms": 4, "prompt": "secret"})
    outcome = dataclasses.asdict(dispatcher.pending.get_nowait())
    assert "secret" not in json.dumps(outcome) and "prompt" not in outcome
    monkeypatch.setattr(dispatcher, "try_submit", Mock(side_effect=RuntimeError("do not log me")))
    shadow.complete(observation, {})
    assert dispatcher.coverage_lost()
    assert shadow._COVERAGE_UNKNOWN is bool(fault)


async def test_nested_async_sync_outcome_once_and_early_unresolved(monkeypatch):
    from starlette.concurrency import run_in_threadpool
    from starlette.exceptions import HTTPException

    from trusted_router.gateway_timing import timed_gateway_async, timed_gateway_sync
    dispatcher = shadow.Dispatcher(ReferenceStore(), "p")
    monkeypatch.setattr(shadow, "_RUNTIME", dispatcher)
    settings = Settings(environment="test", speculative_provider_shadow_enabled=True)
    @timed_gateway_sync
    def _authorize_gateway_sync(request, body, settings):
        return {"data": {"ok": True, "byok_secret": "MUST_NOT_QUEUE"}}
    @timed_gateway_async
    async def authorize_gateway(request, body, settings):
        if body == "early":
            raise HTTPException(429, detail={"error": {"type": "rate_limited"}})
        return await run_in_threadpool(_authorize_gateway_sync, request, body, settings)
    await authorize_gateway(None, None, settings)
    assert dispatcher.pending.qsize() == 1
    completed = dispatcher.pending.get_nowait()
    assert completed.status == 200 and dict(completed.timing)["spanner_rpcs"] == 0
    assert "MUST_NOT_QUEUE" not in repr(completed)
    with pytest.raises(HTTPException):
        await authorize_gateway(None, "early", settings)
    early = dispatcher.pending.get_nowait()
    assert early.status == 429 and not early.workspace_id and not early.key_id
    assert dispatcher.pending.empty()


def test_flag_off_import_and_call_sites():
    import os
    import subprocess
    import sys
    root = Path(__file__).parents[1]
    result = subprocess.run([sys.executable, "-c", "import sys; import trusted_router.main; assert 'trusted_router.services.speculation_shadow' not in sys.modules; assert 'trusted_router.storage_gcp_speculation_shadow' not in sys.modules"],
        env={**os.environ, "TR_SPECULATIVE_PROVIDER_SHADOW_ENABLED": "false", "TR_ENVIRONMENT": "test", "PYTHONPATH": str(root / "src")},
        capture_output=True, text=True, timeout=30, cwd=root, check=False)
    assert result.returncode == 0, result.stderr
    # Only the shadow implementation may import the pure protocol at runtime.
    imports = []
    for path in (root / "src").rglob("*.py"):
        if "import" in path.read_text() and "speculation_protocol" in path.read_text() and path.name != "speculation_protocol.py":
            imports.append(path.relative_to(root).as_posix())
    assert imports == ["src/trusted_router/services/speculation_shadow.py"]


def test_memory_backend_reports_unsupported_without_loading_signer(monkeypatch):
    from trusted_router.main import create_app
    monkeypatch.setattr(Path, "read_bytes", Mock(side_effect=AssertionError("signer IO")))
    settings = Settings(environment="test", speculative_provider_shadow_enabled=True)
    with TestClient(create_app(settings, init_observability=False)) as c:
        result = c.get("/internal/speculation/shadow/status")
        assert result.json()["status"] == "shadow-not-supported"


def test_resolved_disabled_key_kept_before_401(monkeypatch):
    from tests.test_gateway_authorize_spanner_operations import (
        _lookup_body,
        _request,
        _seed_typed_gateway_store,
    )
    from trusted_router.routes.internal import gateway
    store, _, key = _seed_typed_gateway_store()
    store.update_key(key.hash, {"disabled": True})
    dispatcher = shadow.Dispatcher(ReferenceStore(), "p")
    monkeypatch.setattr(shadow, "_RUNTIME", dispatcher)
    with pytest.raises(Exception) as error:
        gateway._authorize_gateway_sync(_request(), _lookup_body(key), Settings(environment="test", speculative_provider_shadow_enabled=True))
    assert error.value.status_code == 401
    outcome = dispatcher.pending.get_nowait()
    assert (outcome.workspace_id, outcome.key_id, outcome.lookup_digest, outcome.reason) == (key.workspace_id, key.hash, key.lookup_hash, "key_disabled")


def test_worker_failure_is_loud_and_sticky():
    store = ReferenceStore()
    store.ready = Mock(side_effect=RuntimeError("missing schema"))
    dispatcher = shadow.Dispatcher(store, "p")
    dispatcher.try_submit(shadow.Observation(dispatcher), 200, "success", {})
    dispatcher.start()
    deadline = time.monotonic() + 2
    try:
        while dispatcher.health == "starting" and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        assert dispatcher.health == "shadow-storage-unavailable" and dispatcher.loss.is_set()
    finally:
        dispatcher.close()


def test_stale_policy_and_no_recovery_by_refresh():
    service = ready_service()
    service.settings.speculation_shadow_policy_expires_at = 1999
    with pytest.raises(shadow.ShadowMiss, match="shadow-policy-stale"):
        service.mint(facts(), "boot", 2000)
    service.settings.speculation_shadow_policy_expires_at = 2100
    service.store.put("exposure", "fleet", {"micro": 10_000_000, "ordinal": 50, "owner": "boot"})
    for now in (2000, 2010, 2020):
        with pytest.raises(shadow.ShadowMiss, match="retained-budget"):
            service.mint(facts(), "boot", now)
    assert service.store.get("exposure", "fleet")["micro"] == 10_000_000


@pytest.mark.parametrize("case", ["paid", "grant", "mixed", "missing-coverage", "missing-shard", "divergent", "stale", "window-limit", "bounded-ledger"])
def test_native_source_combiner_is_conservative(case):
    import datetime as dt
    import hashlib
    from contextlib import contextmanager
    from types import SimpleNamespace

    from google.cloud.spanner_v1 import param_types

    from trusted_router.storage_gcp_speculation_shadow import SpannerSpeculationShadow

    key = SimpleNamespace(hash="k", workspace_id="w", lookup_hash="a" * 64, usage_shard_count=1,
        disabled=False, management=False, scopes=[], app_id="", federated_home="", budget_strict=False,
        expires_at=None, limit_microdollars=None, limit_daily_microdollars=None,
        limit_weekly_microdollars=None, limit_monthly_microdollars=None)
    credits = [[0, 20_000_000, 0, 0, 2, None, [], 0, dt.datetime.fromtimestamp(1900, dt.UTC)]]
    limits = [[0, None, None, None, None]]
    events = [["payment", "stripe", 20_000_000, 0, "succeeded", 0, 0]]
    count = 1
    if case == "grant":
        events[0][:2] = ["grant", "operator"]
    if case == "mixed":
        events = [["payment", "stripe", 4_000_000, 0, "succeeded", 0, 0], ["grant", "system", 16_000_000, 0, "succeeded", 0, 0]]
    if case == "missing-shard":
        count = 2
    if case == "divergent":
        count = 2
        credits.append([1, 0, 0, 0, 3, None, [], 0, dt.datetime.fromtimestamp(1900, dt.UTC)])
    if case == "stale":
        credits[0][-1] = dt.datetime.fromtimestamp(-2000, dt.UTC)
    if case == "window-limit":
        limits[0][2] = 100
    if case == "bounded-ledger":
        events = events * 1001
    class Snapshot:
        def read(self, table, columns, keys):
            assert table == "tr_speculation_shadow_paid"
            return [] if case == "missing-coverage" else [[json.dumps(anchor)]]

        def execute_sql(self, sql, **kwargs):
            assert "workspace_id" in kwargs["params"] or "key_id" in kwargs["params"]
            if "tr_credit_balance" in sql:
                return credits
            if "tr_key_limit" in sql:
                return limits
            assert "LIMIT 1001" in sql
            return events
    @contextmanager
    def snapshot(**kwargs):
        assert kwargs == {"multi_use": True}
        yield Snapshot()
    backend = SimpleNamespace(_database=SimpleNamespace(snapshot=snapshot), _param_types=param_types,
        get_key_by_lookup_hash=lambda lookup: key,
        _read_entity_from=lambda snap, kind, pk, cls: {"api_key_lookup": {"key_id": "k"}, "api_key": key, "credit": SimpleNamespace(shard_count=count), "workspace": SimpleNamespace()}[kind])
    settings = Settings(environment="test")
    store = SpannerSpeculationShadow(backend, settings)
    reference = ReferenceStore()
    anchor = dict(coverage_version="audited-v1", source_ledger_digest=hashlib.sha256(shadow.canonical(events)).hexdigest(),
                  source_credits_micro=20_000_000, source_as_of=1990, source_expires_at=2100)
    store.read = lambda table, pk: {} if case == "missing-coverage" else anchor
    store.transaction = reference.transaction
    if case in {"missing-shard", "divergent"}:
        with pytest.raises(shadow.ShadowMiss):
            store.resolve(key.lookup_hash, 2000)
        return
    resolved = store.resolve(key.lookup_hash, 2000)
    evidence = reference.read("paid", "w")
    if case == "paid":
        assert shadow.eligibility(resolved, evidence, history(), 2000) == 20_000_000
    else:
        with pytest.raises(shadow.ShadowMiss):
            shadow.eligibility(resolved, evidence, history(), 2000)


def test_certified_route_must_match_stored_ordinary_route():
    service = ready_service()
    state = key_state(service.store)
    state["route_identity"][2] = "different-upstream-model"
    service.store.put("scope", shadow.identity("key", "w", "k"), state)
    with pytest.raises(shadow.ShadowMiss, match="route-identity-mismatch"):
        service.mint(facts(), "boot", 2000)


def test_worker_rpc_accounting_is_separate_from_request():
    from trusted_router.storage_gcp_io import _SPANNER_RPC_COUNTER, count_spanner_rpcs
    store = ReferenceStore()
    def read():
        _SPANNER_RPC_COUNTER.get().increment()
    store.ready = read
    dispatcher = shadow.Dispatcher(store, "p")
    dispatcher.try_submit(shadow.Observation(dispatcher), 200, "success", {})
    with count_spanner_rpcs() as request_counter:
        request_counter.increment()
        dispatcher.start()
        deadline = time.monotonic() + 2
        try:
            while dispatcher.health == "starting" and time.monotonic() < deadline:
                threading.Event().wait(0.01)
            assert dispatcher.total_rpcs >= 1
            assert request_counter.value() == 1
        finally:
            dispatcher.close()


@pytest.mark.parametrize("deadline", [2011, 2012, 2013, 2025, 2050])
@pytest.mark.parametrize("source", ["trust_fresh_until", "key_expires_at", "price_expires_at"])
def test_cached_grant_rechecks_current_deadlines(source, deadline):
    service = ready_service()
    original = service.mint(facts(), "boot", 2000)
    current = facts()
    if source == "price_expires_at":
        endpoint = service.settings.speculation_shadow_routes[0]
        row = service.store.get("route", endpoint)
        row["route"][source] = deadline
        service.store.put("route", endpoint, row)
    else:
        current[source] = deadline
    before = copy.deepcopy(service.store.rows)
    if deadline <= 2012:
        with pytest.raises(shadow.ShadowMiss, match="start-window-exhausted"):
            service.mint(current, "boot", 2010)
        assert service.store.rows == before
        # Exact reviewer reproduction also fails closed without the cached grant.
        service.store.rows.pop(("grant", shadow.identity("w", "k", "boot")))
        with pytest.raises(shadow.ShadowMiss, match="start-window-exhausted"):
            service.mint(current, "boot", 2010)
    else:
        token = service.mint(current, "boot", 2010)
        assert token != original
        import base64
        claims = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))
        assert claims["exp"] == min(2040, deadline)
        assert claims["start_before"] == min(2040, deadline) - 2


def test_expired_delivery_after_ttl_cannot_requalify_or_extend_retention():
    store = ReferenceStore()
    apply(store, event(1, invocation_nonce="same"))
    # Model TTL deleting both dedup families. Persistent scope/exposure survive.
    store.rows = {k: v for k, v in store.rows.items() if k[0] not in {"event", "success"}}
    store.put("exposure", "fleet", {"micro": 100, "ordinal": 1})
    before = copy.deepcopy(store.rows)
    apply(store, event(1, invocation_nonce="same"), now=2000 + 7 * 86400)
    assert store.get("producer", "p")["lost"] is True
    assert not any(k[0] in {"event", "success"} for k in store.rows)
    assert key_state(store) == before["scope", shadow.identity("key", "w", "k")]
    assert store.get("exposure", "fleet") == before["exposure", "fleet"]


def test_success_older_than_history_does_not_recreate_dedup():
    store = ReferenceStore()
    apply(store, event(1), now=3000)
    assert not key_state(store)["successes"]
    assert not any(k[0] == "success" for k in store.rows)


def test_native_shadow_is_selected_by_ci(tmp_path):
    import os
    import subprocess
    import sys
    root = Path(__file__).resolve().parents[1]
    relative = "tests/conformance/test_speculation_shadow_native.py"
    workflow = (root / ".github/workflows/ci.yml").read_text()
    assert relative in workflow
    result = subprocess.run(  # noqa: S603 - fixed local collection, no database access
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
         relative, "-k", "spanner-emulator", "--basetemp", str(tmp_path / "collection")],
        cwd=root, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "test_native_shadow_atomic_dedup_and_scoped_projection[spanner-emulator]" in result.stdout


def test_shadow_ttl_only_applies_to_event_and_success():
    from tests.conformance.spanner_ddl import DDL
    from tests.conformance.spanner_schema_source import migration_ddl
    assert tuple(DDL) == migration_ddl()
    policies = {statement for statement in DDL if "tr_speculation_shadow_" in statement and "ROW DELETION POLICY" in statement}
    assert policies == {f"ALTER TABLE tr_speculation_shadow_{name} ADD ROW DELETION POLICY (OLDER_THAN(updated_at, INTERVAL 7 DAY))" for name in ("event", "success")}


@pytest.mark.parametrize("fatal", [KeyboardInterrupt, SystemExit])
def test_isolation_preserves_process_control_exceptions(fatal):
    error = fatal()
    with pytest.raises(fatal) as caught, shadow.isolate("test"):
        raise error
    assert caught.value is error


@pytest.mark.parametrize("site", ["_outcome_settings", "outcome_scope", "complete", "_save_outcome_timing", "cleanup", "error-extraction"])
@pytest.mark.parametrize("failure", [False, True])
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_observer_fault_preserves_response_and_exception_identity(monkeypatch, site, failure, asynchronous, recorder_fault):
    from contextlib import contextmanager

    from trusted_router import gateway_timing as timing
    dispatcher = shadow.Dispatcher(ReferenceStore(), "faults")
    monkeypatch.setattr(shadow, "_RUNTIME", dispatcher)
    fault, install_fault = recorder_fault
    install_fault(dispatcher)
    settings = Settings(environment="test", speculative_provider_shadow_enabled=True)
    response = {"data": {"value": object()}}
    error = RuntimeError("ordinary failure")
    holds = []
    def ordinary():
        if failure:
            raise error
        holds.append(600)
        return response
    def fail(*args, **kwargs):
        raise ValueError("observer fault")
    if site == "cleanup":
        original = shadow.outcome_scope
        @contextmanager
        def scope(settings):
            with original(settings) as observation:
                yield observation
            fail()
        monkeypatch.setattr(shadow, "outcome_scope", scope)
    elif site == "error-extraction":
        if not failure:
            return  # extraction only exists on the failure path
        class Error(RuntimeError):
            @property
            def detail(self):
                raise ValueError("observer argument extraction")
        error = Error("ordinary failure")
    else:
        monkeypatch.setattr(timing if site.startswith("_") else shadow, site, fail)
    @timing.timed_gateway_sync
    def _authorize_gateway_sync(request, body, settings):
        return ordinary()
    @timing.timed_gateway_async
    async def authorize_gateway(request, body, settings):
        return ordinary()
    try:
        result = await authorize_gateway(None, None, settings) if asynchronous else _authorize_gateway_sync(None, None, settings)
    except RuntimeError as caught:
        assert failure and caught is error
    else:
        assert not failure and result is response
    assert holds == ([] if failure else [600])
    # Save-response timing runs only on the successful path.
    if site != "_save_outcome_timing" or not failure:
        assert dispatcher.coverage_lost()
        assert shadow._COVERAGE_UNKNOWN is bool(fault)
        assert dispatcher.coverage_loss_reason().startswith("coverage-" if fault else "observer-")
    assert shadow._CURRENT.get() is None
    assert timing._OUTCOME_TIMING.get() is None


def _gateway_callback_sites():
    import ast
    source = Path(__file__).resolve().parents[1] / "src/trusted_router/routes/internal/gateway.py"
    tree = ast.parse(source.read_text())
    return [(node.lineno, node.func.attr) for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name) and node.func.value.id == "speculation_shadow"
            and node.func.attr != "isolate"]


@pytest.mark.parametrize("line,site", _gateway_callback_sites())
@pytest.mark.parametrize("argument_failure", [False, True])
def test_every_gateway_callback_boundary_including_arguments(monkeypatch, line, site, argument_failure, recorder_fault):
    """Execute each actual call-site statement with hostile arguments/callbacks.

    Coupled with the HTTP lifecycle differential, this also covers rare paused
    fallback branches without replacing the money implementation to reach them.
    """
    import ast
    from types import SimpleNamespace
    source = Path(__file__).resolve().parents[1] / "src/trusted_router/routes/internal/gateway.py"
    tree = ast.parse(source.read_text())
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call) and n.lineno == line)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    statement = parents[call]
    assert isinstance(statement, ast.Expr)
    boundary = parents[statement]
    assert isinstance(boundary, ast.With), f"unguarded callback: {site}:{line}"
    dispatcher = shadow.Dispatcher(ReferenceStore(), "faults")
    monkeypatch.setattr(shadow, "_RUNTIME", dispatcher)
    fault, install_fault = recorder_fault
    install_fault(dispatcher)
    def fail(*args, **kwargs):
        raise RuntimeError("callback fault")
    class Hostile:
        def __getattribute__(self, name):
            raise RuntimeError("argument fault")
    if not argument_failure or site == "reason":
        monkeypatch.setattr(shadow, site, fail)
    environment = dict(speculation_shadow=shadow, api_key=SimpleNamespace(disabled=True),
        body=Hostile() if argument_failure else SimpleNamespace(invocation_nonce="nonce"),
        boot_context={} if argument_failure else {"boot_verified": True}, boot_auth=None,
        authorization=None, endpoint_candidates=[(None, Hostile())] if argument_failure else [],
        idempotent_replay=False, endpoint=SimpleNamespace(id="e", provider="p", upstream_id="u"), model=None, region="r")
    response = object()
    original_error = RuntimeError("ordinary exception")
    holds = [600]
    def ordinary():
        exec(compile(ast.Module(body=[boundary], type_ignores=[]), str(source), "exec"), environment)  # noqa: S102 - actual repository statement, controlled namespace
        return response
    assert ordinary() is response
    with pytest.raises(RuntimeError) as caught:
        ordinary()
        raise original_error
    assert caught.value is original_error
    assert holds == [600]
    assert dispatcher.coverage_lost()
    assert shadow._COVERAGE_UNKNOWN is bool(fault)
    assert dispatcher.coverage_loss_reason() == ("coverage-unknown" if fault else "observer-" + site)


def test_current_deadline_miss_is_per_item_on_refresh(monkeypatch):
    import base64
    service = ready_service()
    original = service.mint(facts(), "boot", 2000)
    service.store.facts["a" * 64] = {**facts(), "trust_fresh_until": 2011}
    private = Ed25519PrivateKey.generate()
    def enc(value):
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode()
    service.store.boots["boot"] = GatewayBoot(
        kid="boot", jwk={"kty": "OKP", "crv": "Ed25519", "x": enc(private.public_key().public_bytes_raw())},
        approved=False, verified=True, image_digest="image", attestation_kind="gcp", registered_at="2026-01-01T00:00:00Z")
    item = {k: facts()[k] for k in ("workspace_id", "key_id", "lookup_digest")}
    body = shadow.canonical({"items": [item]})
    path = "/internal/speculation/shadow/refresh"
    auth = BootAuthHeader("boot", enc(private.sign(boot_auth_digest("POST", path, body))))
    monkeypatch.setattr(shadow.time, "time", lambda: 2010)
    assert service.refresh([item], auth, body, "POST", path) == [{**item, "miss": "start-window-exhausted"}]
    assert service.store.get("grant", shadow.identity("w", "k", "boot"))["token"] == original


def test_shadow_migration_retention_is_idempotent_and_refuses_conflict(tmp_path):
    import os
    import subprocess
    # Execute the real shell migration against a metadata-only gcloud stub.
    stub = tmp_path / "gcloud"
    stub.write_text('''#!/bin/bash
case "$*" in
  *"ddl update"*)
    printf '%s\\n' "$*" >> "$SHADOW_DDL_LOG"
    for argument in "$@"; do
      case "$argument" in
        *"ALTER TABLE tr_speculation_shadow_event"*) touch "$SHADOW_STATE/event" ;;
        *"ALTER TABLE tr_speculation_shadow_success"*) touch "$SHADOW_STATE/success" ;;
      esac
    done ;;
  *"ROW_DELETION_POLICY_EXPRESSION"*)
    case "$*" in
      *"tr_speculation_shadow_event"*) table=event ;;
      *) table=success ;;
    esac
    if [ -n "$SHADOW_CONFLICT" ]; then
      printf 'OLDER_THAN(updated_at, INTERVAL 1 DAY)\\n'
    elif [ -f "$SHADOW_STATE/$table" ]; then
      printf 'OLDER_THAN(updated_at, INTERVAL 7 DAY)\\n'
    fi ;;
  *) printf '1\\n' ;;
esac
''')
    stub.chmod(0o755)
    log = tmp_path / "ddl.log"
    env = {**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
           "SPANNER_INSTANCE_ID": "offline", "SPANNER_DATABASE_ID": "offline", "GCP_PROJECT_ID": "offline",
           "SHADOW_STATE": str(tmp_path), "SHADOW_DDL_LOG": str(log), "SHADOW_CONFLICT": ""}
    script = Path(__file__).resolve().parents[1] / "scripts/deploy/migrate_speculation_shadow.sh"
    def run(environment):
        return subprocess.run(["/bin/bash", str(script)], env=environment, capture_output=True, text=True, check=False)  # noqa: S603 - fixed script and fake gcloud
    assert run(env).returncode == 0
    first = log.read_text()
    assert first.count("ADD ROW DELETION POLICY") == 2
    assert "exposure" not in first
    assert run(env).returncode == 0
    assert log.read_text() == first
    conflict = run({**env, "SHADOW_CONFLICT": "1"})
    assert conflict.returncode != 0 and "Refusing unexpected retention policy" in conflict.stderr
    assert log.read_text() == first


class ResetFault:
    """Delegate real context operations except reset, including consumed tokens."""
    def __init__(self, variable, stale=False, steps=None, name=""):
        self.variable, self.stale, self.steps, self.name = variable, stale, steps, name

    def get(self):
        return self.variable.get()

    def set(self, value):
        return self.variable.set(value)

    def reset(self, token):
        if self.steps is not None:
            self.steps.append(self.name)
        if self.stale:
            installed = self.variable.get()
            self.variable.reset(token)
            self.variable.set(installed)
            self.variable.reset(token)  # real already-used-token RuntimeError
        raise RuntimeError("reset failed before restoration")


@pytest.mark.parametrize("scope", ["shadow", "timing", "both"])
@pytest.mark.parametrize("stale", [False, True])
def test_failed_reset_restores_context_for_next_sync_authorize(monkeypatch, scope, stale):
    from trusted_router import gateway_timing as timing
    dispatcher = shadow.Dispatcher(ReferenceStore(), "reset")
    monkeypatch.setattr(shadow, "_RUNTIME", dispatcher)
    previous = {"previous": 17}
    timing_token = timing._OUTCOME_TIMING.set(previous)
    try:
        if scope in {"shadow", "both"}:
            monkeypatch.setattr(shadow, "_CURRENT", ResetFault(shadow._CURRENT, stale))
        if scope in {"timing", "both"}:
            monkeypatch.setattr(timing, "_OUTCOME_TIMING", ResetFault(timing._OUTCOME_TIMING, stale))
        @timing.timed_gateway_sync
        def _authorize_gateway_sync(request, body, settings):
            return {"data": {"ok": True}}
        settings = Settings(environment="test", speculative_provider_shadow_enabled=True)
        for sequence in (1, 2):
            assert _authorize_gateway_sync(None, None, settings)["data"]["ok"]
            assert dispatcher.pending.get_nowait().sequence == sequence
            assert shadow._CURRENT.get() is None
            assert timing._OUTCOME_TIMING.get() is previous
        assert dispatcher.coverage_lost()
    finally:
        variable = timing._OUTCOME_TIMING
        (variable.variable if isinstance(variable, ResetFault) else variable).reset(timing_token)


@pytest.mark.parametrize("failure", [False, True])
def test_finalization_order_survives_completion_cleanup_and_recorder_faults(monkeypatch, recorder_fault, failure):
    from trusted_router import gateway_timing as timing
    dispatcher = shadow.Dispatcher(ReferenceStore(), "order")
    monkeypatch.setattr(shadow, "_RUNTIME", dispatcher)
    _, install_fault = recorder_fault
    install_fault(dispatcher)
    steps = []
    monkeypatch.setattr(shadow, "_CURRENT", ResetFault(shadow._CURRENT, steps=steps, name="shadow"))
    monkeypatch.setattr(timing, "_OUTCOME_TIMING", ResetFault(timing._OUTCOME_TIMING, steps=steps, name="timing"))
    def complete(*args, **kwargs):
        steps.append("complete")
        raise ValueError("completion failed")
    monkeypatch.setattr(shadow, "complete", complete)
    original = shadow._record_loss
    def record(*args):
        steps.append("record")
        original(*args)
    monkeypatch.setattr(shadow, "_record_loss", record)
    error = RuntimeError("ordinary")
    response = {"data": {"ok": True}}
    holds = []
    @timing.timed_gateway_sync
    def _authorize_gateway_sync(request, body, settings):
        if failure:
            raise error
        holds.append(600)
        return response
    settings = Settings(environment="test", speculative_provider_shadow_enabled=True)
    if failure:
        with pytest.raises(RuntimeError) as caught:
            _authorize_gateway_sync(None, None, settings)
        assert caught.value is error
    else:
        assert _authorize_gateway_sync(None, None, settings) is response
    assert holds == ([] if failure else [600])
    assert steps == ["complete", "shadow", "timing", "record", "record", "record"]
    assert shadow._CURRENT.get() is None and timing._OUTCOME_TIMING.get() is None
    assert dispatcher.coverage_lost()


@pytest.mark.parametrize("fatal", ["cancel", "base", "keyboard", "exit"])
async def test_abnormal_authorize_is_never_a_success(monkeypatch, fatal, recorder_fault):
    import asyncio
    from types import SimpleNamespace

    from trusted_router import gateway_timing as timing
    dispatcher = shadow.Dispatcher(ReferenceStore(), "aborted")
    monkeypatch.setattr(shadow, "_RUNTIME", dispatcher)
    _, install_fault = recorder_fault
    install_fault(dispatcher)
    error = {"cancel": asyncio.CancelledError, "base": BaseException,
             "keyboard": KeyboardInterrupt, "exit": SystemExit}[fatal]("ordinary abort")
    holds = []
    @timing.timed_gateway_async
    async def authorize_gateway(request, body, settings):
        nonlocal error
        shadow.resolved(SimpleNamespace(workspace_id="w", hash="k", lookup_hash="a" * 64), "nonce")
        holds.append(600)
        shadow.authorized(SimpleNamespace(id="committed", invocation_nonce="nonce"), ("endpoint",), False)
        if fatal == "cancel":
            pending = asyncio.get_running_loop().create_future()
            asyncio.get_running_loop().call_soon(pending.cancel)
            try:
                await pending
            except asyncio.CancelledError as cancelled:
                error = cancelled
                raise
        await asyncio.sleep(0)
        raise error
    with pytest.raises(type(error)) as caught:
        await authorize_gateway(None, None, Settings(environment="test", speculative_provider_shadow_enabled=True))
    assert caught.value is error and holds == [600]
    outcome = dispatcher.pending.get_nowait()
    assert outcome.authorization_id == "committed"
    assert outcome.status == 500 and outcome.reason == "aborted"
    assert dispatcher.coverage_lost()
    apply(dispatcher.store, outcome, now=outcome.occurred_at, loss=dispatcher.coverage_lost())
    assert not key_state(dispatcher.store)["successes"]
    assert not any(table == "success" for table, _ in dispatcher.store.rows)
    assert shadow._CURRENT.get() is None and timing._OUTCOME_TIMING.get() is None


def test_last_resort_loss_reaches_status_worker_and_mint(monkeypatch):
    service = ready_service()
    monkeypatch.setattr(shadow, "_COVERAGE_UNKNOWN", False)
    monkeypatch.setattr(service.dispatcher.loss, "set", Mock(side_effect=RuntimeError("loss recorder")))
    shadow.record_loss("test", dispatcher=service.dispatcher)
    assert shadow._COVERAGE_UNKNOWN
    assert not service.dispatcher.loss.is_set()  # only the last resort knows
    response = client(Settings(environment="test", speculative_provider_shadow_enabled=True), service).get("/internal/speculation/shadow/status")
    assert response.status_code == 503
    assert response.json()["coverage_lost"] is True
    assert response.json()["coverage_loss_reason"] == "coverage-unknown"
    with pytest.raises(shadow.ShadowMiss, match="coverage"):
        service.mint(facts(), "boot", 2000)
    # A fresh dispatcher cannot clear process-local uncertainty either.
    dispatcher = shadow.Dispatcher(ReferenceStore(), "replacement")
    dispatcher.pending.put_nowait(dataclasses.replace(event(1), producer=dispatcher.producer, incarnation=dispatcher.incarnation))
    original = dispatcher.store.transaction
    def flush(operation):
        original(operation)
        dispatcher.stopped.set()
    monkeypatch.setattr(dispatcher.store, "transaction", flush)
    dispatcher.run()
    assert dispatcher.health == "coverage-lost"
    assert dispatcher.store.get("producer", "replacement")["lost"] is True
    assert shadow._COVERAGE_UNKNOWN


@pytest.mark.parametrize("fatal", [KeyboardInterrupt, GeneratorExit, SystemExit, asyncio.CancelledError, BaseException])
@pytest.mark.parametrize("site", ["setup", "completion", "shadow-cleanup", "timing-cleanup"])
def test_interrupted_finalization_restores_scopes_and_records_loss(monkeypatch, fatal, site):
    from types import SimpleNamespace

    from trusted_router import gateway_timing as timing
    dispatcher = shadow.Dispatcher(ReferenceStore(), "interrupted")
    monkeypatch.setattr(shadow, "_RUNTIME", dispatcher)
    monkeypatch.setattr(shadow, "_COVERAGE_UNKNOWN", False)
    error = fatal("observer interrupted")
    holds = []
    settings = Settings(environment="test", speculative_provider_shadow_enabled=True)
    previous = {"previous": 17}
    token = timing._OUTCOME_TIMING.set(previous)
    @timing.timed_gateway_sync
    def _authorize_gateway_sync(request, body, settings):
        holds.append(600)
        shadow.authorized(SimpleNamespace(id="committed", invocation_nonce="nonce"), ("endpoint",), False)
        return {"data": {"ok": True}}
    def interrupt(*args, **kwargs):
        raise error
    class InterruptedReset(ResetFault):
        def reset(self, token):
            raise error
    class InterruptedSetup(ResetFault):
        def get(self):
            # Shadow scope has already been installed by this point.
            assert shadow._CURRENT.get() is not None
            raise error
    try:
        with monkeypatch.context() as fault:
            if site == "completion":
                fault.setattr(shadow, "complete", interrupt)
            elif site == "setup":
                fault.setattr(timing, "_OUTCOME_TIMING", InterruptedSetup(timing._OUTCOME_TIMING))
            elif site == "shadow-cleanup":
                fault.setattr(shadow, "_CURRENT", InterruptedReset(shadow._CURRENT))
            else:
                fault.setattr(timing, "_OUTCOME_TIMING", InterruptedReset(timing._OUTCOME_TIMING))
            with pytest.raises(fatal) as caught:
                _authorize_gateway_sync(None, None, settings)
            assert caught.value is error
        # Keep the traceback alive: cleanup must not depend on generator GC.
        assert holds == ([] if site == "setup" else [600])
        assert shadow._CURRENT.get() is None
        assert timing._OUTCOME_TIMING.get() is previous
        assert dispatcher.coverage_lost()
        while not dispatcher.pending.empty():
            dispatcher.pending.get_nowait()
        assert _authorize_gateway_sync(None, None, settings)["data"]["ok"]
        assert dispatcher.pending.get_nowait().authorization_id == "committed"
        assert dispatcher.pending.empty()
    finally:
        timing._OUTCOME_TIMING.reset(token)


@pytest.mark.parametrize("fatal", [KeyboardInterrupt, GeneratorExit])
def test_loss_recorder_interrupt_propagates_after_completion_failure(monkeypatch, fatal):
    from trusted_router import gateway_timing as timing
    dispatcher = shadow.Dispatcher(ReferenceStore(), "recorder-interrupt")
    monkeypatch.setattr(shadow, "_RUNTIME", dispatcher)
    monkeypatch.setattr(shadow, "_COVERAGE_UNKNOWN", False)
    monkeypatch.setattr(shadow, "complete", Mock(side_effect=ValueError("completion")))
    error = fatal("recording interrupted")
    monkeypatch.setattr(dispatcher.loss, "set", Mock(side_effect=error))
    holds = []
    @timing.timed_gateway_sync
    def _authorize_gateway_sync(request, body, settings):
        holds.append(600)
        return {"data": {"ok": True}}
    with pytest.raises(fatal) as caught:
        _authorize_gateway_sync(None, None, Settings(environment="test", speculative_provider_shadow_enabled=True))
    assert caught.value is error and holds == [600]
    assert shadow._COVERAGE_UNKNOWN
    assert shadow._CURRENT.get() is None and timing._OUTCOME_TIMING.get() is None


def test_double_restoration_failure_retires_before_next_sync_authorize(monkeypatch):
    import contextvars
    from types import SimpleNamespace

    from trusted_router import gateway_timing as timing
    dispatcher = shadow.Dispatcher(ReferenceStore(), "double-restore")
    monkeypatch.setattr(shadow, "_RUNTIME", dispatcher)
    monkeypatch.setattr(shadow, "_COVERAGE_UNKNOWN", False)
    class DoubleFault(ResetFault):
        def set(self, value):
            if value is None or value.retired:
                raise RuntimeError("fallback set also failed")
            return super().set(value)
    # A fresh variable keeps the intentionally unrestorable context test-local.
    variable = contextvars.ContextVar("double-fault-shadow", default=None)
    monkeypatch.setattr(shadow, "_CURRENT", DoubleFault(variable))
    holds = []
    @timing.timed_gateway_sync
    def _authorize_gateway_sync(request, body, settings):
        holds.append(600)
        shadow.authorized(SimpleNamespace(id=f"auth-{len(holds)}", invocation_nonce="nonce"), ("endpoint",), False)
        return {"data": {"ok": True}}
    for sequence in (1, 2):
        assert _authorize_gateway_sync(None, None, Settings(environment="test", speculative_provider_shadow_enabled=True))["data"]["ok"]
        outcome = dispatcher.pending.get_nowait()
        assert outcome.sequence == sequence and outcome.authorization_id == f"auth-{sequence}"
        retired = variable.get()
        assert retired.retired
        before = dataclasses.asdict(dataclasses.replace(retired, dispatcher=None))
        shadow.resolved(SimpleNamespace(workspace_id="other", hash="other", lookup_hash="other"), "other")
        shadow.reason("other")
        shadow.boot_verified(True, "other")
        shadow.authorized(SimpleNamespace(id="other", invocation_nonce="other"), ("other",), True)
        assert dataclasses.asdict(dataclasses.replace(retired, dispatcher=None)) == before
        assert timing._OUTCOME_TIMING.get() is None
    assert holds == [600, 600] and dispatcher.pending.empty()
    assert dispatcher.coverage_lost() and shadow._COVERAGE_UNKNOWN
