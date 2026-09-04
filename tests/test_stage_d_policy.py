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
    watermark: FakeWatermark,
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

    documents[POLICY_URL] = FIXTURE_BYTES
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
    sigstore.__path__ = []  # type: ignore[attr-defined]
    models = types.ModuleType("sigstore.models")
    models.Bundle = Bundle  # type: ignore[attr-defined]
    verify = types.ModuleType("sigstore.verify")
    verify.__path__ = []  # type: ignore[attr-defined]
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
    assert settings.spend_lease_accepted_gcp_digests == frozenset()
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
    store, db, _table = make_fake_store(request_record_write_mode="typed")
    watermark = StorePolicyWatermark(store)
    now = datetime(2026, 9, 3, 22, tzinfo=UTC)

    assert watermark.advance(plane="gcp", sequence=1200, updated_at=now) is True
    assert watermark.advance(plane="gcp", sequence=1200, updated_at=now) is False
    assert watermark.advance(plane="gcp", sequence=1199, updated_at=now) is False
    assert watermark.advance(plane="gcp", sequence=1201, updated_at=now) is True
    assert db.stage_d_policy_watermarks["gcp"]["highest_sequence"] == 1201
