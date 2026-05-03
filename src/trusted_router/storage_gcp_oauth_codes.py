"""Spanner-backed OAuth authorization codes.

Sibling of InMemoryOAuthCodes. The code → lookup_hash index lets the
callback handler resolve the raw code in O(1) without scanning. consume()
runs inside a Spanner transaction so the consumed_at flag race-free."""

from __future__ import annotations

import datetime as dt
from typing import Any

from trusted_router.security import (
    hash_api_key,
    lookup_hash_api_key,
    new_api_key,
    new_hash_salt,
    new_key_id,
    verify_api_key,
)
from trusted_router.storage_gcp_io import SpannerIO
from trusted_router.storage_models import (
    OAuthAuthorizationCode,
    _is_expired,
    iso_now,
    utcnow,
)


class SpannerOAuthCodes:
    def __init__(self, io: SpannerIO) -> None:
        self._io = io

    def create(
        self,
        *,
        workspace_id: str,
        user_id: str | None,
        callback_url: str,
        key_label: str,
        ttl_seconds: int,
        app_id: int,
        limit_microdollars: int | None = None,
        limit_reset: str | None = None,
        expires_at: str | None = None,
        code_challenge: str | None = None,
        code_challenge_method: str | None = None,
        spawn_agent: str | None = None,
        spawn_cloud: str | None = None,
    ) -> tuple[str, OAuthAuthorizationCode]:
        raw = new_api_key(prefix="auth_code")
        code_id = new_key_id(prefix="oauth")
        salt = new_hash_salt()
        lookup_hash = lookup_hash_api_key(raw)
        code_expires_at = (
            utcnow() + dt.timedelta(seconds=max(ttl_seconds, 60))
        ).isoformat().replace("+00:00", "Z")
        code = OAuthAuthorizationCode(
            hash=code_id,
            salt=salt,
            secret_hash=hash_api_key(raw, salt),
            lookup_hash=lookup_hash,
            workspace_id=workspace_id,
            user_id=user_id,
            app_id=app_id,
            callback_url=callback_url,
            key_label=key_label,
            limit_microdollars=limit_microdollars,
            limit_reset=limit_reset,
            expires_at=expires_at,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            code_expires_at=code_expires_at,
            spawn_agent=spawn_agent,
            spawn_cloud=spawn_cloud,
        )
        with self._io.database.batch() as batch:
            self._io.write_entity_batch(batch, "oauth_code", code.hash, code)
            self._io.write_entity_batch(
                batch, "oauth_code_lookup", lookup_hash, {"code_id": code.hash}
            )
        return raw, code

    def consume(self, raw_code: str) -> OAuthAuthorizationCode | None:
        lookup_hash = lookup_hash_api_key(raw_code)

        def txn(transaction: Any) -> OAuthAuthorizationCode | None:
            lookup = self._io.read_entity_tx(
                transaction, "oauth_code_lookup", lookup_hash, dict
            )
            if not lookup:
                return None
            code = self._io.read_entity_tx(
                transaction, "oauth_code", str(lookup["code_id"]), OAuthAuthorizationCode
            )
            if code is None:
                return None
            if code.consumed_at is not None:
                return None
            if _is_expired(code.code_expires_at):
                self._io.delete_entities_tx(transaction, "oauth_code", [code.hash])
                self._io.delete_entities_tx(
                    transaction, "oauth_code_lookup", [lookup_hash]
                )
                return None
            if not verify_api_key(raw_code, code.salt, code.secret_hash):
                return None
            code.consumed_at = iso_now()
            self._io.write_entity_tx(transaction, "oauth_code", code.hash, code)
            return code

        return self._io.database.run_in_transaction(txn)
