"""Non-authorizing, separate-purpose proof of authorize-time billing prices."""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import replace
from typing import Annotated, Any, Literal

from pydantic import Field, ValidationError

from trusted_router.async_settle_ticket import TicketClaims, TicketSigner
from trusted_router.detached_jws import TrustedKey, b64encode, canonical, verify
from trusted_router.storage_models import generation_id_for_authorization

PURPOSE = "async-settle-shadow"
TYP = "tr-async-settle-shadow-v1"
AUDIENCE = "router-shadow"
LIFETIME = 172800
FIXTURE_SHA256 = "68568699b000210413a20916ffa1f56093ab0223bb6a65d935ae453a9d96a28a"


class ShadowClaims(TicketClaims):
    authorization_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
    generation_id: Annotated[str, Field(max_length=128)]
    journal_region: Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_./:@+\-]+$")]
    aud: Literal["router-shadow"]
    async_eligible: Literal[False]


def validate_claims(claims: dict[str, Any], now: int) -> ShadowClaims:
    try:
        parsed = ShadowClaims.model_validate(claims)
    except ValidationError as exc:
        raise ValueError("proof_signature") from exc
    if (type(claims.get("async_eligible")) is not bool or parsed.epoch < 1
            or parsed.exp - parsed.iat != LIFETIME
            or parsed.generation_id != generation_id_for_authorization(parsed.authorization_id)):
        raise ValueError("proof_signature")
    if not parsed.iat <= now < parsed.exp:
        raise ValueError("proof_expired")
    return parsed


def verify_binding(token: str, keys: Sequence[TrustedKey], now: int) -> ShadowClaims:
    if not token.isascii() or len(token) > 2048:
        raise ValueError("proof_signature")
    try:
        claims, key, payload = verify(token, keys, TYP, PURPOSE)
        if payload != canonical(claims) or claims.get("iss") != key.iss or key.aud != AUDIENCE:
            raise ValueError("proof_signature")
    except Exception as exc:
        raise ValueError("proof_signature") from exc
    return validate_claims(claims, now)


class ShadowSigner:
    """Reuse loaded material, never the ticket signing API or key descriptor."""

    def __init__(self, ticket: TicketSigner) -> None:
        self.private = ticket.private
        self.trusted = replace(ticket.trusted, purpose=PURPOSE, aud=AUDIENCE)

    def sign(self, claims: dict[str, Any], now: int) -> str:
        validate_claims(claims, now)
        if claims["iss"] != self.trusted.iss or not re.fullmatch(r"[A-Za-z0-9_./:@+\-]+", self.trusted.kid):
            raise ValueError("proof_signature")
        material = b64encode(canonical({"alg": "EdDSA", "kid": self.trusted.kid, "typ": TYP})) + "." + b64encode(canonical(claims))
        token = material + "." + b64encode(self.private.sign(material.encode("ascii")))
        if len(token) > 2048:
            raise ValueError("proof_signature")
        return token
