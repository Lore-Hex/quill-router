from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from google.api_core import exceptions as gcp_exceptions

from trusted_router.auth import ManagementPrincipal, SettingsDep
from trusted_router.byok_crypto import encrypt_byok_secret
from trusted_router.catalog import PROVIDERS
from trusted_router.errors import api_error
from trusted_router.schemas import UpsertByokRequest
from trusted_router.security import key_label
from trusted_router.serialization import byok_provider_shape
from trusted_router.storage import STORE
from trusted_router.types import ErrorType

log = logging.getLogger(__name__)


def register_byok_routes(router: APIRouter) -> None:
    @router.get("/byok/providers")
    async def byok_providers(principal: ManagementPrincipal) -> dict[str, list[dict[str, Any]]]:
        configs = STORE.list_byok_providers(principal.workspace.id)
        return {"data": [byok_provider_shape(c) for c in configs]}

    @router.get("/byok/providers/{provider}")
    async def byok_provider(provider: str, principal: ManagementPrincipal) -> dict[str, Any]:
        slug = _require_byok_provider(provider)
        config = STORE.get_byok_provider(principal.workspace.id, slug)
        if config is None:
            raise api_error(404, "BYOK provider is not configured", ErrorType.NOT_FOUND)
        return {"data": byok_provider_shape(config)}

    @router.put("/byok/providers/{provider}")
    async def upsert_byok_provider(
        provider: str,
        body: UpsertByokRequest,
        principal: ManagementPrincipal,
        settings: SettingsDep,
    ) -> JSONResponse:
        slug = _require_byok_provider(provider)
        if not settings.byok_registration_enabled:
            # Read-only replicas are not the registration authority. Refuse
            # the write cleanly here before attempting a KMS encrypt that
            # would be denied, and point callers at the primary endpoint.
            raise api_error(
                503,
                "BYOK key registration is handled by the primary control plane. "
                "Register keys at https://api.trustedrouter.com.",
                ErrorType.SERVICE_UNAVAILABLE,
            )
        api_key = body.api_key or body.key
        secret_ref = body.secret_ref
        key_hint = body.key_hint
        encrypted_secret = None

        if api_key is not None:
            if secret_ref is not None and secret_ref.strip() and not secret_ref.strip().startswith("byok://"):
                raise api_error(
                    400,
                    "raw api_key uploads are stored as TrustedRouter BYOK envelopes; omit secret_ref",
                    ErrorType.BAD_REQUEST,
                )
            secret_ref = _default_byok_secret_ref(principal.workspace.id, slug)
            if key_hint is None:
                key_hint = _secret_hint(api_key)
            try:
                encrypted_secret = encrypt_byok_secret(
                    api_key,
                    settings,
                    workspace_id=principal.workspace.id,
                    provider=slug,
                )
            except gcp_exceptions.PermissionDenied as exc:
                # The BYOK envelope DEK is wrapped with the GCP KMS
                # byok-envelope key, which only the primary control-plane SA
                # may encrypt with. Return a clean, actionable 503 instead of
                # an unhandled 500 + KMS stack trace if this endpoint lacks
                # encrypt permission.
                log.warning(
                    "byok.encrypt_permission_denied",
                    extra={"provider": slug, "workspace_id": principal.workspace.id},
                )
                raise api_error(
                    503,
                    "BYOK key registration is not available on this endpoint. "
                    "Register keys through the primary API at https://api.trustedrouter.com.",
                    ErrorType.SERVICE_UNAVAILABLE,
                ) from exc
            except gcp_exceptions.GoogleAPICallError as exc:
                # Any other KMS/RPC failure (transient unavailability, timeout):
                # a best-effort retry-able error, not an unhandled 500.
                log.error(
                    "byok.encrypt_failed",
                    extra={"provider": slug, "error": type(exc).__name__},
                )
                raise api_error(
                    503,
                    "BYOK key encryption is temporarily unavailable. Please retry shortly.",
                    ErrorType.SERVICE_UNAVAILABLE,
                ) from exc
        elif secret_ref is None:
            raise api_error(400, "api_key or secret_ref is required", ErrorType.BAD_REQUEST)

        if not secret_ref:
            raise api_error(400, "secret_ref must be a non-empty string", ErrorType.BAD_REQUEST)
        if encrypted_secret is None and secret_ref.startswith("byok://"):
            raise api_error(
                400,
                "byok:// refs are generated from raw api_key uploads",
                ErrorType.BAD_REQUEST,
            )
        if _looks_like_raw_secret(secret_ref):
            raise api_error(
                400,
                "secret_ref must point to Secret Manager or an environment alias",
                ErrorType.BAD_REQUEST,
            )

        created = STORE.get_byok_provider(principal.workspace.id, slug) is None
        config = STORE.upsert_byok_provider(
            workspace_id=principal.workspace.id,
            provider=slug,
            secret_ref=secret_ref,
            key_hint=key_hint,
            encrypted_secret=encrypted_secret,
        )
        return JSONResponse(
            {"data": byok_provider_shape(config)},
            status_code=201 if created else 200,
        )

    @router.delete("/byok/providers/{provider}")
    async def delete_byok_provider(
        provider: str,
        principal: ManagementPrincipal,
    ) -> dict[str, Any]:
        slug = _require_byok_provider(provider)
        if not STORE.delete_byok_provider(principal.workspace.id, slug):
            raise api_error(404, "BYOK provider is not configured", ErrorType.NOT_FOUND)
        return {"data": {"deleted": True, "provider": slug}}


def _require_byok_provider(provider: str) -> str:
    slug = provider.strip().lower()
    catalog_provider = PROVIDERS.get(slug)
    if catalog_provider is None or not catalog_provider.supports_byok:
        raise api_error(
            400,
            "Provider does not support BYOK",
            ErrorType.PROVIDER_NOT_SUPPORTED,
        )
    return slug


def _default_byok_secret_ref(workspace_id: str, provider: str) -> str:
    return f"byok://workspaces/{workspace_id}/providers/{provider}"


def _secret_hint(api_key: str) -> str:
    stripped = api_key.strip()
    if len(stripped) <= 10:
        return key_label(stripped)
    return f"{stripped[:6]}...{stripped[-4:]}"


def _looks_like_raw_secret(value: str) -> bool:
    stripped = value.strip()
    if stripped.startswith(("secretmanager://", "projects/", "env://")):
        return False
    if stripped.startswith(("sk-", "sk_", "sk-ant-", "AIza", "csk-")):
        return True
    return len(stripped) > 48 and "/" not in stripped and ":" not in stripped
