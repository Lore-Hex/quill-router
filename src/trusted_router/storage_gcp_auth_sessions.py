"""Spanner-backed auth session store. Sibling of InMemoryAuthSessions."""

from __future__ import annotations

import datetime as dt

from trusted_router.security import (
    hash_api_key,
    lookup_hash_api_key,
    new_api_key,
    new_hash_salt,
    new_key_id,
    verify_api_key,
)
from trusted_router.storage_gcp_io import SpannerIO
from trusted_router.storage_models import AuthSession, _is_expired, utcnow


class SpannerAuthSessions:
    def __init__(self, io: SpannerIO) -> None:
        self._io = io

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
        with self._io.database.batch() as batch:
            self._io.write_entity_batch(batch, "auth_session", session.hash, session)
            self._io.write_entity_batch(
                batch, "auth_session_lookup", lookup_hash, {"session_id": session.hash}
            )
        return raw, session

    def upgrade(self, raw_token: str, *, state: str) -> AuthSession | None:
        session = self.get_by_raw(raw_token)
        if session is None:
            return None
        session.state = state
        self._io.write_entity("auth_session", session.hash, session)
        return session

    def set_workspace(self, raw_token: str, workspace_id: str) -> AuthSession | None:
        session = self.get_by_raw(raw_token)
        if session is None:
            return None
        session.workspace_id = workspace_id
        self._io.write_entity("auth_session", session.hash, session)
        return session

    def get_by_raw(self, raw_token: str) -> AuthSession | None:
        lookup_hash = lookup_hash_api_key(raw_token)
        lookup = self._io.read_entity("auth_session_lookup", lookup_hash, dict)
        if not lookup:
            return None
        session = self._io.read_entity(
            "auth_session", str(lookup["session_id"]), AuthSession
        )
        if session is None:
            return None
        if _is_expired(session.expires_at):
            self._io.delete_entities("auth_session", [session.hash])
            self._io.delete_entities("auth_session_lookup", [lookup_hash])
            return None
        if verify_api_key(raw_token, session.salt, session.secret_hash):
            return session
        return None

    def delete_by_raw(self, raw_token: str) -> bool:
        lookup_hash = lookup_hash_api_key(raw_token)
        lookup = self._io.read_entity("auth_session_lookup", lookup_hash, dict)
        if not lookup:
            return False
        self._io.delete_entities("auth_session", [str(lookup["session_id"])])
        self._io.delete_entities("auth_session_lookup", [lookup_hash])
        return True
