from __future__ import annotations

import copy
import logging
import secrets
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from trusted_router.auth import ManagementPrincipal, SettingsDep
from trusted_router.billing_policy import WALLET_ONLY_BYOK_MESSAGE, is_wallet_only_principal
from trusted_router.byok_cloud_credentials import (
    CloudCredentialError,
    is_cloud_credential_provider,
    normalize_cloud_credential,
)
from trusted_router.byok_crypto import encrypt_byok_secret
from trusted_router.catalog import PROVIDERS
from trusted_router.errors import api_error
from trusted_router.key_management import KeyAccessDenied, KeyManagementError
from trusted_router.provider_compat import (
    byok_storage_provider_candidates,
    canonical_byok_provider,
)
from trusted_router.schemas import CreateByokKeyRequest, PatchByokKeyRequest, UpsertByokRequest
from trusted_router.security import key_label
from trusted_router.serialization import byok_key_shape, byok_provider_shape
from trusted_router.storage import STORE
from trusted_router.storage_models import (
    MAX_BYOK_KEYS_PER_PROVIDER,
    ByokKey,
    ByokProviderConfig,
    EncryptedSecretEnvelope,
    iso_now,
)
from trusted_router.types import ErrorType

log = logging.getLogger(__name__)


def register_byok_routes(router: APIRouter) -> None:
    @router.get(
        "/byok/providers",
        summary="List configured BYOK providers",
        description="List BYOK provider configurations for the authenticated workspace. Returns metadata and key hints, never raw credentials. Served on https://trustedrouter.com/v1.",
    )
    def byok_providers(principal: ManagementPrincipal) -> dict[str, list[dict[str, Any]]]:
        configs = STORE.list_byok_providers(principal.workspace.id)
        # Prefer a post-split row when both exist, and never expose the retired
        # `gemini` storage key as a third public provider.
        rows: dict[str, dict[str, Any]] = {}
        for config in sorted(configs, key=lambda item: item.provider == "gemini"):
            public_provider = canonical_byok_provider(config.provider)
            shape = byok_provider_shape(config)
            shape["provider"] = public_provider
            rows.setdefault(public_provider, shape)
        return {"data": list(rows.values())}

    @router.get(
        "/byok/providers/{provider}",
        summary="Get a BYOK provider",
        description="Get one workspace provider configuration; returns 404 when not configured. Served on https://trustedrouter.com/v1.",
    )
    def byok_provider(provider: str, principal: ManagementPrincipal) -> dict[str, Any]:
        slug = _require_byok_provider(provider)
        config = _get_byok_provider(principal.workspace.id, slug)
        if config is None:
            raise api_error(404, "BYOK provider is not configured", ErrorType.NOT_FOUND)
        shape = byok_provider_shape(config)
        shape["provider"] = slug
        return {"data": shape}

    @router.put(
        "/byok/providers/{provider}",
        summary="Set the default BYOK key",
        description="Create or replace the default upstream provider key while retaining other keys. Requires management authentication and BYOK registration eligibility. Upload api_key or a provider-native credential object. Served on https://trustedrouter.com/v1.",
    )
    def upsert_byok_provider(
        provider: str,
        body: UpsertByokRequest,
        principal: ManagementPrincipal,
        settings: SettingsDep,
    ) -> JSONResponse:
        """Set the provider's default key. Its other keys are kept."""
        slug = _require_byok_provider(provider)
        _require_byok_registration(principal, settings)
        # An existing row (possibly a legacy alias) keeps its storage slug, so
        # its other keys stay with it and every key shares one envelope context.
        storage = _storage_provider(principal.workspace.id, slug)
        raw_secret, upload_hint = _byok_upload(slug, body.api_key or body.key, body.credential)
        secret_ref, key_hint, encrypted_secret = _seal_byok_secret(
            raw_secret, body.secret_ref, body.key_hint or upload_hint,
            settings=settings, workspace_id=principal.workspace.id, provider=storage,
        )
        created = False

        def change(current: ByokProviderConfig | None) -> ByokProviderConfig:
            nonlocal created
            created = current is None
            config = current or ByokProviderConfig(
                workspace_id=principal.workspace.id, provider=storage, secret_ref=secret_ref,
                default_key_id=f"k_{secrets.token_hex(6)}",
            )
            config.secret_ref = secret_ref
            config.key_hint = key_hint
            config.encrypted_secret = encrypted_secret
            _apply_key_policy(config, body)
            return config

        config = STORE.mutate_byok_provider(principal.workspace.id, storage, change)
        assert config is not None
        shape = byok_provider_shape(config)
        shape["provider"] = slug
        shape["provider_name"] = PROVIDERS[slug].name
        return JSONResponse(
            {"data": shape},
            status_code=201 if created else 200,
        )

    @router.get(
        "/byok/providers/{provider}/keys",
        summary="List BYOK keys",
        description="List key metadata for a workspace provider, without exposing credentials. An unconfigured provider returns an empty list. Served on https://trustedrouter.com/v1.",
    )
    def list_byok_keys(provider: str, principal: ManagementPrincipal) -> dict[str, Any]:
        slug = _require_byok_provider(provider)
        config = _get_byok_provider(principal.workspace.id, slug)
        keys = [] if config is None else config.all_keys()
        return {"data": [byok_key_shape(key) for key in keys]}

    @router.post(
        "/byok/providers/{provider}/keys",
        summary="Create a BYOK key",
        description="Add an upstream provider credential to the authenticated workspace. The first key becomes the default. Requires management authentication and BYOK registration eligibility. Returns the created key ID and metadata. Served on https://trustedrouter.com/v1.",
    )
    def create_byok_key(
        provider: str,
        body: CreateByokKeyRequest,
        principal: ManagementPrincipal,
        settings: SettingsDep,
    ) -> JSONResponse:
        """Add a key to a provider. The first key of a provider is its default."""
        slug = _require_byok_provider(provider)
        _require_byok_registration(principal, settings)
        storage = _storage_provider(principal.workspace.id, slug)
        raw_secret, upload_hint = _byok_upload(slug, body.api_key or body.key, body.credential)
        secret_ref, key_hint, encrypted_secret = _seal_byok_secret(
            raw_secret, body.secret_ref, body.key_hint or upload_hint,
            settings=settings, workspace_id=principal.workspace.id, provider=storage,
        )
        # Chosen once, outside the transaction, so a retried transaction adds
        # the same key rather than a second one.
        new_key_id = f"k_{secrets.token_hex(6)}"
        added_id = new_key_id

        def change(current: ByokProviderConfig | None) -> ByokProviderConfig:
            nonlocal added_id
            if current is None:
                # A new first key gets a new id too, so a key deleted and
                # recreated never shares an id with its replacement.
                added_id = new_key_id
                config = ByokProviderConfig(
                    workspace_id=principal.workspace.id,
                    provider=storage,
                    secret_ref=secret_ref,
                    key_hint=key_hint,
                    encrypted_secret=encrypted_secret,
                    default_key_id=new_key_id,
                )
            else:
                if len(current.all_keys()) >= MAX_BYOK_KEYS_PER_PROVIDER:
                    raise api_error(
                        400,
                        f"A provider holds at most {MAX_BYOK_KEYS_PER_PROVIDER} keys",
                        ErrorType.BAD_REQUEST,
                    )
                added_id = new_key_id
                config = current
                config.additional_keys.append(ByokKey(
                    id=new_key_id, secret_ref=secret_ref, key_hint=key_hint, encrypted_secret=encrypted_secret,
                ))
            _apply_key_policy(config, body, key_id=added_id)
            return config

        config = STORE.mutate_byok_provider(principal.workspace.id, storage, change)
        assert config is not None
        key = next(key for key in config.all_keys() if key.id == added_id)
        return JSONResponse({"data": byok_key_shape(key)}, status_code=201)

    @router.patch(
        "/byok/providers/{provider}/keys/{key_id}",
        summary="Update a BYOK key",
        description="Update credential or routing policy fields for a workspace BYOK key. Omitted policy fields are unchanged; credential replacement requires registration eligibility. Served on https://trustedrouter.com/v1.",
    )
    def patch_byok_key(
        provider: str,
        key_id: str,
        body: PatchByokKeyRequest,
        principal: ManagementPrincipal,
        settings: SettingsDep,
    ) -> dict[str, Any]:
        slug = _require_byok_provider(provider)
        storage = _require_byok_key(principal.workspace.id, slug, key_id).provider
        raw_secret, upload_hint = _byok_upload(slug, body.api_key or body.key, body.credential)
        sealed: tuple[str, str | None, EncryptedSecretEnvelope | None] | None = None
        if raw_secret is not None:
            _require_byok_registration(principal, settings)
            sealed = _seal_byok_secret(
                raw_secret, None, upload_hint,
                settings=settings, workspace_id=principal.workspace.id, provider=storage,
            )

        def change(current: ByokProviderConfig | None) -> ByokProviderConfig:
            if current is None or all(key.id != key_id for key in current.all_keys()):
                raise api_error(404, "BYOK key is not configured", ErrorType.NOT_FOUND)
            if sealed is not None:
                _set_key_secret(current, key_id, *sealed)
            _apply_key_policy(current, body, key_id=key_id)
            return current

        config = STORE.mutate_byok_provider(principal.workspace.id, storage, change)
        assert config is not None
        key = next(key for key in config.all_keys() if key.id == key_id)
        return {"data": byok_key_shape(key)}

    @router.delete(
        "/byok/providers/{provider}/keys/{key_id}",
        summary="Delete a BYOK key",
        description="Remove one stored workspace credential. Deleting the default promotes another key; deleting the last removes the provider configuration. Does not revoke the upstream credential. Served on https://trustedrouter.com/v1.",
    )
    def delete_byok_key(provider: str, key_id: str, principal: ManagementPrincipal) -> dict[str, Any]:
        slug = _require_byok_provider(provider)
        storage = _require_byok_key(principal.workspace.id, slug, key_id).provider
        removed_last = False
        # Alias rows as they stand now. One is removed below only if it is
        # still exactly this, so a key added to it meanwhile survives.
        aliases = {
            # A copy: a store may hand back its live row.
            alias: copy.deepcopy(STORE.get_byok_provider(principal.workspace.id, alias))
            for alias in byok_storage_provider_candidates(slug)
            if alias != storage
        }

        def change(current: ByokProviderConfig | None) -> ByokProviderConfig | None:
            nonlocal removed_last
            # A retried transaction decides afresh from the row it reads.
            removed_last = False
            if current is None or all(key.id != key_id for key in current.all_keys()):
                raise api_error(404, "BYOK key is not configured", ErrorType.NOT_FOUND)
            if not current.additional_keys:
                removed_last = True
                return None
            if key_id == current.default_key_id:
                # The next key takes the default slot, so the row keeps one.
                _promote_to_default(current, current.additional_keys.pop(0))
            else:
                current.additional_keys = [key for key in current.additional_keys if key.id != key_id]
            return current

        STORE.mutate_byok_provider(principal.workspace.id, storage, change)
        if removed_last:
            # Removing the provider's last key removes its other alias rows too,
            # so a superseded legacy credential cannot become routable again.
            # Each is removed in its own transaction, and only if unchanged
            # since it was read: a key added to it meanwhile is kept.
            for alias, seen in aliases.items():
                if seen is None:
                    continue

                def remove_if_unchanged(
                    current: ByokProviderConfig | None, seen: ByokProviderConfig = seen,
                ) -> ByokProviderConfig | None:
                    if current is None or current != seen:
                        return current
                    return None

                STORE.mutate_byok_provider(principal.workspace.id, alias, remove_if_unchanged)
        return {"data": {"deleted": True, "provider": slug, "id": key_id}}

    @router.delete(
        "/byok/providers/{provider}",
        summary="Delete a BYOK provider",
        description="Remove the provider configuration and all its stored workspace keys. Does not revoke credentials at the upstream provider. Served on https://trustedrouter.com/v1.",
    )
    def delete_byok_provider(
        provider: str,
        principal: ManagementPrincipal,
    ) -> dict[str, Any]:
        slug = _require_byok_provider(provider)
        deleted = False
        for storage_slug in byok_storage_provider_candidates(slug):
            deleted = STORE.delete_byok_provider(principal.workspace.id, storage_slug) or deleted
        if not deleted:
            raise api_error(404, "BYOK provider is not configured", ErrorType.NOT_FOUND)
        return {"data": {"deleted": True, "provider": slug}}


def _require_byok_provider(provider: str) -> str:
    slug = canonical_byok_provider(provider)
    catalog_provider = PROVIDERS.get(slug)
    if catalog_provider is None or not catalog_provider.supports_byok:
        raise api_error(
            400,
            "Provider does not support BYOK",
            ErrorType.PROVIDER_NOT_SUPPORTED,
        )
    return slug


def _get_byok_provider(workspace_id: str, provider: str) -> Any | None:
    for storage_slug in byok_storage_provider_candidates(provider):
        config = STORE.get_byok_provider(workspace_id, storage_slug)
        if config is not None:
            return config
    return None


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


def _byok_upload(
    provider: str, api_key: str | None, credential: dict[str, Any] | None,
) -> tuple[str | None, str | None]:
    """The secret a request uploads and its display hint.

    A cloud provider's upload is a native credential, validated and
    normalized here; any other provider's is a plain API key."""
    if credential is not None and api_key is not None:
        raise api_error(400, "send api_key or credential, not both", ErrorType.BAD_REQUEST)
    if not is_cloud_credential_provider(provider):
        if credential is not None:
            raise api_error(
                400,
                "credential objects are for amazon-bedrock, google-vertex and azure; send api_key",
                ErrorType.BAD_REQUEST,
            )
        return api_key, None
    upload = credential if credential is not None else api_key
    if upload is None:
        return None, None
    try:
        return normalize_cloud_credential(provider, upload)
    except CloudCredentialError as exc:
        raise api_error(400, str(exc), ErrorType.BAD_REQUEST) from exc


def _require_byok_registration(principal: Any, settings: Any) -> None:
    # Wallet-only signups are free and scriptable; bursts of them were
    # used to probe stolen provider keys through BYOK (2026-11-08).
    if is_wallet_only_principal(principal):
        raise api_error(403, WALLET_ONLY_BYOK_MESSAGE, ErrorType.FORBIDDEN)
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


def _seal_byok_secret(
    api_key: str | None,
    secret_ref: str | None,
    key_hint: str | None,
    *,
    settings: Any,
    workspace_id: str,
    provider: str,
) -> tuple[str, str | None, EncryptedSecretEnvelope | None]:
    """Turn an upload into (secret_ref, key_hint, envelope)."""
    encrypted_secret = None
    if api_key is not None:
        if secret_ref is not None and secret_ref.strip() and not secret_ref.strip().startswith("byok://"):
            raise api_error(
                400,
                "raw api_key uploads are stored as TrustedRouter BYOK envelopes; omit secret_ref",
                ErrorType.BAD_REQUEST,
            )
        secret_ref = _default_byok_secret_ref(workspace_id, provider)
        if key_hint is None:
            key_hint = _secret_hint(api_key)
        try:
            encrypted_secret = encrypt_byok_secret(
                api_key,
                settings,
                workspace_id=workspace_id,
                provider=provider,
            )
        except KeyAccessDenied as exc:
            # The BYOK envelope DEK is wrapped with the configured
            # envelope key, which only the primary control-plane
            # principal may encrypt with. Return a clean, actionable 503 instead of
            # an unhandled 500 + KMS stack trace if this endpoint lacks
            # encrypt permission.
            log.warning(
                "byok.encrypt_permission_denied",
                extra={"provider": provider, "workspace_id": workspace_id},
            )
            raise api_error(
                503,
                "BYOK key registration is not available on this endpoint. "
                "Register keys through the primary API at https://api.trustedrouter.com.",
                ErrorType.SERVICE_UNAVAILABLE,
            ) from exc
        except KeyManagementError as exc:
            # Any other key-service failure (unavailability, timeout):
            # a best-effort retry-able error, not an unhandled 500.
            log.error(
                "byok.encrypt_failed",
                extra={"provider": provider, "error": type(exc).__name__},
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
    return secret_ref, key_hint, encrypted_secret


def _apply_key_policy(config: ByokProviderConfig, body: Any, *, key_id: str | None = None) -> bool:
    """Copy the policy fields the request set onto one key. True if any were.

    Each field is named; nothing is looked up by a computed name."""
    changed = False
    if key_id is None or key_id == config.default_key_id:
        if body.label is not None:
            config.label, changed = body.label, True
        if body.models is not None:
            config.models, changed = list(body.models), True
        if body.priority is not None:
            config.priority, changed = body.priority, True
        if body.fallback_to_credits is not None:
            config.fallback_to_credits, changed = body.fallback_to_credits, True
        if body.disabled is not None:
            config.key_disabled, changed = body.disabled, True
        return changed
    key = next(key for key in config.additional_keys if key.id == key_id)
    if body.label is not None:
        key.label, changed = body.label, True
    if body.models is not None:
        key.models, changed = list(body.models), True
    if body.priority is not None:
        key.priority, changed = body.priority, True
    if body.fallback_to_credits is not None:
        key.fallback_to_credits, changed = body.fallback_to_credits, True
    if body.disabled is not None:
        key.disabled, changed = body.disabled, True
    if changed:
        key.updated_at = iso_now()
    return changed


def _set_key_secret(
    config: ByokProviderConfig,
    key_id: str,
    secret_ref: str,
    key_hint: str | None,
    encrypted_secret: EncryptedSecretEnvelope | None,
) -> None:
    target: Any = config
    if key_id != config.default_key_id:
        target = next(key for key in config.additional_keys if key.id == key_id)
        target.updated_at = iso_now()
    target.secret_ref = secret_ref
    target.key_hint = key_hint
    target.encrypted_secret = encrypted_secret


def _promote_to_default(config: ByokProviderConfig, key: ByokKey) -> None:
    config.default_key_id = key.id
    config.secret_ref = key.secret_ref
    config.key_hint = key.key_hint
    config.encrypted_secret = key.encrypted_secret
    config.label = key.label
    config.models = list(key.models)
    config.priority = key.priority
    config.fallback_to_credits = key.fallback_to_credits
    config.key_disabled = key.disabled
    config.created_at = key.created_at


def _storage_provider(workspace_id: str, provider: str) -> str:
    """The storage slug of the provider's existing row, else the provider's."""
    config = _get_byok_provider(workspace_id, provider)
    return str(config.provider) if config is not None else provider


def _require_byok_key(workspace_id: str, provider: str, key_id: str) -> ByokProviderConfig:
    config = _get_byok_provider(workspace_id, provider)
    if config is None or all(key.id != key_id for key in config.all_keys()):
        raise api_error(404, "BYOK key is not configured", ErrorType.NOT_FOUND)
    return config
