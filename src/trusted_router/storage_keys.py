"""API key + reservation + gateway-authorization lifecycle.

The "spend control" half of the in-memory store. Owns four dicts:
  - keys + key_ids_by_lookup_hash: API key CRUD + per-key spend cap state.
  - reservations: outstanding pre-authorizations against credit accounts,
    settled or refunded once the actual cost is known.
  - gateway_authorizations: cross-request reservation handles for the
    enclave gateway path (settle/refund arrive on a separate request from
    the authorize call).

Three of those dicts are owned outright; reservations need read+write access
to the workspace credit ledger (CreditMoney.reserved/total_usage), so the
class accepts the money dict by reference at construction time. The
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
from trusted_router.scopes import validate_api_key_scopes
from trusted_router.security import (
    hash_api_key,
    key_label,
    lookup_hash_api_key,
    new_api_key,
    new_hash_salt,
    new_key_id,
    verify_api_key,
)
from trusted_router.spend_leases import SpendLeaseArtifact
from trusted_router.spend_windows import (
    KeyLimitExceeded,
    KeyWindowLimitDecision,
    KeyWindowLimitExceeded,
    decide_key_window_limits,
    enforced_window_limits,
    utcnow,
    window_floors,
)
from trusted_router.storage_errors import DeferredSettlementCapReached
from trusted_router.storage_models import (
    ApiKey,
    ApiKeyUsageSnapshot,
    CreditAccount,
    CreditMoney,
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
        credit_money_by_workspace: dict[str, CreditMoney],
        lock: threading.RLock,
    ) -> None:
        self._lock = lock
        self._credits = credits_by_workspace
        self._credit_money = credit_money_by_workspace
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
        #: Deferred settlement: unsettled spend this plane has admitted on
        #: credit at the home plane's ledger, per workspace.
        self.deferred_outstanding: dict[str, int] = {}
        # Per-key window usage: key_hash -> {window: [start_datetime, usage_micro]}.
        # Same lazy fixed-UTC-window semantics as the typed tr_key_limit columns
        # (spend_windows.py); lives beside the key so ApiKey stays a plain record.
        self.window_usage: dict[str, dict[str, list]] = {}

    def reset(self) -> None:
        # Caller holds the parent lock during the global reset, so we
        # don't reacquire it here.
        self.keys.clear()
        self.key_ids_by_lookup_hash.clear()
        self.reservations.clear()
        self.reservation_id_by_idempotency_key.clear()
        self.gateway_authorizations.clear()
        self.gateway_authorization_id_by_idempotency_key.clear()
        self.window_usage.clear()

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
        limit_daily_microdollars: int | None = None,
        limit_weekly_microdollars: int | None = None,
        limit_monthly_microdollars: int | None = None,
        budget_alert_only: bool = False,
        tags: dict[str, str] | None = None,
        scopes: list[str] | None = None,
        app_id: str = "",
    ) -> tuple[str, ApiKey]:
        with self._lock:
            validated_scopes = validate_api_key_scopes(scopes, management=management)
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
                scopes=validated_scopes,
                app_id=app_id,
                management=management,
                limit_microdollars=limit_microdollars,
                limit_reset=limit_reset,
                include_byok_in_limit=include_byok_in_limit,
                expires_at=expires_at,
                limit_daily_microdollars=limit_daily_microdollars,
                limit_weekly_microdollars=limit_weekly_microdollars,
                limit_monthly_microdollars=limit_monthly_microdollars,
                budget_alert_only=budget_alert_only,
                tags=dict(tags or {}),
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

    def list_with_usage_for_workspace(self, workspace_id: str) -> list[ApiKeyUsageSnapshot]:
        """Atomically snapshot every key and its display counters."""
        with self._lock:
            keys = [key for key in self.keys.values() if key.workspace_id == workspace_id]
            return [
                ApiKeyUsageSnapshot(
                    api_key=key,
                    usage_microdollars=key.usage_microdollars,
                    byok_usage_microdollars=key.byok_usage_microdollars,
                    reserved_microdollars=key.reserved_microdollars,
                    windows=self.window_usage_snapshot(key.hash),
                )
                for key in keys
            ]

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
            for window in ("daily", "weekly", "monthly"):
                field = f"limit_{window}_microdollars"
                if field in patch:
                    setattr(key, field, patch[field])
            if "include_byok_in_limit" in patch:
                key.include_byok_in_limit = bool(patch["include_byok_in_limit"])
            if patch.get("budget_alert_only") is not None:
                key.budget_alert_only = bool(patch["budget_alert_only"])
            if "budget_alerted" in patch:
                key.budget_alerted = dict(patch["budget_alerted"] or {})
            if "tags" in patch:
                key.tags = dict(patch["tags"] or {})
            if "scopes" in patch:
                key.scopes = validate_api_key_scopes(
                    patch["scopes"],
                    management=key.management,
                )
            key.updated_at = iso_now()
            return key

    # ── Per-key spend-cap lifecycle ─────────────────────────────────────
    def window_usage_snapshot(
        self,
        key_hash: str,
        *,
        now: Any | None = None,
    ) -> dict[str, int]:
        """Current-window usage per window (micro), lazily zeroing stale windows.
        Mirrors what the typed tr_key_limit row reports for a Spanner store."""
        with self._lock:
            floors = window_floors(now or utcnow())
            state = self.window_usage.get(key_hash, {})
            out: dict[str, int] = {}
            for window in ("daily", "weekly", "monthly"):
                entry = state.get(window)
                if entry is None or entry[0] < floors[window]:
                    out[window] = 0
                else:
                    out[window] = int(entry[1])
            return out

    def reserve_limit(
        self,
        key_hash: str,
        amount_microdollars: int,
        *,
        usage_type: str,
    ) -> KeyWindowLimitDecision | None:
        with self._lock:
            key = self.keys[key_hash]
            if _is_byok(usage_type) and not key.include_byok_in_limit:
                return None  # BYOK excluded from this key's caps (lifetime AND windows)
            # Window limits are independent of the lifetime cap: check first,
            # approximately (in-flight reserved is deliberately not counted —
            # same semantics as the typed authorize check).
            window_limits = enforced_window_limits(key)  # {} in alert mode → never blocks
            decision = None
            if window_limits:
                now = utcnow()
                decision = decide_key_window_limits(
                    window_limits,
                    self.window_usage_snapshot(key_hash, now=now),
                    amount_microdollars,
                    now=now,
                )
                if decision is not None and not decision.allowed:
                    raise KeyWindowLimitExceeded(decision)
            if key.limit_microdollars is None:
                return decision
            used = key.usage_microdollars
            if key.include_byok_in_limit:
                used += key.byok_usage_microdollars
            available = key.limit_microdollars - used - key.reserved_microdollars
            if amount_microdollars > available:
                raise KeyLimitExceeded(decision)
            key.reserved_microdollars += amount_microdollars
            return decision

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
            # Window counters book by the cap semantics (BYOK only when the
            # key includes it), lazily resetting stale windows — the InMemory
            # twin of the typed release_key window bump.
            if is_byok and not key.include_byok_in_limit:
                return
            floors = window_floors(utcnow())
            state = self.window_usage.setdefault(key_hash, {})
            for window, floor in floors.items():
                entry = state.get(window)
                if entry is None or entry[0] < floor:
                    state[window] = [floor, cost_microdollars]
                else:
                    entry[1] += cost_microdollars

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
            account = self._credit_money[workspace_id]
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
            m = self._credit_money[reservation.workspace_id]
            m.reserved_microdollars -= reservation.amount_microdollars
            m.total_usage_microdollars += actual_microdollars
            reservation.settled = True

    def refund(self, reservation_id: str) -> None:
        with self._lock:
            reservation = self.reservations[reservation_id]
            if reservation.settled:
                return
            m = self._credit_money[reservation.workspace_id]
            m.reserved_microdollars -= reservation.amount_microdollars
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
        authorization_id: str | None = None,
        requested_model_id: str | None = None,
        candidate_model_ids: list[str] | None = None,
        region: str | None = None,
        endpoint_id: str | None = None,
        candidate_endpoint_ids: list[str] | None = None,
        idempotency_key: str | None = None,
        tags: dict[str, str] | None = None,
        idempotency_fingerprint: str | None = None,
        app_id: str = "",
        app_markup_basis_points: int = 0,
        receipt_fee_basis_points: int = 0,
        app_owner_user_id: str = "",
        custom_model_id: str | None = None,
        custom_model_revision: int | None = None,
        custom_model_markup_basis_points: int = 0,
        custom_model_owner_user_id: str = "",
        user_provided_model_id: str | None = None,
        user_provided_model_revision: int | None = None,
        user_model_prompt_price_microdollars_per_m: int | None = None,
        user_model_completion_price_microdollars_per_m: int | None = None,
        user_model_owner_user_id: str | None = None,
        additional_cost_reservation_microdollars: int = 0,
        native_batch_eligible: bool = False,
        settlement: str = "local",
        expires_at: str | None = None,
        deferred_cap_microdollars: int | None = None,
        spend_lease: SpendLeaseArtifact | None = None,
    ) -> GatewayAuthorization:
        with self._lock:
            if idempotency_key is not None:
                existing_id = self.gateway_authorization_id_by_idempotency_key.get(
                    self._gateway_authorization_idempotency_index_key(
                        workspace_id, key_hash, idempotency_key
                    )
                )
                if existing_id is not None:
                    # A REPLAY writes no new authorization, so it must not
                    # consume cap either — the same reason the Postgres store
                    # does the counter move inside this method rather than
                    # before it.
                    return self.gateway_authorizations[existing_id]
            if deferred_cap_microdollars is not None:
                held = self.deferred_outstanding.get(workspace_id, 0)
                if held + estimated_microdollars > deferred_cap_microdollars:
                    raise DeferredSettlementCapReached(
                        f"deferred settlement cap reached for workspace {workspace_id}"
                    )
                self.deferred_outstanding[workspace_id] = held + estimated_microdollars
            authorization = GatewayAuthorization(
                id=authorization_id or f"gwa-{uuid.uuid4().hex}",
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
                tags=dict(tags or {}),
                idempotency_fingerprint=idempotency_fingerprint,
                app_id=app_id,
                app_markup_basis_points=app_markup_basis_points,
                receipt_fee_basis_points=receipt_fee_basis_points,
                app_owner_user_id=app_owner_user_id,
                custom_model_id=custom_model_id,
                custom_model_revision=custom_model_revision,
                custom_model_markup_basis_points=custom_model_markup_basis_points,
                custom_model_owner_user_id=custom_model_owner_user_id,
                user_provided_model_id=user_provided_model_id,
                user_provided_model_revision=user_provided_model_revision,
                user_model_prompt_price_microdollars_per_m=(
                    user_model_prompt_price_microdollars_per_m
                ),
                user_model_completion_price_microdollars_per_m=(
                    user_model_completion_price_microdollars_per_m
                ),
                user_model_owner_user_id=user_model_owner_user_id,
                additional_cost_reservation_microdollars=additional_cost_reservation_microdollars,
                native_batch_eligible=native_batch_eligible,
                settlement=settlement,
                expires_at=expires_at,
                spend_lease_token=spend_lease.token if spend_lease else None,
                spend_lease_id=spend_lease.lease_id if spend_lease else None,
                spend_lease_cap_micro=spend_lease.cap_micro if spend_lease else None,
                spend_lease_gen=spend_lease.gen if spend_lease else None,
                spend_lease_iat=spend_lease.iat if spend_lease else None,
                spend_lease_exp=spend_lease.exp if spend_lease else None,
                spend_lease_issuer_kid=spend_lease.issuer_kid if spend_lease else None,
                spend_lease_boot_kid=spend_lease.boot_kid if spend_lease else None,
                spend_lease_catalog_version=(spend_lease.catalog_version if spend_lease else None),
                spend_lease_status=spend_lease.lease_status if spend_lease else None,
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
