from __future__ import annotations

import json
import sys
import types
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.fakes.spanner import make_fake_store
from trusted_router.config import Settings
from trusted_router.stage_d_policy import (
    PolicyWatermark,
    SigstoreBundleVerifier,
    StageDPolicyResolver,
    StorePolicyWatermark,
    parse_stage_d_policy,
)

FIXTURE = Path(__file__).parent / "fixtures" / "stage_d" / "stage-d-accepted.json"
FIXTURE_BYTES = FIXTURE.read_bytes()
POLICY_URL = "https://trust.example/trust/gcp/stage-d-accepted.json"


class FakeVerifier:
    def __init__(self) -> None:
        self.calls: list[tuple[bytes, bytes, str, str]] = []
        self.failure: Exception | None = None

    def verify(
        self,
        document: bytes,
        bundle: bytes,
        *,
        certificate_identity: str,
        oidc_issuer: str,
    ) -> None:
        self.calls.append((document, bundle, certificate_identity, oidc_issuer))
        if self.failure is not None:
            raise self.failure


class FakeWatermark:
    def __init__(self) -> None:
        self.highest = 0

    def advance(self, *, plane: str, sequence: int, updated_at: datetime) -> bool:
        assert plane == "gcp"
        assert updated_at.tzinfo is not None
        if sequence <= self.highest:
            return False
        self.highest = sequence
        return True

    def highest_sequence(self, *, plane: str) -> int | None:
        assert plane == "gcp"
        return self.highest or None


def _client_factory(documents: dict[str, bytes]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        body = documents.get(str(request.url))
        if body is None:
            return httpx.Response(404, request=request)
        return httpx.Response(200, content=body, request=request)

    def factory(**kwargs: Any) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

    return factory


def _resolver(
    verifier: FakeVerifier,
    watermark: PolicyWatermark,
    documents: dict[str, bytes],
) -> StageDPolicyResolver:
    return StageDPolicyResolver(
        Settings(
            environment="test",
            trust_gcp_release_url="https://trust.example/trust/gcp-release.json",
            trust_gcp_release_fallback_urls="",
        ),
        watermark,
        verifier=verifier,
        wall_clock=lambda: datetime(2026, 9, 3, 22, tzinfo=UTC),
        client_factory=_client_factory(documents),
    )


def _policy_document(sequence: int) -> bytes:
    value = json.loads(FIXTURE_BYTES)
    value["sequence"] = sequence
    return json.dumps(value).encode()


def test_literal_cross_repo_fixture_is_verified_before_becoming_live() -> None:
    verifier = FakeVerifier()
    watermark = FakeWatermark()
    resolver = _resolver(
        verifier,
        watermark,
        {POLICY_URL: FIXTURE_BYTES, POLICY_URL + ".bundle": b"literal-bundle"},
    )

    assert resolver.accepted_image_digests() == frozenset()
    assert resolver.refresh() is True
    assert resolver.current_policy() is not None
    assert resolver.current_policy().sequence == 1200  # type: ignore[union-attr]
    assert resolver.accepted_image_digests() == frozenset(
        json.loads(FIXTURE_BYTES)["image_digests"]
    )
    settings = Settings(environment="test")
    assert verifier.calls == [
        (
            FIXTURE_BYTES,
            b"literal-bundle",
            settings.stage_d_policy_cert_identity,
            settings.stage_d_policy_oidc_issuer,
        )
    ]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(extra=True),
        lambda value: value.update(schema="tr.stage-d-accepted/2"),
        lambda value: value.update(plane="aws"),
        lambda value: value.update(sequence=True),
        lambda value: value.update(kind="other"),
        lambda value: value.update(issued_at="not-a-time"),
        lambda value: value.update(issued_at="2026-09-03 21:00:00Z"),
        lambda value: value.update(image_digests=[]),
        lambda value: value.update(image_digests=["sha256:bad"]),
        lambda value: value.update(image_digests=list(reversed(value["image_digests"]))),
        lambda value: value.update(image_digests=value["image_digests"] * 2),
    ],
)
def test_policy_schema_is_strict(mutation: Any) -> None:
    value = json.loads(FIXTURE_BYTES)
    mutation(value)
    with pytest.raises(ValueError):
        parse_stage_d_policy(
            json.dumps(value).encode(),
            now=datetime(2026, 9, 3, 22, tzinfo=UTC),
        )


def test_policy_schema_rejects_duplicate_keys_and_non_utf8_json() -> None:
    duplicate = FIXTURE_BYTES.rstrip().removesuffix(b"}") + b',"plane":"gcp"}\n'
    with pytest.raises(ValueError, match="duplicate key"):
        parse_stage_d_policy(duplicate, now=datetime(2026, 9, 3, 22, tzinfo=UTC))
    with pytest.raises(ValueError, match="UTF-8"):
        parse_stage_d_policy(
            FIXTURE_BYTES.decode().encode("utf-16"),
            now=datetime(2026, 9, 3, 22, tzinfo=UTC),
        )


def test_signature_schema_and_rollback_failures_retain_last_verified_policy() -> None:
    verifier = FakeVerifier()
    watermark = FakeWatermark()
    documents = {
        POLICY_URL: FIXTURE_BYTES,
        POLICY_URL + ".bundle": b"bundle",
    }
    resolver = _resolver(verifier, watermark, documents)
    assert resolver.refresh() is True
    accepted = resolver.accepted_image_digests()

    verifier.failure = ValueError("bad signature")
    assert resolver.refresh() is False
    assert resolver.accepted_image_digests() == accepted

    verifier.failure = None
    documents.pop(POLICY_URL + ".bundle")
    assert resolver.refresh() is False
    assert resolver.accepted_image_digests() == accepted
    documents[POLICY_URL + ".bundle"] = b"bundle"

    invalid = json.loads(FIXTURE_BYTES)
    invalid["sequence"] = 1201
    invalid["unknown"] = True
    documents[POLICY_URL] = json.dumps(invalid).encode()
    assert resolver.refresh() is False
    assert resolver.accepted_image_digests() == accepted

    documents[POLICY_URL] = _policy_document(1199)
    assert resolver.refresh() is False
    assert resolver.accepted_image_digests() == accepted

    future = json.loads(FIXTURE_BYTES)
    future["sequence"] = 1201
    future["issued_at"] = "2026-09-04T00:00:00Z"
    documents[POLICY_URL] = json.dumps(future).encode()
    assert resolver.refresh() is False
    assert resolver.accepted_image_digests() == accepted


def test_sigstore_adapter_verifies_literal_bytes_with_pinned_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, Any] = {}

    class Bundle:
        @classmethod
        def from_json(cls, raw: str) -> object:
            calls["bundle_json"] = raw
            return object()

    class VerifierInstance:
        def verify_artifact(self, **kwargs: Any) -> None:
            calls["verify"] = kwargs

    class Verifier:
        @classmethod
        def production(cls) -> VerifierInstance:
            calls["production"] = True
            return VerifierInstance()

    class Identity:
        def __init__(self, *, identity: str, issuer: str) -> None:
            calls["identity"] = (identity, issuer)

    sigstore = types.ModuleType("sigstore")
    sigstore.__path__ = []
    models = types.ModuleType("sigstore.models")
    models.Bundle = Bundle  # type: ignore[attr-defined]
    verify = types.ModuleType("sigstore.verify")
    verify.__path__ = []
    verify.Verifier = Verifier  # type: ignore[attr-defined]
    policy = types.ModuleType("sigstore.verify.policy")
    policy.Identity = Identity  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sigstore", sigstore)
    monkeypatch.setitem(sys.modules, "sigstore.models", models)
    monkeypatch.setitem(sys.modules, "sigstore.verify", verify)
    monkeypatch.setitem(sys.modules, "sigstore.verify.policy", policy)

    settings = Settings(environment="test")
    SigstoreBundleVerifier().verify(
        FIXTURE_BYTES,
        b'{"mediaType":"bundle"}',
        certificate_identity=settings.stage_d_policy_cert_identity,
        oidc_issuer=settings.stage_d_policy_oidc_issuer,
    )

    assert calls["production"] is True
    assert calls["bundle_json"] == '{"mediaType":"bundle"}'
    assert calls["identity"] == (
        settings.stage_d_policy_cert_identity,
        settings.stage_d_policy_oidc_issuer,
    )
    assert calls["verify"]["input_"] == FIXTURE_BYTES


def test_missing_durable_watermark_fails_closed() -> None:
    class MissingWatermark:
        def advance(self, **_kwargs: Any) -> bool:
            return False

        def highest_sequence(self, **_kwargs: Any) -> int | None:
            return None

    resolver = StageDPolicyResolver(
        Settings(
            environment="test",
            trust_gcp_release_url="https://trust.example/trust/gcp-release.json",
            trust_gcp_release_fallback_urls="",
        ),
        MissingWatermark(),
        verifier=FakeVerifier(),
        wall_clock=lambda: datetime(2026, 9, 3, 22, tzinfo=UTC),
        client_factory=_client_factory(
            {POLICY_URL: FIXTURE_BYTES, POLICY_URL + ".bundle": b"bundle"}
        ),
    )
    assert resolver.refresh() is False
    assert resolver.accepted_image_digests() == frozenset()


def test_stage_d_settings_defaults_are_fail_closed_and_contract_pinned() -> None:
    settings = Settings(environment="test")
    assert settings.stage_d_eligibility_enabled is False
    assert settings.stage_d_heartbeat_enabled is True
    assert settings.stage_d_pilot_workspaces == {
        "45819281-0ce9-4811-a0cd-c660ab3a116d"
    }
    assert settings.stage_d_policy_refresh_seconds == 60
    assert settings.stage_d_policy_cert_identity == (
        "https://github.com/Lore-Hex/quill-cloud-proxy/.github/workflows/"
        "publish-trust-gcp.yml@refs/heads/main"
    )
    assert settings.stage_d_policy_oidc_issuer == (
        "https://token.actions.githubusercontent.com"
    )
    assert settings.stage_d_accepted_gcp_digests == frozenset()
    assert settings.reap_snapshot_booking_enabled is False


def test_deployed_internal_surface_registers_nonblocking_policy_warm() -> None:
    from trusted_router.main import create_app

    settings = Settings(environment="test", service_surface="internal")
    # Build the validated test fixture first, then model a deployed worker so
    # this test proves the production-only startup wiring without credentials.
    settings.environment = "worker"
    app = create_app(
        settings,
        configure_store_arg=False,
        init_observability=False,
    )

    assert "_warm_stage_d_policy" in {
        handler.__name__ for handler in app.router.on_startup
    }


def test_spanner_typed_watermark_is_strictly_monotonic() -> None:
    store, db = make_fake_store(request_record_write_mode="typed")
    watermark = StorePolicyWatermark(store)
    now = datetime(2026, 9, 3, 22, tzinfo=UTC)

    assert watermark.highest_sequence(plane="gcp") is None
    assert watermark.advance(plane="gcp", sequence=1200, updated_at=now) is True
    assert watermark.highest_sequence(plane="gcp") == 1200
    assert watermark.advance(plane="gcp", sequence=1200, updated_at=now) is False
    assert watermark.advance(plane="gcp", sequence=1199, updated_at=now) is False
    assert watermark.advance(plane="gcp", sequence=1201, updated_at=now) is True
    assert db.stage_d_policy_watermarks["gcp"]["highest_sequence"] == 1201


def test_two_replicas_accept_same_verified_sequence_without_second_advance() -> None:
    verifier = FakeVerifier()
    store, db = make_fake_store(request_record_write_mode="typed")
    documents = {
        POLICY_URL: _policy_document(852),
        POLICY_URL + ".bundle": b"bundle",
    }
    replica_a = _resolver(verifier, StorePolicyWatermark(store), documents)
    replica_b = _resolver(verifier, StorePolicyWatermark(store), documents)

    assert replica_a.refresh() is True
    watermark_version = db.stage_d_policy_watermark_versions["gcp"]
    assert replica_b.refresh() is True
    assert replica_a.current_policy().sequence == 852  # type: ignore[union-attr]
    assert replica_b.current_policy().sequence == 852  # type: ignore[union-attr]
    assert db.stage_d_policy_watermarks["gcp"]["highest_sequence"] == 852
    assert db.stage_d_policy_watermark_versions["gcp"] == watermark_version


def test_sequence_below_durable_watermark_is_rejected() -> None:
    verifier = FakeVerifier()
    watermark = FakeWatermark()
    documents = {
        POLICY_URL: _policy_document(852),
        POLICY_URL + ".bundle": b"bundle",
    }
    resolver = _resolver(verifier, watermark, documents)
    assert resolver.refresh() is True
    accepted = resolver.current_policy()

    documents[POLICY_URL] = _policy_document(851)
    assert resolver.refresh() is False
    assert resolver.current_policy() is accepted
    assert watermark.highest == 852


# Each release URL's fetch deadline and the refresh's join, shrunk so a
# hang shows within a second or two.
SHORT_FETCH_TIMEOUT = 0.3
SHORT_REFRESH_TIMEOUT = 1.5


@pytest.fixture
def hosts() -> Any:
    """Release hosts on 127.0.0.1, closed (threads joined) after the test."""
    from tests.fakes.release_host import ReleaseHost

    started: list[ReleaseHost] = []

    def start(sequence: int = 1200) -> ReleaseHost:
        host = ReleaseHost(_policy_document(sequence))
        started.append(host)
        return host

    yield start
    for host in started:
        host.close()


@pytest.fixture
def release_host(hosts: Any) -> Any:
    return hosts()


@pytest.fixture
def short_timeouts(monkeypatch: pytest.MonkeyPatch) -> float:
    import trusted_router.stage_d_policy as stage_d_policy

    monkeypatch.setattr(stage_d_policy, "_FETCH_TIMEOUT_SECONDS", SHORT_FETCH_TIMEOUT)
    monkeypatch.setattr(stage_d_policy, "REFRESH_TIMEOUT_SECONDS", SHORT_REFRESH_TIMEOUT)
    return SHORT_REFRESH_TIMEOUT


class BlockingVerifier(FakeVerifier):
    """Verifies once ``gate`` is open: a Sigstore TUF refresh that hangs."""

    def __init__(self) -> None:
        import threading

        super().__init__()
        self.gate = threading.Event()
        self.gate.set()

    def verify(self, document: bytes, bundle: bytes, **kwargs: Any) -> None:
        super().verify(document, bundle, **kwargs)
        self.gate.wait(30)


def _served_resolver(urls: list[str], watermark: PolicyWatermark,
                     verifier: FakeVerifier | None = None) -> StageDPolicyResolver:
    from tests.fakes.release_host import plain_http_client_factory

    return StageDPolicyResolver(
        Settings(environment="test"),
        watermark,
        verifier=verifier or FakeVerifier(),
        release_urls=urls,
        wall_clock=lambda: datetime(2026, 9, 3, 22, tzinfo=UTC),
        client_factory=plain_http_client_factory,
    )


def _within(seconds: float, target: Any) -> Any:
    """target() in a thread: its outcome, failing when it is still running
    after ``seconds``."""
    import threading

    outcome: list[Any] = []
    thread = threading.Thread(target=lambda: outcome.append(target()), daemon=True)
    thread.start()
    thread.join(seconds)
    assert not thread.is_alive(), "still running"
    return outcome[0]


def _network_thread(resolver: StageDPolicyResolver) -> Any:
    """The thread of the resolver's latest network part."""
    run = resolver._network
    thread = getattr(run, "thread", run)  # a bare Thread before _NetworkRun
    assert thread is not None
    return thread


HANGS = ["header-drip", "deflate-forever"]


@pytest.mark.parametrize("mode", HANGS)
def test_a_release_host_that_never_finishes_fails_the_refresh_within_its_timeout(
    release_host: Any, short_timeouts: float, mode: str,
) -> None:
    """Through the real httpx client, neither a header drip nor endless empty
    deflate blocks trips any httpx timeout. The refresh still fails within
    its timeout, keeps the verified policy and releases _refresh_lock, so
    kick's worker queued behind it finishes too."""
    import threading
    import time

    watermark = FakeWatermark()
    resolver = _served_resolver([release_host.release_url], watermark)
    assert resolver.refresh_now() is True
    verified = resolver.current_policy()
    release_host.mode = mode
    release_host.document = _policy_document(1201)
    try:
        started = time.monotonic()
        refreshing: list[bool] = []
        publisher = threading.Thread(target=lambda: refreshing.append(resolver.refresh_now()),
                                     daemon=True)
        publisher.start()
        resolver.kick()
        kick_worker = resolver._worker
        assert kick_worker is not None
        publisher.join(5)
        kick_worker.join(5)
        assert not publisher.is_alive() and not kick_worker.is_alive(), "a refresh hung"
        assert refreshing == [False]
        assert time.monotonic() - started < 2 * short_timeouts + 1
        assert resolver.current_policy() is verified and watermark.highest == 1200
        assert resolver._refresh_lock.acquire(timeout=1)
        resolver._refresh_lock.release()
    finally:
        release_host.end_drips()
    # Positive control: once the host stops, the next refresh accepts the new policy.
    release_host.mode = "ok"
    network = _network_thread(resolver)
    network.join(10)
    assert not network.is_alive()
    assert resolver.refresh() is True
    assert resolver.current_policy().sequence == 1201  # type: ignore[union-attr]


@pytest.mark.parametrize("mode", HANGS)
def test_a_primary_that_never_finishes_falls_back_within_one_refresh(
    hosts: Any, short_timeouts: float, mode: str,
) -> None:
    """Each release URL has its own fetch deadline: a hung primary times out
    and the fallback serves the policy in the same refresh, so a primary
    that never finishes cannot keep a healthy fallback out."""
    import time

    primary, fallback = hosts(1200), hosts(1201)
    primary.mode = mode
    watermark = FakeWatermark()
    resolver = _served_resolver([primary.release_url, fallback.release_url], watermark)
    try:
        started = time.monotonic()
        assert _within(5, resolver.refresh) is True
        assert time.monotonic() - started < short_timeouts
        assert resolver.current_policy().sequence == 1201  # type: ignore[union-attr]
        assert (primary.requests, fallback.requests, watermark.highest) == (1, 2, 1201)
    finally:
        primary.end_drips()
    network = _network_thread(resolver)
    network.join(10)
    assert not network.is_alive()


def test_a_primary_below_the_watermark_falls_back_to_a_newer_release(
    hosts: Any, short_timeouts: float,
) -> None:
    """The watermark is read before the network part and passed in as a
    floor: a validly signed primary below it is skipped for the fallback, so
    a fresh instance does not end up with an empty set."""
    primary, fallback = hosts(1199), hosts(1201)
    watermark = FakeWatermark()
    watermark.highest = 1200
    resolver = _served_resolver([primary.release_url, fallback.release_url], watermark)
    assert resolver.current_policy() is None
    assert _within(5, resolver.refresh) is True
    assert resolver.current_policy().sequence == 1201  # type: ignore[union-attr]
    assert watermark.highest == 1201
    assert (primary.requests, fallback.requests) == (2, 2)
    # Positive control: with every release below the watermark, nothing is accepted.
    fallback.document = _policy_document(1198)
    watermark.highest = 1202
    assert _within(5, resolver.refresh) is False
    assert resolver.current_policy().sequence == 1201  # type: ignore[union-attr]
    assert watermark.highest == 1202


def test_a_verification_that_hangs_fails_the_refresh_and_its_late_result_is_discarded(
    release_host: Any, short_timeouts: float,
) -> None:
    watermark = FakeWatermark()
    verifier = BlockingVerifier()
    resolver = _served_resolver([release_host.release_url], watermark, verifier)
    assert resolver.refresh() is True
    verified = resolver.current_policy()
    release_host.document = _policy_document(1201)
    verifier.gate.clear()
    try:
        assert _within(5, resolver.refresh) is False
        assert resolver.current_policy() is verified and watermark.highest == 1200
    finally:
        verifier.gate.set()
    # The abandoned verification completes with a valid newer policy: it
    # changes nothing, neither the policy nor the watermark.
    network = _network_thread(resolver)
    network.join(10)
    assert not network.is_alive() and len(verifier.calls) == 2
    assert resolver.current_policy() is verified and watermark.highest == 1200
    # Positive control: the next refresh accepts it.
    assert resolver.refresh() is True
    assert resolver.current_policy().sequence == 1201  # type: ignore[union-attr]
    assert watermark.highest == 1201


def test_no_second_fetch_starts_while_an_abandoned_one_runs(
    release_host: Any, short_timeouts: float, caplog: pytest.LogCaptureFixture,
) -> None:
    """A hang the fetch deadlines do not cover costs one thread, not one per
    refresh, and is logged once, then once more when it ends."""
    import logging
    import time

    caplog.set_level(logging.INFO, logger="trusted_router.stage_d_policy")
    verifier = BlockingVerifier()
    resolver = _served_resolver([release_host.release_url], FakeWatermark(), verifier)
    verifier.gate.clear()
    try:
        assert _within(5, resolver.refresh) is False
        abandoned = _network_thread(resolver)
        assert abandoned.is_alive()
        requests, verifications = release_host.requests, len(verifier.calls)
        for _ in range(3):
            started = time.monotonic()
            assert _within(5, resolver.refresh) is False
            # Failed at once: no join, no fetch, no verification, no thread.
            assert time.monotonic() - started < short_timeouts / 2
            assert _network_thread(resolver) is abandoned and abandoned.is_alive()
        assert (release_host.requests, len(verifier.calls)) == (requests, verifications)
    finally:
        verifier.gate.set()
    abandoned.join(10)
    assert not abandoned.is_alive()
    messages = [record.getMessage() for record in caplog.records]
    assert messages.count("stage_d.policy_refresh_failed error_class=PreviousFetchRunning") == 1
    assert messages.count("stage_d.policy_abandoned_fetch_ended error_class=StageDPolicy") == 1
    # Positive control: with the abandoned fetch gone, a refresh fetches again.
    assert resolver.refresh() is True
    assert release_host.requests == requests + 2
    latest = _network_thread(resolver)
    assert latest is not abandoned
    latest.join(10)
    assert not latest.is_alive()
