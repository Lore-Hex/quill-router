"""Spanner-backed BYOK provider configuration store.

Sibling of InMemoryByok (storage_byok.py). Both implement upsert / get /
list / delete on (workspace_id, provider) keys. SpannerBigtableStore
composes this through the SpannerIO adapter."""

from __future__ import annotations

from trusted_router.storage_gcp_codec import byok_id
from trusted_router.storage_gcp_io import SpannerIO
from trusted_router.storage_models import ByokProviderConfig, iso_now


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
    ) -> ByokProviderConfig:
        existing = self.get(workspace_id, provider)
        if existing is None:
            config = ByokProviderConfig(
                workspace_id=workspace_id,
                provider=provider,
                secret_ref=secret_ref,
                key_hint=key_hint,
            )
        else:
            config = existing
            config.secret_ref = secret_ref
            config.key_hint = key_hint
            config.updated_at = iso_now()
        self._io.write_entity("byok", byok_id(workspace_id, provider), config)
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
