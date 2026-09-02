from __future__ import annotations

import base64
import hashlib
import json

import cbor2
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.receipt_keys import (
    GCP_AUDIENCE,
    GCP_ISSUER,
    b64url_encode,
    receipt_key_commitment,
    verify_gcp_attestation_chain,
)
from trusted_router.services import receipt_key_collector as collector
from trusted_router.storage import InMemoryStore, configure_store
from trusted_router.storage_models import ReceiptKey


def _jwk(seed: bytes = b"receipt-key") -> dict[str, str]:
    return {
        "kty": "OKP",
        "crv": "Ed25519",
        "x": b64url_encode(hashlib.sha256(seed).digest()),
    }


def _kid(jwk: dict[str, str]) -> str:
    return collector.receipt_kid(jwk)


def _jwt(payload: dict[str, object]) -> str:
    header = b64url_encode(json.dumps({"alg": "RS256", "kid": "issuer-key"}).encode())
    body = b64url_encode(json.dumps(payload).encode())
    return f"{header}.{body}.c2ln"


def _signed_gcp_jwt(
    private_key: rsa.RSAPrivateKey,
    payload: dict[str, object],
) -> tuple[str, dict[str, object]]:
    header_segment = b64url_encode(
        json.dumps({"alg": "RS256", "kid": "issuer-key"}).encode()
    )
    payload_segment = b64url_encode(json.dumps(payload).encode())
    signing_input = f"{header_segment}.{payload_segment}".encode()
    signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    numbers = private_key.public_key().public_numbers()

    def integer_bytes(value: int) -> bytes:
        return value.to_bytes((value.bit_length() + 7) // 8, "big")

    jwks: dict[str, object] = {
        "keys": [
            {
                "kty": "RSA",
                "kid": "issuer-key",
                "n": b64url_encode(integer_bytes(numbers.n)),
                "e": b64url_encode(integer_bytes(numbers.e)),
            }
        ]
    }
    return f"{header_segment}.{payload_segment}.{b64url_encode(signature)}", jwks


def _gcp_payload(
    jwk: dict[str, str],
    *,
    kid: str | None = None,
    include_commitment: bool = True,
) -> dict[str, object]:
    nonces = [receipt_key_commitment(jwk).hex()] if include_commitment else ["00" * 32]
    return {
        "kid": kid or _kid(jwk),
        "jwk": jwk,
        "att": _jwt({"eat_nonce": nonces}),
        "att_kind": "gcp-cs-jwt",
    }


def _aws_payload(jwk: dict[str, str]) -> dict[str, object]:
    user_data = bytes(96) + receipt_key_commitment(jwk)
    cose = cbor2.dumps(
        [
            b"protected",
            {},
            cbor2.dumps({"user_data": user_data}),
            b"signature",
        ]
    )
    return {
        "kid": _kid(jwk),
        "jwk": jwk,
        "att": b64url_encode(cose),
        "att_kind": "aws-nitro-cose",
    }


def _azure_payload(jwk: dict[str, str]) -> dict[str, object]:
    runtime = base64.b64encode(
        json.dumps({"receipt_key_fp": receipt_key_commitment(jwk).hex()}).encode()
    ).decode()
    return {
        "kid": _kid(jwk),
        "jwk": jwk,
        "att": _jwt({"x-ms-runtime": runtime}),
        "att_kind": "azure-maa-jwt",
    }


def _one_target(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        collector,
        "discover_receipt_key_targets",
        lambda _settings: [collector.ReceiptKeyTarget("api.example", "192.0.2.10")],
    )


def test_discovery_resolves_canonical_and_regional_a_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    addresses = {
        "api.example": ["192.0.2.1", "192.0.2.2"],
        "regional-endpoint.example": ["198.51.100.7"],
    }

    def getaddrinfo(host: str, *_args, **_kwargs):
        return [
            (2, 1, 6, "", (address, 443))
            for address in addresses[host]
        ]

    monkeypatch.setattr(collector.socket, "getaddrinfo", getaddrinfo)
    settings = Settings(
        environment="test",
        api_base_url="https://api.example/v1",
        synthetic_gateway_region_targets=(
            "region-a=regional-endpoint.example@api-region-a.example"
        ),
    )

    assert collector.discover_receipt_key_targets(settings) == [
        collector.ReceiptKeyTarget("api-region-a.example", "198.51.100.7"),
        collector.ReceiptKeyTarget("api.example", "192.0.2.1"),
        collector.ReceiptKeyTarget("api.example", "192.0.2.2"),
    ]


def test_instance_fetch_connects_by_ip_with_gateway_sni_and_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class Response:
        content = b'{"kid":"sample"}'

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            return {"kid": "sample"}

    class Client:
        def __init__(self, **kwargs) -> None:
            captured["client"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def build_request(self, method: str, url: str, **kwargs):
            captured.update({"method": method, "url": url, **kwargs})
            return object()

        def send(self, _request: object) -> Response:
            return Response()

    monkeypatch.setattr(collector.httpx, "Client", Client)

    assert collector._fetch_receipt_key(  # noqa: SLF001 - transport contract
        collector.ReceiptKeyTarget("api.example", "192.0.2.10"),
        verify_tls=True,
    ) == {"kid": "sample"}
    assert captured["url"] == "https://192.0.2.10/receipt-key"
    assert captured["headers"] == {"Host": "api.example", "Accept": "application/json"}
    assert captured["extensions"] == {"sni_hostname": "api.example"}


@pytest.mark.parametrize(
    "payload",
    [
        _gcp_payload(_jwk(), kid="wrong-kid"),
        _gcp_payload(_jwk(), include_commitment=False),
    ],
    ids=["bad-kid", "commitment-absent"],
)
def test_collector_rejects_bad_key_material(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object],
) -> None:
    _one_target(monkeypatch)
    monkeypatch.setattr(collector, "_fetch_receipt_key", lambda *_args, **_kwargs: payload)
    monkeypatch.setattr(collector, "verify_gcp_attestation_chain", lambda _att: None)
    store = InMemoryStore()

    result = collector.collect_receipt_keys(
        Settings(environment="test", api_base_url="https://api.example/v1"),
        store=store,
    )

    assert result["errors"] == 1
    assert store.list_receipt_keys() == []


@pytest.mark.parametrize("payload", [_aws_payload(_jwk()), _azure_payload(_jwk())])
def test_unverifiable_chain_kind_is_logged_as_unverified(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object],
) -> None:
    _one_target(monkeypatch)
    monkeypatch.setattr(collector, "_fetch_receipt_key", lambda *_args, **_kwargs: payload)
    store = InMemoryStore()

    result = collector.collect_receipt_keys(
        Settings(environment="test", api_base_url="https://api.example/v1"),
        store=store,
    )

    assert result["appended"] == 1
    assert store.list_receipt_keys()[0].verified is False


def test_failed_gcp_chain_is_not_appended(monkeypatch: pytest.MonkeyPatch) -> None:
    _one_target(monkeypatch)
    payload = _gcp_payload(_jwk())
    monkeypatch.setattr(collector, "_fetch_receipt_key", lambda *_args, **_kwargs: payload)

    def reject(_att: str) -> None:
        raise ValueError("bad issuer signature")

    monkeypatch.setattr(collector, "verify_gcp_attestation_chain", reject)
    store = InMemoryStore()

    result = collector.collect_receipt_keys(
        Settings(environment="test", api_base_url="https://api.example/v1"),
        store=store,
    )

    assert result["errors"] == 1
    assert store.list_receipt_keys() == []


def test_gcp_chain_verifier_checks_signature_validity_and_debug_state() -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = 1_777_000_000
    claims: dict[str, object] = {
        "iss": GCP_ISSUER,
        "aud": GCP_AUDIENCE,
        "iat": now - 10,
        "nbf": now - 10,
        "exp": now + 60,
        "dbgstat": "disabled-since-boot",
    }
    token, jwks = _signed_gcp_jwt(private_key, claims)
    verify_gcp_attestation_chain(token, now=now, jwks=jwks)

    debug_token, _ = _signed_gcp_jwt(private_key, {**claims, "dbgstat": "enabled"})
    with pytest.raises(ValueError, match="debug status is enabled"):
        verify_gcp_attestation_chain(debug_token, now=now, jwks=jwks)

    expired_token, _ = _signed_gcp_jwt(private_key, {**claims, "exp": now - 301})
    with pytest.raises(ValueError, match="expired"):
        verify_gcp_attestation_chain(expired_token, now=now, jwks=jwks)


def test_good_key_appends_once_and_reobservation_only_advances_last_seen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _one_target(monkeypatch)
    payload = _gcp_payload(_jwk())
    monkeypatch.setattr(collector, "_fetch_receipt_key", lambda *_args, **_kwargs: payload)
    monkeypatch.setattr(collector, "verify_gcp_attestation_chain", lambda _att: None)
    seen = iter(["2026-08-26T00:00:00Z", "2026-08-26T00:05:00Z"])
    monkeypatch.setattr(collector, "iso_now", lambda: next(seen))
    store = InMemoryStore()
    settings = Settings(environment="test", api_base_url="https://api.example/v1")

    first = collector.collect_receipt_keys(settings, store=store)
    second = collector.collect_receipt_keys(settings, store=store)

    assert first["appended"] == 1
    assert second["appended"] == 0
    assert second["refreshed"] == 1
    assert len(store.list_receipt_keys()) == 1
    row = store.list_receipt_keys()[0]
    assert row.first_seen == "2026-08-26T00:00:00Z"
    assert row.last_seen == "2026-08-26T00:05:00Z"
    assert row.verified is True


def test_public_routes_narrow_receipt_key_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    jwk = _jwk()
    record = ReceiptKey(
        kid=_kid(jwk),
        jwk={**jwk, "d": "private-material-must-not-escape"},
        att="public-attestation",
        att_kind="gcp-cs-jwt",
        plane="api.example",
        first_seen="2026-08-26T00:00:00Z",
        last_seen="2026-08-26T00:05:00Z",
        verified=True,
    )
    store = InMemoryStore()
    configure_store(store)
    monkeypatch.setattr(InMemoryStore, "list_receipt_keys", lambda _self, *, limit: [record])
    client = TestClient(
        create_app(
            Settings(environment="test"),
            configure_store_arg=False,
            init_observability=False,
        )
    )

    well_known = client.get("/.well-known/inference-receipt-keys")
    mirror = client.get("/trust/receipt-keys.json")

    assert well_known.status_code == 200
    assert mirror.status_code == 200
    payload = well_known.json()
    assert payload["spec"] == "inference-receipt/1"
    assert payload["degraded"] is False
    assert payload["keys"] == mirror.json()["keys"]
    assert set(payload["keys"][0]) == {
        "kid",
        "jwk",
        "att",
        "att_kind",
        "plane",
        "first_seen",
        "last_seen",
        "revoked",
        "verified",
    }
    assert payload["keys"][0]["jwk"] == jwk

    def unavailable(_self, *, limit: int):
        raise RuntimeError(f"storage unavailable at limit {limit}")

    monkeypatch.setattr(InMemoryStore, "list_receipt_keys", unavailable)
    degraded = client.get("/.well-known/inference-receipt-keys")
    assert degraded.status_code == 200
    assert degraded.json()["degraded"] is True
    assert degraded.json()["keys"] == payload["keys"]


def test_scheduler_route_rejects_anonymous() -> None:
    client = TestClient(
        create_app(
            Settings(
                environment="test",
                internal_gateway_token="receipt-collector-test-token",  # noqa: S106
            ),
            configure_store_arg=False,
            init_observability=False,
        )
    )

    response = client.post("/internal/gateway/receipt-keys/collect")

    assert response.status_code == 401


def test_scheduler_route_collects_and_records_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trusted_router.routes.internal import gateway

    heartbeats: list[str] = []
    monkeypatch.setattr(
        gateway,
        "collect_receipt_keys",
        lambda _settings: {
            "discovered": 1,
            "fetched": 1,
            "appended": 1,
            "refreshed": 0,
            "skipped": 0,
            "errors": 0,
        },
    )
    monkeypatch.setattr(
        gateway,
        "record_heartbeat",
        lambda name, *, settings: heartbeats.append(name),
    )
    token = "receipt-collector-test-token"  # noqa: S105
    client = TestClient(
        create_app(
            Settings(environment="test", internal_gateway_token=token),
            configure_store_arg=False,
            init_observability=False,
        )
    )

    response = client.post(
        "/v1/internal/gateway/receipt-keys/collect",
        headers={"x-trustedrouter-internal-token": token},
    )

    assert response.status_code == 200
    assert response.json()["appended"] == 1
    assert heartbeats == ["job:receipt-key-collector"]
