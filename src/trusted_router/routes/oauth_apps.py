from __future__ import annotations

import dataclasses
import re
import unicodedata
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from trusted_router.auth import SettingsDep
from trusted_router.config import Settings
from trusted_router.errors import api_error
from trusted_router.oauth_app_policy import user_can_receive_creator_payouts
from trusted_router.routes.console._shared import require_console_context
from trusted_router.routes.helpers import json_body
from trusted_router.routes.oauth_keys import _validate_callback_url
from trusted_router.storage import STORE, OAuthApp, User
from trusted_router.types import ErrorType

OAUTH_APP_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
OAUTH_APP_RESERVED_IDS = frozenset(
    {"trustedrouter", "tr", "api", "console", "admin", "www"}
)
OAUTH_APP_PROTECTED_TERMS = frozenset(
    {"trustedrouter", "trusted router", "veriff", "lorehex", "quill"}
)
# This curated confusables map is an impersonation heuristic, not a security
# boundary. The verified owner-disclosure line on consent is the real defense.
OAUTH_APP_CONFUSABLES: dict[str, str] = {
    # Cyrillic lookalikes (input is casefolded before this map is applied).
    "а": "a",
    "е": "e",
    "о": "o",
    "р": "p",
    "с": "c",
    "х": "x",
    "у": "y",
    "т": "t",
    "в": "b",
    "м": "m",
    "н": "h",
    "к": "k",
    "і": "i",
    "ѕ": "s",
    "г": "r",
    # Greek lookalikes.
    "ο": "o",
    "α": "a",
    "ε": "e",
    "ρ": "p",
    "ι": "i",
    "κ": "k",
    "τ": "t",
    "β": "b",
    "μ": "m",
    "ν": "n",
    # Common digit/letter substitutions.
    "0": "o",
    "1": "l",
    "3": "e",
    "5": "s",
    "@": "a",
}
OAUTH_APP_CREATE_FIELDS = frozenset(
    {
        "id",
        "name",
        "redirect_uris",
        "logo_url",
        "markup_basis_points",
        "suspended",
    }
)
OAUTH_APP_PATCH_FIELDS = OAUTH_APP_CREATE_FIELDS - {"id"}


def register_oauth_app_routes(router: APIRouter) -> None:
    @router.post("/oauth/apps")
    async def create_oauth_app(
        request: Request,
        settings: SettingsDep,
    ) -> JSONResponse:
        owner = _resolved_user(request, settings)
        body = await json_body(request)
        values = _validated_create(body)
        if values["markup_basis_points"] > 0 and not values["suspended"]:
            _require_identity_verification(owner)
        app = OAuthApp(owner_user_id=owner.id, **values)
        try:
            STORE.create_oauth_app(app)
        except ValueError as exc:
            if str(exc) == "oauth_app_id_taken":
                raise api_error(
                    409,
                    "OAuth app id is already in use",
                    ErrorType.CONFLICT,
                ) from exc
            raise
        return JSONResponse({"data": _oauth_app_shape(app)}, status_code=201)

    @router.get("/oauth/apps")
    async def list_oauth_apps(
        request: Request,
        settings: SettingsDep,
    ) -> dict[str, Any]:
        owner = _resolved_user(request, settings)
        return {
            "data": [
                _oauth_app_shape(app)
                for app in STORE.list_oauth_apps_for_user(owner.id)
            ]
        }

    @router.get("/oauth/apps/{app_id}")
    async def get_oauth_app(
        app_id: str,
        request: Request,
        settings: SettingsDep,
    ) -> dict[str, Any]:
        owner = _resolved_user(request, settings)
        return {"data": _oauth_app_shape(_owned_app(app_id, owner))}

    @router.patch("/oauth/apps/{app_id}")
    async def patch_oauth_app(
        app_id: str,
        request: Request,
        settings: SettingsDep,
    ) -> dict[str, Any]:
        owner = _resolved_user(request, settings)
        app = _owned_app(app_id, owner)
        patch = _validated_patch(await json_body(request))
        resulting_markup = int(patch.get("markup_basis_points", app.markup_basis_points))
        resulting_suspended = bool(patch.get("suspended", app.suspended))
        if resulting_markup > 0 and not resulting_suspended:
            _require_identity_verification(owner)
        updated = STORE.update_oauth_app(app.id, patch=patch)
        if updated is None:
            raise api_error(404, "Resource not found", ErrorType.NOT_FOUND)
        return {"data": _oauth_app_shape(updated)}


def _resolved_user(request: Request, settings: Settings) -> User:
    """Bind every registry operation to an active browser session cookie."""
    try:
        context = require_console_context(request, settings)
    except HTTPException as exc:
        if exc.status_code != 302:
            raise
        raise _console_session_required() from exc
    if not context.can_manage:
        raise _console_session_required()
    return context.user


def _console_session_required() -> HTTPException:
    return api_error(
        403,
        "OAuth app management requires a signed-in console session cookie",
        ErrorType.FORBIDDEN,
    )


def _require_identity_verification(user: User) -> None:
    if not user_can_receive_creator_payouts(user):
        raise api_error(
            403,
            "Full identity verification is required to enable earnings on a monetized "
            "OAuth app; complete the Veriff identity verification flow first.",
            ErrorType.VERIFICATION_REQUIRED,
        )


def _owned_app(app_id: str, owner: User) -> OAuthApp:
    app = STORE.get_oauth_app(app_id)
    if app is None or app.owner_user_id != owner.id:
        raise api_error(404, "Resource not found", ErrorType.NOT_FOUND)
    return app


def _validated_create(body: dict[str, Any]) -> dict[str, Any]:
    unknown = set(body) - OAUTH_APP_CREATE_FIELDS
    if unknown:
        raise api_error(400, "Unknown OAuth app field", ErrorType.BAD_REQUEST)
    app_id = body.get("id")
    if isinstance(app_id, str) and _is_reserved_app_id(app_id):
        raise api_error(400, "id is reserved", ErrorType.BAD_REQUEST)
    if not isinstance(app_id, str) or not OAUTH_APP_ID_PATTERN.fullmatch(app_id):
        raise api_error(
            400,
            "id must be 3-64 lowercase letters, numbers, or hyphens",
            ErrorType.BAD_REQUEST,
        )
    return {
        "id": app_id,
        "name": _validated_name(body.get("name")),
        "redirect_uris": _validated_redirect_uris(body.get("redirect_uris")),
        "logo_url": _validated_logo_url(body.get("logo_url")),
        "markup_basis_points": _validated_markup_basis_points(
            body.get("markup_basis_points", 0)
        ),
        "suspended": _validated_suspended(body.get("suspended", False)),
    }


def _validated_patch(body: dict[str, Any]) -> dict[str, Any]:
    unknown = set(body) - OAUTH_APP_PATCH_FIELDS
    if unknown:
        raise api_error(
            400,
            "Only name, redirect_uris, logo_url, markup_basis_points, and suspended can be updated",
            ErrorType.BAD_REQUEST,
        )
    if not body:
        raise api_error(400, "At least one field is required", ErrorType.BAD_REQUEST)
    patch: dict[str, Any] = {}
    if "name" in body:
        patch["name"] = _validated_name(body["name"])
    if "redirect_uris" in body:
        patch["redirect_uris"] = _validated_redirect_uris(body["redirect_uris"])
    if "logo_url" in body:
        patch["logo_url"] = _validated_logo_url(body["logo_url"])
    if "markup_basis_points" in body:
        patch["markup_basis_points"] = _validated_markup_basis_points(
            body["markup_basis_points"]
        )
    if "suspended" in body:
        patch["suspended"] = _validated_suspended(body["suspended"])
    return patch


def _validated_name(raw: Any) -> str:
    if not isinstance(raw, str):
        raise api_error(400, "name must be a string", ErrorType.BAD_REQUEST)
    name = raw.strip()
    if not 1 <= len(name) <= 80:
        raise api_error(400, "name must be 1-80 characters", ErrorType.BAD_REQUEST)
    validate_oauth_app_name(name)
    return name


def _normalized_app_identity(value: str) -> str:
    """Fold accents, case, separators, and curated cross-script lookalikes."""
    decomposed = unicodedata.normalize("NFKD", value)
    without_marks = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    confusable_folded = "".join(
        OAUTH_APP_CONFUSABLES.get(character, character)
        for character in without_marks.casefold()
    )
    return "".join(character for character in confusable_folded if character.isalnum())


def _normalized_protected_terms(terms: frozenset[str]) -> frozenset[str]:
    return frozenset(_normalized_app_identity(term) for term in terms)


def _is_reserved_app_id(app_id: str) -> bool:
    # Valid slugs are ASCII lowercase. Exact compact normalization additionally
    # keeps prior homoglyph/separator spellings on the reserved side of errors.
    normalized = _normalized_app_identity(app_id)
    return any(
        app_id == term
        or app_id.startswith(f"{term}-")
        or normalized == _normalized_app_identity(term)
        for term in OAUTH_APP_RESERVED_IDS
    )


def validate_oauth_app_name(name: str) -> None:
    """Reject branding that could impersonate TrustedRouter or trust partners."""
    normalized = _normalized_app_identity(name)
    if any(
        protected in normalized
        for protected in _normalized_protected_terms(OAUTH_APP_PROTECTED_TERMS)
    ):
        raise api_error(
            400,
            "names that could be mistaken for TrustedRouter are not allowed",
            ErrorType.BAD_REQUEST,
        )


def _validated_redirect_uris(raw: Any) -> list[str]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= 10:
        raise api_error(
            400,
            "redirect_uris must contain 1-10 entries",
            ErrorType.BAD_REQUEST,
        )
    redirects: list[str] = []
    for value in raw:
        if not isinstance(value, str):
            raise api_error(
                400,
                "redirect_uris entries must be strings",
                ErrorType.BAD_REQUEST,
            )
        # Keep the validated string byte-for-byte. Registered callbacks are an
        # exact-match allowlist, not normalized URL equivalents.
        redirects.append(_validate_callback_url(value))
    return redirects


def _validated_logo_url(raw: Any) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or len(raw) > 2048:
        raise api_error(400, "logo_url must be an https URL", ErrorType.BAD_REQUEST)
    parsed = urlsplit(raw)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise api_error(400, "logo_url must be an https URL", ErrorType.BAD_REQUEST)
    return raw


def _validated_markup_basis_points(raw: Any) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or not 0 <= raw <= 30_000:
        raise api_error(
            400,
            "markup_basis_points must be an integer from 0 to 30000",
            ErrorType.BAD_REQUEST,
        )
    return raw


def _validated_suspended(raw: Any) -> bool:
    if not isinstance(raw, bool):
        raise api_error(400, "suspended must be a boolean", ErrorType.BAD_REQUEST)
    return raw


def _oauth_app_shape(app: OAuthApp) -> dict[str, Any]:
    return dataclasses.asdict(app)
