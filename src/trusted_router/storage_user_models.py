from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable
from typing import Any

from trusted_router.storage_custom_models import (
    CUSTOM_MODEL_ID_CHARS,
    CUSTOM_MODEL_ID_RANDOM_LENGTH,
    CUSTOM_MODEL_PREFIX,
    custom_model_id_from_slug,
    custom_model_slug,
    normalize_custom_model_id,
)
from trusted_router.storage_models import (
    EncryptedSecretEnvelope,
    UserProvidedModel,
    iso_now,
)

USER_PROVIDED_MODEL_LIMIT_PER_USER = 3

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


class InMemoryUserProvidedModels:
    def __init__(self, *, lock: threading.RLock) -> None:
        self._lock = lock
        self.models: dict[str, UserProvidedModel] = {}
        # model_id -> {authorization_id: expires_at (monotonic seconds)}
        self.slots: dict[str, dict[str, float]] = {}

    def reset(self) -> None:
        self.models.clear()
        self.slots.clear()

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
        other_model_exists: Callable[[str], bool] | None = None,
    ) -> UserProvidedModel:
        with self._lock:
            existing = [
                model
                for model in self.models.values()
                if model.owner_user_id == owner_user_id
            ]
            if len(existing) >= USER_PROVIDED_MODEL_LIMIT_PER_USER:
                raise ValueError("custom_model_limit_exceeded")
            model_id = (
                self._new_id_locked(other_model_exists)
                if slug is None
                else custom_model_id_from_slug(slug)
            )
            if model_id in self.models or (
                other_model_exists is not None and other_model_exists(model_id)
            ):
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
            self.models[model.id] = model
            return model

    def list_for_user(self, owner_user_id: str) -> list[UserProvidedModel]:
        with self._lock:
            rows = [
                model
                for model in self.models.values()
                if model.owner_user_id == owner_user_id
            ]
        rows.sort(key=lambda item: item.created_at)
        return rows

    def get(self, model_id: str) -> UserProvidedModel | None:
        with self._lock:
            return self.models.get(normalize_custom_model_id(model_id))

    def update(
        self,
        model_id: str,
        *,
        owner_user_id: str,
        patch: dict[str, Any],
        other_model_exists: Callable[[str], bool] | None = None,
    ) -> UserProvidedModel:
        values = dict(patch)
        with self._lock:
            model = self.models.get(normalize_custom_model_id(model_id))
            if model is None or model.owner_user_id != owner_user_id:
                raise ValueError("custom_model_not_found")
            new_id = None
            if "slug" in values:
                new_id = custom_model_id_from_slug(str(values.pop("slug")))
                if new_id != model.id and (
                    new_id in self.models
                    or (other_model_exists is not None and other_model_exists(new_id))
                ):
                    raise ValueError("custom_model_slug_taken")
            for key in _EDITABLE_FIELDS:
                if key in values:
                    setattr(model, key, values[key])
            if new_id is not None and new_id != model.id:
                self.models.pop(model.id, None)
                model.id = new_id
                self.models[model.id] = model
            model.revision += 1
            model.updated_at = iso_now()
            return model

    def delete(self, model_id: str, *, owner_user_id: str) -> bool:
        with self._lock:
            canonical = normalize_custom_model_id(model_id)
            model = self.models.get(canonical)
            if model is None or model.owner_user_id != owner_user_id:
                return False
            self.models.pop(canonical, None)
            return True

    def set_online(
        self,
        model_id: str,
        *,
        owner_user_id: str,
        online: bool,
    ) -> UserProvidedModel:
        with self._lock:
            model = self._owned_model(model_id, owner_user_id)
            if model.online != online:
                model.online = online
                model.online_changed_at = iso_now()
            if online:
                # A clock-in is a fresh start: strikes from the previous shift
                # must not make the next single failure clock the owner out.
                model.consecutive_dispatch_failures = 0
            return model

    def record_heartbeat(self, model_id: str, *, expires_at: str) -> UserProvidedModel:
        with self._lock:
            model = self._required_model(model_id)
            model.heartbeat_expires_at = expires_at
            return model

    def record_probe(
        self,
        model_id: str,
        *,
        status: str,
        checked_at: str,
    ) -> UserProvidedModel:
        with self._lock:
            model = self._required_model(model_id)
            model.probe_status = status
            model.probe_checked_at = checked_at
            if status == "ok":
                # A passing probe is direct evidence the endpoint answers.
                model.consecutive_dispatch_failures = 0
            return model

    def record_dispatch_result(
        self,
        model_id: str,
        *,
        success: bool,
    ) -> UserProvidedModel:
        with self._lock:
            model = self._required_model(model_id)
            if success:
                model.consecutive_dispatch_failures = 0
                return model
            model.consecutive_dispatch_failures += 1
            if model.consecutive_dispatch_failures >= 3 and model.online:
                model.online = False
                model.online_changed_at = iso_now()
            return model

    def acquire_slot(
        self,
        model_id: str,
        authorization_id: str,
        *,
        limit: int,
        ttl_seconds: int,
    ) -> bool:
        """Admit one in-flight authorization if the model has capacity.

        A slot expires after ``ttl_seconds`` (the caller passes the kind's
        total dispatch budget plus grace) so an enclave that dies between
        authorize and settle cannot black a model out for longer than one
        dispatch could legitimately have taken.
        """
        with self._lock:
            now = time.monotonic()
            slots = self.slots.setdefault(normalize_custom_model_id(model_id), {})
            for stale in [aid for aid, exp in slots.items() if exp <= now]:
                slots.pop(stale, None)
            if authorization_id in slots:
                return True
            if limit <= 0 or len(slots) >= limit:
                return False
            slots[authorization_id] = now + max(1, ttl_seconds)
            return True

    def release_slot(self, model_id: str, authorization_id: str) -> None:
        with self._lock:
            canonical = normalize_custom_model_id(model_id)
            slots = self.slots.get(canonical)
            if slots is None:
                return
            slots.pop(authorization_id, None)
            if not slots:
                self.slots.pop(canonical, None)

    def list_public(self, *, kind: str | None = None) -> list[UserProvidedModel]:
        with self._lock:
            rows = [
                model
                for model in self.models.values()
                if model.enabled
                and model.status == "active"
                and (kind is None or model.kind == kind)
            ]
        rows.sort(key=lambda item: item.created_at)
        return rows

    def _owned_model(self, model_id: str, owner_user_id: str) -> UserProvidedModel:
        model = self.models.get(normalize_custom_model_id(model_id))
        if model is None or model.owner_user_id != owner_user_id:
            raise ValueError("custom_model_not_found")
        return model

    def _required_model(self, model_id: str) -> UserProvidedModel:
        model = self.models.get(normalize_custom_model_id(model_id))
        if model is None:
            raise ValueError("custom_model_not_found")
        return model

    def _new_id_locked(
        self,
        other_model_exists: Callable[[str], bool] | None,
    ) -> str:
        for _ in range(100):
            suffix = "".join(
                secrets.choice(CUSTOM_MODEL_ID_CHARS)
                for _ in range(CUSTOM_MODEL_ID_RANDOM_LENGTH)
            )
            model_id = f"{CUSTOM_MODEL_PREFIX}{suffix}"
            if model_id not in self.models and not (
                other_model_exists is not None and other_model_exists(model_id)
            ):
                return model_id
        raise RuntimeError("could not allocate custom model id")
