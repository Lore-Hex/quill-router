from __future__ import annotations

from dataclasses import replace
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from trusted_router.auth import InferencePrincipal, ManagementPrincipal, Principal
from trusted_router.errors import api_error, assert_workspace_billing_active
from trusted_router.money import dollars_to_microdollars
from trusted_router.request_tags import InvalidTags, validate_tags
from trusted_router.schemas import (
    BulkDeleteKeysRequest,
    CreateKeyRequest,
    PatchKeyRequest,
    model_to_dict,
)
from trusted_router.scopes import SCOPE_PROFILE, validate_api_key_scopes
from trusted_router.serialization import key_shape
from trusted_router.storage import STORE, ApiKey
from trusted_router.types import ErrorType

# The application validation handler returns 400, not FastAPI's default 422.
# A 4XX response also suppresses that automatic 422 OpenAPI declaration.
_KEY_VALIDATION_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"description": "Invalid parameters; standard error envelope with type bad_request."},
    "4XX": {"description": "Client error; authentication or request rejected."},
}


def _enriched_key_shape(key: ApiKey) -> dict[str, Any]:
    """key_shape backed by the typed counters when available: live lifetime
    usage/reserved (the JSON copies froze at the typed flip) + real current-
    window spend. Falls back to the JSON values (typed off / row missing)."""
    # This GET shape cannot authorize spend; hard budget enforcement has its
    # own strong authorize-path read.
    usage = STORE.typed_key_usage(key.hash, allow_stale=True)
    if usage is None:
        return key_shape(key)
    key = replace(
        key,
        usage_microdollars=usage["usage"],
        byok_usage_microdollars=usage["byok_usage"],
        reserved_microdollars=usage["reserved"],
    )
    return key_shape(key, window_usage=usage["windows"])


def self_key_shape(key: ApiKey) -> dict[str, Any]:
    """Return self-introspection without widening a delegated key's grant."""
    shape = _enriched_key_shape(key)
    if not key.scopes:
        # Preserve the pre-scope /key field set exactly for legacy callers.
        shape.pop("scopes", None)
        return shape

    # A key needs its own limits and live usage for agent budget display, but
    # self-introspection must not leak workspace metadata or person identifiers
    # to a delegated credential that was not granted profile access.
    allowed = {
        field
        for field in shape
        if field.startswith("limit_")
        or field.startswith("usage")
        or field.startswith("byok_usage")
    }
    allowed.update(
        {
            "hash",
            "name",
            "label",
            "scopes",
            "disabled",
            "expires_at",
            "limit",
            "reserved_microdollars",
            "include_byok_in_limit",
            "budget_alert_only",
            "budget_strict",
        }
    )
    if SCOPE_PROFILE in key.scopes:
        allowed.add("creator_user_id")
    return {field: value for field, value in shape.items() if field in allowed}


def register_key_routes(router: APIRouter) -> None:
    @router.get("/key")
    async def key(principal: InferencePrincipal) -> dict[str, Any]:
        assert principal.api_key is not None
        return {"data": self_key_shape(principal.api_key)}

    # These storage-heavy management operations run in FastAPI's worker pool,
    # so a batch or a slow database round trip cannot block the ASGI event loop.
    @router.get("/keys", responses=_KEY_VALIDATION_RESPONSES)
    def keys(
        principal: ManagementPrincipal,
        limit: int | None = Query(default=None, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
        include_disabled: bool = Query(default=True),
    ) -> dict[str, Any]:
        """List keys newest first (hash breaks ties), with batched usage.

        Omit limit to return all remaining keys, including disabled keys by
        default for backward compatibility. With limit, next_offset is null
        at the end; otherwise pass it as offset with the same filters. Pages
        are stable while the key set is unchanged, not a cross-request snapshot.
        """
        snapshots = STORE.list_api_keys_with_usage(
            principal.workspace.id,
            limit=None if limit is None else limit + 1,
            offset=offset,
            include_disabled=include_disabled,
        )
        has_more = limit is not None and len(snapshots) > limit
        if limit is not None:
            snapshots = snapshots[:limit]
        data = [
            key_shape(
                replace(
                    snapshot.api_key,
                    usage_microdollars=snapshot.usage_microdollars,
                    byok_usage_microdollars=snapshot.byok_usage_microdollars,
                    reserved_microdollars=snapshot.reserved_microdollars,
                ),
                window_usage=snapshot.windows if snapshot.typed_usage_available else None,
            )
            for snapshot in snapshots
        ]
        response: dict[str, Any] = {"data": data}
        if limit is not None:
            response["next_offset"] = offset + len(data) if has_more else None
        return response

    @router.post("/keys/bulk-delete", responses=_KEY_VALIDATION_RESPONSES)
    def bulk_delete_keys(
        body: BulkDeleteKeysRequest,
        principal: ManagementPrincipal,
    ) -> dict[str, Any]:
        """Delete 1–1000 hashes in workspace-scoped transactions of at most 100.

        Results follow input order; duplicate hashes repeat their first result.
        Foreign and missing keys both report not_found. Batches commit
        independently; after a transient failure retrying is safe, but keys
        already deleted will report not_found.
        """
        results = STORE.delete_keys(principal.workspace.id, body.hashes)
        return {
            "data": [
                {"hash": key_hash, "status": "deleted" if results[key_hash] else "not_found"}
                for key_hash in body.hashes
            ]
        }

    @router.post("/keys")
    async def create_key(body: CreateKeyRequest, principal: ManagementPrincipal) -> JSONResponse:
        limit_microdollars: int | None = None
        if body.limit is not None:
            limit_microdollars = dollars_to_microdollars(body.limit)
            if limit_microdollars < 0:
                raise api_error(400, "limit must be non-negative", ErrorType.BAD_REQUEST)
        workspace_id = body.workspace_id or principal.workspace.id
        if workspace_id != principal.workspace.id:
            raise api_error(403, "Forbidden", ErrorType.FORBIDDEN)
        # Quiesce: no new keys while paused, so the key set is stable through a flip.
        assert_workspace_billing_active(principal.workspace)
        try:
            tags = validate_tags(body.tags)
        except InvalidTags as exc:
            raise api_error(400, str(exc), ErrorType.INVALID_TAGS) from exc
        try:
            scopes = validate_api_key_scopes(body.scopes, management=body.management)
        except ValueError as exc:
            raise api_error(400, str(exc), ErrorType.BAD_REQUEST) from exc
        raw, k = STORE.create_api_key(
            workspace_id=workspace_id,
            name=body.name,
            creator_user_id=principal.user.id if principal.user else None,
            management=body.management,
            limit_microdollars=limit_microdollars,
            limit_reset=body.limit_reset,
            include_byok_in_limit=body.include_byok_in_limit,
            expires_at=body.expires_at,
            limit_daily_microdollars=(
                None if body.limit_daily is None else dollars_to_microdollars(body.limit_daily)
            ),
            limit_weekly_microdollars=(
                None if body.limit_weekly is None else dollars_to_microdollars(body.limit_weekly)
            ),
            limit_monthly_microdollars=(
                None if body.limit_monthly is None else dollars_to_microdollars(body.limit_monthly)
            ),
            budget_alert_only=body.budget_alert_only,
            budget_strict=body.budget_strict,
            tags=tags,
            scopes=scopes,
        )
        return JSONResponse({"data": key_shape(k), "key": raw}, status_code=201)

    @router.get("/keys/{hash}")
    async def get_key(hash: str, principal: ManagementPrincipal) -> dict[str, Any]:  # noqa: A002
        return {"data": _enriched_key_shape(_require_key_in_workspace(hash, principal))}

    @router.patch("/keys/{hash}")
    async def patch_key(
        hash: str,  # noqa: A002
        body: PatchKeyRequest,
        principal: ManagementPrincipal,
    ) -> dict[str, Any]:
        _require_key_in_workspace(hash, principal)
        if "scopes" in (body.model_extra or {}):
            raise api_error(
                400,
                "API key scopes are immutable; create a new key to change them",
                ErrorType.BAD_REQUEST,
            )
        patch = model_to_dict(body)
        if "tags" in patch:
            try:
                patch["tags"] = validate_tags(patch["tags"])
            except InvalidTags as exc:
                raise api_error(400, str(exc), ErrorType.INVALID_TAGS) from exc
        if "limit" in patch:
            limit_microdollars = dollars_to_microdollars(patch.pop("limit"))
            if limit_microdollars < 0:
                raise api_error(400, "limit must be non-negative", ErrorType.BAD_REQUEST)
            patch["limit_microdollars"] = limit_microdollars
        for window in ("daily", "weekly", "monthly"):
            if f"limit_{window}" in patch:
                micro = dollars_to_microdollars(patch.pop(f"limit_{window}"))
                if micro < 0:
                    raise api_error(400, "limit must be non-negative", ErrorType.BAD_REQUEST)
                patch[f"limit_{window}_microdollars"] = micro
        try:
            updated = STORE.update_key(hash, patch)
        except ValueError as exc:
            raise api_error(409, str(exc), ErrorType.CONFLICT) from exc
        if updated is None:
            raise api_error(404, "Resource not found", ErrorType.NOT_FOUND)
        return {"data": key_shape(updated)}

    @router.delete("/keys/{hash}")
    def delete_key(hash: str, principal: ManagementPrincipal) -> dict[str, Any]:  # noqa: A002
        _require_key_in_workspace(hash, principal)
        if not STORE.delete_key(hash):
            raise api_error(404, "Resource not found", ErrorType.NOT_FOUND)
        return {"data": {"deleted": True, "hash": hash}}


def _require_key_in_workspace(key_hash: str, principal: Principal) -> ApiKey:
    key = STORE.get_key_by_hash(key_hash)
    if key is None or key.workspace_id != principal.workspace.id:
        raise api_error(404, "Resource not found", ErrorType.NOT_FOUND)
    return key
