"""Spanner-backed SIWE wallet-challenge nonces.

Sibling of InMemoryWalletChallenges (storage_wallet_challenges.py). Both
implement the same `create` / `consume` surface; SpannerBigtableStore
composes this class via its `_io` adapter so wallet-challenge logic lives
in its own module rather than scattered through storage_gcp.py.
"""

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
from trusted_router.storage_models import WalletChallenge, _is_expired, iso_now, utcnow


class SpannerWalletChallenges:
    def __init__(self, io: SpannerIO) -> None:
        self._io = io

    def create(
        self,
        *,
        address: str,
        message: str,
        ttl_seconds: int,
        raw_nonce: str | None = None,
    ) -> tuple[str, WalletChallenge]:
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
        with self._io.database.batch() as batch:
            self._io.write_entity_batch(batch, "wallet_challenge", record.hash, record)
            self._io.write_entity_batch(
                batch, "wallet_challenge_lookup", lookup_hash, {"challenge_id": record.hash}
            )
        return raw_nonce, record

    def consume(self, raw_nonce: str) -> WalletChallenge | None:
        lookup_hash = lookup_hash_api_key(raw_nonce)

        def txn(transaction: Any) -> WalletChallenge | None:
            lookup = self._io.read_entity_tx(transaction, "wallet_challenge_lookup", lookup_hash, dict)
            if not lookup:
                return None
            record = self._io.read_entity_tx(
                transaction, "wallet_challenge", str(lookup["challenge_id"]), WalletChallenge
            )
            if record is None:
                return None
            if record.consumed_at is not None:
                return None
            if _is_expired(record.expires_at):
                return None
            if not verify_api_key(raw_nonce, record.salt, record.secret_hash):
                return None
            record.consumed_at = iso_now()
            self._io.write_entity_tx(transaction, "wallet_challenge", record.hash, record)
            return record

        return self._io.database.run_in_transaction(txn)
