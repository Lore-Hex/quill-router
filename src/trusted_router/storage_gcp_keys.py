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
from trusted_router.spend_windows import KeyWindowLimitDecision
from trusted_router.storage_gcp_codec import workspace_key_id as _workspace_key_id
from trusted_router.storage_gcp_counters import (
    KEY_LIMIT_COLUMNS,
    KEY_LIMIT_TABLE,
    key_limit_mirror_rows,
    key_usage_shard_count,
)
from trusted_router.storage_gcp_io import SpannerIO, run_in_transaction_with_retry
from trusted_router.storage_key_patch import TYPED_LIMIT_PATCH_FIELDS, apply_key_patch
from trusted_router.storage_key_usage import api_key_from_json, api_key_usage_snapshot
from trusted_router.storage_models import (
    ApiKey,
    ApiKeyUsageSnapshot,
    GatewayAuthorization,
    _is_byok,
)
from trusted_router.types import UsageType

_CONSOLE_API_KEYS_SQL = """
    /* console_api_keys */
    SELECT
      key_record.body,
      key_limit.shard,
      key_limit.usage,
      key_limit.byok_usage,
      key_limit.reserved,
      key_limit.day_usage,
      key_limit.day_start,
      key_limit.week_usage,
      key_limit.week_start,
      key_limit.month_usage,
      key_limit.month_start
    FROM tr_entities AS key_index
    JOIN tr_entities AS key_record
      ON key_record.kind='api_key'
     AND key_record.id=JSON_VALUE(key_index.body, '$.key_id')
     AND JSON_VALUE(key_record.body, '$.hash')=key_record.id
    LEFT JOIN tr_key_limit AS key_limit
      ON key_limit.key_hash=key_record.id
     AND key_limit.shard>=0
     AND key_limit.shard<COALESCE(
       CAST(JSON_VALUE(key_record.body, '$.usage_shard_count') AS INT64),
       1
     )
    WHERE key_index.kind='api_key_by_workspace'
      AND STARTS_WITH(key_index.id, @prefix)
      AND key_index.id=CONCAT(@workspace_id, '#', key_record.id)
      AND JSON_VALUE(key_record.body, '$.workspace_id')=@workspace_id
    ORDER BY JSON_VALUE(key_record.body, '$.created_at') DESC,
             key_record.id,
             key_limit.shard
"""

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
        budget_alert_only: bool = False,
        tags: dict[str, str] | None = None,
        scopes: list[str] | None = None,
        app_id: str = "",
        usage_shard_count: int = 1,
    ) -> tuple[str, ApiKey]:
        validated_scopes = validate_api_key_scopes(scopes, management=management)
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
            usage_shard_count=usage_shard_count,
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
        refs = self._io.list_entities("api_key_by_workspace", prefix=f"{workspace_id}#", cls=dict)
        keys: list[ApiKey] = []
        for ref in refs:
            key = self.get_by_hash(str(ref["key_id"]))
            if key is not None and key.workspace_id == workspace_id:
                keys.append(key)
        keys.sort(key=lambda item: item.created_at, reverse=True)
        return keys

    def list_with_usage_for_workspace(self, workspace_id: str) -> list[ApiKeyUsageSnapshot]:
        """Fetch every page key and all configured usage shards in one RPC.

        The snapshot is deliberately strong.  Key creation, deletion, and
        budget edits are management operations with read-your-write semantics;
        weakening the combined read just to make display counters stale would
        make the newly-created or deleted key itself stale too.
        """
        pt = self._io.param_types
        with self._io.database.snapshot() as snapshot:
            rows = list(
                snapshot.execute_sql(
                    _CONSOLE_API_KEYS_SQL,
                    params={
                        "workspace_id": workspace_id,
                        "prefix": f"{workspace_id}#",
                    },
                    param_types={
                        "workspace_id": pt.STRING,
                        "prefix": pt.STRING,
                    },
                )
            )

        grouped: dict[str, tuple[ApiKey, list[list[Any]]]] = {}
        for row in rows:
            api_key = api_key_from_json(row[0])
            # SQL carries the same ownership predicate.  Keep the boundary in
            # Python as defense in depth if a future query edit or backend
            # adapter returns a broader row set.
            if api_key.workspace_id != workspace_id:
                continue
            entry = grouped.setdefault(api_key.hash, (api_key, []))
            if row[1] is not None:
                entry[1].append(list(row[1:]))
        return [
            api_key_usage_snapshot(api_key, usage_rows) for api_key, usage_rows in grouped.values()
        ]

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
        if not (TYPED_LIMIT_PATCH_FIELDS & patch.keys()):
            # Metadata edits do not touch typed counter configuration. Besides
            # avoiding unnecessary hot-row writes, this preserves an existing
            # escrow partition exactly.
            apply_key_patch(key, patch)
            self._io.write_entity("api_key", key.hash, key)
            return key

        # A cap edit must repartition against strongly-read usage and holds in
        # the same transaction. A snapshot followed by a batch could race an
        # authorize and accidentally mint headroom on another shard.
        pt = self._io.param_types

        def txn(transaction: Any) -> ApiKey | None:
            current = self._io.read_entity_tx(
                transaction,
                "api_key",
                key_hash,
                ApiKey,
            )
            if current is None:
                return None
            apply_key_patch(current, patch)
            shard_count = key_usage_shard_count(current)
            usage_rows = list(
                transaction.execute_sql(
                    "SELECT shard, usage, byok_usage, reserved "
                    "FROM tr_key_limit WHERE key_hash=@kh AND shard>=0 "
                    "AND shard<@shard_count ORDER BY shard",
                    params={"kh": key_hash, "shard_count": shard_count},
                    param_types={"kh": pt.STRING, "shard_count": pt.INT64},
                )
            )
            config_rows = key_limit_mirror_rows(
                current.hash,
                current,
                self._io.spanner_module.COMMIT_TIMESTAMP,
                usage_rows=usage_rows,
            )
            self._io.write_entity_tx(transaction, "api_key", current.hash, current)
            transaction.insert_or_update(
                table=KEY_LIMIT_TABLE,
                columns=KEY_LIMIT_COLUMNS,
                values=config_rows,
            )
            return current

        return run_in_transaction_with_retry(self._io.database, txn)

    # ── Per-key spend-cap lifecycle ─────────────────────────────────────
    def reserve_limit(
        self,
        key_hash: str,
        amount_microdollars: int,
        *,
        usage_type: str,
    ) -> KeyWindowLimitDecision | None:
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
        # This retired JSON-counter path has no authoritative spend-window
        # counters. Production gateway authorization uses the typed path below;
        # never fabricate window headers here from stale JSON mirrors.
        return None

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
        invocation_nonce: str | None = None,
    ) -> GatewayAuthorization:
        if deferred_cap_microdollars is not None:
            # Deferred settlement is a PEER-plane mechanism: a plane spending
            # on credit at some other plane's ledger. This is the HOME plane's
            # own store, so a caller asking it to defer has a configuration
            # error, and silently ignoring the argument would admit spend
            # against a cap that is not being enforced anywhere.
            raise NotImplementedError(
                "deferred settlement is not available on the home plane's store"
            )
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
            id=authorization_id or f"gwa-{uuid.uuid4().hex}",
            workspace_id=workspace_id,
            key_hash=key_hash,
            model_id=model_id,
            provider=provider,
            usage_type=UsageType.coerce(usage_type),
            estimated_microdollars=estimated_microdollars,
            credit_reservation_id=credit_reservation_id,
            settlement=settlement,
            expires_at=expires_at,
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
            user_model_prompt_price_microdollars_per_m=(user_model_prompt_price_microdollars_per_m),
            user_model_completion_price_microdollars_per_m=(
                user_model_completion_price_microdollars_per_m
            ),
            user_model_owner_user_id=user_model_owner_user_id,
            additional_cost_reservation_microdollars=additional_cost_reservation_microdollars,
            native_batch_eligible=native_batch_eligible,
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
            invocation_nonce=invocation_nonce,
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

    def get_gateway_authorization(self, authorization_id: str) -> GatewayAuthorization | None:
        return self._io.read_entity("gateway_authorization", authorization_id, GatewayAuthorization)

    def get_gateway_authorization_by_idempotency_key(
        self, workspace_id: str, key_hash: str, idempotency_key: str
    ) -> GatewayAuthorization | None:
        ref = self._io.read_entity(
            "gateway_authorization_idempotency",
            _gateway_authorization_idempotency_index_id(workspace_id, key_hash, idempotency_key),
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
