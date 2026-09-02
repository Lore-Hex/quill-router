from __future__ import annotations

from typing import Any

from trusted_router.storage_gcp_io import SpannerIO, run_in_transaction_with_retry
from trusted_router.storage_models import OAuthApp
from trusted_router.storage_oauth_apps import apply_oauth_app_patch


class SpannerOAuthApps:
    def __init__(self, io: SpannerIO) -> None:
        self._io = io

    def create(self, app: OAuthApp) -> OAuthApp:
        def txn(transaction: Any) -> OAuthApp:
            existing = self._io.read_entity_tx(
                transaction,
                "oauth_app",
                app.id,
                OAuthApp,
            )
            if existing is not None:
                raise ValueError("oauth_app_id_taken")
            self._io.write_entity_tx(transaction, "oauth_app", app.id, app)
            return app

        return run_in_transaction_with_retry(self._io.database, txn)

    def get(self, app_id: str) -> OAuthApp | None:
        return self._io.read_entity("oauth_app", app_id, OAuthApp)

    def list_for_user(self, owner_user_id: str) -> list[OAuthApp]:
        apps = [
            app
            for app in self._io.list_entities("oauth_app", cls=OAuthApp)
            if app.owner_user_id == owner_user_id
        ]
        return sorted(apps, key=lambda app: (app.created_at, app.id))

    def update(self, app_id: str, *, patch: dict[str, Any]) -> OAuthApp | None:
        def txn(transaction: Any) -> OAuthApp | None:
            app = self._io.read_entity_tx(transaction, "oauth_app", app_id, OAuthApp)
            if app is None:
                return None
            apply_oauth_app_patch(app, patch)
            self._io.write_entity_tx(transaction, "oauth_app", app.id, app)
            return app

        return run_in_transaction_with_retry(self._io.database, txn)
