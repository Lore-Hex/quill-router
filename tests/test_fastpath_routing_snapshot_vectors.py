"""The routing-snapshot golden files are what the Python compiler and signer write now.

fastpath/internal/routingsnapshot holds the Go reader to
fastpath/testdata/routing_snapshot_v1.{body,manifest}.json; this test holds the
files to `trusted_router.routing_snapshot`, so a change to the schema, the
compiler or a catalog rule it reads fails here until the files follow.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from trusted_router.detached_jws import JWSError, TrustedKey, b64encode
from trusted_router.routing_snapshot import (
    Plane,
    SnapshotRefused,
    canonical_snapshot_bytes,
    compile_routing_snapshot,
    parse_routing_snapshot,
)
from trusted_router.routing_snapshot_signing import (
    PURPOSE,
    TEST_KID,
    TEST_PUBLIC_KEY_B64URL,
    RoutingSnapshotSigner,
    verify_manifest,
)

ROOT = Path(__file__).resolve().parents[1]
HOUR = 3_600


def _generator() -> ModuleType:
    path = ROOT / "scripts" / "fastpath" / "routing_snapshot_vectors.py"
    spec = importlib.util.spec_from_file_location("routing_snapshot_vectors", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GENERATOR = _generator()
GOLDEN_PLANE = Plane(cloud="gcp", region="us-central1")


def _verify(token: str, keys: list[TrustedKey], body: bytes, *, now: int, **overrides):  # type: ignore[no-untyped-def]
    """The golden verifier: this plane, the test audience, test mode on."""
    arguments = {"expected_plane": GOLDEN_PLANE, "expected_aud": GENERATOR.TEST_KEY.aud,
                 "allow_test_key": True, **overrides}
    return verify_manifest(token, keys, body, now=now, **arguments)


def _body() -> bytes:
    return GENERATOR.OUTPUT_BODY.read_bytes()


def _manifest() -> dict:
    return json.loads(GENERATOR.OUTPUT_MANIFEST.read_text())


def test_the_golden_files_are_what_the_python_compiler_writes_now() -> None:
    stale = (
        "fastpath/testdata/routing_snapshot_v1.*.json is stale: run "
        "`uv run python scripts/fastpath/routing_snapshot_vectors.py` and check the Go reader"
    )
    assert _body() == GENERATOR.render_body(), stale
    assert GENERATOR.OUTPUT_MANIFEST.read_text() == GENERATOR.render_manifest(), stale


def test_the_body_is_canonical_ascii_without_a_trailing_newline() -> None:
    body = _body()
    assert body.isascii() and not body.endswith(b"\n")
    assert canonical_snapshot_bytes(parse_routing_snapshot(body)) == body


def test_the_manifest_verifies_and_binds_the_body() -> None:
    body, manifest = _body(), _manifest()
    key = TrustedKey(manifest["kid"], manifest["purpose"], manifest["public_key_b64url"],
                     manifest["iss"], manifest["aud"])
    snapshot = parse_routing_snapshot(body)
    claims = _verify(manifest["jws"], [key], body, now=snapshot.compiled_at)
    assert claims.sha256 == hashlib.sha256(body).hexdigest() and claims.size == len(body)
    assert (claims.compiled_at, claims.not_after) == (snapshot.compiled_at, snapshot.not_after)
    assert claims.plane == snapshot.plane and claims.source_revision == "golden-fixture"


def test_the_manifest_key_is_the_named_test_key() -> None:
    manifest = _manifest()
    derived = Ed25519PrivateKey.from_private_bytes(GENERATOR.TEST_KEY_SEED).public_key()
    assert manifest["test_key"] is True and "test" in manifest["kid"]
    assert manifest["public_key_b64url"] == b64encode(derived.public_bytes_raw())
    assert manifest["purpose"] == PURPOSE == "routing-snapshot"
    assert (manifest["kid"], manifest["public_key_b64url"]) == (TEST_KID, TEST_PUBLIC_KEY_B64URL)


def test_verification_binds_the_verifiers_plane_and_audience() -> None:
    body, manifest, key = _body(), _manifest(), GENERATOR.TEST_KEY
    now = parse_routing_snapshot(body).compiled_at
    # Positive control: this plane and this audience verify.
    _verify(manifest["jws"], [key], body, now=now)
    with pytest.raises(ValueError, match="manifest plane"):
        _verify(manifest["jws"], [key], body, now=now,
                expected_plane=Plane(cloud="gcp", region="europe-west4"))
    with pytest.raises(ValueError, match="manifest plane"):
        _verify(manifest["jws"], [key], body, now=now,
                expected_plane=Plane(cloud="aws", region="us-central1"))
    with pytest.raises(ValueError, match="manifest audience"):
        _verify(manifest["jws"], [key], body, now=now, expected_aud="tr-other-consumer")


def test_the_public_test_key_is_refused_outside_test_mode() -> None:
    body, manifest, key = _body(), _manifest(), GENERATOR.TEST_KEY
    now = parse_routing_snapshot(body).compiled_at
    private = Ed25519PrivateKey.from_private_bytes(GENERATOR.TEST_KEY_SEED)
    with pytest.raises(ValueError, match="test key"):
        _verify(manifest["jws"], [key], body, now=now, allow_test_key=False)
    with pytest.raises(ValueError, match="test key"):
        RoutingSnapshotSigner(private, key)
    # The public key under another kid, and the kid over another key, are refused too.
    renamed = TrustedKey("routing-snapshot-prod-1", key.purpose, key.public_key_b64url,
                         key.iss, key.aud)
    with pytest.raises(ValueError, match="test key"):
        RoutingSnapshotSigner(private, renamed)
    other = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    reused_kid = TrustedKey(TEST_KID, key.purpose,
                            b64encode(other.public_key().public_bytes_raw()), key.iss, key.aud)
    with pytest.raises(ValueError, match="test key"):
        RoutingSnapshotSigner(other, reused_kid)
    # Positive control: a non-test key signs and verifies without test mode.
    production = TrustedKey("routing-snapshot-prod-1", key.purpose,
                            b64encode(other.public_key().public_bytes_raw()), key.iss, key.aud)
    token = RoutingSnapshotSigner(other, production).sign(body)
    _verify(token, [production], body, now=now, allow_test_key=False)


def test_verification_refuses_tampering_staleness_and_the_wrong_key() -> None:
    body, manifest = _body(), _manifest()
    key = GENERATOR.TEST_KEY
    snapshot = parse_routing_snapshot(body)
    now = snapshot.compiled_at
    # Positive control: the untouched pair verifies at every instant of the window.
    for instant in (now, snapshot.not_after - 1):
        _verify(manifest["jws"], [key], body, now=instant)
    altered = body.replace(b'"golden-fixture"', b'"golden-fixturf"')
    assert altered != body
    with pytest.raises(ValueError, match="manifest binding"):
        _verify(manifest["jws"], [key], altered, now=now)
    with pytest.raises(ValueError, match="not canonical"):
        _verify(manifest["jws"], [key], body + b"\n", now=now)
    with pytest.raises(ValueError, match="freshness"):
        _verify(manifest["jws"], [key], body, now=snapshot.not_after)
    with pytest.raises(ValueError, match="freshness"):
        _verify(manifest["jws"], [key], body, now=now - 1)
    other = Ed25519PrivateKey.from_private_bytes(bytes(32)).public_key().public_bytes_raw()
    impostor = TrustedKey(key.kid, key.purpose, b64encode(other), key.iss, key.aud)
    with pytest.raises(JWSError, match="signature"):
        _verify(manifest["jws"], [impostor], body, now=now)
    wrong_purpose = TrustedKey(key.kid, "async-settle-ticket", key.public_key_b64url,
                               key.iss, key.aud)
    with pytest.raises(JWSError, match="purpose"):
        _verify(manifest["jws"], [wrong_purpose], body, now=now)
    head, payload, signature = manifest["jws"].split(".")
    flipped = base64.urlsafe_b64decode(signature + "==")
    forged = head + "." + payload + "." + b64encode(bytes([flipped[0] ^ 1]) + flipped[1:])
    with pytest.raises(JWSError, match="signature"):
        _verify(forged, [key], body, now=now)


def test_the_signer_refuses_a_key_shared_with_another_purpose() -> None:
    private = Ed25519PrivateKey.from_private_bytes(GENERATOR.TEST_KEY_SEED)
    key = GENERATOR.TEST_KEY
    # Positive control: an unrelated reserved key is accepted.
    unrelated = Ed25519PrivateKey.from_private_bytes(bytes(32)).public_key().public_bytes_raw()
    RoutingSnapshotSigner(private, key, allow_test_key=True, reserved=[
        TrustedKey("ticket-1", "async-settle-ticket", b64encode(unrelated), "a", "b"),
    ])
    with pytest.raises(ValueError, match="separate key"):
        RoutingSnapshotSigner(private, key, allow_test_key=True, reserved=[
            TrustedKey("ticket-1", "async-settle-ticket", key.public_key_b64url, "a", "b"),
        ])
    with pytest.raises(ValueError, match="separate kid"):
        RoutingSnapshotSigner(private, key, allow_test_key=True, reserved=[
            TrustedKey(key.kid, "async-settle-ticket", b64encode(unrelated), "a", "b"),
        ])
    with pytest.raises(ValueError, match="configuration"):
        RoutingSnapshotSigner(private, TrustedKey(key.kid, "async-settle-ticket",
                                                  key.public_key_b64url, key.iss, key.aud),
                              allow_test_key=True)


def _endpoint(snapshot_json: dict, provider: str, usage: str = "Credits") -> dict:
    # By provider, not by endpoint id: tests/ may not name an id that retires.
    (found,) = [e for e in snapshot_json["endpoints"]
                if e["provider"] == provider and e["usage_type"] == usage]
    return found


def test_the_golden_covers_what_the_go_reader_must_handle() -> None:
    body = json.loads(_body())
    at = body["compiled_at"]
    # The catalog deadline at 05:30 cuts the 6 h horizon short.
    assert body["not_after"] == at + 5 * HOUR + 30 * 60
    for scheduled in (_endpoint(body, "deepseek"), _endpoint(body, "tencent")):
        starts = [epoch["starts_at"] for epoch in scheduled["epochs"]]
        assert starts == [at, at + HOUR, at + 4 * HOUR]
        off, peak, after = (epoch["headline"] for epoch in scheduled["epochs"])
        assert off == after and peak["prompt"] == 2 * off["prompt"]
    wafer = _endpoint(body, "wafer")
    assert wafer["response"]["wafer_zdr_required"] and 2 in wafer["privacy"]["meets"]
    bedrock = _endpoint(body, "amazon-bedrock")["gateway_regions"]
    assert bedrock["requires_explicit_origin"] and "us-central1" in bedrock["only"]
    sakana = _endpoint(body, "sakana")["gateway_regions"]
    assert sakana == {"deny": ["europe-west4"], "only": None, "requires_explicit_origin": False}
    gemini = _endpoint(body, "google-ai-studio", "BYOK")
    assert len(gemini["epochs"][0]["price"]["tiers"]) == 2
    providers = {p["slug"]: p for p in body["providers"]}
    assert providers["google-ai-studio"]["byok_storage_slugs"] == ["google-ai-studio", "gemini"]
    deepinfra = _endpoint(body, "deepinfra")
    assert deepinfra["catalog_valid_until"] == body["not_after"]
    assert deepinfra["retired_at"] is not None and deepinfra["retired_at"] > body["not_after"]
    refused = {r["id"]: r["refused_status"] for r in body["requested_models"]}
    assert refused["trustedrouter/synth"] == 501 and refused["trustedrouter/auto"] is None
    assert body["ranking"]["measured_from_us"] % 1_000_000 == 250_000
    assert body["settings"]["stage_d"]["pilot_workspaces"] == ["ws_test_pilot_a", "ws_test_pilot_b"]


def test_a_price_change_off_the_hour_refuses_to_compile(monkeypatch: pytest.MonkeyPatch) -> None:
    from trusted_router import catalog
    from trusted_router.provider_lifecycle import ProviderPrice

    real = catalog.provider_price_microdollars
    inputs = GENERATOR.fixture_inputs()
    arguments = {
        "at": GENERATOR.COMPILED_AT, "horizon": GENERATOR.HORIZON_SECONDS,
        "settings": GENERATOR.fixture_settings(), "source_revision": "golden-fixture",
        "plane": Plane(cloud="gcp", region="us-central1"), "inputs": inputs,
    }
    # Positive control: the real schedule compiles.
    compile_routing_snapshot(**arguments)

    def half_past(provider: str, model_id: str, *, at=None):  # type: ignore[no-untyped-def]
        if provider == "wafer" and at is not None and at.minute >= 30:
            return ProviderPrice(1_000_000, 3_000_000)
        return real(provider, model_id, at=at)

    monkeypatch.setattr(catalog, "provider_price_microdollars", half_past)
    with pytest.raises(SnapshotRefused, match="off the hour"):
        compile_routing_snapshot(**arguments)


def test_a_catalog_deadline_between_the_expansions_and_compiled_at_refuses() -> None:
    from dataclasses import replace

    inputs = GENERATOR.fixture_inputs()
    arguments = {
        "horizon": GENERATOR.HORIZON_SECONDS, "settings": GENERATOR.fixture_settings(),
        "source_revision": "golden-fixture", "plane": Plane(cloud="gcp", region="us-central1"),
    }
    at = GENERATOR.COMPILED_AT
    deadline = at + 5 * HOUR + 30 * 60
    # Expansions computed before the deadline, the window opening after it.
    later = replace(inputs, expansions_clock=(at, at))
    compile_routing_snapshot(at=at, inputs=later, **arguments)
    with pytest.raises(SnapshotRefused, match="deadline"):
        compile_routing_snapshot(at=deadline + HOUR, inputs=later, **arguments)
    with pytest.raises(SnapshotRefused, match="deadline"):
        compile_routing_snapshot(
            at=at, inputs=replace(inputs, expansions_clock=(at, deadline)), **arguments,
        )


def test_a_retirement_between_compiled_at_and_the_import_refuses() -> None:
    from dataclasses import replace

    inputs = GENERATOR.fixture_inputs()
    at = GENERATOR.COMPILED_AT
    arguments = {
        "at": at, "horizon": GENERATOR.HORIZON_SECONDS, "settings": GENERATOR.fixture_settings(),
        "source_revision": "golden-fixture", "plane": GOLDEN_PLANE,
    }
    # A real retirement: DeepInfra's GPT-OSS-120B Turbo route, 2026-10-20 00:00 UTC.
    retirement = at + 24 * HOUR
    # Positive control: imported 12 h after compiled_at, with no retirement between.
    compile_routing_snapshot(inputs=replace(inputs, catalog_resolved_at=at + 12 * HOUR), **arguments)
    # Imported after the retirement, the registry no longer holds routes available at `at`.
    with pytest.raises(SnapshotRefused, match="retirement"):
        compile_routing_snapshot(inputs=replace(inputs, catalog_resolved_at=retirement + 1),
                                 **arguments)
    # A window that reaches the retirement ends there.
    clamped = compile_routing_snapshot(**{
        **arguments, "at": retirement - 2 * HOUR, "horizon": 6 * HOUR,
        "inputs": replace(inputs, catalog_resolved_at=retirement - 2 * HOUR,
                          expansions_clock=(retirement - 2 * HOUR,) * 2),
    })
    assert clamped.not_after == retirement


def test_a_retirement_between_the_import_and_compiled_at_refuses_on_either_side() -> None:
    """The import instant is not in the body, so the body must not depend on it.

    A process imported before a retirement still holds what a process imported
    after it has dropped; compiling one window in both would give two documents.
    """
    from dataclasses import replace

    from trusted_router.routing_snapshot import canonical_snapshot_bytes

    # A real retirement: DeepInfra's GPT-OSS-120B Turbo route, 2026-10-20 00:00 UTC.
    retirement = GENERATOR.COMPILED_AT + 24 * HOUR
    arguments = {
        "at": retirement, "horizon": 6 * HOUR, "settings": GENERATOR.fixture_settings(),
        "source_revision": "golden-fixture", "plane": GOLDEN_PLANE,
    }

    def compiled(resolved: int) -> bytes:
        inputs = replace(GENERATOR.fixture_inputs(), catalog_resolved_at=resolved,
                         expansions_clock=(retirement, retirement))
        return canonical_snapshot_bytes(compile_routing_snapshot(inputs=inputs, **arguments))

    # Positive control: imported at or after the retirement, two processes
    # compile the window, into the same bytes.
    at_import = compiled(retirement)
    assert compiled(retirement + HOUR // 2) == at_import
    # Imported before it: refused, never a second document for the window.
    for resolved in (retirement - HOUR // 2, retirement - 1):
        with pytest.raises(SnapshotRefused, match="retirement"):
            compiled(resolved)


def test_a_provider_other_than_the_catalogs_refuses() -> None:
    from dataclasses import replace

    inputs = GENERATOR.fixture_inputs()
    arguments = {
        "at": GENERATOR.COMPILED_AT, "horizon": GENERATOR.HORIZON_SECONDS,
        "settings": GENERATOR.fixture_settings(), "source_revision": "golden-fixture",
        "plane": GOLDEN_PLANE,
    }
    compile_routing_snapshot(inputs=inputs, **arguments)
    providers = dict(inputs.providers)
    providers["wafer"] = replace(providers["wafer"], provider_zero_data_retention=False)
    with pytest.raises(SnapshotRefused, match="provider wafer"):
        compile_routing_snapshot(inputs=replace(inputs, providers=providers), **arguments)
