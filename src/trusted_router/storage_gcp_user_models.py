from __future__ import annotations

import datetime as dt
import json
import secrets
from collections.abc import Callable
from dataclasses import fields
from typing import Any

from trusted_router.storage_custom_models import (
    CUSTOM_MODEL_ID_CHARS,
    CUSTOM_MODEL_ID_RANDOM_LENGTH,
    CUSTOM_MODEL_PREFIX,
    custom_model_id_from_slug,
    custom_model_slug,
    normalize_custom_model_id,
)
from trusted_router.storage_gcp_io import SpannerIO, run_in_transaction_with_retry
from trusted_router.storage_models import (
    EncryptedSecretEnvelope,
    UserProvidedModel,
    iso_now,
)
from trusted_router.storage_user_models import USER_PROVIDED_MODEL_LIMIT_PER_USER
from trusted_router.user_model_rules import GATEWAY_RESERVATION_TTL_SECONDS

_CUSTOM_MODEL_KIND = "custom_model"
_USER_PROVIDED_MODEL_KIND = "user_provided_model"
_USER_MODEL_SLOT_KIND = "user_model_slot"
_EDITABLE_FIELDS = (
    "name",
    "kind",
    "description",
    "display_identity",
    "display_name",
    "endpoint_url",
    "upstream_model_id",
    "encrypted_endpoint_api_key",
    "endpoint_key_hint",
    "encrypted_signing_secret",
    "supports_streaming",
    "heartbeat_interval_seconds",
    "max_concurrency",
    "prompt_price_microdollars_per_million_tokens",
    "completion_price_microdollars_per_million_tokens",
    "human_verified",
    "enabled",
    "status",
)


class SpannerUserProvidedModels:
    def __init__(self, io: SpannerIO) -> None:
        self._io = io

    def create(
        self,
        *,
        owner_user_id: str,
        owner_workspace_id: str,
        name: str,
        kind: str,
        description: str = "",
        display_identity: str = "handle",
        display_name: str = "",
        endpoint_url: str,
        upstream_model_id: str | None = None,
        encrypted_endpoint_api_key: EncryptedSecretEnvelope | None = None,
        endpoint_key_hint: str | None = None,
        encrypted_signing_secret: EncryptedSecretEnvelope | None = None,
        supports_streaming: bool = True,
        heartbeat_interval_seconds: int | None = None,
        max_concurrency: int = 4,
        prompt_price_microdollars_per_million_tokens: int = 0,
        completion_price_microdollars_per_million_tokens: int = 0,
        human_verified: bool = False,
        enabled: bool = True,
        status: str = "active",
        slug: str | None = None,
    ) -> UserProvidedModel:
        def txn(transaction: Any) -> UserProvidedModel:
            existing = self._list_for_user_tx(transaction, owner_user_id)
            if len(existing) >= USER_PROVIDED_MODEL_LIMIT_PER_USER:
                raise ValueError("custom_model_limit_exceeded")
            model_id = (
                self._new_id_tx(transaction)
                if slug is None
                else custom_model_id_from_slug(slug)
            )
            if self._model_id_exists_tx(transaction, model_id):
                raise ValueError("custom_model_slug_taken")
            model = UserProvidedModel(
                id=model_id,
                owner_user_id=owner_user_id,
                owner_workspace_id=owner_workspace_id,
                name=name,
                kind=kind,
                description=description,
                display_identity=display_identity,
                display_name=display_name,
                endpoint_url=endpoint_url,
                upstream_model_id=upstream_model_id or custom_model_slug(model_id),
                encrypted_endpoint_api_key=encrypted_endpoint_api_key,
                endpoint_key_hint=endpoint_key_hint,
                encrypted_signing_secret=encrypted_signing_secret,
                supports_streaming=supports_streaming,
                heartbeat_interval_seconds=heartbeat_interval_seconds,
                max_concurrency=max_concurrency,
                prompt_price_microdollars_per_million_tokens=(
                    prompt_price_microdollars_per_million_tokens
                ),
                completion_price_microdollars_per_million_tokens=(
                    completion_price_microdollars_per_million_tokens
                ),
                human_verified=human_verified,
                enabled=enabled,
                status=status,
            )
            self._io.write_entity_tx(
                transaction, _USER_PROVIDED_MODEL_KIND, model.id, model
            )
            self._io.write_entity_tx(
                transaction,
                "user_provided_model_by_user",
                _user_model_id(owner_user_id, model.id),
                {"model_id": model.id},
            )
            return model

        return run_in_transaction_with_retry(self._io.database, txn)

    def list_for_user(self, owner_user_id: str) -> list[UserProvidedModel]:
        # Join the owner index to its model rows in one strong statement.  The
        # body-owner predicate retains the old fail-closed check for a dangling
        # or mis-keyed index row rather than trusting the denormalized pointer.
        pt = self._io.param_types
        with self._io.database.snapshot() as snapshot:
            rows = snapshot.execute_sql(
                "/* user_model_list_for_user */ "
                "SELECT model_record.body "
                "FROM tr_entities AS model_ref "
                "JOIN tr_entities AS model_record "
                "ON model_record.kind='user_provided_model' "
                "AND model_record.id=JSON_VALUE(model_ref.body, '$.model_id') "
                "WHERE model_ref.kind='user_provided_model_by_user' "
                "AND STARTS_WITH(model_ref.id, @prefix) "
                "AND model_ref.id=CONCAT(@owner_user_id, '#', "
                "JSON_VALUE(model_ref.body, '$.model_id')) "
                "AND JSON_VALUE(model_record.body, '$.owner_user_id')=@owner_user_id "
                "ORDER BY JSON_VALUE(model_record.body, '$.created_at'), model_ref.id",
                params={
                    "prefix": f"{owner_user_id}#",
                    "owner_user_id": owner_user_id,
                },
                param_types={
                    "prefix": pt.STRING,
                    "owner_user_id": pt.STRING,
                },
            )
            return [_decode_user_model(row[0]) for row in rows]

    def get(self, model_id: str) -> UserProvidedModel | None:
        return self._io.read_entity(
            _USER_PROVIDED_MODEL_KIND,
            normalize_custom_model_id(model_id),
            UserProvidedModel,
        )

    def get_many(self, model_ids: list[str]) -> dict[str, UserProvidedModel]:
        canonical_ids = list(
            dict.fromkeys(normalize_custom_model_id(model_id) for model_id in model_ids)
        )
        if not canonical_ids:
            return {}
        pt = self._io.param_types
        with self._io.database.snapshot() as snapshot:
            rows = snapshot.execute_sql(
                "/* user_models_by_id */ "
                "SELECT id, body FROM tr_entities "
                "WHERE kind='user_provided_model' AND id IN UNNEST(@model_ids)",
                params={"model_ids": canonical_ids},
                param_types={"model_ids": pt.Array(pt.STRING)},
            )
            return {str(row[0]): _decode_user_model(row[1]) for row in rows}

    def update(
        self,
        model_id: str,
        *,
        owner_user_id: str,
        patch: dict[str, Any],
    ) -> UserProvidedModel:
        values = dict(patch)

        def txn(transaction: Any) -> UserProvidedModel:
            model = self._io.read_entity_tx(
                transaction,
                _USER_PROVIDED_MODEL_KIND,
                normalize_custom_model_id(model_id),
                UserProvidedModel,
            )
            if model is None or model.owner_user_id != owner_user_id:
                raise ValueError("custom_model_not_found")
            old_id = model.id
            new_id = None
            if "slug" in values:
                new_id = custom_model_id_from_slug(str(values.pop("slug")))
                if new_id != model.id and self._model_id_exists_tx(transaction, new_id):
                    raise ValueError("custom_model_slug_taken")
            for key in _EDITABLE_FIELDS:
                if key in values:
                    setattr(model, key, values[key])
            if new_id is not None:
                model.id = new_id
            model.revision += 1
            model.updated_at = iso_now()
            self._io.write_entity_tx(
                transaction, _USER_PROVIDED_MODEL_KIND, model.id, model
            )
            if old_id != model.id:
                self._io.delete_entities_tx(
                    transaction, _USER_PROVIDED_MODEL_KIND, [old_id]
                )
                self._io.delete_entities_tx(
                    transaction,
                    "user_provided_model_by_user",
                    [_user_model_id(owner_user_id, old_id)],
                )
                self._io.write_entity_tx(
                    transaction,
                    "user_provided_model_by_user",
                    _user_model_id(owner_user_id, model.id),
                    {"model_id": model.id},
                )
            return model

        return run_in_transaction_with_retry(self._io.database, txn)

    def delete(self, model_id: str, *, owner_user_id: str) -> bool:
        def txn(transaction: Any) -> bool:
            model = self._io.read_entity_tx(
                transaction,
                _USER_PROVIDED_MODEL_KIND,
                normalize_custom_model_id(model_id),
                UserProvidedModel,
            )
            if model is None or model.owner_user_id != owner_user_id:
                return False
            self._io.delete_entities_tx(
                transaction, _USER_PROVIDED_MODEL_KIND, [model.id]
            )
            self._io.delete_entities_tx(
                transaction,
                "user_provided_model_by_user",
                [_user_model_id(owner_user_id, model.id)],
            )
            return True

        return run_in_transaction_with_retry(self._io.database, txn)

    def set_online(
        self,
        model_id: str,
        *,
        owner_user_id: str,
        online: bool,
    ) -> UserProvidedModel:
        def mutate(model: UserProvidedModel) -> None:
            if model.online != online:
                model.online = online
                model.online_changed_at = iso_now()
            if online:
                # A clock-in is a fresh start: strikes from the previous shift
                # must not make the next single failure clock the owner out.
                model.consecutive_dispatch_failures = 0

        return self._mutate(model_id, mutate, owner_user_id=owner_user_id)

    def record_heartbeat(self, model_id: str, *, expires_at: str) -> UserProvidedModel:
        def mutate(model: UserProvidedModel) -> None:
            model.heartbeat_expires_at = expires_at

        return self._mutate(model_id, mutate)

    def record_probe(
        self,
        model_id: str,
        *,
        status: str,
        checked_at: str,
    ) -> UserProvidedModel:
        def mutate(model: UserProvidedModel) -> None:
            model.probe_status = status
            model.probe_checked_at = checked_at
            if status == "ok":
                # A passing probe is direct evidence the endpoint answers.
                model.consecutive_dispatch_failures = 0

        return self._mutate(model_id, mutate)

    def record_dispatch_result(
        self,
        model_id: str,
        *,
        success: bool,
    ) -> UserProvidedModel:
        def mutate(model: UserProvidedModel) -> None:
            if success:
                model.consecutive_dispatch_failures = 0
                return
            model.consecutive_dispatch_failures += 1
            if model.consecutive_dispatch_failures >= 3 and model.online:
                model.online = False
                model.online_changed_at = iso_now()

        return self._mutate(model_id, mutate)

    def acquire_slot(
        self,
        model_id: str,
        authorization_id: str,
        *,
        limit: int,
        ttl_seconds: int,
    ) -> bool:
        """Admit one in-flight authorization if the model has capacity.

        Each row carries its own ``expires_at`` (now + the kind's total
        dispatch budget + grace, chosen by the caller). Rows past it are
        swept on the next acquire for that model, so an enclave crash between
        authorize and settle blacks a model out for at most one dispatch
        budget, not a fixed multi-hour window. Legacy rows without
        ``expires_at`` fall back to ``created_at`` + the reservation TTL.
        """
        canonical = normalize_custom_model_id(model_id)
        slot_id = _user_model_slot_id(canonical, authorization_id)
        prefix = f"{canonical}#"
        now = dt.datetime.now(dt.UTC)
        legacy_cutoff = now - dt.timedelta(seconds=GATEWAY_RESERVATION_TTL_SECONDS)
        expires_at = now + dt.timedelta(seconds=max(1, ttl_seconds))

        def txn(transaction: Any) -> bool:
            rows = list(
                transaction.execute_sql(
                    "SELECT body FROM tr_entities "
                    "WHERE kind=@kind AND STARTS_WITH(id, @prefix) ORDER BY id",
                    params={"kind": _USER_MODEL_SLOT_KIND, "prefix": prefix},
                    param_types={
                        "kind": self._io.param_types.STRING,
                        "prefix": self._io.param_types.STRING,
                    },
                )
            )
            live_authorizations: set[str] = set()
            expired_ids: list[str] = []
            for (raw_body,) in rows:
                try:
                    payload = json.loads(str(raw_body))
                    stored_authorization_id = str(payload["authorization_id"])
                    created_at = _parse_slot_time(str(payload["created_at"]))
                    raw_expires = payload.get("expires_at")
                    row_expires_at = (
                        _parse_slot_time(str(raw_expires))
                        if raw_expires
                        else created_at + dt.timedelta(seconds=GATEWAY_RESERVATION_TTL_SECONDS)
                    )
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    # Malformed rows fail closed toward capacity until an
                    # operator repairs them; silently ignoring one overbooks.
                    live_authorizations.add(f"malformed:{len(live_authorizations)}")
                    continue
                if row_expires_at <= now or created_at < legacy_cutoff:
                    expired_ids.append(
                        _user_model_slot_id(canonical, stored_authorization_id)
                    )
                else:
                    live_authorizations.add(stored_authorization_id)
            if expired_ids:
                self._io.delete_entities_tx(
                    transaction,
                    _USER_MODEL_SLOT_KIND,
                    expired_ids,
                )
            if authorization_id in live_authorizations:
                return True
            if limit <= 0 or len(live_authorizations) >= limit:
                return False
            self._io.write_entity_tx(
                transaction,
                _USER_MODEL_SLOT_KIND,
                slot_id,
                {
                    "model_id": canonical,
                    "authorization_id": authorization_id,
                    "created_at": now.isoformat().replace("+00:00", "Z"),
                    "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
                },
            )
            return True

        return run_in_transaction_with_retry(self._io.database, txn)

    def release_slot(self, model_id: str, authorization_id: str) -> None:
        self._io.delete_entities(
            _USER_MODEL_SLOT_KIND,
            [_user_model_slot_id(normalize_custom_model_id(model_id), authorization_id)],
        )

    def list_public(
        self,
        *,
        kind: str | None = None,
        limit: int | None = None,
    ) -> list[UserProvidedModel]:
        where = (
            "kind=@entity_kind "
            "AND JSON_VALUE(body, '$.enabled')='true' "
            "AND JSON_VALUE(body, '$.status')='active'"
        )
        params: dict[str, Any] = {"entity_kind": _USER_PROVIDED_MODEL_KIND}
        param_types: dict[str, Any] = {
            "entity_kind": self._io.param_types.STRING,
        }
        if kind is not None:
            where += " AND JSON_VALUE(body, '$.kind')=@model_kind"
            params["model_kind"] = kind
            param_types["model_kind"] = self._io.param_types.STRING
        suffix = " ORDER BY JSON_VALUE(body, '$.created_at'), id"
        if limit is not None:
            suffix += " LIMIT @limit"
            params["limit"] = max(0, int(limit))
            param_types["limit"] = self._io.param_types.INT64
        with self._io.database.snapshot() as snapshot:
            rows = snapshot.execute_sql(
                f"SELECT body FROM tr_entities WHERE {where}{suffix}",  # noqa: S608 - predicates are fixed; values are bound parameters.
                params=params,
                param_types=param_types,
            )
            return [_decode_user_model(row[0]) for row in rows]

    def _mutate(
        self,
        model_id: str,
        mutate: Callable[[UserProvidedModel], None],
        *,
        owner_user_id: str | None = None,
    ) -> UserProvidedModel:
        def txn(transaction: Any) -> UserProvidedModel:
            model = self._io.read_entity_tx(
                transaction,
                _USER_PROVIDED_MODEL_KIND,
                normalize_custom_model_id(model_id),
                UserProvidedModel,
            )
            if model is None or (
                owner_user_id is not None and model.owner_user_id != owner_user_id
            ):
                raise ValueError("custom_model_not_found")
            mutate(model)
            self._io.write_entity_tx(
                transaction, _USER_PROVIDED_MODEL_KIND, model.id, model
            )
            return model

        return run_in_transaction_with_retry(self._io.database, txn)

    def _list_for_user_tx(
        self,
        transaction: Any,
        owner_user_id: str,
    ) -> list[UserProvidedModel]:
        refs = self._io.list_entities(
            "user_provided_model_by_user",
            prefix=f"{owner_user_id}#",
            cls=dict,
        )
        models: list[UserProvidedModel] = []
        for ref in refs:
            model_id = str(ref.get("model_id", ""))
            if not model_id:
                continue
            model = self._io.read_entity_tx(
                transaction,
                _USER_PROVIDED_MODEL_KIND,
                model_id,
                UserProvidedModel,
            )
            if model is not None:
                models.append(model)
        return models

    def _new_id_tx(self, transaction: Any) -> str:
        for _ in range(100):
            suffix = "".join(
                secrets.choice(CUSTOM_MODEL_ID_CHARS)
                for _ in range(CUSTOM_MODEL_ID_RANDOM_LENGTH)
            )
            model_id = f"{CUSTOM_MODEL_PREFIX}{suffix}"
            if not self._model_id_exists_tx(transaction, model_id):
                return model_id
        raise RuntimeError("could not allocate custom model id")

    def _model_id_exists_tx(
        self,
        transaction: Any,
        model_id: str,
    ) -> bool:
        if (
            self._io.read_entity_tx(
                transaction,
                _USER_PROVIDED_MODEL_KIND,
                model_id,
                UserProvidedModel,
            )
            is not None
        ):
            return True
        return (
            self._io.read_entity_tx(
                transaction, _CUSTOM_MODEL_KIND, model_id, dict
            )
            is not None
        )


def _user_model_id(owner_user_id: str, model_id: str) -> str:
    return f"{owner_user_id}#{model_id}"


def _decode_user_model(body: str) -> UserProvidedModel:
    data = json.loads(body)
    known = {field.name for field in fields(UserProvidedModel)}
    return UserProvidedModel(**{key: value for key, value in data.items() if key in known})


def _user_model_slot_id(model_id: str, authorization_id: str) -> str:
    return f"{model_id}#{authorization_id}"


def _parse_slot_time(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)
