"""API key + reservation + gateway-authorization lifecycle.

The "spend control" half of the in-memory store. Owns four dicts:
  - keys + key_ids_by_lookup_hash: API key CRUD + per-key spend cap state.
  - reservations: outstanding pre-authorizations against credit accounts,
    settled or refunded once the actual cost is known.
  - gateway_authorizations: cross-request reservation handles for the
    enclave gateway path (settle/refund arrive on a separate request from
    the authorize call).

Three of those dicts are owned outright; reservations need read+write access
to the workspace credit ledger (CreditAccount.reserved/total_usage), so the
class accepts the credits dict by reference at construction time. The
parent InMemoryStore's lock is shared so reserve→credit-debit happens
atomically.

`add_usage` is the inverse callout — when a Generation lands and we need to
roll its cost into the per-key counters, the parent calls into here so we
don't leak ApiKey internals.
"""

from __future__ import annotations

import threading
import uuid
from typing import Any

from trusted_router.money import dollars_to_microdollars
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
    CreditAccount,
    GatewayAuthorization,
    Reservation,
    _is_byok,
    iso_now,
)
from trusted_router.types import UsageType


class InMemoryApiKeys:
    def __init__(
        self,
        *,
        credits_by_workspace: dict[str, CreditAccount],
        lock: threading.RLock,
    ) -> None:
        self._lock = lock
        self._credits = credits_by_workspace
        self.keys: dict[str, ApiKey] = {}
        self.key_ids_by_lookup_hash: dict[str, str] = {}
        self.reservations: dict[str, Reservation] = {}
        # Idempotency-key → reservation_id index. Populated whenever
        # reserve() runs with a non-None idempotency_key. Looking up by
        # key returns the existing reservation; a duplicate reserve()
        # call with the same key is then a read, not a second debit.
        # Required for safe dual-write across two Spanner instances
        # (Stage 5a) and safe change-stream replay (Stage 1 ZDM).
        self.reservation_id_by_idempotency_key: dict[str, str] = {}
        self.gateway_authorizations: dict[str, GatewayAuthorization] = {}
        self.gateway_authorization_id_by_idempotency_key: dict[str, str] = {}

    def reset(self) -> None:
        # Caller holds the parent lock during the global reset, so we
        # don't reacquire it here.
        self.keys.clear()
        self.key_ids_by_lookup_hash.clear()
        self.reservations.clear()
        self.reservation_id_by_idempotency_key.clear()
        self.gateway_authorizations.clear()
        self.gateway_authorization_id_by_idempotency_key.clear()

    # ── API key CRUD ────────────────────────────────────────────────────
    def create(
        self,
        *,
        workspace_id: str,
        name: str,
        creator_user_id: str | None,
        management: bool = False,
        raw_key: str | None = None,
        limit_microdollars: int | None = None,
        limit_reset: str | None = None,
        include_byok_in_limit: bool = True,
        expires_at: str | None = None,
    ) -> tuple[str, ApiKey]:
        with self._lock:
            key = raw_key or new_api_key()
            key_id = new_key_id()
            salt = new_hash_salt()
            digest = hash_api_key(key, salt)
            lookup_hash = lookup_hash_api_key(key)
            api_key = ApiKey(
                hash=key_id,
                salt=salt,
                secret_hash=digest,
                lookup_hash=lookup_hash,
                name=name,
                label=key_label(key),
                workspace_id=workspace_id,
                creator_user_id=creator_user_id,
                management=management,
                limit_microdollars=limit_microdollars,
                limit_reset=limit_reset,
                include_byok_in_limit=include_byok_in_limit,
                expires_at=expires_at,
            )
            self.keys[key_id] = api_key
            self.key_ids_by_lookup_hash[lookup_hash] = key_id
            return key, api_key

    def get_by_hash(self, key_hash: str) -> ApiKey | None:
        with self._lock:
            return self.keys.get(key_hash)

    def get_by_lookup_hash(self, lookup_hash: str) -> ApiKey | None:
        with self._lock:
            key_id = self.key_ids_by_lookup_hash.get(lookup_hash)
            return self.keys.get(key_id) if key_id is not None else None

    def get_by_raw(self, raw_key: str) -> ApiKey | None:
        with self._lock:
            lookup_hash = lookup_hash_api_key(raw_key)
            key_id = self.key_ids_by_lookup_hash.get(lookup_hash)
            if key_id is not None:
                key = self.keys.get(key_id)
                if key is not None and verify_api_key(raw_key, key.salt, key.secret_hash):
                    return key
            for key in self.keys.values():
                if verify_api_key(raw_key, key.salt, key.secret_hash):
                    self.key_ids_by_lookup_hash[lookup_hash] = key.hash
                    return key
            return None

    def list_for_workspace(self, workspace_id: str) -> list[ApiKey]:
        with self._lock:
            return [key for key in self.keys.values() if key.workspace_id == workspace_id]

    def delete(self, key_hash: str) -> bool:
        with self._lock:
            key = self.keys.pop(key_hash, None)
            if key is None:
                return False
            self.key_ids_by_lookup_hash.pop(key.lookup_hash, None)
            return True

    def update(self, key_hash: str, patch: dict[str, Any]) -> ApiKey | None:
        with self._lock:
            key = self.keys.get(key_hash)
            if key is None:
                return None
            if "name" in patch and patch["name"]:
                key.name = str(patch["name"])
            if "disabled" in patch:
                key.disabled = bool(patch["disabled"])
            if "limit" in patch:
                value = patch["limit"]
                key.limit_microdollars = None if value is None else dollars_to_microdollars(value)
            if "limit_microdollars" in patch:
                key.limit_microdollars = patch["limit_microdollars"]
            if "limit_reset" in patch:
                key.limit_reset = patch["limit_reset"]
            if "include_byok_in_limit" in patch:
                key.include_byok_in_limit = bool(patch["include_byok_in_limit"])
            key.updated_at = iso_now()
            return key

    # ── Per-key spend-cap lifecycle ─────────────────────────────────────
    def reserve_limit(
        self,
        key_hash: str,
        amount_microdollars: int,
        *,
        usage_type: str,
    ) -> None:
        with self._lock:
            key = self.keys[key_hash]
            if key.limit_microdollars is None:
                return
            if _is_byok(usage_type) and not key.include_byok_in_limit:
                return
            used = key.usage_microdollars
            if key.include_byok_in_limit:
                used += key.byok_usage_microdollars
            available = key.limit_microdollars - used - key.reserved_microdollars
            if amount_microdollars > available:
                raise ValueError("key limit exceeded")
            key.reserved_microdollars += amount_microdollars

    def settle_limit(
        self,
        key_hash: str,
        reserved_microdollars: int,
        actual_microdollars: int,
        *,
        usage_type: str,
    ) -> None:
        with self._lock:
            key = self.keys.get(key_hash)
            if key is None or key.limit_microdollars is None:
                return
            if _is_byok(usage_type) and not key.include_byok_in_limit:
                return
            key.reserved_microdollars = max(0, key.reserved_microdollars - reserved_microdollars)
            # Actual usage is added by add_usage; this method only releases
            # the estimated key-limit hold.
            _ = actual_microdollars

    def refund_limit(
        self,
        key_hash: str,
        reserved_microdollars: int,
        *,
        usage_type: str,
    ) -> None:
        with self._lock:
            key = self.keys.get(key_hash)
            if key is None or key.limit_microdollars is None:
                return
            if _is_byok(usage_type) and not key.include_byok_in_limit:
                return
            key.reserved_microdollars = max(0, key.reserved_microdollars - reserved_microdollars)

    def add_usage(self, key_hash: str, cost_microdollars: int, *, is_byok: bool) -> None:
        """Roll a settled generation's actual cost into the key counters.
        Called by InMemoryStore.add_generation; lives here so ApiKey
        internals stay encapsulated."""
        with self._lock:
            key = self.keys.get(key_hash)
            if key is None:
                return
            if is_byok:
                key.byok_usage_microdollars += cost_microdollars
            else:
                key.usage_microdollars += cost_microdollars

    # ── Credit reservations ─────────────────────────────────────────────
    def reserve(
        self,
        workspace_id: str,
        key_hash: str,
        amount_microdollars: int,
        *,
        idempotency_key: str | None = None,
    ) -> Reservation:
        with self._lock:
            # Idempotency check first. If the same key was already used,
            # return the existing reservation without debiting credit a
            # second time. The amount on the existing reservation may
            # differ from what the caller passed (e.g., a retry with a
            # newer cost estimate); we trust the first one — that's the
            # whole point of idempotency.
            if idempotency_key is not None:
                existing_id = self.reservation_id_by_idempotency_key.get(idempotency_key)
                if existing_id is not None:
                    return self.reservations[existing_id]
            account = self._credits[workspace_id]
            available = (
                account.total_credits_microdollars
                - account.total_usage_microdollars
                - account.reserved_microdollars
            )
            if amount_microdollars > available:
                raise ValueError("insufficient credits")
            account.reserved_microdollars += amount_microdollars
            reservation = Reservation(
                id=str(uuid.uuid4()),
                workspace_id=workspace_id,
                key_hash=key_hash,
                amount_microdollars=amount_microdollars,
                idempotency_key=idempotency_key,
            )
            self.reservations[reservation.id] = reservation
            if idempotency_key is not None:
                self.reservation_id_by_idempotency_key[idempotency_key] = reservation.id
            return reservation

    def settle(self, reservation_id: str, actual_microdollars: int) -> None:
        with self._lock:
            reservation = self.reservations[reservation_id]
            if reservation.settled:
                return
            account = self._credits[reservation.workspace_id]
            account.reserved_microdollars -= reservation.amount_microdollars
            account.total_usage_microdollars += actual_microdollars
            reservation.settled = True

    def refund(self, reservation_id: str) -> None:
        with self._lock:
            reservation = self.reservations[reservation_id]
            if reservation.settled:
                return
            account = self._credits[reservation.workspace_id]
            account.reserved_microdollars -= reservation.amount_microdollars
            reservation.settled = True

    # ── Gateway authorizations ──────────────────────────────────────────
    def create_gateway_authorization(
        self,
        *,
        workspace_id: str,
        key_hash: str,
        model_id: str,
        provider: str,
        usage_type: UsageType | str,
        estimated_microdollars: int,
        credit_reservation_id: str | None,
        requested_model_id: str | None = None,
        candidate_model_ids: list[str] | None = None,
        region: str | None = None,
        endpoint_id: str | None = None,
        candidate_endpoint_ids: list[str] | None = None,
        idempotency_key: str | None = None,
        idempotency_fingerprint: str | None = None,
    ) -> GatewayAuthorization:
        with self._lock:
            if idempotency_key is not None:
                existing_id = self.gateway_authorization_id_by_idempotency_key.get(
                    self._gateway_authorization_idempotency_index_key(
                        workspace_id, key_hash, idempotency_key
                    )
                )
                if existing_id is not None:
                    return self.gateway_authorizations[existing_id]
            authorization = GatewayAuthorization(
                id=f"gwa-{uuid.uuid4().hex}",
                workspace_id=workspace_id,
                key_hash=key_hash,
                model_id=model_id,
                provider=provider,
                usage_type=UsageType.coerce(usage_type),
                estimated_microdollars=estimated_microdollars,
                credit_reservation_id=credit_reservation_id,
                requested_model_id=requested_model_id,
                candidate_model_ids=list(candidate_model_ids or []),
                region=region,
                endpoint_id=endpoint_id,
                candidate_endpoint_ids=list(candidate_endpoint_ids or []),
                idempotency_key=idempotency_key,
                idempotency_fingerprint=idempotency_fingerprint,
            )
            self.gateway_authorizations[authorization.id] = authorization
            if idempotency_key is not None:
                self.gateway_authorization_id_by_idempotency_key[
                    self._gateway_authorization_idempotency_index_key(
                        workspace_id, key_hash, idempotency_key
                    )
                ] = authorization.id
            return authorization

    def get_gateway_authorization(self, authorization_id: str) -> GatewayAuthorization | None:
        with self._lock:
            return self.gateway_authorizations.get(authorization_id)

    def get_gateway_authorization_by_idempotency_key(
        self, workspace_id: str, key_hash: str, idempotency_key: str
    ) -> GatewayAuthorization | None:
        with self._lock:
            authorization_id = self.gateway_authorization_id_by_idempotency_key.get(
                self._gateway_authorization_idempotency_index_key(
                    workspace_id, key_hash, idempotency_key
                )
            )
            if authorization_id is None:
                return None
            return self.gateway_authorizations.get(authorization_id)

    def mark_gateway_authorization_settled(self, authorization_id: str) -> None:
        with self._lock:
            authorization = self.gateway_authorizations[authorization_id]
            authorization.settled = True

    @staticmethod
    def _gateway_authorization_idempotency_index_key(
        workspace_id: str, key_hash: str, idempotency_key: str
    ) -> str:
        return f"{workspace_id}\0{key_hash}\0{idempotency_key}"
