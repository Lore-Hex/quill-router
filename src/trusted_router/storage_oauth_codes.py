"""OAuth authorization codes: short-lived single-use grants used by the
TrustedRouter-as-OAuth-IdP flow to issue scoped API keys to third-party
apps. Mints a raw secret + record on `create`, validates + marks consumed
on `consume`. PKCE challenge is stored on the record but verification
lives in the OAuth route handler."""

from __future__ import annotations

import datetime as dt
import threading

from trusted_router.security import (
    hash_api_key,
    lookup_hash_api_key,
    new_api_key,
    new_hash_salt,
    new_key_id,
    verify_api_key,
)
from trusted_router.storage_models import (
    OAuthAuthorizationCode,
    _is_expired,
    iso_now,
    utcnow,
)


class InMemoryOAuthCodes:
    def __init__(self, *, lock: threading.RLock) -> None:
        self._lock = lock
        self.codes: dict[str, OAuthAuthorizationCode] = {}
        self.code_ids_by_lookup_hash: dict[str, str] = {}

    def reset(self) -> None:
        self.codes.clear()
        self.code_ids_by_lookup_hash.clear()

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
        with self._lock:
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
            self.codes[code_id] = code
            self.code_ids_by_lookup_hash[lookup_hash] = code_id
            return raw, code

    def consume(self, raw_code: str) -> OAuthAuthorizationCode | None:
        with self._lock:
            lookup_hash = lookup_hash_api_key(raw_code)
            code_id = self.code_ids_by_lookup_hash.get(lookup_hash)
            if code_id is None:
                return None
            code = self.codes.get(code_id)
            if code is None:
                return None
            if code.consumed_at is not None:
                return None
            if _is_expired(code.code_expires_at):
                self.codes.pop(code_id, None)
                self.code_ids_by_lookup_hash.pop(lookup_hash, None)
                return None
            if not verify_api_key(raw_code, code.salt, code.secret_hash):
                return None
            code.consumed_at = iso_now()
            return code
