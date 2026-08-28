from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from trusted_router.auth import Principal, SettingsDep, principal_from_request
from trusted_router.errors import api_error
from trusted_router.money import dollars_to_microdollars, microdollars_to_decimal
from trusted_router.routes.helpers import json_body
from trusted_router.storage import STORE, ApiKey, ApiKeyUsageSnapshot, OAuthApp
from trusted_router.types import ErrorType

log = logging.getLogger(__name__)


def require_authorized_app_management(
    request: Request,
    settings: SettingsDep,
) -> Principal:
    """Require the user's own authority, never authority delegated to an app."""
    principal = principal_from_request(request, settings)
    if principal.api_key is not None and principal.api_key.app_id:
        raise api_error(
            403,
            "A delegated key cannot manage OAuth grants",
            ErrorType.FORBIDDEN,
        )
    if not principal.is_management:
        raise api_error(
            403,
            "Only a management key or active console session can manage OAuth grants",
            ErrorType.FORBIDDEN,
        )
    return principal


AuthorizedAppsPrincipal = Annotated[
    Principal, Depends(require_authorized_app_management)
]


def register_oauth_authorized_app_routes(router: APIRouter) -> None:
    @router.get("/oauth/authorized-apps")
    async def list_authorized_apps(
        principal: AuthorizedAppsPrincipal,
    ) -> dict[str, list[dict[str, Any]]]:
        snapshots = _live_delegated_snapshots(principal.workspace.id)
        grouped: dict[str, list[ApiKeyUsageSnapshot]] = {}
        for snapshot in snapshots:
            grouped.setdefault(snapshot.api_key.app_id, []).append(snapshot)
        return {
            "data": [
                _authorized_app_shape(app, grouped[app_id])
                for app_id in sorted(grouped)
                if (app := STORE.get_oauth_app(app_id)) is not None
            ]
        }

    @router.patch("/oauth/authorized-apps/{app_id}")
    async def patch_authorized_app(
        app_id: str,
        request: Request,
        principal: AuthorizedAppsPrincipal,
    ) -> dict[str, Any]:
        snapshots = _owned_app_snapshots(principal.workspace.id, app_id)
        body = await json_body(request)
        if set(body) != {"monthly_budget"}:
            raise api_error(
                400,
                "monthly_budget is the only supported field",
                ErrorType.BAD_REQUEST,
            )
        monthly_budget = _monthly_budget(body["monthly_budget"])
        keys = [snapshot.api_key for snapshot in snapshots]
        _update_all_or_rollback(
            keys,
            field="limit_monthly_microdollars",
            value=monthly_budget,
        )
        refreshed = _owned_app_snapshots(principal.workspace.id, app_id)
        app = STORE.get_oauth_app(app_id)
        assert app is not None
        return {"data": _authorized_app_shape(app, refreshed)}

    @router.delete("/oauth/authorized-apps/{app_id}")
    async def revoke_authorized_app(
        app_id: str,
        principal: AuthorizedAppsPrincipal,
    ) -> dict[str, Any]:
        # An existing registered app with no live keys is the idempotent second
        # DELETE case. An unknown app remains indistinguishable from another
        # workspace's grant.
        app = STORE.get_oauth_app(app_id)
        if app is None:
            raise api_error(404, "Resource not found", ErrorType.NOT_FOUND)
        snapshots = _app_snapshots(principal.workspace.id, app_id, live_only=False)
        if not snapshots:
            raise api_error(404, "Resource not found", ErrorType.NOT_FOUND)
        live_keys = [row.api_key for row in snapshots if not row.api_key.disabled]
        if live_keys:
            _update_all_or_rollback(live_keys, field="disabled", value=True)
        return {"data": {"app_id": app_id, "revoked": True}}


def _live_delegated_snapshots(workspace_id: str) -> list[ApiKeyUsageSnapshot]:
    return [
        row
        for row in STORE.list_api_keys_with_usage(workspace_id)
        if row.api_key.app_id and not row.api_key.disabled
    ]


def _app_snapshots(
    workspace_id: str,
    app_id: str,
    *,
    live_only: bool,
) -> list[ApiKeyUsageSnapshot]:
    return [
        row
        for row in STORE.list_api_keys_with_usage(workspace_id)
        if row.api_key.app_id == app_id
        and (not live_only or not row.api_key.disabled)
    ]


def _owned_app_snapshots(workspace_id: str, app_id: str) -> list[ApiKeyUsageSnapshot]:
    app = STORE.get_oauth_app(app_id)
    snapshots = _app_snapshots(workspace_id, app_id, live_only=True)
    if app is None or not snapshots:
        raise api_error(404, "Resource not found", ErrorType.NOT_FOUND)
    return snapshots


def _monthly_budget(raw: Any) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
        raise api_error(400, "monthly_budget must be a dollar amount", ErrorType.BAD_REQUEST)
    try:
        value = dollars_to_microdollars(raw)
    except ValueError as exc:
        raise api_error(
            400,
            "monthly_budget must be a dollar amount",
            ErrorType.BAD_REQUEST,
        ) from exc
    if value < 0:
        raise api_error(400, "monthly_budget must be non-negative", ErrorType.BAD_REQUEST)
    return value


def _update_all_or_rollback(
    keys: Iterable[ApiKey],
    *,
    field: str,
    value: Any,
) -> None:
    originals = {key.hash: getattr(key, field) for key in keys}
    try:
        for key_hash in originals:
            result = STORE.update_key(key_hash, {field: value})
            if result is None or getattr(result, field) != value:
                raise RuntimeError("OAuth grant update did not persist")
        for key_hash in originals:
            persisted = STORE.get_key_by_hash(key_hash)
            if persisted is None or getattr(persisted, field) != value:
                raise RuntimeError("OAuth grant update verification failed")
    except Exception as exc:
        # Repair every key, including the write which may have persisted and
        # then raised before the caller could record that it succeeded.
        for key_hash, original in originals.items():
            try:
                STORE.update_key(key_hash, {field: original})
            except Exception as repair_exc:
                log.error(
                    "oauth_grant_repair_write_failed key_id=%s error=%s",
                    key_hash,
                    repair_exc,
                )
        disagreeing = []
        for key_hash, original in originals.items():
            try:
                persisted = STORE.get_key_by_hash(key_hash)
            except Exception:
                persisted = None
            if persisted is None or getattr(persisted, field) != original:
                disagreeing.append(key_hash)
        if disagreeing:
            app_ids = sorted({key.app_id for key in keys})
            log.critical(
                "oauth_grant_repair_failed app_ids=%s disagreeing_key_ids=%s",
                app_ids,
                disagreeing,
            )
        raise api_error(
            500,
            "Could not update every key in the OAuth grant",
            ErrorType.INTERNAL_ERROR,
        ) from exc


def _authorized_app_shape(
    app: OAuthApp,
    snapshots: list[ApiKeyUsageSnapshot],
) -> dict[str, Any]:
    keys = [snapshot.api_key for snapshot in snapshots]
    owner = STORE.get_user(app.owner_user_id)
    owner_name = (owner.identity_verified_name or "").strip() if owner else ""
    scopes = sorted({scope for key in keys for scope in key.scopes})
    monthly_limits = {key.limit_monthly_microdollars for key in keys}
    monthly_limit = next(iter(monthly_limits)) if len(monthly_limits) == 1 else None
    monthly_usage = sum(snapshot.windows.get("monthly", 0) for snapshot in snapshots)
    created_at = min(key.created_at for key in keys)
    markup_disclosure = (
        f"This app adds {app.markup_basis_points / 100:g}% on top of "
        "TrustedRouter token costs."
        if app.markup_basis_points
        else None
    )
    return {
        "app_id": app.id,
        "name": app.name,
        "logo_url": app.logo_url,
        "owner_verified_legal_name": owner_name,
        "scopes": scopes,
        "markup_basis_points": app.markup_basis_points,
        "markup_disclosure": markup_disclosure,
        "budget": {
            "monthly_budget": (
                None if monthly_limit is None else microdollars_to_decimal(monthly_limit)
            ),
            "limit_microdollars": monthly_limit,
            "used_microdollars": monthly_usage,
            "reset_window": "monthly",
        },
        "key_count": len(keys),
        "created_at": created_at,
    }
