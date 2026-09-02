"""Append-only receipt-key semantics shared by every storage backend."""

from __future__ import annotations

import base64
import hashlib
import json
import logging

from trusted_router.receipt_keys import b64url_encode, receipt_key_commitment
from trusted_router.storage_models import ReceiptKey
from trusted_router.store_protocol import Store


def _jwk(seed: str) -> dict[str, str]:
    public_key = hashlib.sha256(seed.encode()).digest()
    return {"kty": "OKP", "crv": "Ed25519", "x": b64url_encode(public_key)}


def _kid(jwk: dict[str, str]) -> str:
    raw = base64.urlsafe_b64decode(jwk["x"] + "==")
    return b64url_encode(hashlib.sha256(raw).digest())


def _gcp_att(jwk: dict[str, str], marker: str) -> str:
    header = b64url_encode(json.dumps({"alg": "RS256"}).encode())
    payload = b64url_encode(
        json.dumps(
            {
                "eat_nonce": [receipt_key_commitment(jwk).hex()],
                "marker": marker,
            }
        ).encode()
    )
    return f"{header}.{payload}.c2ln"


def test_receipt_key_log_is_append_only(
    store: Store,
    unique: str,
    caplog,
) -> None:
    jwk = _jwk(unique)
    kid = _kid(jwk)
    first = ReceiptKey(
        kid=kid,
        jwk=jwk,
        att=_gcp_att(jwk, "first"),
        att_kind="gcp-cs-jwt",
        plane="api.example",
        first_seen="2026-08-26T00:00:00Z",
        last_seen="2026-08-26T00:00:00Z",
        verified=False,
    )
    refreshed = ReceiptKey(
        kid=kid,
        jwk={**jwk, "d": "must-never-be-stored"},
        att=_gcp_att(jwk, "refreshed"),
        att_kind="gcp-cs-jwt",
        plane="attacker.example",
        first_seen="2099-01-01T00:00:00Z",
        last_seen="2026-08-26T00:05:00Z",
        verified=True,
    )

    assert store.observe_receipt_key(first) == "appended"
    assert store.observe_receipt_key(refreshed) == "refreshed"

    row = next(item for item in store.list_receipt_keys() if item.kid == kid)
    assert row.jwk == jwk
    assert row.att == refreshed.att
    assert row.plane == first.plane
    assert row.first_seen == first.first_seen
    assert row.last_seen == refreshed.last_seen
    assert row.verified is True

    collision = ReceiptKey(
        kid=kid,
        jwk=_jwk(f"{unique}-different"),
        att=_gcp_att(_jwk(f"{unique}-different"), "collision"),
        att_kind="gcp-cs-jwt",
        plane="api.example",
        first_seen="2026-08-26T00:10:00Z",
        last_seen="2026-08-26T00:10:00Z",
    )
    with caplog.at_level(logging.ERROR, logger="trusted_router.receipt_keys"):
        assert store.observe_receipt_key(collision) == "conflict"
    assert "ALERT receipt_key_kid_collision" in caplog.text
    assert next(item for item in store.list_receipt_keys() if item.kid == kid) == row
