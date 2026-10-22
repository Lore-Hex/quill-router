"""Publishing the routing snapshot, dark (docs/design/routing-snapshot-v1.md, "Publishing").

The publisher compiles the golden fixture catalog here (its clocks follow the
fake clock), except the budget test, which compiles the live catalog.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import inspect
import json
import logging
import resource
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from trusted_router import catalog_registry
from trusted_router.config import Settings
from trusted_router.detached_jws import TrustedKey, b64encode
from trusted_router.main import create_app
from trusted_router.routes.internal import fastpath_snapshot
from trusted_router.routes.internal.fastpath_snapshot import PATH
from trusted_router.routing_snapshot import (
    EPOCH_SECONDS,
    CatalogInputs,
    SnapshotRefused,
    publication_window,
)
from trusted_router.routing_snapshot_signing import PURPOSE, verify_manifest
from trusted_router.services import routing_snapshot_publisher as publisher_module
from trusted_router.services.routing_snapshot_publisher import (
    AUDIENCE,
    ISSUER,
    AsOfSource,
    RoutingSnapshotPublisher,
    SnapshotUnavailable,
    build_publisher,
    load_signer,
    plane_of,
)

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "internal-token-for-the-routing-snapshot-tests"  # noqa: S105 - test value
KID = "tr-routing-snapshot-test-run"
DAY = int(datetime(2026, 10, 19, tzinfo=UTC).timestamp())
HOUR = EPOCH_SECONDS


def _generator() -> ModuleType:
    path = ROOT / "scripts" / "fastpath" / "routing_snapshot_vectors.py"
    spec = importlib.util.spec_from_file_location("routing_snapshot_vectors", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VECTORS = _generator()
# The fixture's inkling route has a catalog deadline at 05:30 UTC.
DEADLINE = int(datetime(2026, 10, 19, 5, 30, tzinfo=UTC).timestamp())


class Clock:
    def __init__(self, now: int) -> None:
        self.now = now

    def __call__(self) -> float:
        return float(self.now)


def _fixture_inputs(clock: Clock) -> Callable[[], CatalogInputs]:
    """The golden catalog, imported at 00:00, its expansions computed at the clock's now."""
    def inputs() -> CatalogInputs:
        return replace(VECTORS.fixture_inputs(), expansions_clock=(clock.now, clock.now))
    return inputs


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
        "routing_snapshot_publish_enabled": True,
        "routing_snapshot_private_key_file": str(path),
        "routing_snapshot_kid": KID,
        "routing_snapshot_public_key": public,
        "source_revision": "a" * 40,
        **overrides,
    }
    return Settings(**values)


def _app(settings: Settings, clock: Clock | None = None,
         inputs: Callable[[], CatalogInputs] | None = None) -> FastAPI:
    app = create_app(settings, init_observability=False)
    if clock is not None:
        app.state.routing_snapshot_publisher = build_publisher(
            settings, clock=clock, inputs=inputs or _fixture_inputs(clock),
        )
    return app


def _get(client: TestClient, **headers: str) -> Any:
    return client.get(PATH, headers={"Authorization": f"Bearer {TOKEN}", **headers})


def _trusted(public: str) -> TrustedKey:
    return TrustedKey(KID, PURPOSE, public, ISSUER, AUDIENCE)


def _verify(response: Any, settings: Settings, public: str, now: int) -> tuple[bytes, Any]:
    payload = response.json()
    assert set(payload) == {"v", "kind", "manifest", "body", "refresh_at"}
    body = base64.b64decode(payload["body"], validate=True)
    claims = verify_manifest(
        payload["manifest"], [_trusted(public)], body, now=now,
        expected_plane=plane_of(settings), expected_aud=AUDIENCE,
    )
    return body, claims


# --- off by default ------------------------------------------------------------------------


def test_publishing_is_off_by_default_and_the_route_is_absent() -> None:
    settings = Settings(environment="test", sentry_dsn=None, internal_gateway_token=TOKEN)
    assert settings.routing_snapshot_publish_enabled is False
    app = create_app(settings, init_observability=False)
    assert not hasattr(app.state, "routing_snapshot_publisher")
    client = TestClient(app)
    assert _get(client).status_code == 404
    assert client.get("/v1" + PATH, headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 404


def test_the_route_lives_only_on_the_internal_surfaces(key) -> None:
    for surface, present in (("combined", True), ("internal", True), ("control", False),
                             ("public", False), ("observer", False)):
        settings = _settings(key, service_surface=surface)
        if surface not in {"combined", "internal"}:
            # Credentials owned by other surfaces fail closed at construction.
            settings = settings.model_copy(update={"internal_gateway_token": None})
        app = _app(settings, Clock(DAY + HOUR))
        assert (_get(TestClient(app)).status_code == 200) is present, surface


# --- authentication ------------------------------------------------------------------------


def test_the_route_refuses_without_the_internal_token(key) -> None:
    clock = Clock(DAY + HOUR + 300)
    client = TestClient(_app(_settings(key), clock))
    assert client.get(PATH).status_code == 401
    assert client.get(PATH, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get(PATH, headers={"X-TrustedRouter-Internal-Token": "wrong"}).status_code == 401
    assert client.get(PATH, headers={"X-TrustedRouter-Internal-Token": TOKEN}).status_code == 200
    ok = _get(client)
    assert ok.status_code == 200
    # Authentication comes before every other answer: a matching ETag, ...
    etag = {"If-None-Match": ok.headers["etag"]}
    assert _get(client, **etag).status_code == 304
    assert client.get(PATH, headers=etag).status_code == 401
    assert client.get(PATH, headers={**etag, "Authorization": "Bearer wrong"}).status_code == 401
    # ... a refused window, ...
    refusing = TestClient(_app(_settings(key), Clock(DEADLINE + 600),
                               inputs=_fixture_inputs(Clock(DAY))))
    assert _get(refusing).status_code == 503
    assert refusing.get(PATH).status_code == 401
    # ... and no usable key.
    other = b64encode(Ed25519PrivateKey.generate().public_key().public_bytes_raw())
    unsigned = TestClient(create_app(_settings(key, routing_snapshot_public_key=other),
                                     init_observability=False))
    assert _get(unsigned).status_code == 503
    assert unsigned.get(PATH).status_code == 401


def test_a_deployed_plane_without_a_token_refuses() -> None:
    from fastapi import HTTPException
    from starlette.requests import Request

    from trusted_router.routes.internal._shared import require_internal_gateway

    scope = {"type": "http", "method": "GET", "path": PATH, "headers": [], "query_string": b""}
    settings = Settings.model_construct(environment="production", internal_gateway_token=None,
                                        service_surface="internal")
    with pytest.raises(HTTPException) as refused:
        require_internal_gateway(Request(scope), settings)
    assert refused.value.status_code == 403


# --- what it serves --------------------------------------------------------------------------


def test_the_response_verifies_for_the_planes_edge(key) -> None:
    settings = _settings(key)
    now = DAY + HOUR + 300
    client = TestClient(_app(settings, Clock(now)))
    response = _get(client)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body, claims = _verify(response, settings, key[1], now)
    assert claims.compiled_at == DAY + HOUR and claims.plane == plane_of(settings)
    assert claims.source_revision == "a" * 40 and claims.iss == ISSUER
    assert response.json()["refresh_at"] == DAY + 2 * HOUR
    assert response.headers["etag"] == f'"{hashlib.sha256(body).hexdigest()}"'
    # A poll naming the body it holds gets a 304 and no body.
    unchanged = _get(client, **{"If-None-Match": response.headers["etag"]})
    assert unchanged.status_code == 304 and unchanged.content == b""
    # Another plane's or another consumer's verifier refuses it.
    with pytest.raises(ValueError, match="plane"):
        verify_manifest(response.json()["manifest"], [_trusted(key[1])], body, now=now,
                        expected_plane=plane_of(settings).model_copy(update={"region": "europe-west4"}),
                        expected_aud=AUDIENCE)


def test_two_app_instances_in_one_hour_serve_identical_bytes(key) -> None:
    settings = _settings(key)
    early = TestClient(_app(settings, Clock(DAY + HOUR + 60)))
    late = TestClient(_app(settings, Clock(DAY + 2 * HOUR - 1)))
    first, second = _get(early), _get(late)
    assert first.status_code == second.status_code == 200
    assert first.content == second.content
    next_hour = TestClient(_app(settings, Clock(DAY + 2 * HOUR)))
    third = _get(next_hour)
    assert third.json()["manifest"] != first.json()["manifest"]
    assert _verify(third, settings, key[1], DAY + 2 * HOUR)[1].compiled_at == DAY + 2 * HOUR


def test_the_cache_refreshes_at_the_hour(key) -> None:
    clock = Clock(DAY + HOUR + 60)
    publisher = build_publisher(_settings(key), clock=clock, inputs=_fixture_inputs(clock))
    first = publisher.current()
    assert (first.compiled_at, first.refresh_at, publisher.compiles) == (DAY + HOUR, DAY + 2 * HOUR, 1)
    clock.now = DAY + 2 * HOUR - 1
    assert publisher.current() is first and publisher.compiles == 1
    clock.now = DAY + 2 * HOUR
    second = publisher.current()
    assert second.compiled_at == DAY + 2 * HOUR and publisher.compiles == 2


def test_the_cache_refreshes_at_not_after_and_cold_instances_agree(key) -> None:
    settings = _settings(key)
    clock = Clock(DAY + 5 * HOUR + 600)
    warm = build_publisher(settings, clock=clock, inputs=_fixture_inputs(clock))
    before = warm.current()
    # The 05:30 catalog deadline ends the window before the hour does.
    assert (before.compiled_at, before.not_after, before.refresh_at) == (
        DAY + 5 * HOUR, DEADLINE, DEADLINE,
    )
    clock.now = DEADLINE - 1
    assert warm.current() is before
    clock.now = DEADLINE
    after = warm.current()
    assert after.compiled_at == DEADLINE and after.refresh_at == DAY + 6 * HOUR
    assert warm.compiles == 2
    # An instance that starts later in the hour derives the same window start.
    cold_clock = Clock(DEADLINE + 900)
    cold = build_publisher(settings, clock=cold_clock, inputs=_fixture_inputs(cold_clock))
    # The expansions clock differs, but it is not in the body.
    assert cold.current().response == after.response
    assert publication_window(DEADLINE + 900, _fixture_inputs(cold_clock)()) == (
        DEADLINE, DAY + 6 * HOUR,
    )


def test_a_refused_window_answers_503_and_is_not_compiled_again(key) -> None:
    settings = _settings(key)
    clock = Clock(DEADLINE + 600)
    # Expansions computed before the 05:30 deadline cannot be frozen after it.
    stale = Clock(DAY)
    client_app = _app(settings, clock, inputs=_fixture_inputs(stale))
    publisher: RoutingSnapshotPublisher = client_app.state.routing_snapshot_publisher
    client = TestClient(client_app)
    for _ in range(2):
        refused = _get(client)
        assert refused.status_code == 503
        assert refused.json() == {"status": "snapshot-refused", "retry_at": DAY + 6 * HOUR}
        assert refused.headers["retry-after"] == str(DAY + 6 * HOUR - clock.now)
    assert publisher.compiles == 1
    # The next window is tried again.
    clock.now = DAY + 6 * HOUR
    assert _get(client).status_code == 503 and publisher.compiles == 2
    stale.now = clock.now
    assert _get(client).status_code == 503 and publisher.compiles == 2
    clock.now = DAY + 7 * HOUR
    assert _get(client).status_code == 200 and publisher.compiles == 3


def test_an_instance_imported_before_a_retirement_refuses_the_windows_after_it(key) -> None:
    # The fixture's retirement: DeepInfra's GPT-OSS-120B Turbo, 2026-10-20 00:00.
    retirement = DAY + 24 * HOUR
    settings = _settings(key)
    clock = Clock(retirement + 1_800)

    def imported_at(resolved: int) -> Callable[[], CatalogInputs]:
        def inputs() -> CatalogInputs:
            return replace(_fixture_inputs(clock)(), catalog_resolved_at=resolved)
        return inputs

    warm = build_publisher(settings, clock=clock, inputs=imported_at(retirement - 1_800))
    cold = build_publisher(settings, clock=clock, inputs=imported_at(retirement + 1_200))
    colder = build_publisher(settings, clock=clock, inputs=imported_at(retirement + 1_500))
    with pytest.raises(SnapshotUnavailable) as refused:
        warm.current()
    assert refused.value.status == "snapshot-refused"
    # Positive control: instances imported after it publish, the same bytes.
    assert cold.current().response == colder.current().response


def test_a_refusal_while_loading_the_inputs_is_held_for_the_hour(key) -> None:
    # CatalogInputs.live itself refuses some catalogs (an unknown variant
    # suffix or override, a sub-second instant): a 503, never a 500, and no
    # second multi-second load until the next hour.
    clock = Clock(DAY + HOUR + 60)
    loads: list[int] = []
    good = _fixture_inputs(clock)

    def refusing() -> CatalogInputs:
        loads.append(clock.now)
        if clock.now < DAY + 2 * HOUR:
            raise SnapshotRefused("requested model override is unknown")
        return good()

    app = _app(_settings(key), clock, inputs=refusing)
    client = TestClient(app, raise_server_exceptions=True)
    for _ in range(2):
        refused = _get(client)
        assert refused.status_code == 503
        assert refused.json() == {"status": "snapshot-refused", "retry_at": DAY + 2 * HOUR}
    assert loads == [DAY + HOUR + 60]
    clock.now = DAY + 2 * HOUR - 1
    assert _get(client).status_code == 503 and len(loads) == 1
    # Positive control: the next hour loads again, and publishes.
    clock.now = DAY + 2 * HOUR
    assert _get(client).status_code == 200 and len(loads) == 2


def test_a_transient_failure_is_retried_after_a_short_backoff(key) -> None:
    clock = Clock(DAY + HOUR + 60)
    calls: list[int] = []
    good = _fixture_inputs(clock)

    def flaky() -> CatalogInputs:
        calls.append(clock.now)
        if len(calls) == 1:
            raise OSError("evidence file briefly unreadable")
        return good()

    publisher = build_publisher(_settings(key), clock=clock, inputs=flaky)
    with pytest.raises(SnapshotUnavailable) as failed:
        publisher.current()
    assert failed.value.status == "snapshot-error"
    assert failed.value.retry_after == publisher_module.ERROR_BACKOFF_SECONDS
    # Within the backoff, no attempt; not a refusal held for the window.
    clock.now += publisher_module.ERROR_BACKOFF_SECONDS - 1
    with pytest.raises(SnapshotUnavailable):
        publisher.current()
    assert len(calls) == 1
    clock.now += 1
    assert publisher.current().compiled_at == DAY + HOUR and len(calls) == 2


def test_a_clock_before_compiled_at_is_not_served_the_cached_window(key) -> None:
    clock = Clock(DAY + HOUR + 60)
    publisher = build_publisher(_settings(key), clock=clock, inputs=_fixture_inputs(clock))
    later = publisher.current()
    clock.now = DAY + HOUR - 1
    earlier = publisher.current()
    assert earlier is not later and earlier.compiled_at == DAY and publisher.compiles == 2


def test_a_poll_during_a_compile_answers_compiling_at_once(key) -> None:
    import threading

    clock = Clock(DAY + HOUR + 60)
    entered, release = threading.Event(), threading.Event()
    good = _fixture_inputs(clock)

    def slow() -> CatalogInputs:
        entered.set()
        assert release.wait(30)
        return good()

    settings = _settings(key)
    publisher = build_publisher(settings, clock=clock, inputs=slow)
    results: list[Any] = []
    first = threading.Thread(target=lambda: results.append(publisher.current()))
    first.start()
    assert entered.wait(30)
    # A second poll does not wait on the lock: 503 compiling, come back soon.
    started = time.monotonic()
    with pytest.raises(SnapshotUnavailable) as busy:
        publisher.current()
    assert time.monotonic() - started < 1.0
    assert busy.value.status == "compiling"
    assert busy.value.retry_after == publisher_module.COMPILING_RETRY_SECONDS
    app = _app(settings)
    app.state.routing_snapshot_publisher = publisher
    response = _get(TestClient(app))
    assert response.status_code == 503 and response.json()["status"] == "compiling"
    assert response.headers["retry-after"] == str(publisher_module.COMPILING_RETRY_SECONDS)
    release.set()
    first.join(30)
    # Positive control: the compile finishes, and later polls get its bytes.
    assert results and publisher.current() is results[0] and publisher.compiles == 1


def test_repeated_failures_back_off_exponentially_up_to_the_window_end(key) -> None:
    clock = Clock(DAY + HOUR + 60)

    def broken() -> CatalogInputs:
        raise RuntimeError("deterministic")

    publisher = build_publisher(_settings(key), clock=clock, inputs=broken)
    window_end = DAY + 2 * HOUR
    backoffs: list[int] = []
    while clock.now < window_end:
        with pytest.raises(SnapshotUnavailable) as failed:
            publisher.current()
        assert failed.value.status == "snapshot-error"
        backoffs.append(failed.value.retry_at - clock.now)
        # Within the backoff, no attempt.
        clock.now = failed.value.retry_at - 1
        with pytest.raises(SnapshotUnavailable):
            publisher.current()
        clock.now = failed.value.retry_at
    assert backoffs[:5] == [5, 10, 20, 40, 80]
    assert clock.now == window_end and len(backoffs) == 10
    # A new window starts the backoff again.
    with pytest.raises(SnapshotUnavailable) as again:
        publisher.current()
    assert again.value.retry_at - clock.now == 5


# --- the worker's answer, as the serving process checks it ---------------------------------


class _Worker:
    """A stub of the worker process: records the command, returns a canned answer."""

    def __init__(self, stdout: bytes, returncode: int = 0) -> None:
        self.stdout, self.returncode, self.calls = stdout, returncode, []

    def __call__(self, argv: list[str], **kwargs: Any) -> Any:
        import subprocess

        self.calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, b"")


def _signed_answer(settings: Settings, at: int, *, revision: str | None = None,
                   signer: Any = None) -> tuple[bytes, str]:
    from trusted_router.routing_snapshot import canonical_snapshot_bytes, compile_routing_snapshot

    snapshot = compile_routing_snapshot(
        at=at, horizon=6 * HOUR, settings=VECTORS.fixture_settings(),
        source_revision=revision or settings.source_revision, plane=plane_of(settings),
        inputs=replace(VECTORS.fixture_inputs(), expansions_clock=(at, at)),
    )
    body = canonical_snapshot_bytes(snapshot)
    return body, (signer or load_signer(settings)).sign(body)


def _answer(body: bytes, manifest: str) -> bytes:
    return json.dumps({"body": base64.b64encode(body).decode("ascii"), "manifest": manifest}).encode()


def _as_of_publisher(settings: Settings, clock: Clock, worker: _Worker) -> RoutingSnapshotPublisher:
    from trusted_router.routing_snapshot import snapshot_settings_from
    from trusted_router.services.routing_snapshot_publisher import SnapshotIdentity

    identity = SnapshotIdentity(snapshot_settings=snapshot_settings_from(settings),
                                plane=plane_of(settings), source_revision=settings.source_revision)
    source = AsOfSource(identity, auto_model_order="", key_file=settings.routing_snapshot_private_key_file,
                        runner=worker)
    return RoutingSnapshotPublisher(signer=load_signer(settings), source=source, clock=clock)


def test_the_worker_answer_is_checked_before_it_is_served(key, caplog: pytest.LogCaptureFixture) -> None:
    from trusted_router.routing_snapshot import publication_window_as_of

    settings = _settings(key)
    now = DAY + 60
    start = publication_window_as_of(now)[0]
    body, manifest = _signed_answer(settings, start)
    other_key = Ed25519PrivateKey.generate()
    impostor = publisher_module.RoutingSnapshotSigner(other_key, TrustedKey(
        KID, PURPOSE, b64encode(other_key.public_key().public_bytes_raw()), ISSUER, AUDIENCE))
    flipped = body[:100] + bytes([body[100] ^ 1]) + body[101:]
    cases: dict[str, tuple[_Worker, str]] = {
        "another window": (_Worker(_answer(*_signed_answer(settings, start + HOUR))), "snapshot-error"),
        "another revision": (_Worker(_answer(*_signed_answer(settings, start, revision="b" * 40))),
                             "snapshot-error"),
        "another key": (_Worker(_answer(*_signed_answer(settings, start, signer=impostor))),
                        "snapshot-error"),
        "a changed byte": (_Worker(_answer(flipped, manifest)), "snapshot-error"),
        "an unsigned manifest": (_Worker(_answer(body, manifest.rsplit(".", 1)[0] + ".")),
                                 "snapshot-error"),
        "a non-zero exit": (_Worker(b'{"error":"OSError"}', returncode=1), "snapshot-error"),
        "a garbled error": (_Worker(b'{"error":"not a class name"}', returncode=1), "snapshot-error"),
        "malformed stdout": (_Worker(b"not json"), "snapshot-error"),
        "an empty answer": (_Worker(b""), "snapshot-error"),
        "a list": (_Worker(b"[]"), "snapshot-error"),
        "a refusal": (_Worker(b'{"refused":"a catalog deadline falls between 1 and 2"}'),
                      "snapshot-refused"),
    }
    caplog.set_level(logging.WARNING)
    for name, (worker, status) in cases.items():
        publisher = _as_of_publisher(settings, Clock(now), worker)
        with pytest.raises(SnapshotUnavailable) as refused:
            publisher.current()
        assert refused.value.status == status, name
        app = _app(settings)
        app.state.routing_snapshot_publisher = _as_of_publisher(settings, Clock(now), worker)
        response = _get(TestClient(app))
        assert (response.status_code, response.json()["status"]) == (503, status), name
    messages = [record.getMessage() for record in caplog.records]
    assert "routing snapshot compile failed: worker:OSError" in messages
    assert "routing snapshot compile failed: worker:unknown" in messages
    # Positive control: the right answer is served, from the right command.
    worker = _Worker(_answer(body, manifest))
    publication = _as_of_publisher(settings, Clock(now), worker).current()
    assert publication.body == body and publication.compiled_at == start
    argv = worker.calls[0][0]
    assert argv[1:] == ["-P", "-m", publisher_module.WORKER_MODULE]
    request = json.loads(worker.calls[0][1]["input"])
    assert request["at"] == start and request["key"]["file"] == str(key[0])
    assert "BEGIN" not in worker.calls[0][1]["input"].decode()


def test_the_worker_prints_only_the_failures_class() -> None:
    import subprocess
    import sys

    result = subprocess.run(  # noqa: S603 - this interpreter and a fixed module
        [sys.executable, "-P", "-m", publisher_module.WORKER_MODULE],
        input=b'{"at": -1, "key": {"file": "/secret/path.pem"}}', capture_output=True,
        timeout=120, check=False,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout) == {"error": "ValueError"}
    assert result.stderr == b""


# --- key custody ---------------------------------------------------------------------------


def test_the_publisher_refuses_a_key_that_is_not_the_pinned_one(key, tmp_path: Path) -> None:
    other = b64encode(Ed25519PrivateKey.generate().public_key().public_bytes_raw())
    for overrides, reason in (
        ({"routing_snapshot_public_key": other}, "pinned public key"),
        ({"routing_snapshot_public_key": ""}, "not configured"),
        ({"routing_snapshot_kid": ""}, "not configured"),
        ({"routing_snapshot_private_key_file": ""}, "not configured"),
        ({"routing_snapshot_private_key_file": str(tmp_path / "absent.pem")}, "unreadable"),
        ({"async_settle_ticket_private_key_file": str(key[0])}, "separate key file"),
        ({"async_settle_ticket_kid": KID}, "separate kid"),
        ({"speculation_shadow_kid": KID}, "separate kid"),
    ):
        with pytest.raises(ValueError, match=reason):
            load_signer(_settings(key, **overrides))
    # The same key under another purpose's file is refused by its public half.
    shared = tmp_path / "shadow.pem"
    shared.write_bytes(key[0].read_bytes())
    with pytest.raises(ValueError, match="separate key required"):
        load_signer(_settings(key, speculation_shadow_private_key_file=str(shared)))
    # Positive control: the pinned key loads.
    assert load_signer(_settings(key)).trusted == _trusted(key[1])


def test_an_unusable_key_leaves_the_route_answering_signer_unavailable(key) -> None:
    other = b64encode(Ed25519PrivateKey.generate().public_key().public_bytes_raw())
    app = create_app(_settings(key, routing_snapshot_public_key=other), init_observability=False)
    assert app.state.routing_snapshot_publisher is None
    response = _get(TestClient(app))
    assert response.status_code == 503 and response.json() == {"status": "signer-unavailable"}


def test_no_secret_appears_in_responses_or_logs(key, caplog: pytest.LogCaptureFixture) -> None:
    path, public, pem = key
    raw = serialization.load_pem_private_key(path.read_bytes(), password=None)
    assert isinstance(raw, Ed25519PrivateKey)
    private_raw = raw.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption(),
    )
    secrets = [TOKEN, pem, pem.splitlines()[1], b64encode(private_raw),
               base64.b64encode(private_raw).decode("ascii"), private_raw.hex()]
    caplog.set_level(logging.DEBUG)
    seen: list[str] = []
    clock = Clock(DAY + HOUR + 60)
    client = TestClient(_app(_settings(key), clock))
    for response in (_get(client), client.get(PATH)):
        seen += [response.text, json.dumps(dict(response.headers))]
    body = base64.b64decode(_get(client).json()["body"])
    seen.append(body.decode("ascii"))
    clock.now = DEADLINE + 600
    refusing = TestClient(_app(_settings(key), clock, inputs=_fixture_inputs(Clock(DAY))))
    seen.append(_get(refusing).text)
    other = b64encode(Ed25519PrivateKey.generate().public_key().public_bytes_raw())
    seen.append(_get(TestClient(create_app(
        _settings(key, routing_snapshot_public_key=other), init_observability=False))).text)
    seen += [record.getMessage() for record in caplog.records]
    assert any("routing snapshot published" in line for line in seen)
    assert any("routing snapshot compile refused" in line for line in seen)
    assert any("routing snapshot publisher disabled" in line for line in seen)
    for line in seen:
        for secret in secrets:
            assert secret not in line
    # The public key is not a secret; the response's manifest names only the kid.
    assert public not in body.decode("ascii")


# --- the compile's cost ------------------------------------------------------------------------


def test_the_live_compile_runs_in_a_worker_within_budget(key) -> None:
    """The live catalog compiles as of the window's start in a worker process:
    lazily under the publisher's lock, in the threadpool worker of a sync route,
    never on the event loop, once a window, with its CPU outside this process."""
    router = APIRouter()
    fastpath_snapshot.register(router)
    route = next(r for r in router.routes if isinstance(r, APIRoute) and r.path == PATH)
    assert not inspect.iscoroutinefunction(route.endpoint)

    clock = Clock(int(catalog_registry.CATALOG_RESOLVED_AT.timestamp()))
    publisher = build_publisher(_settings(key), clock=clock)
    assert isinstance(publisher._source, AsOfSource)
    def children_cpu() -> float:
        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        return usage.ru_utime + usage.ru_stime

    started, cpu, worker_cpu = time.monotonic(), time.process_time(), children_cpu()
    publication = publisher.current()
    elapsed = time.monotonic() - started
    own_cpu, worker_cpu = time.process_time() - cpu, children_cpu() - worker_cpu
    assert len(publication.body) > 1_000_000
    # About 3 to 6 s end to end on development machines (the worker's import
    # and compile); the bound catches a runaway, not a slow host.
    assert elapsed < 120
    # The compile's CPU is the worker's; this process parses, checks and signs.
    assert own_cpu < worker_cpu, (own_cpu, worker_cpu)
    started = time.monotonic()
    assert publisher.current() is publication and publisher.compiles == 1
    assert time.monotonic() - started < 1.0


def test_the_publisher_module_holds_no_settings_knob_for_the_window() -> None:
    # The window's shape is the same on every instance: horizon, issuer and
    # audience are code, not configuration.
    assert publisher_module.HORIZON_SECONDS == 6 * HOUR
    assert (publisher_module.ISSUER, publisher_module.AUDIENCE) == (
        "https://trustedrouter.com", "tr-fastpath-edge",
    )
