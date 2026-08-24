"""SIWE wallet-challenge nonces. One-shot, time-bounded, and scope-bounded.

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

WALLET_CHALLENGE_SCOPE_KIND = "wallet_challenge_by_scope"
_SIWE_HEADER_SUFFIX = " wants you to sign in with your Ethereum account:"
_SIWE_NONCE_PREFIX = "Nonce: "


def normalize_wallet_address(address: str) -> str:
    return address.strip().lower()


def parse_siwe_challenge_scope(message: str) -> tuple[str, str] | None:
    """Extract the canonical SIWE domain and raw nonce from our prompt."""
    lines = message.splitlines()
    if not lines or not lines[0].endswith(_SIWE_HEADER_SUFFIX):
        return None
    domain = lines[0][: -len(_SIWE_HEADER_SUFFIX)].strip().lower().rstrip(".")
    nonce_lines = [
        line[len(_SIWE_NONCE_PREFIX) :] for line in lines if line.startswith(_SIWE_NONCE_PREFIX)
    ]
    if not domain or len(nonce_lines) != 1 or not nonce_lines[0]:
        return None
    return domain, nonce_lines[0]


def wallet_challenge_scope_id(address: str, message: str) -> str:
    """Return the fixed-width key for one wallet/domain challenge slot."""
    parsed = parse_siwe_challenge_scope(message)
    domain = parsed[0] if parsed is not None else ""
    return lookup_hash_api_key(f"{normalize_wallet_address(address)}\x00{domain}")


def reusable_wallet_challenge_nonce(
    record: WalletChallenge,
    *,
    scope_id: str,
) -> str | None:
    """Recover a valid active nonce without persisting its plaintext twice."""
    parsed = parse_siwe_challenge_scope(record.message)
    if parsed is None:
        return None
    raw_nonce = parsed[1]
    if not wallet_challenge_nonce_is_valid(
        record,
        raw_nonce=raw_nonce,
        scope_id=scope_id,
    ):
        return None
    return raw_nonce


def wallet_challenge_nonce_is_valid(
    record: WalletChallenge,
    *,
    raw_nonce: str,
    scope_id: str,
) -> bool:
    if record.consumed_at is not None or _is_expired(record.expires_at):
        return False
    if wallet_challenge_scope_id(record.address, record.message) != scope_id:
        return False
    if lookup_hash_api_key(raw_nonce) != record.lookup_hash:
        return False
    if not verify_api_key(raw_nonce, record.salt, record.secret_hash):
        return False
    return True


class InMemoryWalletChallenges:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._challenges: dict[str, WalletChallenge] = {}
        self._ids_by_lookup_hash: dict[str, str] = {}
        self._ids_by_scope: dict[str, str] = {}

    def reset(self) -> None:
        with self._lock:
            self._challenges.clear()
            self._ids_by_lookup_hash.clear()
            self._ids_by_scope.clear()

    def create(
        self,
        *,
        address: str,
        message: str,
        ttl_seconds: int,
        raw_nonce: str | None = None,
    ) -> tuple[str, WalletChallenge]:
        """Get or mint the active SIWE challenge for one wallet/domain. The caller may pre-generate
        `raw_nonce` so it can bake the nonce into the SIWE message before
        persistence; otherwise we generate one."""
        with self._lock:
            normalized_address = normalize_wallet_address(address)
            parsed = parse_siwe_challenge_scope(message)
            if raw_nonce is None:
                raw_nonce = parsed[1] if parsed is not None else secrets.token_urlsafe(32)
            elif parsed is not None and parsed[1] != raw_nonce:
                raise ValueError("SIWE message nonce does not match raw_nonce")
            scope_id = wallet_challenge_scope_id(normalized_address, message)
            previous_id = self._ids_by_scope.get(scope_id)
            if previous_id is not None:
                previous = self._challenges.get(previous_id)
                if previous is not None:
                    reusable_nonce = reusable_wallet_challenge_nonce(
                        previous,
                        scope_id=scope_id,
                    )
                    if reusable_nonce is not None:
                        return reusable_nonce, previous
                    self._challenges.pop(previous_id, None)
                    self._ids_by_lookup_hash.pop(previous.lookup_hash, None)

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
            self._challenges[challenge_id] = record
            self._ids_by_lookup_hash[lookup_hash] = challenge_id
            self._ids_by_scope[scope_id] = challenge_id
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
            if record.lookup_hash != lookup_hash:
                return None
            scope_id = wallet_challenge_scope_id(record.address, record.message)
            if self._ids_by_scope.get(scope_id) != challenge_id:
                return None
            if record.consumed_at is not None:
                return None
            if _is_expired(record.expires_at):
                self._challenges.pop(challenge_id, None)
                self._ids_by_lookup_hash.pop(lookup_hash, None)
                if self._ids_by_scope.get(scope_id) == challenge_id:
                    self._ids_by_scope.pop(scope_id, None)
                return None
            if not verify_api_key(raw_nonce, record.salt, record.secret_hash):
                return None
            record.consumed_at = iso_now()
            return record
