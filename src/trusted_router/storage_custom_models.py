from __future__ import annotations

import re
import secrets
import string
import threading
from typing import Any

from trusted_router.storage_models import CustomModel, iso_now

CUSTOM_MODEL_PREFIX = "trustedrouter/user-"
CUSTOM_MODEL_ID_CHARS = string.ascii_lowercase + string.digits
CUSTOM_MODEL_ID_RANDOM_LENGTH = 8
CUSTOM_MODEL_LIMIT_PER_USER = 10
CUSTOM_MODEL_PROMPT_CHAR_LIMIT = 262_144
CUSTOM_MODEL_SLUG_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,62}[a-z0-9])?$")


class InMemoryCustomModels:
    def __init__(self, *, lock: threading.RLock) -> None:
        self._lock = lock
        self.models: dict[str, CustomModel] = {}

    def reset(self) -> None:
        self.models.clear()

    def create(
        self,
        *,
        owner_user_id: str,
        owner_workspace_id: str,
        name: str,
        base_model_id: str,
        hidden_prompt: str,
        enabled: bool = True,
        slug: str | None = None,
    ) -> CustomModel:
        with self._lock:
            existing = [
                model
                for model in self.models.values()
                if model.owner_user_id == owner_user_id
            ]
            if len(existing) >= CUSTOM_MODEL_LIMIT_PER_USER:
                raise ValueError("custom_model_limit_exceeded")
            model_id = (
                self._new_id_locked()
                if slug is None
                else custom_model_id_from_slug(slug)
            )
            if model_id in self.models:
                raise ValueError("custom_model_slug_taken")
            model = CustomModel(
                id=model_id,
                owner_user_id=owner_user_id,
                owner_workspace_id=owner_workspace_id,
                name=name,
                base_model_id=base_model_id,
                hidden_prompt=hidden_prompt,
                enabled=enabled,
            )
            self.models[model.id] = model
            return model

    def list_for_user(self, owner_user_id: str) -> list[CustomModel]:
        with self._lock:
            rows = [
                model
                for model in self.models.values()
                if model.owner_user_id == owner_user_id
            ]
        rows.sort(key=lambda item: item.created_at)
        return rows

    def get(self, model_id: str) -> CustomModel | None:
        with self._lock:
            return self.models.get(normalize_custom_model_id(model_id))

    def update(
        self,
        model_id: str,
        *,
        owner_user_id: str,
        patch: dict[str, Any],
    ) -> CustomModel | None:
        with self._lock:
            model = self.models.get(normalize_custom_model_id(model_id))
            if model is None or model.owner_user_id != owner_user_id:
                return None
            new_id = None
            if "slug" in patch:
                new_id = custom_model_id_from_slug(str(patch.pop("slug")))
                if new_id != model.id and new_id in self.models:
                    raise ValueError("custom_model_slug_taken")
            for key in ("name", "base_model_id", "hidden_prompt", "enabled"):
                if key in patch:
                    setattr(model, key, patch[key])
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

    def _new_id_locked(self) -> str:
        for _ in range(100):
            suffix = "".join(
                secrets.choice(CUSTOM_MODEL_ID_CHARS)
                for _ in range(CUSTOM_MODEL_ID_RANDOM_LENGTH)
            )
            model_id = f"{CUSTOM_MODEL_PREFIX}{suffix}"
            if model_id not in self.models:
                return model_id
        raise RuntimeError("could not allocate custom model id")


def normalize_custom_model_id(model_id: str) -> str:
    value = model_id.strip().lower()
    if value.startswith(CUSTOM_MODEL_PREFIX):
        return value
    if value.startswith("user-"):
        return f"trustedrouter/{value}"
    return value


def custom_model_slug(model_id: str) -> str:
    value = normalize_custom_model_id(model_id)
    if value.startswith(CUSTOM_MODEL_PREFIX):
        return value.removeprefix(CUSTOM_MODEL_PREFIX)
    return value


def custom_model_id_from_slug(slug: str) -> str:
    value = slug.strip().lower()
    if value.startswith(CUSTOM_MODEL_PREFIX):
        value = value.removeprefix(CUSTOM_MODEL_PREFIX)
    elif value.startswith("trustedrouter/"):
        value = value.removeprefix("trustedrouter/")
    if value.startswith("user-"):
        value = value.removeprefix("user-")
    if not CUSTOM_MODEL_SLUG_PATTERN.fullmatch(value):
        raise ValueError("invalid_custom_model_slug")
    return f"{CUSTOM_MODEL_PREFIX}{value}"


def is_custom_model_id(model_id: str | None) -> bool:
    if not model_id:
        return False
    return normalize_custom_model_id(model_id).startswith(CUSTOM_MODEL_PREFIX)
