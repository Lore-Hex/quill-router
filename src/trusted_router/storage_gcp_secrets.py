"""GCP Secret Manager adapters for lazily loaded runtime material."""

from __future__ import annotations

import base64
from collections.abc import Callable

from trusted_router.spend_leases import decode_secret_seed


def _secret_manager_payload_loader(
    *, project_id: str, secret_name: str
) -> Callable[[], bytes]:
    """Build a lazy raw-payload loader without importing a Google SDK module."""

    def load() -> bytes:
        import google.auth
        from google.auth.transport.requests import AuthorizedSession

        credentials, detected_project = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        project = project_id or detected_project
        if not project:
            raise ValueError("a GCP project is required to load the spend-lease seed")
        resource = (
            secret_name
            if secret_name.startswith("projects/")
            else f"projects/{project}/secrets/{secret_name}"
        )
        response = AuthorizedSession(credentials).get(
            f"https://secretmanager.googleapis.com/v1/{resource}/versions/latest:access",
            timeout=10,
        )
        response.raise_for_status()
        encoded = response.json().get("payload", {}).get("data")
        if not isinstance(encoded, str):
            raise ValueError("Secret Manager response omitted payload.data")
        return base64.b64decode(encoded, validate=True)

    return load


def secret_manager_seed_loader(*, project_id: str, secret_name: str) -> Callable[[], bytes]:
    """Build the existing lazy spend-lease signing-seed loader."""
    payload_loader = _secret_manager_payload_loader(
        project_id=project_id,
        secret_name=secret_name,
    )

    def load() -> bytes:
        return decode_secret_seed(payload_loader())

    return load


def secret_manager_text_loader(*, project_id: str, secret_name: str) -> Callable[[], str]:
    """Build a lazy UTF-8 secret loader for synthetic credentials."""
    payload_loader = _secret_manager_payload_loader(
        project_id=project_id,
        secret_name=secret_name,
    )

    def load() -> str:
        value = payload_loader().decode("utf-8").strip()
        if not value:
            raise ValueError("Secret Manager secret is empty")
        return value

    return load
