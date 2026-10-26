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
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace
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

    def refresh_now(self) -> bool:
        return self.refresh()

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
    # The resolver reads the watermark as a floor before it fetches: none yet.
    no_watermark = SimpleNamespace(highest_sequence=lambda plane: None)
    failing = StageDPolicyResolver(settings, no_watermark, verifier=object(),  # type: ignore[arg-type]
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

    # A poll that waited on the worker takes HANG seconds and sees the policy
    # the worker then compiles; one that did not takes milliseconds, a few
    # seconds on a loaded runner. The bound sits between.
    HANG = 60.0
    entered, release = threading.Event(), threading.Event()
    resolver = Resolver(frozenset({DIGEST_A}))
    resolver.on_refresh = lambda: (entered.set(), release.wait(HANG))
    settings = _settings(key)
    clock = Clock(T0)
    publisher = _publisher(settings, clock, resolver, background=True)
    client = TestClient(_app(settings, publisher))
    try:
        # No copy yet: 503 compiling at once, while the worker hangs.
        started = time.monotonic()
        response = _get(client)
        assert time.monotonic() - started < HANG / 2
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
        assert answers == [503] * 16 and max(durations) < HANG / 2
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
            assert time.monotonic() - started < HANG / 2
        assert entered.wait(30)
        assert resolver.refreshes == 2 and publisher.attempts == 2
    finally:
        release.set()
    assert publisher.idle(30)
    # Positive control: the background signing landed and is now served.
    later = publisher.current()
    assert later is not first and later.issued_at == first.refresh_at


def test_a_hung_worker_is_abandoned_after_its_deadline(key, caplog) -> None:
    import threading

    entered, release = threading.Event(), threading.Event()
    resolver = Resolver(frozenset({DIGEST_A}))
    hang = {"on": True}

    def maybe_hang() -> None:
        entered.set()
        if hang["on"]:
            release.wait(60)

    resolver.on_refresh = maybe_hang
    settings = _settings(key)
    ticks = Clock(1_000)
    publisher = build_publisher(settings, clock=Clock(T0), resolver=lambda: resolver,
                                typed_store=lambda: None, monotonic=ticks)
    caplog.set_level(logging.WARNING)
    try:
        with pytest.raises(PolicyUnavailable) as busy:
            publisher.current()
        assert busy.value.status == "compiling" and entered.wait(30)
        # Within the deadline: still in flight, no second worker.
        ticks.now += int(publisher_module.WORKER_DEADLINE_SECONDS)
        with pytest.raises(PolicyUnavailable):
            publisher.current()
        assert (publisher.attempts, publisher.abandoned) == (1, 0)
        # Past it: the hung worker is abandoned and a fresh one starts.
        hang["on"] = False
        ticks.now += 1
        try:
            publisher.current()
        except PolicyUnavailable as exc:
            assert exc.status == "compiling"
        assert (publisher.attempts, publisher.abandoned) == (2, 1)
        deadline = int(publisher_module.WORKER_DEADLINE_SECONDS)
        assert f"edge policy signing worker abandoned after {deadline} s" in [
            r.getMessage() for r in caplog.records]
        assert publisher.idle(30)
        fresh = publisher.current()
        assert publisher.signatures == 1
    finally:
        release.set()
    # The abandoned worker finishes late: its outcome is discarded.
    deadline = time.monotonic() + 30
    while publisher.signatures < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert publisher.signatures == 2
    time.sleep(0.05)
    assert publisher.current() is fresh


def test_resolver_refreshes_are_serialized_with_kicks_worker(key) -> None:
    """refresh_now() waits for an in-flight kick refresh: two refreshes never
    overlap, so an older verified policy cannot land after a newer one."""
    import threading

    from trusted_router.stage_d_policy import StageDPolicyResolver

    settings = _settings(key)
    resolver = StageDPolicyResolver(settings, object(), verifier=object(),  # type: ignore[arg-type]
                                    release_urls=["https://release.invalid/trust"])
    gate, entered = threading.Event(), threading.Semaphore(0)
    state = {"active": 0, "peak": 0, "calls": 0}
    lock = threading.Lock()

    def slow_refresh() -> bool:
        with lock:
            state["active"] += 1
            state["calls"] += 1
            state["peak"] = max(state["peak"], state["active"])
        entered.release()
        gate.wait(30)
        with lock:
            state["active"] -= 1
        return True

    resolver.refresh = slow_refresh  # type: ignore[method-assign]
    resolver.kick()
    assert entered.acquire(timeout=30)
    publisher_refresh = threading.Thread(target=resolver.refresh_now)
    publisher_refresh.start()
    # It waits on the kick worker's refresh rather than overlapping it.
    assert not entered.acquire(timeout=0.3)
    assert state["calls"] == 1
    gate.set()
    publisher_refresh.join(30)
    assert state["calls"] == 2 and state["peak"] == 1
    # Positive control: the unserialized refresh() would overlap.
    gate.clear()
    state.update(active=0, peak=0, calls=0)
    overlapping = [threading.Thread(target=resolver.refresh) for _ in range(2)]
    for thread in overlapping:
        thread.start()
    assert entered.acquire(timeout=30) and entered.acquire(timeout=30)
    gate.set()
    for thread in overlapping:
        thread.join(30)
    assert state["peak"] == 2


STAGE_D_FIXTURE = ROOT / "tests" / "fixtures" / "stage_d" / "stage-d-accepted.json"


class Ticks:
    """A monotonic clock the fake release host advances, shared by the
    resolver's fetches and the publisher's worker deadline."""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class SlowReleaseHost:
    """A release host behind StageDPolicyResolver's ``client_factory`` that
    takes ``op_seconds`` of ``ticks`` to answer each request. ``unavailable``
    hosts answer a bundle 503. ``hold`` maps a request's index to an event
    it waits on before answering, and ``arrived`` to one it sets on arrival.
    """

    def __init__(self, ticks: Ticks, documents: list[bytes]) -> None:
        import threading

        self.ticks, self.documents = ticks, documents
        self.op_seconds = 1.0
        self.unavailable: set[str] = set()
        self.requests = 0
        self.hold: dict[int, Any] = {}
        self.arrived: dict[int, Any] = {}
        self._lock = threading.Lock()

    def __call__(self, **kwargs: Any) -> Any:
        return _HostClient(self)


class _HostClient:
    def __init__(self, host: SlowReleaseHost) -> None:
        self.host = host

    def __enter__(self) -> _HostClient:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def stream(self, method: str, url: Any, headers: Any = None) -> Any:
        import contextlib

        import httpx

        host = self.host
        with host._lock:
            index = host.requests
            host.requests += 1
        if index in host.arrived:
            host.arrived[index].set()
        if index in host.hold:
            assert host.hold[index].wait(30)
        host.ticks.now += host.op_seconds
        url = httpx.URL(str(url))
        bundle = url.path.endswith(".bundle")
        status = 503 if bundle and url.host in host.unavailable else 200
        body = b"bundle" if bundle else host.documents[0]

        def chunks() -> Any:
            yield body

        def raise_for_status() -> None:
            if status != 200:
                request = httpx.Request(method, url)
                raise httpx.HTTPStatusError(
                    "unavailable", request=request,
                    response=httpx.Response(status, request=request))

        response = SimpleNamespace(headers={}, iter_bytes=chunks,
                                   raise_for_status=raise_for_status)
        return contextlib.nullcontext(response)


class AcceptAll:
    def verify(self, document: bytes, bundle: bytes, **kwargs: Any) -> None:
        return None

    def advance(self, **kwargs: Any) -> bool:
        return True

    def highest_sequence(self, **kwargs: Any) -> int | None:
        return None


def _stage_d_document(sequence: int) -> bytes:
    value = json.loads(STAGE_D_FIXTURE.read_bytes())
    value["sequence"] = sequence
    return json.dumps(value).encode()


def _real_resolver(settings: Settings, host: Any, ticks: Ticks,
                   release_urls: list[str] | None = None) -> Any:
    from datetime import UTC, datetime

    from trusted_router.stage_d_policy import StageDPolicyResolver

    return StageDPolicyResolver(
        settings, AcceptAll(), verifier=AcceptAll(), release_urls=release_urls,
        monotonic=ticks, wall_clock=lambda: datetime(2026, 9, 3, 22, tzinfo=UTC),
        client_factory=host,
    )


def _within(seconds: float, target: Any) -> Any:
    """Runs target in a thread and returns or raises its outcome, failing
    when it is still running after ``seconds``."""
    import threading

    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            outcome["value"] = target()
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            outcome["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(seconds)
    assert not thread.is_alive(), "still running"
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


def test_a_slow_release_host_within_its_timeouts_does_not_trip_the_signing_deadline(key) -> None:
    """Two release URLs, every fetch 4.9 s, the primary's bundle 503: each
    refresh takes 19.6 s, and the publisher's queues behind kick's. The
    worker runs 34 s, past the old fixed 30 s deadline, and is not
    abandoned: the deadline covers two refreshes' bound."""
    import threading

    ticks = Ticks()
    host = SlowReleaseHost(ticks, [_stage_d_document(1200)])
    host.op_seconds = 4.9
    host.unavailable = {"primary.invalid"}
    settings = _settings(
        key, trust_gcp_release_url="https://primary.invalid/trust/gcp-release.json",
        trust_gcp_release_fallback_urls="https://fallback.invalid/trust/gcp-release.json")
    resolver = _real_resolver(settings, host, ticks)
    publisher = build_publisher(settings, clock=Clock(T0), resolver=lambda: resolver,
                                typed_store=lambda: None, monotonic=ticks)
    # Requests 0-3 are kick's refresh, 4-7 the publisher's.
    kick_in, kick_go = threading.Event(), threading.Event()
    publisher_last, publisher_go = threading.Event(), threading.Event()
    host.hold, host.arrived = {0: kick_go, 7: publisher_go}, {0: kick_in, 7: publisher_last}
    try:
        resolver.kick()
        # Kick's refresh holds _refresh_lock before the publisher's worker starts.
        assert kick_in.wait(30)
        with pytest.raises(PolicyUnavailable) as busy:
            publisher.current()
        assert busy.value.status == "compiling"
        kick_go.set()
        assert publisher_last.wait(30)
        assert ticks.now - 1_000.0 > 30
        with pytest.raises(PolicyUnavailable) as still:
            publisher.current()
        assert still.value.status == "compiling"
        assert (publisher.attempts, publisher.abandoned) == (1, 0)
    finally:
        kick_go.set()
        publisher_go.set()
    assert publisher.idle(30)
    served = publisher.current()
    assert json.loads(served.body)["accepted_image_digests"] == sorted(
        resolver.accepted_image_digests())
    assert (publisher.attempts, publisher.abandoned, host.requests) == (1, 0, 8)


def test_a_release_host_that_never_finishes_keeps_the_last_verified_set_and_signs_nothing(
    key, monkeypatch,
) -> None:
    """Through the real httpx client: a header drip fails the publisher's
    refresh within the resolver's timeout. The cached copy is served, then
    503 digest-policy-unavailable; nothing unverified or empty is signed."""
    from tests.fakes.release_host import ReleaseHost, plain_http_client_factory
    from trusted_router import stage_d_policy

    monkeypatch.setattr(stage_d_policy, "_FETCH_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(stage_d_policy, "REFRESH_TIMEOUT_SECONDS", 1.5)
    host = ReleaseHost(_stage_d_document(1200))
    settings = _settings(key)
    resolver = _real_resolver(settings, plain_http_client_factory, Ticks(),
                              [host.release_url])
    clock = Clock(T0)
    publisher = build_publisher(settings, clock=clock, resolver=lambda: resolver,
                                typed_store=lambda: None, spawn=_inline)
    try:
        first = publisher.current()
        digests = json.loads(first.body)["accepted_image_digests"]
        assert digests == sorted(resolver.accepted_image_digests()) and digests
        host.mode = "header-drip"
        host.document = _stage_d_document(1201)
        clock.now = first.refresh_at
        assert _within(5, publisher.current) is first
        assert publisher.signatures == 1
        assert sorted(resolver.accepted_image_digests()) == digests
        clock.now = first.not_after
        with pytest.raises(PolicyUnavailable) as refused:
            _within(5, publisher.current)
        assert refused.value.status == "digest-policy-unavailable"
        assert publisher.signatures == 1
        assert sorted(resolver.accepted_image_digests()) == digests
    finally:
        host.end_drips()
        network = getattr(resolver._network, "thread", resolver._network)
        if network is not None:
            network.join(10)
            assert not network.is_alive()
        host.close()

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
    assert (TYP, EDGE_POLICY_TYP) == ("tr-routing-snapshot-v2", "tr-edge-policy-v1")


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
