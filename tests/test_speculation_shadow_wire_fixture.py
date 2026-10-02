"""PR 3 wire pin: the enclave (quill-cloud-proxy PR 4) pins the same literal bytes.

tests/fixtures/speculation_v1/shadow-refresh-wire.json was produced through the
real route. Any change to these bytes is a wire-contract change: regenerate the
fixture in both repositories deliberately, never adjust one side to pass.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tests.test_speculation_shadow import client, facts, ready_service
from trusted_router.config import Settings
from trusted_router.gateway_boot import boot_auth_digest
from trusted_router.services import speculation_shadow as shadow
from trusted_router.speculation_protocol import TrustedKey, verify_grant
from trusted_router.storage_models import GatewayBoot

FIXTURE = Path(__file__).parent / "fixtures/speculation_v1/shadow-refresh-wire.json"
WIRE = json.loads(FIXTURE.read_text())
PATH = WIRE["path"]


def enc(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def registered(service):
    boot = WIRE["boot"]
    service.store.boots[boot["kid"]] = GatewayBoot(
        kid=boot["kid"],
        jwk=boot["jwk"],
        approved=False,
        verified=True,
        image_digest=boot["image_digest"],
        attestation_kind="gcp",
        registered_at="2026-01-01T00:00:00Z",
    )
    return service


def test_request_bytes_and_boot_signature_reproduce_from_the_fixture_seed():
    body = shadow.canonical({"items": WIRE["items"]})
    assert body.decode() == WIRE["request_body_exact"]
    assert hashlib.sha256(body).hexdigest() == WIRE["request_body_sha256"]
    private = Ed25519PrivateKey.from_private_bytes(
        bytes.fromhex(WIRE["boot"]["test_only_private_seed_hex"])
    )
    assert (
        enc(private.public_key().public_bytes_raw())
        == WIRE["boot"]["public_key_b64url"]
        == WIRE["boot"]["jwk"]["x"]
    )
    signature = enc(private.sign(boot_auth_digest(WIRE["method"], PATH, body)))
    assert WIRE["boot_auth_header"] == f"kid={WIRE['boot']['kid']},sig={signature}"


def test_frozen_grant_verifies_as_shadow_with_the_frozen_key_and_context():
    keys = [TrustedKey(**k) for k in WIRE["trusted_test_keys"]]
    assert [k.purpose for k in keys] == ["shadow-grant"]
    verified = verify_grant(WIRE["grant_jws"], keys, WIRE["context"], WIRE["now"], shadow=True)
    assert verified.shadow and verified.claims == WIRE["grant_claims"]
    with pytest.raises(ValueError, match="type"):
        verify_grant(WIRE["grant_jws"], keys, WIRE["context"], WIRE["now"])
    envelope = json.loads(WIRE["response_body_exact"])
    assert envelope == {
        "items": [{**WIRE["items"][0], "grant": WIRE["grant_jws"]}],
        "authority": "shadow-only",
    }


def test_live_route_reproduces_the_success_envelope_bytes_and_claims():
    service = registered(ready_service())
    with patch.object(shadow.time, "time", lambda: WIRE["now"]):
        response = client(service.settings, service).post(
            PATH,
            content=WIRE["request_body_exact"].encode(),
            headers={"X-TR-Boot-Auth": WIRE["boot_auth_header"]},
        )
    assert response.status_code == WIRE["response_status"] == 200
    token = json.loads(response.content)["items"][0]["grant"]
    # Only the signature differs: this run's issuer key is ephemeral. Every
    # byte around it, key order included, is exact.
    assert (
        response.content.decode().replace(token, WIRE["grant_jws"]) == WIRE["response_body_exact"]
    )
    claims = verify_grant(
        token, [service.signer.trusted], WIRE["context"], WIRE["now"], shadow=True
    ).claims
    assert claims == WIRE["grant_claims"]


@pytest.mark.parametrize("case", WIRE["misses"], ids=[m["name"] for m in WIRE["misses"]])
def test_every_frozen_miss_reproduces_status_and_body_bytes(case):
    body = case["request_body_exact"].encode()
    headers = {"X-TR-Boot-Auth": case["boot_auth_header"]} if case["boot_auth_header"] else {}
    if "feature-disabled" in case["name"]:
        responses = [
            client(Settings(environment="test"), Mock()).post(PATH, content=body, headers=headers)
        ]
    else:
        service = registered(ready_service())
        if "start-window-exhausted" in case["name"]:
            service.store.facts[WIRE["items"][0]["lookup_digest"]] = {
                **facts(),
                "trust_fresh_until": 2011,
            }
        with patch.object(shadow.time, "time", lambda: case["now"]):
            c = client(service.settings, service)
            responses = [c.post(PATH, content=body, headers=headers)]
            if "refresh-rate-limited" in case["name"]:
                assert responses[0].status_code == 200
                responses.append(c.post(PATH, content=body, headers=headers))
    response = responses[-1]
    assert (response.status_code, response.content.decode()) == (
        case["response_status"],
        case["response_body_exact"],
    )
