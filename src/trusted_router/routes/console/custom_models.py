from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from trusted_router.auth import SettingsDep
from trusted_router.catalog import MODELS
from trusted_router.custom_model_rules import (
    is_allowed_custom_model_base,
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
    async def console_custom_models(
        request: Request,
        ctx: ConsoleDep,
        settings: SettingsDep,
    ) -> Response:
        return HTMLResponse(_render_page(ctx, settings, request=request))

    @app.post("/console/custom-models")
    async def console_create_custom_model(
        ctx: ConsoleDep,
        settings: SettingsDep,
        name: str = Form(..., min_length=1, max_length=120),
        slug: str | None = Form(default=None, max_length=96),
        base_model_id: str = Form(..., min_length=1, max_length=256),
        hidden_prompt: str = Form("", max_length=CUSTOM_MODEL_PROMPT_CHAR_LIMIT),
        enabled: bool = Form(False),
    ) -> Response:
        _require_base_model(base_model_id)
        try:
            STORE.create_custom_model(
                owner_user_id=ctx.user.id,
                owner_workspace_id=ctx.workspace.id,
                name=name,
                base_model_id=base_model_id,
                hidden_prompt=hidden_prompt,
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

    @app.post("/console/custom-models/{model_id:path}")
    async def console_update_custom_model(
        ctx: ConsoleDep,
        model_id: str,
        name: str = Form(..., min_length=1, max_length=120),
        slug: str | None = Form(default=None, min_length=3, max_length=96),
        base_model_id: str = Form(..., min_length=1, max_length=256),
        hidden_prompt: str = Form("", max_length=CUSTOM_MODEL_PROMPT_CHAR_LIMIT),
        enabled: bool = Form(False),
    ) -> Response:
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

    @app.post("/console/custom-models/{model_id:path}/delete")
    async def console_delete_custom_model(ctx: ConsoleDep, model_id: str) -> Response:
        model = _require_owner_model(model_id, ctx.user.id)
        STORE.delete_custom_model(model.id, owner_user_id=ctx.user.id)
        return RedirectResponse(url="/console/custom-models?saved=deleted", status_code=303)


def _render_page(ctx: ConsoleDep, settings: SettingsDep, *, request: Request) -> str:
    models = [_model_view(model) for model in STORE.list_custom_models_for_user(ctx.user.id)]
    return render(
        "console/custom_models.html",
        settings=settings,
        user=ctx.user,
        workspace=ctx.workspace,
        active="custom-models",
        page_title="Custom Models",
        page_subtitle="Create hidden-prompt model aliases that run through the attested gateway.",
        models=models,
        base_models=_base_model_options(),
        limit=CUSTOM_MODEL_LIMIT_PER_USER,
        prompt_limit=CUSTOM_MODEL_PROMPT_CHAR_LIMIT,
        flash=_flash_message(request.query_params.get("saved"), request.query_params.get("error")),
    )


def _model_view(model: CustomModel) -> dict[str, Any]:
    base = MODELS.get(model.base_model_id)
    return {
        "id": model.id,
        "slug": model.id.removeprefix("trustedrouter/user-"),
        "name": model.name,
        "base_model_id": model.base_model_id,
        "base_model_name": base.name if base else model.base_model_id,
        "hidden_prompt": model.hidden_prompt,
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
    if saved:
        return {"type": "success", "text": "Custom model saved."}
    return None
