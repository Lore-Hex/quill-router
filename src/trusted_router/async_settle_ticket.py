"""Separate-purpose, detached async settlement tickets. No key IO at request time."""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import Field

from trusted_router.billing_snapshot import Digest, Frozen, Identity, UInt
from trusted_router.speculation_protocol import (
    TrustedKey,
    _b64encode,
    _canonical,
    _verify,
)
from trusted_router.storage_models import generation_id_for_authorization

PURPOSE = "async-settle-ticket"
TYP = "tr-async-settle-v1"
NativeIdentity = Annotated[Identity, Field(max_length=64)]


class TicketClaims(Frozen):
    authorization_id: NativeIdentity
    generation_id: Identity
    workspace_id: NativeIdentity
    key_id: NativeIdentity
    invocation_nonce: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
    billing_authority: Literal["local"]
    journal_region: Identity
    epoch: UInt
    snapshot_version: Literal[1]
    snapshot_hash: Digest
    route_type: Literal["chat.completions", "responses"]
    streamed: Annotated[bool, Field(strict=True)]
    reservation_id: NativeIdentity
    settle_origin: Literal["typed"]
    async_eligible: Annotated[bool, Field(strict=True)]
    iss: Identity
    aud: Identity
    iat: UInt
    exp: UInt


def validate_claims(claims: Mapping[str, Any], now: int) -> TicketClaims:
    parsed = TicketClaims.model_validate(dict(claims))
    if (parsed.epoch < 1 or not parsed.iat <= now < parsed.exp
            or parsed.generation_id != generation_id_for_authorization(parsed.authorization_id)):
        raise ValueError("ticket validity")
    return parsed


def verify_ticket(token: str, keys: Sequence[TrustedKey], expected: Mapping[str, Any],
                  now: int) -> TicketClaims:
    """Verify every claim against caller-owned context, including authority epoch.

    No partial binding API: future settlement callers must supply all claims.
    Expired-ticket lookup (PR C) must not use this fresh-acceptance validator.
    """
    claims, key, payload = _verify(token, keys, TYP, PURPOSE)
    parsed = validate_claims(claims, now)
    if payload != _canonical(claims) or claims["iss"] != key.iss or claims["aud"] != key.aud:
        raise ValueError("ticket issuer or encoding")
    # Canonical bytes preserve bool vs integer distinctions and reject missing,
    # extra, or altered bindings; equality of just the snapshot hash is unsafe.
    if set(expected) != set(TicketClaims.model_fields) or _canonical(dict(expected)) != payload:
        raise ValueError("ticket binding")
    return parsed


class TicketSigner:
    def __init__(self, private: Ed25519PrivateKey, trusted: TrustedKey) -> None:
        if (trusted.purpose != PURPOSE or not all((trusted.kid, trusted.iss, trusted.aud))
                or _b64encode(private.public_key().public_bytes_raw()) != trusted.public_key_b64url
                or any(re.fullmatch(r"[A-Za-z0-9_./:@+\-]{1,512}", v) is None
                       for v in (trusted.kid, trusted.iss, trusted.aud))):
            raise ValueError("ticket key configuration")
        self.private, self.trusted = private, trusted

    def sign(self, claims: dict[str, Any], now: int) -> str:
        validate_claims(claims, now)
        header = {"alg": "EdDSA", "kid": self.trusted.kid, "typ": TYP}
        material = _b64encode(_canonical(header)) + "." + _b64encode(_canonical(claims))
        token = material + "." + _b64encode(self.private.sign(material.encode("ascii")))
        verify_ticket(token, [self.trusted], claims, now)
        return token
