"""Publishing the edge policy, dark (docs/design/routing-snapshot-v1.md, "The edge policy").

The golden files fastpath/testdata/edge_policy_v1.{body,manifest}.json are
held to scripts/fastpath/edge_policy_vectors.py here; the Go reader (G8) holds
itself to the same files.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from pydantic import ValidationError

from trusted_router.config import Settings
from trusted_router.detached_jws import JWSError, TrustedKey, b64encode
from trusted_router.edge_policy import (
    LIFETIME_SECONDS,
    REFRESH_SECONDS,
    canonical_edge_policy_bytes,
    parse_edge_policy,
)
from trusted_router.main import create_app
from trusted_router.routes.internal.fastpath_edge_policy import PATH
from trusted_router.routing_snapshot import Plane
from trusted_router.routing_snapshot_signing import (
    EDGE_POLICY_TYP,
    PURPOSE,
    TYP,
    EdgePolicySigner,
    RoutingSnapshotSigner,
    edge_policy_manifest_claims,
    verify_edge_policy_manifest,
    verify_manifest,
)
from trusted_router.services import edge_policy_publisher as publisher_module
from trusted_router.services.edge_policy_publisher import (
    EdgePolicyPublisher,
    PolicyUnavailable,
    build_publisher,
)
from trusted_router.services.routing_snapshot_publisher import AUDIENCE, ISSUER, plane_of

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "internal-token-for-the-edge-policy-tests"  # noqa: S105 - test value
KID = "tr-routing-snapshot-test-run"
T0 = 1_792_368_000  # 2026-10-19T00:00Z, a window start
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64


def _load(name: str) -> ModuleType:
    path = ROOT / "scripts" / "fastpath" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VECTORS = _load("edge_policy_vectors")
ROUTING_VECTORS = VECTORS.ROUTING
GOLDEN_PLANE = Plane(cloud="gcp", region="us-central1")


class Clock:
    def __init__(self, now: int) -> None:
        self.now = now

    def __call__(self) -> float:
        return float(self.now)


class Resolver:
    """The Stage D policy resolver's surface the publisher and authorize read."""

    def __init__(self, digests: frozenset[str], *, release: bool = True) -> None:
        self.digests, self.release = digests, release
        self.kicks = self.refreshes = 0
        self.refresh_ok = True
        self.on_refresh: Any = None

    def kick(self) -> None:
        self.kicks += 1

    def release_configured(self) -> bool:
        return self.release

    def refresh(self) -> bool:
        self.refreshes += 1
        if self.on_refresh is not None:
            self.on_refresh()
        return self.refresh_ok

    def accepted_image_digests(self) -> frozenset[str]:
        return self.digests


class Outbox:
    def activity_insert_statement(self, generation: Any) -> Any:
        return None


class DurableStore:
    _operational_analytics_outbox = Outbox()


@pytest.fixture
def key(tmp_path: Path) -> tuple[Path, str, str]:
    private = Ed25519PrivateKey.generate()
    pem = private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    path = tmp_path / "signing.pem"
    path.write_bytes(pem)
    return path, b64encode(private.public_key().public_bytes_raw()), pem.decode("ascii")


def _settings(key: tuple[Path, str, str], **overrides: Any) -> Settings:
    path, public, _pem = key
    values: dict[str, Any] = {
        "environment": "test", "sentry_dsn": None, "internal_gateway_token": TOKEN,
        "edge_policy_publish_enabled": True,
        "routing_snapshot_private_key_file": str(path),
        "routing_snapshot_kid": KID,
        "routing_snapshot_public_key": public,
        **overrides,
    }
    return Settings(**values)


class _Finished:
    def is_alive(self) -> bool:
        return False

    def join(self, timeout: float | None = None) -> None:
        return None


def _inline(run: Any) -> _Finished:
    """Runs the background worker inside the poll that starts it: deterministic tests."""
    run()
    return _Finished()


def _publisher(settings: Settings, clock: Clock, resolver: Resolver | None = None,
               store: Any = None, *, background: bool = False) -> EdgePolicyPublisher:
    extra: dict[str, Any] = {} if background else {"spawn": _inline}
    return build_publisher(settings, clock=clock, resolver=lambda: resolver,
                           typed_store=lambda: store, **extra)


def _app(settings: Settings, publisher: EdgePolicyPublisher | None = None) -> Any:
    app = create_app(settings, init_observability=False)
    if publisher is not None:
        app.state.edge_policy_publisher = publisher
    return app


def _get(client: TestClient, **headers: str) -> Any:
    return client.get(PATH, headers={"Authorization": f"Bearer {TOKEN}", **headers})


def _trusted(public: str) -> TrustedKey:
    return TrustedKey(KID, PURPOSE, public, ISSUER, AUDIENCE)


def _verify(response: Any, settings: Settings, public: str, now: int) -> tuple[bytes, Any]:
    payload = response.json()
    assert set(payload) == {"v", "kind", "manifest", "body"}
    assert (payload["v"], payload["kind"]) == (1, "tr-edge-policy-publication")
    body = base64.b64decode(payload["body"], validate=True)
    claims = verify_edge_policy_manifest(
        payload["manifest"], [_trusted(public)], body, now=now,
        expected_plane=plane_of(settings), expected_aud=AUDIENCE,
    )
    return body, claims


def _golden_body() -> bytes:
    return VECTORS.OUTPUT_BODY.read_bytes()


def _golden_manifest() -> dict[str, Any]:
    return json.loads(VECTORS.OUTPUT_MANIFEST.read_text())


def _golden_verify(token: str, body: bytes, *, now: int = T0, **overrides: Any) -> Any:
    arguments = {"expected_plane": GOLDEN_PLANE, "expected_aud": VECTORS.TEST_KEY.aud,
                 "allow_test_key": True, **overrides}
    return verify_edge_policy_manifest(token, [VECTORS.TEST_KEY], body, now=now, **arguments)


# --- off by default ------------------------------------------------------------------------


def test_publishing_is_off_by_default_and_the_route_is_absent() -> None:
    settings = Settings(environment="test", sentry_dsn=None, internal_gateway_token=TOKEN)
    assert settings.edge_policy_publish_enabled is False
    app = create_app(settings, init_observability=False)
    assert not hasattr(app.state, "edge_policy_publisher")
    client = TestClient(app)
    assert _get(client).status_code == 404
    assert client.get("/v1" + PATH, headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 404


def test_the_setting_reads_its_environment_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TR_EDGE_POLICY_PUBLISH_ENABLED", "true")
    assert Settings(environment="test", sentry_dsn=None).edge_policy_publish_enabled is True
    monkeypatch.setenv("TR_EDGE_POLICY_PUBLISH_ENABLED", "false")
    assert Settings(environment="test", sentry_dsn=None).edge_policy_publish_enabled is False


_EDGE_MODULES = (
    "trusted_router.edge_policy",
    "trusted_router.services.edge_policy_publisher",
    "trusted_router.routes.internal.fastpath_edge_policy",
)


def _modules_loaded_by_an_app(extra_env: dict[str, str]) -> list[str]:
    script = (
        "import sys; from trusted_router.config import Settings; "
        "from trusted_router.main import create_app; "
        "app = create_app(Settings(), init_observability=False); "
        f"print(','.join(m for m in {_EDGE_MODULES!r} if m in sys.modules))"
    )
    env = {**os.environ, "TR_ENVIRONMENT": "test", "TR_STORAGE_BACKEND": "memory",
           "TR_INTERNAL_GATEWAY_TOKEN": TOKEN, "PYTHONPATH": str(ROOT / "src")}
    env.pop("TR_EDGE_POLICY_PUBLISH_ENABLED", None)
    env.pop("TR_ROUTING_SNAPSHOT_PUBLISH_ENABLED", None)
    env.update(extra_env)
    result = subprocess.run(  # noqa: S603 - fixed Python script, no shell or caller input
        [sys.executable, "-c", script], env=env,
        # A cold import of trusted_router.main takes about 13 s on an idle
        # machine and far longer on a loaded one; the bound only catches a hang.
        capture_output=True, text=True, timeout=300, cwd=ROOT, check=False,
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.strip().splitlines()
    return [m for m in lines[-1].split(",") if m] if lines else []


def test_off_an_app_imports_no_edge_policy_module() -> None:
    # Off, with routing snapshot publishing on too: that signer does not load
    # the edge policy either.
    assert _modules_loaded_by_an_app({}) == []
    assert _modules_loaded_by_an_app({"TR_ROUTING_SNAPSHOT_PUBLISH_ENABLED": "true"}) == []
    # Positive control: on, the publisher and the route are loaded.
    assert sorted(_modules_loaded_by_an_app({"TR_EDGE_POLICY_PUBLISH_ENABLED": "true"})) == \
        sorted(_EDGE_MODULES)


def test_only_the_wiring_publisher_and_signer_import_the_edge_policy_modules() -> None:
    importers = sorted(
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "src").rglob("*.py")
        if re.search(r"^\s*(from|import)\s+trusted_router\.(edge_policy\b|services\.edge_policy_publisher\b"
                     r"|routes\.internal\.fastpath_edge_policy\b)|"
                     r"^\s*from \. import fastpath_edge_policy\b",
                     path.read_text(), re.MULTILINE)
    )
    assert importers == [
        "src/trusted_router/main.py",
        "src/trusted_router/routes/internal/__init__.py",
        # Inside the route's handler, when it runs.
        "src/trusted_router/routes/internal/fastpath_edge_policy.py",
        "src/trusted_router/routing_snapshot_signing.py",
        "src/trusted_router/services/edge_policy_publisher.py",
    ]


def test_the_route_lives_only_on_the_internal_surfaces(key) -> None:
    for surface, present in (("combined", True), ("internal", True), ("control", False),
                             ("public", False), ("observer", False)):
        settings = _settings(key, service_surface=surface)
        if surface not in {"combined", "internal"}:
            settings = settings.model_copy(update={"internal_gateway_token": None})
        app = _app(settings, _publisher(settings, Clock(T0 + 5)))
        assert (_get(TestClient(app)).status_code == 200) is present, surface


# --- authentication ------------------------------------------------------------------------


def test_the_route_refuses_without_the_internal_token(key) -> None:
    settings = _settings(key)
    client = TestClient(_app(settings, _publisher(settings, Clock(T0 + 5))))
    assert client.get(PATH).status_code == 401
    assert client.get(PATH, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get(PATH, headers={"X-TrustedRouter-Internal-Token": TOKEN}).status_code == 200
    ok = _get(client)
    assert ok.status_code == 200
    etag = {"If-None-Match": ok.headers["etag"]}
    assert _get(client, **etag).status_code == 304
    assert client.get(PATH, headers=etag).status_code == 401
    other = b64encode(Ed25519PrivateKey.generate().public_key().public_bytes_raw())
    unsigned = TestClient(_app(_settings(key, routing_snapshot_public_key=other)))
    assert _get(unsigned).status_code == 503
    assert unsigned.get(PATH).status_code == 401


# --- what it serves --------------------------------------------------------------------------


def test_the_response_verifies_for_the_planes_edge(key) -> None:
    settings = _settings(key, fastpath_pilot_enabled=True,
                         fastpath_pilot_workspaces="ws-test-b,ws-test-a",
                         spend_lease_accepted_gcp_image_digests=DIGEST_C)
    now = T0 + 7
    client = TestClient(_app(settings, _publisher(settings, Clock(now), Resolver(frozenset({DIGEST_B, DIGEST_A})))))
    response = _get(client)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body, claims = _verify(response, settings, key[1], now)
    # Issued at the publisher's clock.
    assert (claims.issued_at, claims.not_after) == (now, now + LIFETIME_SECONDS)
    assert claims.iss == ISSUER
    assert json.loads(body)["refresh_at"] == now + REFRESH_SECONDS
    assert response.headers["etag"] == f'"{hashlib.sha256(body).hexdigest()}"'
    policy = parse_edge_policy(body)
    assert canonical_edge_policy_bytes(policy) == body
    assert policy.plane.model_dump() == plane_of(settings).model_dump()
    assert policy.accepted_image_digests == (DIGEST_A, DIGEST_B, DIGEST_C)
    assert policy.fastpath_pilot_enabled is True
    assert policy.fastpath_pilot_workspaces == ("ws-test-a", "ws-test-b")
    unchanged = _get(client, **{"If-None-Match": response.headers["etag"]})
    assert unchanged.status_code == 304 and unchanged.content == b""
    # Another plane's or another consumer's verifier refuses it.
    with pytest.raises(ValueError, match="manifest plane"):
        verify_edge_policy_manifest(
            response.json()["manifest"], [_trusted(key[1])], body, now=now,
            expected_plane=plane_of(settings).model_copy(update={"region": "europe-west4"}),
            expected_aud=AUDIENCE)
    with pytest.raises(ValueError, match="manifest audience"):
        verify_edge_policy_manifest(
            response.json()["manifest"], [_trusted(key[1])], body, now=now,
            expected_plane=plane_of(settings), expected_aud="tr-other")


def test_the_body_carries_every_live_fact(key) -> None:
    monitor = "sk-tr-v1-synthetic-monitor-for-tests"
    settings = _settings(
        key, stage_d_eligibility_enabled=True, stage_d_heartbeat_enabled=False,
        stage_d_require_heartbeat_declaration=True, stage_d_pilot_workspace_ids="ws-sd-2,ws-sd-1",
        settle_outbox_enabled=True, fastpath_pilot_regions="us-east4,us-central1",
        gateway_authorize_max_in_flight_per_key=7, synthetic_monitor_api_key=monitor,
        async_settle_shadow_policy="shadow-v2", async_settle_shadow_workspaces="ws-shadow-1",
        primary_region="us-central1", regions="europe-west4,us-east4", multi_region_enabled=False,
    )
    publication = _publisher(settings, Clock(T0), store=DurableStore()).current()
    body = json.loads(publication.body)
    assert body["stage_d"] == {
        "eligibility_enabled": True, "heartbeat_enabled": False,
        "require_heartbeat_declaration": True, "pilot_workspaces": ["ws-sd-1", "ws-sd-2"],
    }
    assert body["records_durable"] is True
    assert body["fastpath_pilot_regions"] == ["us-central1", "us-east4"]
    assert body["gateway_authorize_max_in_flight_per_key"] == 7
    assert body["monitor_lookup_hash"] == hashlib.sha256(monitor.encode()).hexdigest()
    assert monitor not in publication.body.decode()
    assert (body["async_settle_shadow_policy"], body["async_settle_shadow_workspaces"]) == (
        "shadow-v2", ["ws-shadow-1"])
    # The configured order, primary first when it was not listed (regions.configured_regions).
    assert body["configured_regions"] == ["us-central1", "europe-west4", "us-east4"]
    assert (body["primary_region"], body["multi_region_enabled"]) == ("us-central1", False)
    # Defaults: no monitor key is null, and the settle outbox off is not durable.
    plain_body = _publisher(_settings(key), Clock(T0), store=DurableStore()).current().body
    plain = json.loads(plain_body)
    assert plain["monitor_lookup_hash"] is None and plain["records_durable"] is False
    assert b'"monitor_lookup_hash":null' in plain_body
    assert plain["async_settle_shadow_policy"] == ""


def test_internal_models_are_the_catalogs_internal_only_set(key) -> None:
    """The set require_internal_model_key protects, from the live catalog, sorted."""
    from trusted_router.catalog import MODELS, MONITOR_MODEL_ID
    from trusted_router.internal_model_access import internal_model_ids, requests_internal_model

    published = json.loads(_publisher(_settings(key), Clock(T0)).current().body)["internal_models"]
    assert published == sorted(published) == sorted(internal_model_ids())
    assert published == sorted({MONITOR_MODEL_ID} | {m for m, model in MODELS.items() if model.internal_only})
    assert {"trustedrouter/monitor", "trustedrouter/fortanix-test"} <= set(published)
    # Parity with the admission check itself: exactly the ids it refuses to a public key.
    for model_id in {*MODELS, MONITOR_MODEL_ID}:
        assert requests_internal_model({"model": model_id}) is (model_id in published), model_id


def test_internal_models_are_read_at_signing_time(key, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock(T0)
    publisher = _publisher(_settings(key), clock)
    first = json.loads(publisher.current().body)["internal_models"]
    monkeypatch.setattr(publisher_module, "internal_model_ids",
                        lambda: frozenset({*first, "trustedrouter/new-internal-probe"}))
    clock.now = T0 + REFRESH_SECONDS
    later = json.loads(publisher.current().body)["internal_models"]
    assert later == sorted({*first, "trustedrouter/new-internal-probe"})


def test_the_digests_and_durability_are_what_authorize_reads(key) -> None:
    """Parity with gateway.py: the same union its boot check reads (here,
    after a successful refresh), the same records_durable its pilot reads."""
    from starlette.requests import Request

    from trusted_router.routes.internal import gateway
    from trusted_router.services import fastpath_frontdoor

    for override, resolver in (("", None), (DIGEST_C, None), ("", Resolver(frozenset({DIGEST_A}))),
                               (DIGEST_A, Resolver(frozenset({DIGEST_A, DIGEST_B})))):
        settings = _settings(key, spend_lease_accepted_gcp_image_digests=override)
        app = _app(settings)
        app.state.stage_d_policy_resolver = resolver
        request = Request({"type": "http", "app": app, "headers": [], "method": "GET", "path": "/"})
        expected = gateway._stage_d_accepted_image_digests(request, settings)
        publication = _publisher(settings, Clock(T0), resolver).current()
        assert json.loads(publication.body)["accepted_image_digests"] == sorted(expected)
    for outbox, store, durable in ((False, DurableStore(), False), (True, None, False),
                                   (True, object(), False), (True, DurableStore(), True)):
        settings = _settings(key, settle_outbox_enabled=outbox)
        assert fastpath_frontdoor.records_durable(outbox, store) is durable
        publication = _publisher(settings, Clock(T0), store=store).current()
        assert json.loads(publication.body)["records_durable"] is durable


def test_each_signing_refreshes_the_stage_d_policy_first(key) -> None:
    clock = Clock(T0)
    resolver = Resolver(frozenset({DIGEST_A}))
    publisher = _publisher(_settings(key), clock, resolver)
    # The refresh lands a new set: the body signed after it carries that set.
    resolver.on_refresh = lambda: setattr(resolver, "digests", frozenset({DIGEST_A, DIGEST_B}))
    first = publisher.current()
    assert json.loads(first.body)["accepted_image_digests"] == [DIGEST_A, DIGEST_B]
    # Bounded by the interval cache: polls before refresh_at neither refresh nor sign.
    for instant in (T0 + 1, T0 + REFRESH_SECONDS - 1):
        clock.now = instant
        assert publisher.current() is first
    assert (resolver.refreshes, publisher.signatures, resolver.kicks) == (1, 1, 0)
    clock.now = T0 + REFRESH_SECONDS
    publisher.current()
    assert (resolver.refreshes, publisher.signatures) == (2, 2)


def test_a_failed_refresh_signs_nothing_and_answers_503_after_not_after(key, caplog) -> None:
    settings = _settings(key)
    clock = Clock(T0)
    resolver = Resolver(frozenset({DIGEST_A}))
    publisher = _publisher(settings, clock, resolver)
    first = publisher.current()
    resolver.refresh_ok = False
    caplog.set_level(logging.WARNING)
    # Past refresh_at the refresh fails: nothing new is signed, and the last
    # copy is served while it is within its not_after.
    clock.now = T0 + REFRESH_SECONDS
    assert publisher.current() is first
    assert (resolver.refreshes, publisher.signatures) == (2, 1)
    assert "edge policy unavailable: digest-policy-unavailable" in [r.getMessage() for r in caplog.records]
    # Within the backoff, no second refresh.
    clock.now = T0 + REFRESH_SECONDS + publisher_module.ERROR_RETRY_SECONDS - 1
    assert publisher.current() is first and resolver.refreshes == 2
    # At not_after the copy is gone: 503 digest-policy-unavailable.
    clock.now = T0 + LIFETIME_SECONDS
    client = TestClient(_app(settings, publisher))
    response = _get(client)
    assert response.status_code == 503
    assert response.json() == {"status": "digest-policy-unavailable",
                               "retry_at": clock.now + publisher_module.ERROR_RETRY_SECONDS}
    assert response.headers["retry-after"] == str(publisher_module.ERROR_RETRY_SECONDS)
    assert (resolver.refreshes, publisher.signatures) == (3, 1)
    clock.now += publisher_module.ERROR_RETRY_SECONDS - 1
    assert _get(client).json()["status"] == "digest-policy-unavailable"
    assert resolver.refreshes == 3
    # Positive control: the refresh succeeds again, and a new body is signed.
    resolver.refresh_ok = True
    clock.now += 1
    assert _get(client).status_code == 200
    assert (resolver.refreshes, publisher.signatures) == (4, 2)


def test_without_a_release_the_set_is_the_break_glass_override_alone(key) -> None:
    settings = _settings(key, spend_lease_accepted_gcp_image_digests=DIGEST_C)
    resolver = Resolver(frozenset({DIGEST_A}), release=False)
    body = json.loads(_publisher(settings, Clock(T0), resolver).current().body)
    assert body["accepted_image_digests"] == [DIGEST_C] and resolver.refreshes == 0
    # No resolver at all: the same.
    body = json.loads(_publisher(settings, Clock(T0), None).current().body)
    assert body["accepted_image_digests"] == [DIGEST_C]
    # Positive control: with a release, the refreshed set joins the override.
    resolver.release = True
    body = json.loads(_publisher(settings, Clock(T0), resolver).current().body)
    assert body["accepted_image_digests"] == [DIGEST_A, DIGEST_C] and resolver.refreshes == 1


def test_the_real_resolver_refreshes_and_its_failure_is_a_503(key) -> None:
    import httpx

    from trusted_router.stage_d_policy import StageDPolicyResolver

    fetches: list[Any] = []

    def unreachable(**kwargs: Any) -> Any:
        fetches.append(kwargs)
        raise httpx.ConnectError("unreachable")

    settings = _settings(key, spend_lease_accepted_gcp_image_digests=DIGEST_C)
    failing = StageDPolicyResolver(settings, object(), verifier=object(),  # type: ignore[arg-type]
                                   release_urls=["https://release.invalid/trust"],
                                   client_factory=unreachable)
    assert failing.release_configured() is True
    with pytest.raises(PolicyUnavailable) as refused:
        _publisher(settings, Clock(T0), failing).current()  # type: ignore[arg-type]
    assert refused.value.status == "digest-policy-unavailable" and len(fetches) == 1
    # Positive control: no release configured, no fetch, the override alone.
    idle = StageDPolicyResolver(settings, object(), verifier=object(),  # type: ignore[arg-type]
                                release_urls=[], client_factory=unreachable)
    assert idle.release_configured() is False
    body = json.loads(_publisher(settings, Clock(T0), idle).current().body)  # type: ignore[arg-type]
    assert body["accepted_image_digests"] == [DIGEST_C] and len(fetches) == 1


def test_a_hanging_refresh_never_delays_a_poll_and_runs_once(key) -> None:
    """The refresh and signing run in one background worker: polls neither
    wait on it nor start a second one."""
    import threading
    import time

    entered, release = threading.Event(), threading.Event()
    resolver = Resolver(frozenset({DIGEST_A}))
    resolver.on_refresh = lambda: (entered.set(), release.wait(60))
    settings = _settings(key)
    clock = Clock(T0)
    publisher = _publisher(settings, clock, resolver, background=True)
    client = TestClient(_app(settings, publisher))
    try:
        # No copy yet: 503 compiling at once, while the worker hangs.
        started = time.monotonic()
        response = _get(client)
        assert time.monotonic() - started < 2.0
        assert response.status_code == 503 and response.json()["status"] == "compiling"
        assert response.headers["retry-after"] == str(publisher_module.COMPILING_RETRY_SECONDS)
        assert entered.wait(30)
        # Concurrent polls: every one answers at once, and none starts a second refresh.
        answers: list[int] = []
        durations: list[float] = []

        def poll() -> None:
            began = time.monotonic()
            try:
                publisher.current()
                answers.append(200)
            except PolicyUnavailable as exc:
                answers.append(503 if exc.status == "compiling" else 0)
            durations.append(time.monotonic() - began)

        threads = [threading.Thread(target=poll) for _ in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        assert answers == [503] * 16 and max(durations) < 1.0
        assert resolver.refreshes == 1 and publisher.attempts == 1
    finally:
        release.set()
    assert publisher.idle(30)
    first = publisher.current()
    assert resolver.refreshes == 1 and publisher.signatures == 1
    # Past refresh_at, a hanging refresh: the cached copy is served at once.
    entered.clear()
    release.clear()
    clock.now = first.refresh_at
    try:
        for _ in range(8):
            started = time.monotonic()
            assert publisher.current() is first
            assert time.monotonic() - started < 1.0
        assert entered.wait(30)
        assert resolver.refreshes == 2 and publisher.attempts == 2
    finally:
        release.set()
    assert publisher.idle(30)
    # Positive control: the background signing landed and is now served.
    later = publisher.current()
    assert later is not first and later.issued_at == first.refresh_at


def test_one_signature_per_refresh_interval_served_until_refresh_at(key) -> None:
    clock = Clock(T0 + 1)
    resolver = Resolver(frozenset({DIGEST_A}))
    publisher = _publisher(_settings(key), clock, resolver)
    first = publisher.current()
    assert (first.issued_at, first.refresh_at, first.not_after) == (
        T0 + 1, T0 + 16, T0 + 61)
    # Every poll until refresh_at gets that publication, even after a fact changed.
    resolver.digests = frozenset({DIGEST_A, DIGEST_B})
    clock.now = first.refresh_at - 1
    assert publisher.current() is first and publisher.signatures == 1
    # The first poll at refresh_at reads the facts afresh and signs once.
    clock.now = first.refresh_at
    later = publisher.current()
    assert later.issued_at == first.refresh_at and publisher.signatures == 2
    assert json.loads(later.body)["accepted_image_digests"] == [DIGEST_A, DIGEST_B]
    assert publisher.current() is later and publisher.signatures == 2


def test_two_instances_at_one_instant_serve_identical_bytes(key) -> None:
    # Ed25519 signatures are deterministic: same facts, same clock, same bytes.
    settings = _settings(key)
    first = _publisher(settings, Clock(T0), Resolver(frozenset({DIGEST_A})))
    second = _publisher(settings, Clock(T0), Resolver(frozenset({DIGEST_A})))
    assert first.current().response == second.current().response


def test_a_fact_outside_the_schema_answers_503_and_recovers(key, caplog) -> None:
    settings = _settings(key)
    clock = Clock(T0)
    resolver = Resolver(frozenset({"sha256:not-a-digest"}))
    publisher = _publisher(settings, clock, resolver)
    caplog.set_level(logging.WARNING)
    with pytest.raises(PolicyUnavailable) as failed:
        publisher.current()
    assert failed.value.status == "policy-error"
    assert failed.value.retry_after == publisher_module.ERROR_RETRY_SECONDS
    response = _get(TestClient(_app(settings, publisher)))
    assert response.status_code == 503
    assert response.json() == {"status": "policy-error", "retry_at": T0 + publisher_module.ERROR_RETRY_SECONDS}
    assert response.headers["retry-after"] == str(publisher_module.ERROR_RETRY_SECONDS)
    assert "edge policy publication failed: ValidationError" in [r.getMessage() for r in caplog.records]
    # Within the backoff, no second attempt even with a valid set; after it, a valid set publishes.
    resolver.digests = frozenset({DIGEST_A})
    clock.now += publisher_module.ERROR_RETRY_SECONDS - 1
    assert _get(TestClient(_app(settings, publisher))).status_code == 503
    assert resolver.refreshes == 1
    clock.now += 1
    assert _get(TestClient(_app(settings, publisher))).status_code == 200


def test_an_unusable_key_leaves_the_route_answering_signer_unavailable(key) -> None:
    other = b64encode(Ed25519PrivateKey.generate().public_key().public_bytes_raw())
    app = create_app(_settings(key, routing_snapshot_public_key=other), init_observability=False)
    assert app.state.edge_policy_publisher is None
    response = _get(TestClient(app))
    assert response.status_code == 503 and response.json() == {"status": "signer-unavailable"}
    # Positive control: the pinned key installs a publisher that signs in the
    # background (the first poll finds it compiling) and then serves.
    app = create_app(_settings(key), init_observability=False)
    publisher = app.state.edge_policy_publisher
    assert isinstance(publisher, EdgePolicyPublisher)
    client = TestClient(app)
    first = _get(client)
    assert first.status_code in {200, 503}
    if first.status_code == 503:
        assert first.json()["status"] == "compiling"
    assert publisher.idle(30)
    assert _get(client).status_code == 200


def test_no_secret_appears_in_responses_or_logs(key, caplog: pytest.LogCaptureFixture) -> None:
    path, public, pem = key
    raw = serialization.load_pem_private_key(path.read_bytes(), password=None)
    assert isinstance(raw, Ed25519PrivateKey)
    private_raw = raw.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption(),
    )
    monitor = "sk-tr-v1-synthetic-monitor-secret-for-tests"
    secrets = [TOKEN, pem, pem.splitlines()[1], b64encode(private_raw), monitor,
               base64.b64encode(private_raw).decode("ascii"), private_raw.hex()]
    caplog.set_level(logging.DEBUG)
    seen: list[str] = []
    settings = _settings(key, synthetic_monitor_api_key=monitor)
    client = TestClient(_app(settings, _publisher(settings, Clock(T0))))
    for response in (_get(client), client.get(PATH)):
        seen += [response.text, json.dumps(dict(response.headers))]
    seen.append(base64.b64decode(_get(client).json()["body"]).decode("ascii"))
    broken = _publisher(settings, Clock(T0), Resolver(frozenset({"bad"})))
    seen.append(_get(TestClient(_app(settings, broken))).text)
    other = b64encode(Ed25519PrivateKey.generate().public_key().public_bytes_raw())
    seen.append(_get(TestClient(create_app(
        _settings(key, routing_snapshot_public_key=other), init_observability=False))).text)
    seen += [record.getMessage() for record in caplog.records]
    assert any("edge policy published" in line for line in seen)
    assert any("edge policy publication failed" in line for line in seen)
    assert any("edge policy publisher disabled" in line for line in seen)
    for line in seen:
        for secret in secrets:
            assert secret not in line


# --- signing: the edge policy and the routing snapshot cannot stand in for each other ------


def test_each_verifier_refuses_the_others_manifest() -> None:
    edge_body, edge_token = _golden_body(), _golden_manifest()["jws"]
    routing_body = ROUTING_VECTORS.OUTPUT_BODY.read_bytes()
    routing_token = json.loads(ROUTING_VECTORS.OUTPUT_MANIFEST.read_text())["jws"]
    key = VECTORS.TEST_KEY
    routing_now = json.loads(base64.urlsafe_b64decode(routing_token.split(".")[1] + "=="))["compiled_at"]
    routing_args = {"expected_plane": GOLDEN_PLANE, "expected_aud": key.aud, "allow_test_key": True}
    # Positive controls: each verifies under its own verifier.
    _golden_verify(edge_token, edge_body)
    verify_manifest(routing_token, [key], routing_body, now=routing_now, **routing_args)
    # The refusal is the header type, before any claim is read: the same key,
    # purpose, plane and audience are not enough.
    for body in (edge_body, routing_body):
        with pytest.raises(JWSError, match="^type$"):
            verify_manifest(edge_token, [key], body, now=T0, **routing_args)
        with pytest.raises(JWSError, match="^type$"):
            _golden_verify(routing_token, body, now=routing_now)
    assert (TYP, EDGE_POLICY_TYP) == ("tr-routing-snapshot-v1", "tr-edge-policy-v1")


def test_the_claims_differ_too() -> None:
    """Under the other's typ, the claims still would not validate."""
    from trusted_router.routing_snapshot_signing import EdgePolicyManifestClaims, ManifestClaims

    edge_claims = json.loads(base64.urlsafe_b64decode(_golden_manifest()["jws"].split(".")[1] + "=="))
    routing_token = json.loads(ROUTING_VECTORS.OUTPUT_MANIFEST.read_text())["jws"]
    routing_claims = json.loads(base64.urlsafe_b64decode(routing_token.split(".")[1] + "=="))
    assert set(edge_claims) == {"v", "sha256", "size", "issued_at", "not_after", "plane", "iss", "aud"}
    with pytest.raises(ValidationError):
        ManifestClaims.model_validate(edge_claims)
    with pytest.raises(ValidationError):
        EdgePolicyManifestClaims.model_validate(routing_claims)
    # Positive control.
    EdgePolicyManifestClaims.model_validate(edge_claims)
    ManifestClaims.model_validate(routing_claims)


def test_the_edge_signer_keeps_the_routing_signers_key_checks() -> None:
    private = Ed25519PrivateKey.from_private_bytes(VECTORS.TEST_KEY_SEED)
    # The public test key is refused outside test mode, by the routing signer it is built from.
    with pytest.raises(ValueError, match="test key"):
        EdgePolicySigner(RoutingSnapshotSigner(private, VECTORS.TEST_KEY))
    with pytest.raises(ValueError, match="test key outside test mode"):
        _golden_verify(_golden_manifest()["jws"], _golden_body(), allow_test_key=False)
    # Positive control: a non-test key signs and verifies without test mode.
    other = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    production = TrustedKey("routing-snapshot-prod-1", PURPOSE,
                            b64encode(other.public_key().public_bytes_raw()), ISSUER, AUDIENCE)
    token = EdgePolicySigner(RoutingSnapshotSigner(other, production)).sign(_golden_body())
    verify_edge_policy_manifest(token, [production], _golden_body(), now=T0,
                                expected_plane=GOLDEN_PLANE, expected_aud=AUDIENCE)


# --- the golden files ----------------------------------------------------------------------


def test_the_golden_files_are_what_the_python_publisher_writes_now() -> None:
    stale = (
        "fastpath/testdata/edge_policy_v1.*.json is stale: run "
        "`uv run python scripts/fastpath/edge_policy_vectors.py` and check the Go reader"
    )
    assert _golden_body() == VECTORS.render_body(), stale
    assert VECTORS.OUTPUT_MANIFEST.read_text() == VECTORS.render_manifest(), stale
    # Deterministic: a second render is byte-identical.
    assert VECTORS.render_body() == VECTORS.render_body()
    assert VECTORS.render_manifest() == VECTORS.render_manifest()


def test_the_golden_body_is_canonical_and_covers_every_field() -> None:
    body = _golden_body()
    assert body.isascii() and not body.endswith(b"\n")
    policy = parse_edge_policy(body)
    assert canonical_edge_policy_bytes(policy) == body
    decoded = json.loads(body)
    assert set(decoded) == {
        "v", "kind", "plane", "issued_at", "refresh_at", "not_after", "accepted_image_digests",
        "stage_d", "records_durable", "fastpath_pilot_enabled", "fastpath_pilot_workspaces",
        "fastpath_pilot_regions", "gateway_authorize_max_in_flight_per_key", "monitor_lookup_hash",
        "async_settle_shadow_policy", "async_settle_shadow_workspaces",
        "configured_regions", "primary_region", "multi_region_enabled", "internal_models",
    }
    # The union of the resolver's set and the break-glass override, deduplicated.
    assert len(decoded["accepted_image_digests"]) == 3
    # Every set non-empty and the booleans not all one value, so the Go reader
    # is exercised on each shape.
    for name in ("fastpath_pilot_workspaces", "async_settle_shadow_workspaces", "configured_regions",
                 "internal_models"):
        assert decoded[name], name
    assert decoded["stage_d"]["pilot_workspaces"] and decoded["monitor_lookup_hash"]
    flags = [decoded[n] for n in ("records_durable", "fastpath_pilot_enabled", "multi_region_enabled")]
    flags += [decoded["stage_d"][n] for n in ("eligibility_enabled", "heartbeat_enabled",
                                              "require_heartbeat_declaration")]
    assert True in flags and False in flags
    # Synthetic workspace ids only.
    for name in ("fastpath_pilot_workspaces", "async_settle_shadow_workspaces"):
        assert all(ws.startswith("ws-synthetic-") for ws in decoded[name])
    assert all(ws.startswith("ws-synthetic-") for ws in decoded["stage_d"]["pilot_workspaces"])


def test_the_golden_manifest_verifies_and_binds_the_body() -> None:
    body, manifest = _golden_body(), _golden_manifest()
    key = TrustedKey(manifest["kid"], manifest["purpose"], manifest["public_key_b64url"],
                     manifest["iss"], manifest["aud"])
    # Shaped exactly like the routing snapshot's golden manifest.
    routing = json.loads(ROUTING_VECTORS.OUTPUT_MANIFEST.read_text())
    assert set(manifest) == set(routing) == {
        "aud", "generator", "iss", "jws", "kid", "public_key_b64url", "purpose", "test_key"}
    assert manifest["test_key"] is True
    assert (manifest["kid"], manifest["purpose"]) == (ROUTING_VECTORS.TEST_KEY.kid, PURPOSE)
    header = json.loads(base64.urlsafe_b64decode(manifest["jws"].split(".")[0] + "=="))
    assert header == {"alg": "EdDSA", "kid": manifest["kid"], "typ": "tr-edge-policy-v1"}
    claims = verify_edge_policy_manifest(manifest["jws"], [key], body, now=T0,
                                         expected_plane=GOLDEN_PLANE, expected_aud=key.aud,
                                         allow_test_key=True)
    assert claims.sha256 == hashlib.sha256(body).hexdigest() and claims.size == len(body)
    assert claims.model_dump(mode="json") == edge_policy_manifest_claims(body, iss=key.iss, aud=key.aud)


def test_verification_refuses_tampering_staleness_and_the_wrong_key() -> None:
    body, token = _golden_body(), _golden_manifest()["jws"]
    # Positive control: the untouched pair verifies across its window.
    for instant in (T0, T0 + LIFETIME_SECONDS - 1):
        _golden_verify(token, body, now=instant)
    with pytest.raises(ValueError, match="freshness"):
        _golden_verify(token, body, now=T0 + LIFETIME_SECONDS)
    with pytest.raises(ValueError, match="freshness"):
        _golden_verify(token, body, now=T0 - 1)
    altered = body.replace(b'"gateway_authorize_max_in_flight_per_key":4',
                           b'"gateway_authorize_max_in_flight_per_key":5')
    assert altered != body
    with pytest.raises(ValueError, match="manifest binding"):
        _golden_verify(token, altered, now=T0)
    with pytest.raises(ValueError, match="not canonical"):
        _golden_verify(token, body + b"\n", now=T0)
    with pytest.raises(ValueError, match="manifest plane"):
        _golden_verify(token, body, expected_plane=Plane(cloud="gcp", region="europe-west4"))
    with pytest.raises(ValueError, match="manifest audience"):
        _golden_verify(token, body, expected_aud="tr-other")
    other = Ed25519PrivateKey.from_private_bytes(bytes(32)).public_key().public_bytes_raw()
    key = VECTORS.TEST_KEY
    impostor = TrustedKey(key.kid, key.purpose, b64encode(other), key.iss, key.aud)
    with pytest.raises(JWSError, match="signature"):
        verify_edge_policy_manifest(token, [impostor], body, now=T0, expected_plane=GOLDEN_PLANE,
                                    expected_aud=key.aud, allow_test_key=True)
    wrong_purpose = TrustedKey(key.kid, "async-settle-ticket", key.public_key_b64url, key.iss, key.aud)
    with pytest.raises(JWSError, match="purpose"):
        verify_edge_policy_manifest(token, [wrong_purpose], body, now=T0, expected_plane=GOLDEN_PLANE,
                                    expected_aud=key.aud, allow_test_key=True)


@pytest.mark.parametrize(("name", "edit"), [
    ("a float", lambda d: {**d, "gateway_authorize_max_in_flight_per_key": 4.0}),
    ("a string integer", lambda d: {**d, "issued_at": str(d["issued_at"])}),
    ("an integer boolean", lambda d: {**d, "records_durable": 1}),
    ("a null where none is allowed", lambda d: {**d, "primary_region": None}),
    ("an empty monitor hash", lambda d: {**d, "monitor_lookup_hash": ""}),
    ("an unknown field", lambda d: {**d, "extra": 1}),
    ("a missing field", lambda d: {k: v for k, v in d.items() if k != "multi_region_enabled"}),
    ("another version", lambda d: {**d, "v": 2}),
    ("another kind", lambda d: {**d, "kind": "tr-routing-snapshot"}),
    ("an unsorted set", lambda d: {**d, "accepted_image_digests": d["accepted_image_digests"][::-1]}),
    ("a duplicate set member", lambda d: {**d, "fastpath_pilot_workspaces": ["ws-a", "ws-a"]}),
    ("a malformed digest", lambda d: {**d, "accepted_image_digests": ["sha256:abc"]}),
    ("a primary outside the regions", lambda d: {**d, "primary_region": "us-west1"}),
    ("a window past the lifetime", lambda d: {**d, "not_after": d["issued_at"] + 61}),
    ("a short window", lambda d: {**d, "not_after": d["issued_at"] + 59}),
    ("a refresh off by one", lambda d: {**d, "refresh_at": d["issued_at"] + 16}),
    ("a zero in-flight limit", lambda d: {**d, "gateway_authorize_max_in_flight_per_key": 0}),
    ("unsorted internal models", lambda d: {**d, "internal_models": d["internal_models"][::-1]}),
    ("no internal models field", lambda d: {k: v for k, v in d.items() if k != "internal_models"}),
])
def test_the_body_parser_is_strict(name: str, edit: Any) -> None:
    decoded = json.loads(_golden_body())
    # Positive control: the untouched body re-encoded the same way parses.
    parse_edge_policy(json.dumps(decoded, sort_keys=True, separators=(",", ":")))
    with pytest.raises((ValueError, ValidationError)):
        parse_edge_policy(json.dumps(edit(decoded), sort_keys=True, separators=(",", ":")))


def test_the_body_parser_refuses_duplicate_keys() -> None:
    body = _golden_body()
    duplicated = body[:-1] + b',"v":1}'
    with pytest.raises((ValueError, ValidationError)):
        parse_edge_policy(duplicated)


def test_the_window_is_code_not_configuration() -> None:
    assert (REFRESH_SECONDS, LIFETIME_SECONDS) == (15, 60)
    assert publisher_module.RESPONSE_KIND == "tr-edge-policy-publication"


def test_a_null_monitor_hash_parses() -> None:
    decoded = {**json.loads(_golden_body()), "monitor_lookup_hash": None}
    assert parse_edge_policy(json.dumps(decoded, sort_keys=True, separators=(",", ":"))) \
        .monitor_lookup_hash is None
