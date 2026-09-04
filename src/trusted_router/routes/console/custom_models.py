from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from trusted_router.auth import SettingsDep
from trusted_router.catalog import MODELS
from trusted_router.creator_identity import creator_username_for_models
from trusted_router.custom_model_rules import (
    is_allowed_custom_model_base,
    missing_custom_model_requirements,
    require_custom_model_base_model,
)
from trusted_router.routes.console._shared import ConsoleDep, render
from trusted_router.storage import STORE, CustomModel
from trusted_router.storage_custom_models import (
    CUSTOM_MODEL_LIMIT_PER_USER,
    CUSTOM_MODEL_PROMPT_CHAR_LIMIT,
)


def register(app: FastAPI) -> None:
    @app.get("/console/custom-models")
    def console_custom_models(
        request: Request,
        ctx: ConsoleDep,
        settings: SettingsDep,
    ) -> Response:
        return HTMLResponse(_render_page(ctx, settings, request=request))

    @app.post("/console/custom-models")
    def console_create_custom_model(
        ctx: ConsoleDep,
        settings: SettingsDep,
        name: str = Form(..., min_length=1, max_length=120),
        slug: str | None = Form(default=None, max_length=96),
        base_model_id: str = Form(..., min_length=1, max_length=256),
        hidden_prompt: str = Form("", max_length=CUSTOM_MODEL_PROMPT_CHAR_LIMIT),
        markup_percent: Decimal = Form(Decimal("0"), ge=0, le=300),
        enabled: bool = Form(False),
    ) -> Response:
        if missing_custom_model_requirements(ctx.user, settings):
            return _custom_model_redirect("error=verification")
        owner_username = creator_username_for_models(
            ctx.user,
            enforce_verification=settings.custom_models_verification_enforced,
        )
        _require_base_model(base_model_id)
        try:
            STORE.create_custom_model(
                owner_user_id=ctx.user.id,
                owner_workspace_id=ctx.workspace.id,
                owner_username=owner_username,
                name=name,
                base_model_id=base_model_id,
                hidden_prompt=hidden_prompt,
                markup_basis_points=_percent_to_basis_points(markup_percent),
                enabled=enabled,
                slug=slug or None,
            )
        except ValueError as exc:
            error = str(exc)
            if error == "custom_model_limit_exceeded":
                return _custom_model_redirect("error=limit")
            if error == "invalid_custom_model_slug":
                return _custom_model_redirect("error=slug")
            if error == "custom_model_slug_taken":
                return _custom_model_redirect("error=slug_taken")
            raise
        return RedirectResponse(url="/console/custom-models?saved=created", status_code=303)

    # Register the action route before the catch-all model path below. Custom
    # model IDs contain slashes, so the path converter would otherwise consume
    # the trailing /delete segment and dispatch this POST to the edit handler.
    @app.post("/console/custom-models/{model_id:path}/delete")
    def console_delete_custom_model(ctx: ConsoleDep, model_id: str) -> Response:
        model = _require_owner_model(model_id, ctx.user.id)
        STORE.delete_custom_model(model.id, owner_user_id=ctx.user.id)
        return RedirectResponse(url="/console/custom-models?saved=deleted", status_code=303)

    @app.post("/console/custom-models/{model_id:path}")
    def console_update_custom_model(
        ctx: ConsoleDep,
        settings: SettingsDep,
        model_id: str,
        name: str = Form(..., min_length=1, max_length=120),
        slug: str | None = Form(default=None, min_length=3, max_length=96),
        base_model_id: str = Form(..., min_length=1, max_length=256),
        hidden_prompt: str = Form("", max_length=CUSTOM_MODEL_PROMPT_CHAR_LIMIT),
        markup_percent: Decimal = Form(Decimal("0"), ge=0, le=300),
        enabled: bool = Form(False),
    ) -> Response:
        if missing_custom_model_requirements(ctx.user, settings):
            return _custom_model_redirect("error=verification")
        model = _require_owner_model(model_id, ctx.user.id)
        _require_base_model(base_model_id)
        try:
            STORE.update_custom_model(
                model.id,
                owner_user_id=ctx.user.id,
                patch={
                    "name": name,
                    "slug": slug,
                    "base_model_id": base_model_id,
                    "hidden_prompt": hidden_prompt,
                    "markup_basis_points": _percent_to_basis_points(markup_percent),
                    "enabled": enabled,
                },
            )
        except ValueError as exc:
            error = str(exc)
            if error == "invalid_custom_model_slug":
                return _custom_model_redirect("error=slug")
            if error == "custom_model_slug_taken":
                return _custom_model_redirect("error=slug_taken")
            raise
        return RedirectResponse(url="/console/custom-models?saved=updated", status_code=303)

def _render_page(ctx: ConsoleDep, settings: SettingsDep, *, request: Request) -> str:
    models = [_model_view(model) for model in STORE.list_custom_models_for_user(ctx.user.id)]
    missing_requirements = missing_custom_model_requirements(ctx.user, settings)
    owner_username = creator_username_for_models(
        ctx.user,
        enforce_verification=False,
    )
    return render(
        "console/custom_models.html",
        settings=settings,
        ctx=ctx,
        active="custom-models",
        page_title="Custom Models",
        page_subtitle="Create hidden-prompt model aliases that run through the attested gateway.",
        models=models,
        base_models=_base_model_options(),
        limit=CUSTOM_MODEL_LIMIT_PER_USER,
        prompt_limit=CUSTOM_MODEL_PROMPT_CHAR_LIMIT,
        model_prefix=f"tr-custom-model/{owner_username}-",
        verification={
            "met": not missing_requirements,
            "missing_requirements": missing_requirements,
            "url": "/console/account/verification",
        },
        flash=_flash_message(request.query_params.get("saved"), request.query_params.get("error")),
    )


def _model_view(model: CustomModel) -> dict[str, Any]:
    base = MODELS.get(model.base_model_id)
    return {
        "id": model.id,
        "slug": model.slug,
        "name": model.name,
        "base_model_id": model.base_model_id,
        "base_model_name": base.name if base else model.base_model_id,
        "hidden_prompt": model.hidden_prompt,
        "markup_percent": _basis_points_to_percent(model.markup_basis_points),
        "revision": model.revision,
        "enabled": model.enabled,
        "created_at": model.created_at,
        "updated_at": model.updated_at,
        "test_url": f"/user-chat?model={model.id}",
    }


def _base_model_options() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for model in MODELS.values():
        if not is_allowed_custom_model_base(model):
            continue
        rows.append({"id": model.id, "name": model.name})
    rows.sort(key=lambda row: (row["name"].lower(), row["id"]))
    return rows


def _require_owner_model(model_id: str, owner_user_id: str) -> CustomModel:
    model = STORE.get_custom_model(model_id)
    if model is None or model.owner_user_id != owner_user_id:
        raise HTTPException(status_code=404, detail="Custom model not found")
    return model


def _require_base_model(model_id: str) -> None:
    require_custom_model_base_model(model_id)


def _custom_model_redirect(query: str) -> RedirectResponse:
    return RedirectResponse(url=f"/console/custom-models?{query}", status_code=303)


def _percent_to_basis_points(value: Decimal) -> int:
    basis_points = value * 100
    if basis_points != basis_points.to_integral_value():
        raise HTTPException(status_code=400, detail="Markup supports two decimal places")
    return int(basis_points)


def _basis_points_to_percent(value: int) -> str:
    return format(Decimal(value) / 100, "f")


def _flash_message(saved: str | None, error: str | None) -> dict[str, str] | None:
    if error == "limit":
        return {
            "type": "error",
            "text": f"Custom model limit reached ({CUSTOM_MODEL_LIMIT_PER_USER}).",
        }
    if error == "slug":
        return {
            "type": "error",
            "text": "Slug must be 3-64 lowercase letters, numbers, or hyphens.",
        }
    if error == "slug_taken":
        return {"type": "error", "text": "That custom model slug is already in use."}
    if error == "verification":
        return {
            "type": "error",
            "text": "Verify your account before creating or editing custom models.",
            "href": "/console/account/verification",
            "link_text": "Complete verification",
        }
    if saved:
        return {"type": "success", "text": "Custom model saved."}
    return None
