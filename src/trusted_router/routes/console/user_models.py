from __future__ import annotations

import secrets
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from trusted_router.auth import SettingsDep
from trusted_router.custom_model_billing import validate_custom_model_price
from trusted_router.custom_model_rules import missing_custom_model_requirements
from trusted_router.routes.console._shared import ConsoleDep, render
from trusted_router.serialization import user_model_owner_shape
from trusted_router.services.user_model_probe import probe_user_model
from trusted_router.services.user_model_secrets import (
    encrypt_user_model_endpoint_key,
    encrypt_user_model_signing_secret,
)
from trusted_router.storage import STORE
from trusted_router.storage_models import UserProvidedModel
from trusted_router.storage_user_models import USER_PROVIDED_MODEL_LIMIT_PER_USER
from trusted_router.user_model_rules import (
    validate_endpoint_url,
    validate_user_model_display_name,
    validate_user_model_slug,
)


def register(app: FastAPI) -> None:
    @app.get("/console/user-models")
    async def console_user_models(
        request: Request,
        ctx: ConsoleDep,
        settings: SettingsDep,
    ) -> Response:
        return HTMLResponse(_render_page(ctx, settings, request=request))

    @app.post("/console/user-models")
    async def console_create_user_model(
        request: Request,
        ctx: ConsoleDep,
        settings: SettingsDep,
        name: str = Form(..., min_length=1, max_length=120),
        slug: str | None = Form(default=None, max_length=96),
        kind: str = Form(...),
        description: str = Form("", max_length=2000),
        display_identity: str = Form("handle"),
        display_name: str = Form(..., min_length=1, max_length=120),
        endpoint_url: str = Form(..., min_length=1, max_length=2048),
        upstream_model_id: str | None = Form(default=None, max_length=256),
        endpoint_api_key: str | None = Form(default=None, max_length=8192),
        supports_streaming: bool = Form(False),
        heartbeat_interval_seconds: int | None = Form(default=None, ge=5, le=3600),
        max_concurrency: int = Form(4, ge=1, le=100),
        prompt_price_microdollars_per_million_tokens: int = Form(0, ge=0),
        completion_price_microdollars_per_million_tokens: int = Form(0, ge=0),
    ) -> Response:
        if missing_custom_model_requirements(ctx.user, settings):
            return _redirect("error=verification")
        _validate_form_values(
            kind=kind,
            display_identity=display_identity,
            prompt_price=prompt_price_microdollars_per_million_tokens,
            completion_price=completion_price_microdollars_per_million_tokens,
        )
        normalized_slug = validate_user_model_slug(slug) if slug else None
        normalized_display_name = validate_user_model_display_name(display_name)
        normalized_endpoint = await validate_endpoint_url(endpoint_url, settings)
        signing_secret = secrets.token_urlsafe(32)
        try:
            STORE.create_user_model(
                owner_user_id=ctx.user.id,
                owner_workspace_id=ctx.workspace.id,
                name=name,
                kind=kind,
                description=description,
                display_identity=display_identity,
                display_name=normalized_display_name,
                endpoint_url=normalized_endpoint,
                upstream_model_id=upstream_model_id or None,
                encrypted_endpoint_api_key=(
                    encrypt_user_model_endpoint_key(
                        endpoint_api_key,
                        settings,
                        workspace_id=ctx.workspace.id,
                    )
                    if endpoint_api_key
                    else None
                ),
                endpoint_key_hint=_secret_hint(endpoint_api_key),
                encrypted_signing_secret=encrypt_user_model_signing_secret(
                    signing_secret,
                    settings,
                    workspace_id=ctx.workspace.id,
                ),
                supports_streaming=supports_streaming,
                heartbeat_interval_seconds=heartbeat_interval_seconds,
                max_concurrency=max_concurrency,
                prompt_price_microdollars_per_million_tokens=(
                    prompt_price_microdollars_per_million_tokens
                ),
                completion_price_microdollars_per_million_tokens=(
                    completion_price_microdollars_per_million_tokens
                ),
                human_verified=kind == "human",
                slug=normalized_slug,
            )
        except ValueError as exc:
            return _store_error_redirect(exc)
        return HTMLResponse(
            _render_page(
                ctx,
                settings,
                request=request,
                one_time_secret=signing_secret,
                one_time_reason="created",
            ),
            status_code=201,
        )

    @app.post("/console/user-models/{model_id:path}/edit")
    async def console_update_user_model(
        ctx: ConsoleDep,
        settings: SettingsDep,
        model_id: str,
        name: str = Form(..., min_length=1, max_length=120),
        slug: str = Form(..., min_length=3, max_length=96),
        kind: str = Form(...),
        description: str = Form("", max_length=2000),
        display_identity: str = Form("handle"),
        display_name: str = Form(..., min_length=1, max_length=120),
        endpoint_url: str = Form(..., min_length=1, max_length=2048),
        upstream_model_id: str | None = Form(default=None, max_length=256),
        endpoint_api_key: str | None = Form(default=None, max_length=8192),
        supports_streaming: bool = Form(False),
        heartbeat_interval_seconds: int | None = Form(default=None, ge=5, le=3600),
        max_concurrency: int = Form(4, ge=1, le=100),
        prompt_price_microdollars_per_million_tokens: int = Form(0, ge=0),
        completion_price_microdollars_per_million_tokens: int = Form(0, ge=0),
    ) -> Response:
        if missing_custom_model_requirements(ctx.user, settings):
            return _redirect("error=verification")
        model = _require_owner_model(model_id, ctx.user.id)
        _validate_form_values(
            kind=kind,
            display_identity=display_identity,
            prompt_price=prompt_price_microdollars_per_million_tokens,
            completion_price=completion_price_microdollars_per_million_tokens,
        )
        patch: dict[str, Any] = {
            "name": name,
            "slug": validate_user_model_slug(slug),
            "kind": kind,
            "description": description,
            "display_identity": display_identity,
            "display_name": validate_user_model_display_name(display_name),
            "endpoint_url": await validate_endpoint_url(endpoint_url, settings),
            "upstream_model_id": upstream_model_id or None,
            "supports_streaming": supports_streaming,
            "heartbeat_interval_seconds": heartbeat_interval_seconds,
            "max_concurrency": max_concurrency,
            "prompt_price_microdollars_per_million_tokens": (
                prompt_price_microdollars_per_million_tokens
            ),
            "completion_price_microdollars_per_million_tokens": (
                completion_price_microdollars_per_million_tokens
            ),
            "human_verified": kind == "human",
        }
        if endpoint_api_key:
            patch["encrypted_endpoint_api_key"] = encrypt_user_model_endpoint_key(
                endpoint_api_key,
                settings,
                workspace_id=model.owner_workspace_id,
            )
            patch["endpoint_key_hint"] = _secret_hint(endpoint_api_key)
        try:
            STORE.update_user_model(
                model.id,
                owner_user_id=ctx.user.id,
                patch=patch,
            )
        except ValueError as exc:
            return _store_error_redirect(exc)
        return _redirect("saved=updated")

    @app.post("/console/user-models/{model_id:path}/clock-in")
    async def console_clock_in_user_model(
        ctx: ConsoleDep,
        settings: SettingsDep,
        model_id: str,
    ) -> Response:
        model = _require_owner_model(model_id, ctx.user.id)
        result = await probe_user_model(model, settings)
        if not result.ok:
            STORE.set_user_model_online(
                model.id, owner_user_id=ctx.user.id, online=False
            )
            return _redirect("error=probe")
        STORE.set_user_model_online(model.id, owner_user_id=ctx.user.id, online=True)
        return _redirect("saved=clocked-in")

    @app.post("/console/user-models/{model_id:path}/clock-out")
    async def console_clock_out_user_model(ctx: ConsoleDep, model_id: str) -> Response:
        model = _require_owner_model(model_id, ctx.user.id)
        STORE.set_user_model_online(model.id, owner_user_id=ctx.user.id, online=False)
        return _redirect("saved=clocked-out")

    @app.post("/console/user-models/{model_id:path}/rotate-secrets")
    async def console_rotate_user_model_secret(
        request: Request,
        ctx: ConsoleDep,
        settings: SettingsDep,
        model_id: str,
    ) -> Response:
        model = _require_owner_model(model_id, ctx.user.id)
        signing_secret = secrets.token_urlsafe(32)
        STORE.update_user_model(
            model.id,
            owner_user_id=ctx.user.id,
            patch={
                "encrypted_signing_secret": encrypt_user_model_signing_secret(
                    signing_secret,
                    settings,
                    workspace_id=model.owner_workspace_id,
                )
            },
        )
        return HTMLResponse(
            _render_page(
                ctx,
                settings,
                request=request,
                one_time_secret=signing_secret,
                one_time_reason="rotated",
            )
        )

    @app.post("/console/user-models/{model_id:path}/delete")
    async def console_delete_user_model(ctx: ConsoleDep, model_id: str) -> Response:
        model = _require_owner_model(model_id, ctx.user.id)
        STORE.delete_user_model(model.id, owner_user_id=ctx.user.id)
        return _redirect("saved=deleted")


def _render_page(
    ctx: ConsoleDep,
    settings: SettingsDep,
    *,
    request: Request,
    one_time_secret: str | None = None,
    one_time_reason: str | None = None,
) -> str:
    missing = missing_custom_model_requirements(ctx.user, settings)
    models = [
        user_model_owner_shape(model)
        for model in STORE.list_user_models_for_user(ctx.user.id)
    ]
    return render(
        "console/user_models.html",
        settings=settings,
        ctx=ctx,
        active="user-models",
        page_title="User-provided Models",
        page_subtitle="Operate your own machine, agent, or live human endpoint.",
        models=models,
        limit=USER_PROVIDED_MODEL_LIMIT_PER_USER,
        verification={
            "met": not missing,
            "missing_requirements": missing,
            "url": "/console/account/verification",
        },
        one_time_secret=one_time_secret,
        secret_reason=one_time_reason,
        flash=_flash_message(
            request.query_params.get("saved"), request.query_params.get("error")
        ),
    )


def _require_owner_model(model_id: str, owner_user_id: str) -> UserProvidedModel:
    model = STORE.get_user_model(model_id)
    if model is None or model.owner_user_id != owner_user_id:
        raise HTTPException(status_code=404, detail="User-provided model not found")
    return model


def _validate_form_values(
    *,
    kind: str,
    display_identity: str,
    prompt_price: int,
    completion_price: int,
) -> None:
    if kind not in {"machine", "agent", "human"}:
        raise HTTPException(status_code=400, detail="Invalid model kind")
    if display_identity not in {"handle", "verified_name"}:
        raise HTTPException(status_code=400, detail="Invalid display identity")
    try:
        validate_custom_model_price(prompt_price, completion_price, kind=kind)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Price is outside the allowed range") from exc


def _store_error_redirect(exc: ValueError) -> RedirectResponse:
    error = str(exc)
    if error == "custom_model_limit_exceeded":
        return _redirect("error=limit")
    if error == "custom_model_slug_taken":
        return _redirect("error=slug-taken")
    if error == "invalid_custom_model_slug":
        return _redirect("error=slug")
    raise exc


def _redirect(query: str) -> RedirectResponse:
    return RedirectResponse(url=f"/console/user-models?{query}", status_code=303)


def _secret_hint(secret: str | None) -> str | None:
    stripped = (secret or "").strip()
    return f"...{stripped[-4:]}" if stripped else None


def _flash_message(saved: str | None, error: str | None) -> dict[str, str] | None:
    if error == "verification":
        return {
            "type": "error",
            "text": "Verify your account before creating or editing user-provided models.",
            "href": "/console/account/verification",
            "link_text": "Complete verification",
        }
    if error == "probe":
        return {"type": "error", "text": "Endpoint probe failed; the model stayed offline."}
    if error in {"slug", "slug-taken"}:
        return {"type": "error", "text": "That model slug is invalid or already in use."}
    if error == "limit":
        return {
            "type": "error",
            "text": f"User-provided model limit reached ({USER_PROVIDED_MODEL_LIMIT_PER_USER}).",
        }
    if saved:
        return {"type": "success", "text": "User-provided model saved."}
    return None
