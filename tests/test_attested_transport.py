"""Attested-cert-only probe transport (SyntheticTarget.attested).

An attested target serves a self-signed cert minted inside the TEE, so CA
verification is replaced by a stronger check: the attestation document
must bind the exact cert served on the probe's own TLS connection (SPKI +
sha256 fingerprint), and optionally pin PCR0. These tests build real cert
material so the binding math is exercised for real, not mocked.
"""

from __future__ import annotations

import datetime as dt
import hashlib

import cbor2
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from cryptography.x509.oid import NameOID

from trusted_router.config import Settings
from trusted_router.synthetic.probes import (
    SyntheticTarget,
    _attestation_evidence,
    _attested_ssl_context,
    _aws_cert_binding_ok,
    configured_targets,
)

NONCE = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
PCR0 = bytes(range(48))


def _self_signed_cert_der(cn: str = "api-aws.trustedrouter.com") -> bytes:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(Encoding.DER)


def _spki(cert_der: bytes) -> bytes:
    return (
        x509.load_der_x509_certificate(cert_der)
        .public_key()
        .public_bytes(encoding=Encoding.DER, format=PublicFormat.SubjectPublicKeyInfo)
    )


def _cose(payload: dict[object, object]) -> bytes:
    return cbor2.dumps([b"\xa1\x01\x38\x22", {}, cbor2.dumps(payload), b"sig" * 32])


def _payload(cert_der: bytes | None, *, nonce_hex: str = NONCE, pcr0: bytes = PCR0) -> dict[object, object]:
    payload: dict[object, object] = {
        "module_id": "i-test-enc0",
        "digest": "SHA384",
        "pcrs": {0: pcr0},
        "nonce": bytes.fromhex(nonce_hex),
    }
    if cert_der is not None:
        payload["public_key"] = _spki(cert_der)
        payload["user_data"] = hashlib.sha256(cert_der).digest()
    return payload


class TestCertBinding:
    def test_bound_cert_passes(self) -> None:
        cert = _self_signed_cert_der()
        evidence = _attestation_evidence(_cose(_payload(cert)), NONCE, peer_cert_der=cert)
        assert evidence["error_type"] is None

    def test_substituted_cert_fails_as_cert_binding_mismatch(self) -> None:
        served = _self_signed_cert_der()
        relay = _self_signed_cert_der("relay.example.com")
        evidence = _attestation_evidence(_cose(_payload(served)), NONCE, peer_cert_der=relay)
        assert evidence["error_type"] == "cert_binding_mismatch"

    def test_document_binding_nothing_fails(self) -> None:
        """An attested target whose document binds no cert at all must not
        read as verified — unverifiable is a failure state."""
        cert = _self_signed_cert_der()
        evidence = _attestation_evidence(_cose(_payload(None)), NONCE, peer_cert_der=cert)
        assert evidence["error_type"] == "cert_binding_mismatch"

    def test_spki_match_with_wrong_fingerprint_fails(self) -> None:
        cert = _self_signed_cert_der()
        payload = _payload(cert)
        payload["user_data"] = b"\xff" * 32
        evidence = _attestation_evidence(_cose(payload), NONCE, peer_cert_der=cert)
        assert evidence["error_type"] == "cert_binding_mismatch"

    def test_no_peer_cert_skips_binding_gcp_path_unchanged(self) -> None:
        cert = _self_signed_cert_der()
        evidence = _attestation_evidence(_cose(_payload(cert)), NONCE, peer_cert_der=None)
        assert evidence["error_type"] is None

    def test_binding_helper_rejects_garbage_der(self) -> None:
        cert = _self_signed_cert_der()
        assert _aws_cert_binding_ok({"public_key": _spki(cert)}, b"not a certificate") is False


class TestPcr0Pin:
    def test_matching_pin_passes(self) -> None:
        cert = _self_signed_cert_der()
        evidence = _attestation_evidence(
            _cose(_payload(cert)), NONCE, peer_cert_der=cert, expected_pcr0=PCR0.hex()
        )
        assert evidence["error_type"] is None

    def test_pin_is_case_insensitive(self) -> None:
        evidence = _attestation_evidence(_cose(_payload(None)), NONCE, expected_pcr0=PCR0.hex().upper())
        assert evidence["error_type"] is None

    def test_wrong_measurement_is_pcr0_mismatch(self) -> None:
        cert = _self_signed_cert_der()
        evidence = _attestation_evidence(
            _cose(_payload(cert)), NONCE, peer_cert_der=cert, expected_pcr0="ab" * 48
        )
        assert evidence["error_type"] == "pcr0_mismatch"
        # The digest is still reported so the operator sees WHAT is running.
        assert evidence["attestation_digest"] == PCR0.hex()

    def test_nonce_failure_outranks_pcr0(self) -> None:
        evidence = _attestation_evidence(
            _cose(_payload(None, nonce_hex="ff" * 16)), NONCE, expected_pcr0="ab" * 48
        )
        assert evidence["error_type"] == "nonce_missing"


class TestTargetPlumbing:
    def test_attested_context_disables_verification(self) -> None:
        import ssl

        context = _attested_ssl_context()
        assert context.verify_mode == ssl.CERT_NONE
        assert context.check_hostname is False

    def test_configured_targets_carry_attested_flag(self) -> None:
        settings = Settings(
            environment="test",
            sentry_dsn=None,
            synthetic_canonical_attested=True,
            attestation_expected_pcr0="2c" * 48,
        )
        canonical = configured_targets(settings)[0]
        assert canonical.attested is True
        assert canonical.expected_pcr0 == "2c" * 48

    def test_default_targets_are_not_attested(self) -> None:
        settings = Settings(environment="test", sentry_dsn=None)
        assert all(t.attested is False for t in configured_targets(settings))
        assert all(t.expected_pcr0 is None for t in configured_targets(settings))

    def test_standalone_topology_is_one_canonical_target(self) -> None:
        """Regional alias probing is GCP topology. On a standalone
        deployment the template fabricates a hostname that does not exist
        (api-eu-west-3.quillrouter.com), which would sit permanently down on
        the status page."""
        settings = Settings(
            environment="test",
            sentry_dsn=None,
            api_base_url="https://api-aws.trustedrouter.com/v1",
            primary_region="eu-west-3",
            regions="eu-west-3",
            synthetic_regional_probes_enabled=False,
            synthetic_control_plane_health_url="https://paris.example.com",
        )
        targets = configured_targets(settings)
        assert [t.name for t in targets] == ["canonical"]
        assert targets[0].control_plane_url == "https://paris.example.com"

    def test_gcp_topology_unchanged_by_standalone_settings(self) -> None:
        settings = Settings(environment="test", sentry_dsn=None)
        targets = configured_targets(settings)
        names = [target.name for target in targets]
        assert names[0] == "canonical"
        assert len(names) > 1  # regional targets still present by default
        assert all(target.control_plane_url is None for target in targets)

    def test_regional_health_targets_configured_private_billing_origin(self) -> None:
        origin = (
            "https://trusted-router-billing-44325983244.europe-west4.run.app"
        )
        settings = Settings(
            environment="test",
            sentry_dsn=None,
            regions="us-central1,europe-west4",
            synthetic_monitor_region="europe-west4",
            synthetic_control_plane_health_url=origin,
        )

        targets = configured_targets(settings)

        assert [
            (target.name, target.control_plane_url)
            for target in targets
            if target.control_plane_url is not None
        ] == [("europe-west4", origin)]
        assert all(
            target.control_plane_url is None
            or "trusted-router-billing-" in target.control_plane_url
            for target in targets
        )

    @pytest.mark.parametrize(
        "origin",
        [
            "https://trusted-router-44325983244.europe-west4.run.app",
            "https://trusted-router-console-44325983244.europe-west4.run.app",
            "https://trusted-router-public-44325983244.europe-west4.run.app",
            "https://trusted-router-billing-44325983244.us-central1.run.app",
            "https://trusted-router-billing-44325983244.europe-west4.run.app/health",
            "https://trusted-router-billing-44325983244.europe-west4.run.app?probe=1",
            "http://trusted-router-billing-44325983244.europe-west4.run.app",
            "https://billing.example.com",
        ],
    )
    def test_regional_health_rejects_non_private_billing_origin(self, origin: str) -> None:
        settings = Settings(
            environment="test",
            sentry_dsn=None,
            regions="us-central1,europe-west4",
            synthetic_monitor_region="europe-west4",
            synthetic_control_plane_health_url=origin,
        )

        with pytest.raises(
            ValueError,
            match=r"\$\{SERVICE\}-billing Cloud Run origin",
        ):
            configured_targets(settings)

    def test_target_defaults_are_backward_compatible(self) -> None:
        target = SyntheticTarget("canonical", "https://api.trustedrouter.com/v1")
        assert target.attested is False
        assert target.expected_pcr0 is None


@pytest.mark.parametrize("field", ["public_key", "user_data"])
def test_single_binding_field_alone_is_sufficient(field: str) -> None:
    """The enclave publishes two bindings; either one alone must still
    bind (older enclave builds populated only user_data)."""
    cert = _self_signed_cert_der()
    full = _payload(cert)
    partial: dict[object, object] = {k: v for k, v in full.items() if k != field}
    evidence = _attestation_evidence(_cose(partial), NONCE, peer_cert_der=cert)
    assert evidence["error_type"] is None


def test_evidence_kwargs_default_to_prior_behavior() -> None:
    """No peer cert, no pin — byte-identical semantics to the pre-change
    parser for both formats (regression fence for GCP)."""
    evidence = _attestation_evidence(_cose(_payload(None)), NONCE)
    assert evidence == {
        "nonce_ok": True,
        "error_type": None,
        "attestation_digest": PCR0.hex(),
        "source_commit": None,
    }


class TestHealthReachability:
    """The latency-phase probes measure DNS/TCP/TLS/first-byte, not
    authorization. The AWS Nitro gateway answers /health with 401 (it
    protects every route but /attestation) where GCP answers 200 —
    without treating that 401 as reachable, tls_health reported `up`
    and the two latency probes reported `down` for the SAME response.
    """

    @pytest.mark.parametrize(
        ("status", "body", "expected"),
        [
            (200, b'{"status":"ok"}', True),
            (401, b'{"error":{"message":"Invalid API key","status":401}}', True),
            (401, b'{"error":{"message":"rate limited"}}', False),
            (401, b"not json", False),
            (500, b"boom", False),
            (200, b"garbage", False),
            (503, b'{"error":{"message":"Invalid API key"}}', False),
        ],
    )
    def test_reachability(self, status: int, body: bytes, expected: bool) -> None:
        from trusted_router.synthetic.probes import SyntheticTarget, _warm_path_reachable

        target = SyntheticTarget("canonical", "https://api.trustedrouter.com/v1")
        headers = {"content-type": "application/json"}
        assert _warm_path_reachable(target, status, headers, body) is expected

    @pytest.mark.parametrize(
        ("status", "body", "expected"),
        [
            (200, b'{"status":"ok"}', True),
            # Reachable-but-rejected must NOT license the reuse verdict:
            # reuse is only measurable on a request the gateway served.
            (401, b'{"error":{"message":"Invalid API key","status":401}}', False),
            (500, b"boom", False),
        ],
    )
    def test_served_does_not_tolerate_a_rejected_request(
        self, status: int, body: bytes, expected: bool
    ) -> None:
        from trusted_router.synthetic.probes import SyntheticTarget, _warm_path_served

        target = SyntheticTarget("canonical", "https://api.trustedrouter.com/v1")
        headers = {"content-type": "application/json"}
        assert _warm_path_served(target, status, headers, body) is expected
