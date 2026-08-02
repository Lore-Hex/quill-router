"""_attestation_evidence must understand BOTH live attestation formats.

GCP Confidential Space returns a JWT; AWS Nitro returns a binary
COSE_Sign1/CBOR document. The parser originally only knew the JWT shape,
so every probe against the AWS enclave (api-aws.trustedrouter.com)
reported unsupported_attestation_format and the EU status page sat at 50%
"trust degraded" while the enclave itself was verifiably healthy.
"""

from __future__ import annotations

import base64
import json

import cbor2

from trusted_router.synthetic.probes import _attestation_evidence

NONCE = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
PCR0 = bytes.fromhex("2c12e22215d4269d1a788063d59fc5a8cf565b92bf1bee1761d7651b0b9ca513" + "9274504adf001ca6626c7108fafc6883")


def _cose_sign1(payload: dict[object, object], *, tag: bool = False) -> bytes:
    """Build the COSE_Sign1 array shape the Nitro Security Module emits.

    Signature bytes are fake — _attestation_evidence checks nonce binding
    and extracts PCR0; chain verification is the deploy gate's job.
    """
    document = [b"\xa1\x01\x38\x22", {}, cbor2.dumps(payload), b"sig" * 32]
    if tag:
        return cbor2.dumps(cbor2.CBORTag(18, document))
    return cbor2.dumps(document)


def _aws_payload(nonce_hex: str = NONCE) -> dict[object, object]:
    return {
        "module_id": "i-00abc-enc0123",
        "digest": "SHA384",
        "timestamp": 1754000000000,
        "pcrs": {0: PCR0, 1: b"\x01" * 48, 2: b"\x02" * 48},
        "certificate": b"fake-der",
        "cabundle": [b"fake-root"],
        "nonce": bytes.fromhex(nonce_hex),
        "user_data": b"\x00" * 32,
    }


def _jwt(claims: dict[str, object]) -> bytes:
    header = base64.urlsafe_b64encode(json.dumps({"alg": "RS256"}).encode()).rstrip(b"=")
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=")
    return header + b"." + body + b".c2lnbmF0dXJl"


class TestAwsCose:
    def test_nonce_binding_passes(self) -> None:
        evidence = _attestation_evidence(_cose_sign1(_aws_payload()), NONCE)
        assert evidence["nonce_ok"] is True
        assert evidence["error_type"] is None

    def test_pcr0_reported_as_attestation_digest(self) -> None:
        evidence = _attestation_evidence(_cose_sign1(_aws_payload()), NONCE)
        assert evidence["attestation_digest"] == PCR0.hex()

    def test_tagged_cose_sign1_accepted(self) -> None:
        evidence = _attestation_evidence(_cose_sign1(_aws_payload(), tag=True), NONCE)
        assert evidence["nonce_ok"] is True

    def test_wrong_nonce_is_nonce_missing_not_unsupported(self) -> None:
        evidence = _attestation_evidence(_cose_sign1(_aws_payload("ff" * 16)), NONCE)
        assert evidence["nonce_ok"] is False
        assert evidence["error_type"] == "nonce_missing"
        # A replayed document still reveals its measurement.
        assert evidence["attestation_digest"] == PCR0.hex()

    def test_absent_nonce_field(self) -> None:
        payload = _aws_payload()
        del payload["nonce"]
        evidence = _attestation_evidence(_cose_sign1(payload), NONCE)
        assert evidence["nonce_ok"] is False
        assert evidence["error_type"] == "nonce_missing"

    def test_nonce_uppercase_hex_input_still_binds(self) -> None:
        evidence = _attestation_evidence(_cose_sign1(_aws_payload()), NONCE.upper())
        assert evidence["nonce_ok"] is True

    def test_binary_body_with_dot_bytes_not_misrouted_to_jwt(self) -> None:
        """A CBOR document containing '.' (0x2E) bytes must still parse as
        CBOR. The pre-fix code did a lossy decode and counted dots, so a
        payload like this one was routed down the JWT path and reported
        unsupported_attestation_format."""
        payload = _aws_payload()
        payload["module_id"] = "i-...dots.every.where..."
        evidence = _attestation_evidence(_cose_sign1(payload), NONCE)
        assert evidence["nonce_ok"] is True


class TestGcpJwtUnchanged:
    def test_eat_nonce_binds(self) -> None:
        evidence = _attestation_evidence(_jwt({"eat_nonce": [NONCE], "image_digest": "sha256:abc"}), NONCE)
        assert evidence["nonce_ok"] is True
        assert evidence["attestation_digest"] == "sha256:abc"

    def test_missing_nonce(self) -> None:
        evidence = _attestation_evidence(_jwt({"eat_nonce": ["deadbeef"]}), NONCE)
        assert evidence["error_type"] == "nonce_missing"


class TestUnsupported:
    def test_garbage_is_unsupported(self) -> None:
        evidence = _attestation_evidence(b"not an attestation at all", NONCE)
        assert evidence["error_type"] == "unsupported_attestation_format"

    def test_error_json_body_is_unsupported(self) -> None:
        # What a 404 "route not found" body looks like to the parser.
        evidence = _attestation_evidence(b'{"error":{"message":"route not found"}}', NONCE)
        assert evidence["error_type"] == "unsupported_attestation_format"

    def test_cose_with_non_map_payload_is_unsupported(self) -> None:
        evidence = _attestation_evidence(cbor2.dumps([b"p", {}, cbor2.dumps([1, 2]), b"s"]), NONCE)
        assert evidence["error_type"] == "unsupported_attestation_format"

    def test_truncated_cbor_is_unsupported(self) -> None:
        evidence = _attestation_evidence(_cose_sign1(_aws_payload())[:20], NONCE)
        assert evidence["error_type"] == "unsupported_attestation_format"
