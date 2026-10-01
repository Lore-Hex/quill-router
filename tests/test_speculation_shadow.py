"""Shadow safety gates. The reference store never touches customer state."""
from __future__ import annotations

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
    apply(store, event(1), now=1000)
    denial = dataclasses.replace(event(2), status=status, reason=reason, rate_scope=rate, occurred_at=900)
    apply(store, denial, now=2000)
    workspace = store.get("scope", shadow.identity("workspace", "w"))
    assert workspace["clean_since"] == (2000 if scope == "workspace" else 1000)
    assert key_state(store)["clean_since"] == (2000 if scope == "key" else 1000)


def test_loss_restart_unacknowledged_tail_and_late_membership():
    store = ReferenceStore()
    apply(store, event(1), now=1000)
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
        apply(store, dataclasses.replace(event(seq), occurred_at=1980+seq//2, endpoint_ids=(claims["route"]["endpoint_id"],)), now=1000 if seq == 1 else 2000)
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


def test_content_free_observation_and_observer_failure(monkeypatch):
    dispatcher = shadow.Dispatcher(ReferenceStore(), "p")
    observation = shadow.Observation(dispatcher, workspace_id="w", key_id="k")
    shadow.complete(observation, {"total_ms": 4, "prompt": "secret"})
    outcome = dataclasses.asdict(dispatcher.pending.get_nowait())
    assert "secret" not in json.dumps(outcome) and "prompt" not in outcome
    monkeypatch.setattr(dispatcher, "try_submit", Mock(side_effect=RuntimeError("do not log me")))
    shadow.complete(observation, {})
    assert dispatcher.loss.is_set()


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
