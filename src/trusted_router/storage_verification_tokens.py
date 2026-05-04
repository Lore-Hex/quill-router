"""One-shot verification tokens — used by optional email attach,
password reset, and email change flows."""

from __future__ import annotations

import datetime as dt
import secrets
import threading

from trusted_router.security import (
    hash_api_key,
    lookup_hash_api_key,
    new_hash_salt,
    new_key_id,
    verify_api_key,
)
from trusted_router.storage_models import VerificationToken, _is_expired, iso_now, utcnow


class InMemoryVerificationTokens:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tokens: dict[str, VerificationToken] = {}
        self._ids_by_lookup_hash: dict[str, str] = {}

    def reset(self) -> None:
        with self._lock:
            self._tokens.clear()
            self._ids_by_lookup_hash.clear()

    def create(
        self,
        *,
        user_id: str,
        purpose: str,
        ttl_seconds: int,
    ) -> tuple[str, VerificationToken]:
        with self._lock:
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
            self._tokens[token_id] = token
            self._ids_by_lookup_hash[lookup_hash] = token_id
            return raw, token

    def consume(
        self,
        raw_token: str,
        *,
        purpose: str,
    ) -> VerificationToken | None:
        with self._lock:
            lookup_hash = lookup_hash_api_key(raw_token)
            token_id = self._ids_by_lookup_hash.get(lookup_hash)
            if token_id is None:
                return None
            token = self._tokens.get(token_id)
            if token is None:
                return None
            if token.consumed_at is not None:
                return None
            if token.purpose != purpose:
                return None
            if _is_expired(token.expires_at):
                self._tokens.pop(token_id, None)
                self._ids_by_lookup_hash.pop(lookup_hash, None)
                return None
            if not verify_api_key(raw_token, token.salt, token.secret_hash):
                return None
            token.consumed_at = iso_now()
            return token
