from __future__ import annotations

import secrets
import string
from typing import Any

from trusted_router.storage_custom_models import (
    CUSTOM_MODEL_ID_RANDOM_LENGTH,
    CUSTOM_MODEL_LIMIT_PER_USER,
    CUSTOM_MODEL_PREFIX,
    custom_model_id_from_slug,
    normalize_custom_model_id,
)
from trusted_router.storage_gcp_io import SpannerIO, run_in_transaction_with_retry
from trusted_router.storage_models import CustomModel, iso_now

_ID_CHARS = string.ascii_lowercase + string.digits


class SpannerCustomModels:
    def __init__(self, io: SpannerIO) -> None:
        self._io = io

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
        def txn(transaction: Any) -> CustomModel:
            existing = self._list_for_user_tx(transaction, owner_user_id)
            if len(existing) >= CUSTOM_MODEL_LIMIT_PER_USER:
                raise ValueError("custom_model_limit_exceeded")
            model_id = (
                self._new_id_tx(transaction) if slug is None else custom_model_id_from_slug(slug)
            )
            if (
                self._io.read_entity_tx(transaction, "custom_model", model_id, CustomModel)
                is not None
            ):
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
            self._io.write_entity_tx(transaction, "custom_model", model.id, model)
            self._io.write_entity_tx(
                transaction,
                "custom_model_by_user",
                _user_model_id(owner_user_id, model.id),
                {"model_id": model.id},
            )
            return model

        return run_in_transaction_with_retry(self._io.database, txn)

    def list_for_user(self, owner_user_id: str) -> list[CustomModel]:
        rows = self._io.list_entities(
            "custom_model_by_user",
            prefix=f"{owner_user_id}#",
            cls=dict,
        )
        models: list[CustomModel] = []
        for row in rows:
            model_id = str(row.get("model_id", ""))
            if not model_id:
                continue
            model = self.get(model_id)
            if model is not None and model.owner_user_id == owner_user_id:
                models.append(model)
        models.sort(key=lambda item: item.created_at)
        return models

    def get(self, model_id: str) -> CustomModel | None:
        return self._io.read_entity(
            "custom_model", normalize_custom_model_id(model_id), CustomModel
        )

    def update(
        self,
        model_id: str,
        *,
        owner_user_id: str,
        patch: dict[str, Any],
    ) -> CustomModel | None:
        def txn(transaction: Any) -> CustomModel | None:
            model = self._io.read_entity_tx(
                transaction, "custom_model", normalize_custom_model_id(model_id), CustomModel
            )
            if model is None or model.owner_user_id != owner_user_id:
                return None
            old_id = model.id
            new_id = None
            if "slug" in patch:
                new_id = custom_model_id_from_slug(str(patch.pop("slug")))
                if (
                    new_id != model.id
                    and self._io.read_entity_tx(transaction, "custom_model", new_id, CustomModel)
                    is not None
                ):
                    raise ValueError("custom_model_slug_taken")
            for key in ("name", "base_model_id", "hidden_prompt", "enabled"):
                if key in patch:
                    setattr(model, key, patch[key])
            if new_id is not None:
                model.id = new_id
            model.revision += 1
            model.updated_at = iso_now()
            self._io.write_entity_tx(transaction, "custom_model", model.id, model)
            if old_id != model.id:
                self._io.delete_entities_tx(transaction, "custom_model", [old_id])
                self._io.delete_entities_tx(
                    transaction,
                    "custom_model_by_user",
                    [_user_model_id(owner_user_id, old_id)],
                )
                self._io.write_entity_tx(
                    transaction,
                    "custom_model_by_user",
                    _user_model_id(owner_user_id, model.id),
                    {"model_id": model.id},
                )
            return model

        return run_in_transaction_with_retry(self._io.database, txn)

    def delete(self, model_id: str, *, owner_user_id: str) -> bool:
        def txn(transaction: Any) -> bool:
            model = self._io.read_entity_tx(
                transaction, "custom_model", normalize_custom_model_id(model_id), CustomModel
            )
            if model is None or model.owner_user_id != owner_user_id:
                return False
            self._io.delete_entities_tx(transaction, "custom_model", [model.id])
            self._io.delete_entities_tx(
                transaction,
                "custom_model_by_user",
                [_user_model_id(owner_user_id, model.id)],
            )
            return True

        return run_in_transaction_with_retry(self._io.database, txn)

    def _list_for_user_tx(self, transaction: Any, owner_user_id: str) -> list[CustomModel]:
        refs = self._io.list_entities(
            "custom_model_by_user",
            prefix=f"{owner_user_id}#",
            cls=dict,
        )
        models: list[CustomModel] = []
        for ref in refs:
            model_id = str(ref.get("model_id", ""))
            if not model_id:
                continue
            model = self._io.read_entity_tx(transaction, "custom_model", model_id, CustomModel)
            if model is not None:
                models.append(model)
        return models

    def _new_id_tx(self, transaction: Any) -> str:
        for _ in range(100):
            suffix = "".join(
                secrets.choice(_ID_CHARS) for _ in range(CUSTOM_MODEL_ID_RANDOM_LENGTH)
            )
            model_id = f"{CUSTOM_MODEL_PREFIX}{suffix}"
            if self._io.read_entity_tx(transaction, "custom_model", model_id, CustomModel) is None:
                return model_id
        raise RuntimeError("could not allocate custom model id")


def _user_model_id(owner_user_id: str, model_id: str) -> str:
    return f"{owner_user_id}#{model_id}"
