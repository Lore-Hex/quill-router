"""SIWE wallet-challenge nonces. One-shot, time-bounded.

Lives outside storage.py so wallet sign-in plumbing has its own home
and the main store stays focused on credit/key/workspace state.
"""

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
from trusted_router.storage_models import WalletChallenge, _is_expired, iso_now, utcnow


class InMemoryWalletChallenges:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._challenges: dict[str, WalletChallenge] = {}
        self._ids_by_lookup_hash: dict[str, str] = {}

    def reset(self) -> None:
        with self._lock:
            self._challenges.clear()
            self._ids_by_lookup_hash.clear()

    def create(
        self,
        *,
        address: str,
        message: str,
        ttl_seconds: int,
        raw_nonce: str | None = None,
    ) -> tuple[str, WalletChallenge]:
        """Mint a one-shot SIWE challenge. The caller may pre-generate
        `raw_nonce` so it can bake the nonce into the SIWE message before
        persistence; otherwise we generate one."""
        with self._lock:
            raw_nonce = raw_nonce or secrets.token_urlsafe(32)
            challenge_id = new_key_id(prefix="siwe")
            salt = new_hash_salt()
            lookup_hash = lookup_hash_api_key(raw_nonce)
            expires_at = (
                utcnow() + dt.timedelta(seconds=max(ttl_seconds, 60))
            ).isoformat().replace("+00:00", "Z")
            record = WalletChallenge(
                hash=challenge_id,
                salt=salt,
                secret_hash=hash_api_key(raw_nonce, salt),
                lookup_hash=lookup_hash,
                address=address.strip().lower(),
                message=message,
                expires_at=expires_at,
            )
            self._challenges[challenge_id] = record
            self._ids_by_lookup_hash[lookup_hash] = challenge_id
            return raw_nonce, record

    def consume(self, raw_nonce: str) -> WalletChallenge | None:
        """One-shot: returns the record on first valid call, None on
        missing/expired/replayed/tampered."""
        with self._lock:
            lookup_hash = lookup_hash_api_key(raw_nonce)
            challenge_id = self._ids_by_lookup_hash.get(lookup_hash)
            if challenge_id is None:
                return None
            record = self._challenges.get(challenge_id)
            if record is None:
                return None
            if record.consumed_at is not None:
                return None
            if _is_expired(record.expires_at):
                self._challenges.pop(challenge_id, None)
                self._ids_by_lookup_hash.pop(lookup_hash, None)
                return None
            if not verify_api_key(raw_nonce, record.salt, record.secret_hash):
                return None
            record.consumed_at = iso_now()
            return record
