"""BYOK provider configuration store.

Bring-your-own-key provider configs (Anthropic, OpenAI, Mistral, etc.)
keyed by (workspace_id, provider). One row per workspace per provider;
upsert preserves the original created_at, replaces secret_ref + hint."""

from __future__ import annotations

import threading

from trusted_router.storage_models import ByokProviderConfig, iso_now


class InMemoryByok:
    def __init__(self, *, lock: threading.RLock) -> None:
        self._lock = lock
        self.providers: dict[tuple[str, str], ByokProviderConfig] = {}

    def reset(self) -> None:
        self.providers.clear()

    def upsert(
        self,
        *,
        workspace_id: str,
        provider: str,
        secret_ref: str,
        key_hint: str | None,
    ) -> ByokProviderConfig:
        with self._lock:
            existing = self.providers.get((workspace_id, provider))
            if existing is None:
                config = ByokProviderConfig(
                    workspace_id=workspace_id,
                    provider=provider,
                    secret_ref=secret_ref,
                    key_hint=key_hint,
                )
                self.providers[(workspace_id, provider)] = config
                return config
            existing.secret_ref = secret_ref
            existing.key_hint = key_hint
            existing.updated_at = iso_now()
            return existing

    def list_for_workspace(self, workspace_id: str) -> list[ByokProviderConfig]:
        with self._lock:
            return [
                config
                for (wid, _), config in self.providers.items()
                if wid == workspace_id
            ]

    def get(self, workspace_id: str, provider: str) -> ByokProviderConfig | None:
        with self._lock:
            return self.providers.get((workspace_id, provider))

    def delete(self, workspace_id: str, provider: str) -> bool:
        with self._lock:
            return self.providers.pop((workspace_id, provider), None) is not None
