"""Auth session store: cookie-based browser sessions for the console.

Sessions live keyed by an opaque ID; the raw token is salted+hashed and
indexed by `lookup_hash` so the cookie value lookup is O(1) without ever
storing the raw token. Sessions can be `pending_email` (legacy optional
wallet email attach) or `active`."""

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
from trusted_router.storage_models import AuthSession, _is_expired, utcnow


class InMemoryAuthSessions:
    def __init__(self, *, lock: threading.RLock) -> None:
        self._lock = lock
        self.sessions: dict[str, AuthSession] = {}
        self.session_ids_by_lookup_hash: dict[str, str] = {}

    def reset(self) -> None:
        self.sessions.clear()
        self.session_ids_by_lookup_hash.clear()

    def create(
        self,
        *,
        user_id: str,
        provider: str,
        label: str,
        ttl_seconds: int,
        workspace_id: str | None = None,
        state: str = "active",
    ) -> tuple[str, AuthSession]:
        with self._lock:
            raw = new_api_key(prefix="trsess-v1")
            session_id = new_key_id(prefix="sess")
            salt = new_hash_salt()
            lookup_hash = lookup_hash_api_key(raw)
            expires_at = (
                utcnow() + dt.timedelta(seconds=max(ttl_seconds, 60))
            ).isoformat().replace("+00:00", "Z")
            session = AuthSession(
                hash=session_id,
                salt=salt,
                secret_hash=hash_api_key(raw, salt),
                lookup_hash=lookup_hash,
                user_id=user_id,
                provider=provider,
                label=label,
                workspace_id=workspace_id,
                expires_at=expires_at,
                state=state,
            )
            self.sessions[session_id] = session
            self.session_ids_by_lookup_hash[lookup_hash] = session_id
            return raw, session

    def upgrade(self, raw_token: str, *, state: str) -> AuthSession | None:
        """Change a session state. Returns the updated session or None if
        the token is invalid/expired."""
        with self._lock:
            session = self.get_by_raw(raw_token)
            if session is None:
                return None
            session.state = state
            return session

    def set_workspace(self, raw_token: str, workspace_id: str) -> AuthSession | None:
        with self._lock:
            session = self.get_by_raw(raw_token)
            if session is None:
                return None
            session.workspace_id = workspace_id
            return session

    def get_by_raw(self, raw_token: str) -> AuthSession | None:
        with self._lock:
            lookup_hash = lookup_hash_api_key(raw_token)
            session_id = self.session_ids_by_lookup_hash.get(lookup_hash)
            if session_id is None:
                return None
            session = self.sessions.get(session_id)
            if session is None:
                return None
            if _is_expired(session.expires_at):
                self.sessions.pop(session_id, None)
                self.session_ids_by_lookup_hash.pop(lookup_hash, None)
                return None
            if verify_api_key(raw_token, session.salt, session.secret_hash):
                return session
            return None

    def delete_by_raw(self, raw_token: str) -> bool:
        with self._lock:
            lookup_hash = lookup_hash_api_key(raw_token)
            session_id = self.session_ids_by_lookup_hash.pop(lookup_hash, None)
            if session_id is None:
                return False
            self.sessions.pop(session_id, None)
            return True
