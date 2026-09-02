from __future__ import annotations

import re
import secrets
import string
import threading
from collections.abc import Callable
from typing import Any

from trusted_router.custom_model_markup_billing import (
    validate_custom_model_markup_basis_points,
)
from trusted_router.storage_models import CustomModel, iso_now

CUSTOM_MODEL_PREFIX = "tr-custom-model/"
USER_PROVIDED_MODEL_PREFIX = "tr-user-model/"
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
        owner_username: str,
        name: str,
        base_model_id: str,
        hidden_prompt: str,
        markup_basis_points: int = 0,
        enabled: bool = True,
        slug: str | None = None,
        other_model_exists: Callable[[str], bool] | None = None,
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
                self._new_id_locked(owner_username, other_model_exists)
                if slug is None
                else custom_model_id_from_slug(slug, username=owner_username)
            )
            if model_id in self.models or (
                other_model_exists is not None and other_model_exists(model_id)
            ):
                raise ValueError("custom_model_slug_taken")
            model = CustomModel(
                id=model_id,
                owner_user_id=owner_user_id,
                owner_workspace_id=owner_workspace_id,
                owner_username=owner_username,
                slug=custom_model_slug(model_id, username=owner_username),
                name=name,
                base_model_id=base_model_id,
                hidden_prompt=hidden_prompt,
                markup_basis_points=validate_custom_model_markup_basis_points(
                    markup_basis_points
                ),
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
        other_model_exists: Callable[[str], bool] | None = None,
    ) -> CustomModel | None:
        values = dict(patch)
        with self._lock:
            model = self.models.get(normalize_custom_model_id(model_id))
            if model is None or model.owner_user_id != owner_user_id:
                return None
            new_id = None
            if "slug" in values:
                new_slug = str(values.pop("slug"))
                new_id = custom_model_id_from_slug(
                    new_slug,
                    username=model.owner_username,
                )
                if new_id != model.id and (
                    new_id in self.models
                    or (other_model_exists is not None and other_model_exists(new_id))
                ):
                    raise ValueError("custom_model_slug_taken")
            for key in (
                "name",
                "base_model_id",
                "hidden_prompt",
                "markup_basis_points",
                "enabled",
            ):
                if key in values:
                    if key == "markup_basis_points":
                        values[key] = validate_custom_model_markup_basis_points(
                            values[key]
                        )
                    setattr(model, key, values[key])
            if new_id is not None and new_id != model.id:
                self.models.pop(model.id, None)
                model.id = new_id
                model.slug = custom_model_slug(
                    new_id,
                    username=model.owner_username,
                )
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

    def _new_id_locked(
        self,
        owner_username: str,
        other_model_exists: Callable[[str], bool] | None,
    ) -> str:
        for _ in range(100):
            suffix = "".join(
                secrets.choice(CUSTOM_MODEL_ID_CHARS)
                for _ in range(CUSTOM_MODEL_ID_RANDOM_LENGTH)
            )
            model_id = custom_model_id_from_slug(suffix, username=owner_username)
            if model_id not in self.models and not (
                other_model_exists is not None and other_model_exists(model_id)
            ):
                return model_id
        raise RuntimeError("could not allocate custom model id")


def normalize_custom_model_id(model_id: str) -> str:
    return model_id.strip().lower()


def normalize_user_provided_model_id(model_id: str) -> str:
    return model_id.strip().lower()


def custom_model_slug(model_id: str, *, username: str) -> str:
    value = normalize_custom_model_id(model_id)
    prefix = f"{CUSTOM_MODEL_PREFIX}{username}-"
    if not value.startswith(prefix):
        raise ValueError("invalid_custom_model_id")
    return value.removeprefix(prefix)


def custom_model_id_from_slug(slug: str, *, username: str) -> str:
    value = validate_model_slug(slug)
    return f"{CUSTOM_MODEL_PREFIX}{username}-{value}"


def user_provided_model_id_from_slug(slug: str, *, username: str) -> str:
    value = validate_model_slug(slug)
    return f"{USER_PROVIDED_MODEL_PREFIX}{username}-{value}"


def user_provided_model_slug(model_id: str, *, username: str) -> str:
    value = normalize_user_provided_model_id(model_id)
    prefix = f"{USER_PROVIDED_MODEL_PREFIX}{username}-"
    if not value.startswith(prefix):
        raise ValueError("invalid_user_model_id")
    return value.removeprefix(prefix)


def validate_model_slug(slug: str) -> str:
    value = slug.strip().lower()
    if not CUSTOM_MODEL_SLUG_PATTERN.fullmatch(value):
        raise ValueError("invalid_custom_model_slug")
    return value


def is_custom_model_id(model_id: str | None) -> bool:
    if not model_id:
        return False
    return normalize_custom_model_id(model_id).startswith(CUSTOM_MODEL_PREFIX)


def is_user_provided_model_id(model_id: str | None) -> bool:
    if not model_id:
        return False
    return normalize_user_provided_model_id(model_id).startswith(
        USER_PROVIDED_MODEL_PREFIX
    )


def is_creator_model_id(model_id: str | None) -> bool:
    return is_custom_model_id(model_id) or is_user_provided_model_id(model_id)
