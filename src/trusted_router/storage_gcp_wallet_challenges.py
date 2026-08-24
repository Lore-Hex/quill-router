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
)
from trusted_router.storage_gcp_io import SpannerIO
from trusted_router.storage_models import WalletChallenge, iso_now, utcnow
from trusted_router.storage_wallet_challenges import (
    WALLET_CHALLENGE_SCOPE_KIND,
    normalize_wallet_address,
    parse_siwe_challenge_scope,
    reusable_wallet_challenge_nonce,
    wallet_challenge_nonce_is_valid,
    wallet_challenge_scope_id,
)


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
        normalized_address = normalize_wallet_address(address)
        parsed = parse_siwe_challenge_scope(message)
        if raw_nonce is None:
            raw_nonce = parsed[1] if parsed is not None else secrets.token_urlsafe(32)
        elif parsed is not None and parsed[1] != raw_nonce:
            raise ValueError("SIWE message nonce does not match raw_nonce")
        scope_id = wallet_challenge_scope_id(normalized_address, message)
        challenge_id = new_key_id(prefix="siwe")
        salt = new_hash_salt()
        lookup_hash = lookup_hash_api_key(raw_nonce)
        expires_at = (
            (utcnow() + dt.timedelta(seconds=max(ttl_seconds, 60)))
            .isoformat()
            .replace("+00:00", "Z")
        )
        record = WalletChallenge(
            hash=challenge_id,
            salt=salt,
            secret_hash=hash_api_key(raw_nonce, salt),
            lookup_hash=lookup_hash,
            address=normalized_address,
            message=message,
            expires_at=expires_at,
        )

        def txn(transaction: Any) -> tuple[str, WalletChallenge]:
            active = self._io.read_entity_tx(
                transaction,
                WALLET_CHALLENGE_SCOPE_KIND,
                scope_id,
                dict,
            )
            if active:
                previous_id = active.get("challenge_id")
                previous = (
                    self._io.read_entity_tx(
                        transaction,
                        "wallet_challenge",
                        previous_id,
                        WalletChallenge,
                    )
                    if isinstance(previous_id, str)
                    else None
                )
                if previous is not None:
                    reusable_nonce = reusable_wallet_challenge_nonce(
                        previous,
                        scope_id=scope_id,
                    )
                    if reusable_nonce is not None:
                        return reusable_nonce, previous
                    self._io.delete_entities_tx(
                        transaction,
                        "wallet_challenge",
                        [previous_id],
                    )
                previous_lookup_hash = active.get("lookup_hash")
                if isinstance(previous_lookup_hash, str) and previous_lookup_hash != lookup_hash:
                    self._io.delete_entities_tx(
                        transaction,
                        "wallet_challenge_lookup",
                        [previous_lookup_hash],
                    )
            self._io.write_entity_tx(transaction, "wallet_challenge", record.hash, record)
            self._io.write_entity_tx(
                transaction,
                "wallet_challenge_lookup",
                lookup_hash,
                {"challenge_id": record.hash, "scope_id": scope_id},
            )
            self._io.write_entity_tx(
                transaction,
                WALLET_CHALLENGE_SCOPE_KIND,
                scope_id,
                {"challenge_id": record.hash, "lookup_hash": lookup_hash},
            )
            return raw_nonce, record

        return self._io.database.run_in_transaction(txn)

    def consume(self, raw_nonce: str) -> WalletChallenge | None:
        lookup_hash = lookup_hash_api_key(raw_nonce)

        def txn(transaction: Any) -> WalletChallenge | None:
            lookup = self._io.read_entity_tx(
                transaction, "wallet_challenge_lookup", lookup_hash, dict
            )
            if not lookup:
                return None
            scope_id = lookup.get("scope_id")
            challenge_id = lookup.get("challenge_id")
            if not isinstance(scope_id, str) or not isinstance(challenge_id, str):
                # Challenges created before the bounded scope index existed
                # are deliberately retired. Their five-minute prompts can be
                # reissued, while accepting them would bypass the one-active-
                # challenge scope during the migration.
                return None
            active = self._io.read_entity_tx(
                transaction,
                WALLET_CHALLENGE_SCOPE_KIND,
                scope_id,
                dict,
            )
            if not active:
                return None
            if active.get("challenge_id") != challenge_id:
                return None
            if active.get("lookup_hash") != lookup_hash:
                return None
            record = self._io.read_entity_tx(
                transaction,
                "wallet_challenge",
                challenge_id,
                WalletChallenge,
            )
            if record is None:
                return None
            if record.lookup_hash != lookup_hash:
                return None
            if not wallet_challenge_nonce_is_valid(
                record,
                raw_nonce=raw_nonce,
                scope_id=scope_id,
            ):
                return None
            record.consumed_at = iso_now()
            self._io.write_entity_tx(transaction, "wallet_challenge", record.hash, record)
            return record

        return self._io.database.run_in_transaction(txn)
