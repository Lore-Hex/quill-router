"""Spanner-backed one-shot verification tokens.

Sibling of InMemoryVerificationTokens (storage_verification_tokens.py).
Used today by the wallet sign-in email-confirmation flow; reusable for
password reset / email change."""

from __future__ import annotations

import datetime as dt
import secrets
from typing import Any

from trusted_router.security import (
    hash_api_key,
    lookup_hash_api_key,
    new_hash_salt,
    new_key_id,
    verify_api_key,
)
from trusted_router.storage_gcp_io import SpannerIO
from trusted_router.storage_models import VerificationToken, _is_expired, iso_now, utcnow


class SpannerVerificationTokens:
    def __init__(self, io: SpannerIO) -> None:
        self._io = io

    def create(
        self,
        *,
        user_id: str,
        purpose: str,
        ttl_seconds: int,
    ) -> tuple[str, VerificationToken]:
        raw = secrets.token_urlsafe(32)
        token_id = new_key_id(prefix="verify")
        salt = new_hash_salt()
        lookup_hash = lookup_hash_api_key(raw)
        expires_at = (
            utcnow() + dt.timedelta(seconds=max(ttl_seconds, 60))
        ).isoformat().replace("+00:00", "Z")
        token = VerificationToken(
            hash=token_id,
            salt=salt,
            secret_hash=hash_api_key(raw, salt),
            lookup_hash=lookup_hash,
            user_id=user_id,
            purpose=purpose,
            expires_at=expires_at,
        )
        with self._io.database.batch() as batch:
            self._io.write_entity_batch(batch, "verification_token", token.hash, token)
            self._io.write_entity_batch(
                batch, "verification_token_lookup", lookup_hash, {"token_id": token.hash}
            )
        return raw, token

    def consume(
        self, raw_token: str, *, purpose: str
    ) -> VerificationToken | None:
        lookup_hash = lookup_hash_api_key(raw_token)

        def txn(transaction: Any) -> VerificationToken | None:
            lookup = self._io.read_entity_tx(transaction, "verification_token_lookup", lookup_hash, dict)
            if not lookup:
                return None
            token = self._io.read_entity_tx(
                transaction, "verification_token", str(lookup["token_id"]), VerificationToken
            )
            if token is None:
                return None
            if token.consumed_at is not None:
                return None
            if token.purpose != purpose:
                return None
            if _is_expired(token.expires_at):
                return None
            if not verify_api_key(raw_token, token.salt, token.secret_hash):
                return None
            token.consumed_at = iso_now()
            self._io.write_entity_tx(transaction, "verification_token", token.hash, token)
            return token

        return self._io.database.run_in_transaction(txn)
