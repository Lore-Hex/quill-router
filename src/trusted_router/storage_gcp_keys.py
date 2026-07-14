"""Spanner-backed API key + gateway-authorization lifecycle.

Sibling of InMemoryApiKeys (storage_keys.py). Both expose the same public
surface (create / get_by_hash / get_by_raw / list_for_workspace / delete /
update / reserve_limit / settle_limit / refund_limit /
create_gateway_authorization / get_gateway_authorization /
mark_gateway_authorization_settled / add_usage); SpannerBigtableStore's
public methods become thin one-line delegations.
"""

from __future__ import annotations

import hashlib
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
from trusted_router.storage_gcp_codec import workspace_key_id as _workspace_key_id
from trusted_router.storage_gcp_counters import (
    KEY_LIMIT_COLUMNS,
    KEY_LIMIT_TABLE,
    key_limit_mirror_rows,
)
from trusted_router.storage_gcp_io import SpannerIO, run_in_transaction_with_retry
from trusted_router.storage_models import (
    ApiKey,
    GatewayAuthorization,
    _is_byok,
    iso_now,
)
from trusted_router.types import UsageType


class SpannerApiKeys:
    def __init__(self, io: SpannerIO) -> None:
        self._io = io

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
        budget_alert_only: bool = True,
        tags: dict[str, str] | None = None,
    ) -> tuple[str, ApiKey]:
        raw = raw_key or new_api_key()
        key_id = new_key_id()
        salt = new_hash_salt()
        lookup_hash = lookup_hash_api_key(raw)
        key = ApiKey(
            hash=key_id,
            salt=salt,
            secret_hash=hash_api_key(raw, salt),
            lookup_hash=lookup_hash,
            name=name,
            label=key_label(raw),
            workspace_id=workspace_id,
            creator_user_id=creator_user_id,
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
        with self._io.database.batch() as batch:
            self._io.write_entity_batch(batch, "api_key", key.hash, key)
            batch.insert_or_update(
                table=KEY_LIMIT_TABLE,
                columns=KEY_LIMIT_COLUMNS,
                values=key_limit_mirror_rows(
                    key.hash,
                    key,
                    self._io.spanner_module.COMMIT_TIMESTAMP,
                ),
            )
            self._io.write_entity_batch(batch, "api_key_lookup", lookup_hash, {"key_id": key.hash})
            self._io.write_entity_batch(
                batch,
                "api_key_by_workspace",
                _workspace_key_id(workspace_id, key.hash),
                {"key_id": key.hash},
            )
        return raw, key

    def get_by_hash(self, key_hash: str) -> ApiKey | None:
        return self._io.read_entity("api_key", key_hash, ApiKey)

    def get_by_lookup_hash(self, lookup_hash: str) -> ApiKey | None:
        lookup = self._io.read_entity("api_key_lookup", lookup_hash, dict)
        if not lookup:
            return None
        return self.get_by_hash(str(lookup["key_id"]))

    def get_by_raw(self, raw_key: str) -> ApiKey | None:
        lookup = self._io.read_entity("api_key_lookup", lookup_hash_api_key(raw_key), dict)
        if not lookup:
            return None
        key = self.get_by_hash(str(lookup["key_id"]))
        if key is not None and verify_api_key(raw_key, key.salt, key.secret_hash):
            return key
        return None

    def list_for_workspace(self, workspace_id: str) -> list[ApiKey]:
        refs = self._io.list_entities(
            "api_key_by_workspace", prefix=f"{workspace_id}#", cls=dict
        )
        keys: list[ApiKey] = []
        for ref in refs:
            key = self.get_by_hash(str(ref["key_id"]))
            if key is not None and key.workspace_id == workspace_id:
                keys.append(key)
        keys.sort(key=lambda item: item.created_at, reverse=True)
        return keys

    def delete(self, key_hash: str) -> bool:
        key = self.get_by_hash(key_hash)
        if key is None:
            return False
        self._io.delete_entities("api_key", [key_hash])
        self._io.delete_entities("api_key_lookup", [key.lookup_hash])
        self._io.delete_entities(
            "api_key_by_workspace", [_workspace_key_id(key.workspace_id, key.hash)]
        )
        return True

    def update(self, key_hash: str, patch: dict[str, Any]) -> ApiKey | None:
        key = self.get_by_hash(key_hash)
        if key is None:
            return None
        if key.usage_shard_count > 1:
            requested_limits = [
                patch.get("limit_microdollars"),
                patch.get("limit_daily_microdollars"),
                patch.get("limit_weekly_microdollars"),
                patch.get("limit_monthly_microdollars"),
            ]
            if patch.get("limit") is not None or any(
                value is not None for value in requested_limits
            ):
                raise ValueError(
                    "consolidate API-key usage to one shard before adding a spend limit"
                )
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
        key.updated_at = iso_now()
        # Re-sync the CONFIG columns to tr_key_limit in the same atomic batch.
        # The generic mirror (deleted in C2a) used to do this on every api_key
        # write; the typed authorize path reads the overall per-key cap
        # (limit_micro) from tr_key_limit, so a limit edit that only wrote
        # tr_entities would never reach enforcement (stale cap = spend bypass).
        # KEY_LIMIT_COLUMNS is config-only (no usage/reserved/byok_usage), so
        # this upsert on the existing row cannot clobber typed-owned counters.
        with self._io.database.batch() as batch:
            self._io.write_entity_batch(batch, "api_key", key.hash, key)
            batch.insert_or_update(
                table=KEY_LIMIT_TABLE,
                columns=KEY_LIMIT_COLUMNS,
                values=key_limit_mirror_rows(
                    key.hash,
                    key,
                    self._io.spanner_module.COMMIT_TIMESTAMP,
                ),
            )
        return key

    # ── Per-key spend-cap lifecycle ─────────────────────────────────────
    def reserve_limit(
        self,
        key_hash: str,
        amount_microdollars: int,
        *,
        usage_type: str,
    ) -> None:
        def txn(transaction: Any) -> None:
            key = self._io.read_entity_tx(transaction, "api_key", key_hash, ApiKey)
            if key is None or key.limit_microdollars is None:
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
            self._io.write_entity_tx(transaction, "api_key", key.hash, key)

        run_in_transaction_with_retry(self._io.database, txn)

    def settle_limit(
        self,
        key_hash: str,
        reserved_microdollars: int,
        actual_microdollars: int,
        *,
        usage_type: str,
    ) -> None:
        self._release_limit(key_hash, reserved_microdollars, usage_type=usage_type)
        _ = actual_microdollars

    def refund_limit(
        self,
        key_hash: str,
        reserved_microdollars: int,
        *,
        usage_type: str,
    ) -> None:
        self._release_limit(key_hash, reserved_microdollars, usage_type=usage_type)

    def add_usage(self, key_hash: str, cost_microdollars: int, *, is_byok: bool) -> None:
        """Roll a settled generation's actual cost into the key counters.
        Standalone txn so callers can compose it with their own writes."""
        def txn(transaction: Any) -> None:
            key = self._io.read_entity_tx(transaction, "api_key", key_hash, ApiKey)
            if key is None:
                return
            if is_byok:
                key.byok_usage_microdollars += cost_microdollars
            else:
                key.usage_microdollars += cost_microdollars
            self._io.write_entity_tx(transaction, "api_key", key.hash, key)

        run_in_transaction_with_retry(self._io.database, txn)

    def _release_limit(
        self,
        key_hash: str,
        reserved_microdollars: int,
        *,
        usage_type: str,
    ) -> None:
        def txn(transaction: Any) -> None:
            key = self._io.read_entity_tx(transaction, "api_key", key_hash, ApiKey)
            if key is None or key.limit_microdollars is None:
                return
            if _is_byok(usage_type) and not key.include_byok_in_limit:
                return
            key.reserved_microdollars = max(0, key.reserved_microdollars - reserved_microdollars)
            self._io.write_entity_tx(transaction, "api_key", key.hash, key)

        run_in_transaction_with_retry(self._io.database, txn)

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
        tags: dict[str, str] | None = None,
        idempotency_fingerprint: str | None = None,
        custom_model_id: str | None = None,
        custom_model_revision: int | None = None,
    ) -> GatewayAuthorization:
        existing = (
            self.get_gateway_authorization_by_idempotency_key(
                workspace_id, key_hash, idempotency_key
            )
            if idempotency_key is not None
            else None
        )
        if existing is not None:
            return existing
        auth = GatewayAuthorization(
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
            tags=dict(tags or {}),
            idempotency_fingerprint=idempotency_fingerprint,
            custom_model_id=custom_model_id,
            custom_model_revision=custom_model_revision,
        )
        if idempotency_key is None:
            self._io.write_entity("gateway_authorization", auth.id, auth)
            return auth
        with self._io.database.batch() as batch:
            self._io.write_entity_batch(batch, "gateway_authorization", auth.id, auth)
            self._io.write_entity_batch(
                batch,
                "gateway_authorization_idempotency",
                _gateway_authorization_idempotency_index_id(
                    workspace_id, key_hash, idempotency_key
                ),
                {"authorization_id": auth.id},
            )
        return auth

    def get_gateway_authorization(
        self, authorization_id: str
    ) -> GatewayAuthorization | None:
        return self._io.read_entity(
            "gateway_authorization", authorization_id, GatewayAuthorization
        )

    def get_gateway_authorization_by_idempotency_key(
        self, workspace_id: str, key_hash: str, idempotency_key: str
    ) -> GatewayAuthorization | None:
        ref = self._io.read_entity(
            "gateway_authorization_idempotency",
            _gateway_authorization_idempotency_index_id(
                workspace_id, key_hash, idempotency_key
            ),
            dict,
        )
        if not ref:
            return None
        return self.get_gateway_authorization(str(ref["authorization_id"]))

    def mark_gateway_authorization_settled(self, authorization_id: str) -> None:
        authorization = self.get_gateway_authorization(authorization_id)
        if authorization is None:
            return
        authorization.settled = True
        self._io.write_entity("gateway_authorization", authorization_id, authorization)


def _gateway_authorization_idempotency_index_id(
    workspace_id: str, key_hash: str, idempotency_key: str
) -> str:
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    return f"{workspace_id}#{key_hash}#{digest}"
