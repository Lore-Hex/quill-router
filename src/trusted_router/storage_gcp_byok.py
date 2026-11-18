"""Spanner-backed BYOK provider configuration store.

Sibling of InMemoryByok (storage_byok.py). Both implement upsert / get /
list / delete on (workspace_id, provider) keys. SpannerStore
composes this through the SpannerIO adapter."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from trusted_router.storage_gcp_codec import byok_id
from trusted_router.storage_gcp_io import SpannerIO, run_in_transaction_with_retry
from trusted_router.storage_models import (
    ByokProviderConfig,
    EncryptedSecretEnvelope,
    iso_now,
)


class SpannerByok:
    def __init__(self, io: SpannerIO) -> None:
        self._io = io

    def upsert(
        self,
        *,
        workspace_id: str,
        provider: str,
        secret_ref: str,
        key_hint: str | None,
        encrypted_secret: EncryptedSecretEnvelope | None = None,
    ) -> ByokProviderConfig:
        existing = self.get(workspace_id, provider)
        if existing is None:
            config = ByokProviderConfig(
                workspace_id=workspace_id,
                provider=provider,
                secret_ref=secret_ref,
                key_hint=key_hint,
                encrypted_secret=encrypted_secret,
            )
        else:
            config = existing
            config.secret_ref = secret_ref
            config.key_hint = key_hint
            config.encrypted_secret = encrypted_secret
            config.updated_at = iso_now()
        self._io.write_entity("byok", byok_id(workspace_id, provider), config)
        return config

    def mutate(
        self,
        workspace_id: str,
        provider: str,
        change: Callable[[ByokProviderConfig | None], ByokProviderConfig | None],
    ) -> ByokProviderConfig | None:
        """Read, change and write one provider row in one transaction, so
        concurrent key edits serialize instead of overwriting each other.
        ``change`` may run again on a retried transaction. None deletes."""
        entity_id = byok_id(workspace_id, provider)

        def txn(transaction: Any) -> ByokProviderConfig | None:
            current = self._io.read_entity_tx(transaction, "byok", entity_id, ByokProviderConfig)
            updated = change(current)
            if updated is None:
                if current is not None:
                    self._io.delete_entities_tx(transaction, "byok", [entity_id])
                return None
            updated.updated_at = iso_now()
            self._io.write_entity_tx(transaction, "byok", entity_id, updated)
            return updated

        return cast(ByokProviderConfig | None, run_in_transaction_with_retry(self._io.database, txn))

    def save(self, config: ByokProviderConfig) -> ByokProviderConfig:
        """Write a whole provider row: its default key and every other key."""
        config.updated_at = iso_now()
        self._io.write_entity("byok", byok_id(config.workspace_id, config.provider), config)
        return config

    def list_for_workspace(self, workspace_id: str) -> list[ByokProviderConfig]:
        return self._io.list_entities(
            "byok", prefix=f"{workspace_id}#", cls=ByokProviderConfig
        )

    def get(self, workspace_id: str, provider: str) -> ByokProviderConfig | None:
        return self._io.read_entity(
            "byok", byok_id(workspace_id, provider), ByokProviderConfig
        )

    def delete(self, workspace_id: str, provider: str) -> bool:
        key = byok_id(workspace_id, provider)
        if self._io.read_entity("byok", key, ByokProviderConfig) is None:
            return False
        self._io.delete_entities("byok", [key])
        return True
