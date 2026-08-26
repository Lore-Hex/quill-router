from __future__ import annotations

import threading
from typing import Any

from trusted_router.storage_models import OAuthApp, iso_now

OAUTH_APP_UPDATE_FIELDS = frozenset(
    {
        "name",
        "redirect_uris",
        "logo_url",
        "markup_basis_points",
        "suspended",
    }
)


def apply_oauth_app_patch(app: OAuthApp, patch: dict[str, Any]) -> OAuthApp:
    """Apply only mutable registry fields; identity and ownership never move."""
    unknown = set(patch) - OAUTH_APP_UPDATE_FIELDS
    if unknown:
        raise ValueError("invalid_oauth_app_patch")
    for name in OAUTH_APP_UPDATE_FIELDS:
        if name not in patch:
            continue
        value = patch[name]
        if name == "redirect_uris":
            value = list(value)
        setattr(app, name, value)
    app.updated_at = iso_now()
    return app


class InMemoryOAuthApps:
    def __init__(self, *, lock: threading.RLock) -> None:
        self._lock = lock
        self.apps: dict[str, OAuthApp] = {}

    def reset(self) -> None:
        self.apps.clear()

    def create(self, app: OAuthApp) -> OAuthApp:
        with self._lock:
            if app.id in self.apps:
                raise ValueError("oauth_app_id_taken")
            self.apps[app.id] = app
            return app

    def get(self, app_id: str) -> OAuthApp | None:
        with self._lock:
            return self.apps.get(app_id)

    def list_for_user(self, owner_user_id: str) -> list[OAuthApp]:
        with self._lock:
            apps = [
                app for app in self.apps.values() if app.owner_user_id == owner_user_id
            ]
        return sorted(apps, key=lambda app: (app.created_at, app.id))

    def update(self, app_id: str, *, patch: dict[str, Any]) -> OAuthApp | None:
        with self._lock:
            app = self.apps.get(app_id)
            if app is None:
                return None
            return apply_oauth_app_patch(app, patch)
