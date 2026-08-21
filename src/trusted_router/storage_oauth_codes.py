"""OAuth authorization-code creation and atomic delegated-key exchange.

The raw authorization code and delegated API key are returned once and never
persisted. Exchange belongs in storage because PKCE verification, grant
consumption, and every key/index/limit write must commit or roll back together.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import threading
from dataclasses import dataclass

from trusted_router.security import (
    hash_api_key,
    key_label,
    lookup_hash_api_key,
    new_api_key,
    new_hash_salt,
    new_key_id,
    verify_api_key,
)
from trusted_router.storage_models import (
    ApiKey,
    OAuthAuthorizationCode,
    User,
    Workspace,
    _is_expired,
    iso_now,
    utcnow,
)


class OAuthCodeVerifierRequired(ValueError):
    """The grant requires PKCE but the exchange omitted its verifier."""


class OAuthCodeVerifierNotAscii(ValueError):
    """S256 PKCE requires an ASCII verifier."""


class OAuthCodeVerifierMismatch(ValueError):
    """The supplied PKCE verifier does not satisfy the grant."""


class OAuthCodeMethodMismatch(ValueError):
    """The exchange supplied a method different from the grant's method."""


class OAuthWorkspaceUnavailable(RuntimeError):
    """The grant's workspace disappeared before exchange."""


class OAuthWorkspaceBillingPaused(RuntimeError):
    """The grant's workspace is quiesced and may not mint a key."""


@dataclass(frozen=True)
class OAuthCodeExchange:
    """One committed authorization-code exchange.

    ``user`` is read inside the same transaction so a post-commit identity
    lookup cannot turn a consumed grant into a failed response with no
    recoverable raw key.
    """

    raw_key: str
    api_key: ApiKey
    authorization_code: OAuthAuthorizationCode
    user: User | None


def verify_oauth_pkce(
    code: OAuthAuthorizationCode,
    *,
    code_verifier: str | None,
    code_challenge_method: str | None,
) -> None:
    """Validate the exchange proof without mutating the grant."""
    if not code.code_challenge:
        return
    if (
        code_challenge_method not in {None, ""}
        and code_challenge_method != code.code_challenge_method
    ):
        raise OAuthCodeMethodMismatch
    verifier = code_verifier or ""
    if not verifier:
        raise OAuthCodeVerifierRequired
    if code.code_challenge_method == "plain":
        expected = verifier
    else:
        try:
            verifier_bytes = verifier.encode("ascii")
        except UnicodeEncodeError as exc:
            raise OAuthCodeVerifierNotAscii from exc
        expected = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier_bytes).digest())
            .decode("ascii")
            .rstrip("=")
        )
    if not hmac.compare_digest(expected, code.code_challenge):
        raise OAuthCodeVerifierMismatch


def new_oauth_delegated_api_key(
    code: OAuthAuthorizationCode,
    *,
    usage_shard_count: int = 1,
) -> tuple[str, ApiKey]:
    """Build the response-only raw key and its hash-only durable record."""
    raw = new_api_key()
    salt = new_hash_salt()
    key = ApiKey(
        hash=new_key_id(),
        salt=salt,
        secret_hash=hash_api_key(raw, salt),
        lookup_hash=lookup_hash_api_key(raw),
        name=code.key_label,
        label=key_label(raw),
        workspace_id=code.workspace_id,
        creator_user_id=code.user_id,
        management=False,
        limit_microdollars=code.limit_microdollars,
        limit_reset=code.limit_reset,
        include_byok_in_limit=True,
        expires_at=code.expires_at,
        usage_shard_count=usage_shard_count,
    )
    return raw, key


class InMemoryOAuthCodes:
    def __init__(
        self,
        *,
        lock: threading.RLock,
        workspaces: dict[str, Workspace],
        users: dict[str, User],
        api_keys: dict[str, ApiKey],
        api_key_ids_by_lookup_hash: dict[str, str],
    ) -> None:
        self._lock = lock
        self._workspaces = workspaces
        self._users = users
        self._api_keys = api_keys
        self._api_key_ids_by_lookup_hash = api_key_ids_by_lookup_hash
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

    def exchange(
        self,
        raw_code: str,
        *,
        code_verifier: str | None,
        code_challenge_method: str | None,
    ) -> OAuthCodeExchange | None:
        """Verify, consume, and mint under the Store's shared lock."""
        with self._lock:
            lookup_hash = lookup_hash_api_key(raw_code)
            code_id = self.code_ids_by_lookup_hash.get(lookup_hash)
            code = self.codes.get(code_id) if code_id is not None else None
            if code is None or code.consumed_at is not None:
                return None
            if _is_expired(code.code_expires_at):
                self.codes.pop(code.hash, None)
                self.code_ids_by_lookup_hash.pop(lookup_hash, None)
                return None
            if not verify_api_key(raw_code, code.salt, code.secret_hash):
                return None

            verify_oauth_pkce(
                code,
                code_verifier=code_verifier,
                code_challenge_method=code_challenge_method,
            )
            workspace = self._workspaces.get(code.workspace_id)
            if workspace is None or workspace.deleted:
                raise OAuthWorkspaceUnavailable
            if bool(getattr(workspace, "billing_paused", False)):
                raise OAuthWorkspaceBillingPaused

            # Generate every fallible value before mutating durable state.
            user = self._users.get(code.user_id) if code.user_id else None
            raw_key, key = new_oauth_delegated_api_key(code)
            key_existed = key.hash in self._api_keys
            previous_key = self._api_keys.get(key.hash)
            lookup_existed = key.lookup_hash in self._api_key_ids_by_lookup_hash
            previous_key_id = self._api_key_ids_by_lookup_hash.get(key.lookup_hash)
            previous_consumed_at = code.consumed_at
            try:
                self._api_keys[key.hash] = key
                self._api_key_ids_by_lookup_hash[key.lookup_hash] = key.hash
                code.consumed_at = iso_now()
            except BaseException:
                if key_existed and previous_key is not None:
                    self._api_keys[key.hash] = previous_key
                else:
                    self._api_keys.pop(key.hash, None)
                if lookup_existed and previous_key_id is not None:
                    self._api_key_ids_by_lookup_hash[key.lookup_hash] = previous_key_id
                else:
                    self._api_key_ids_by_lookup_hash.pop(key.lookup_hash, None)
                code.consumed_at = previous_consumed_at
                raise
            return OAuthCodeExchange(
                raw_key=raw_key,
                api_key=key,
                authorization_code=code,
                user=user,
            )
