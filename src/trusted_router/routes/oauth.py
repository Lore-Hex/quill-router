"""Generic OAuth sign-in routes for Google + GitHub.

For each `OAuthProvider` in `OAUTH_PROVIDERS` we register two routes:

- `GET /auth/{slug}/login` — mints a CSRF state cookie (and a
  `?next=…` cookie if supplied) and 302s to the provider's consent page.
- `GET /{slug}_oauth_callback?code&state` — verifies state, exchanges
  the code, fetches the user profile, finds-or-creates the local user,
  mints an active session cookie, and 302s to either the one-shot welcome
  page (new user) or the requested `next` / `/console/api-keys`.

The callback path is `/{slug}_oauth_callback` (not `/auth/{slug}/callback`)
because Google + GitHub require the redirect URL registered with them to
match exactly, and ours are `https://trustedrouter.com/google_oauth_callback`
and `https://TrustedRouter.com/github_oauth_callback`.
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import RedirectResponse

from trusted_router.auth import SettingsDep, set_session_cookie
from trusted_router.config import Settings
from trusted_router.errors import api_error
from trusted_router.oauth_provider import (
    OAUTH_PROVIDERS,
    OAuthProvider,
    exchange_code,
    fetch_user,
)
from trusted_router.storage import STORE
from trusted_router.types import ErrorType

OAUTH_STATE_COOKIE = "tr_oauth_state"
OAUTH_NEXT_COOKIE = "tr_oauth_next"
OAUTH_STATE_COOKIE_MAX_AGE = 600  # 10 minutes


def register_oauth_routes(app: FastAPI, router: APIRouter) -> None:
    for provider in OAUTH_PROVIDERS.values():
        _register_provider(app, router, provider)


def _register_provider(app: FastAPI, router: APIRouter, provider: OAuthProvider) -> None:
    slug = provider.slug

    # Closures bind a single `provider` argument over the module-level
    # handlers below. Keeping the route body as a pass-through one-liner
    # means the registered FastAPI dependency doesn't need a type-ignore
    # around closed-over variables — mypy sees a clean function with
    # explicit-typed parameters.
    @router.get(f"/auth/{slug}/login", name=f"oauth_{slug}_login")
    async def login(
        request: Request,
        settings: SettingsDep,
        next: str | None = None,  # noqa: A002 - OpenRouter-style query param.
    ) -> Response:
        return await _handle_login(provider, request, settings, next)

    @app.get(f"/{slug}_oauth_callback", name=f"oauth_{slug}_callback")
    async def callback(
        request: Request,
        settings: SettingsDep,
        code: str | None = None,
        state: str | None = None,
    ) -> Response:
        return await _handle_callback(provider, request, settings, code, state)


async def _handle_login(
    provider: OAuthProvider,
    request: Request,
    settings: Settings,
    next_path: str | None,
) -> Response:
    if not _enabled(provider, settings):
        raise api_error(
            404, f"{provider.slug.title()} sign-in is not configured", ErrorType.NOT_FOUND,
        )
    redirect_uri = _redirect_uri(provider, request, settings)
    state = secrets.token_urlsafe(24)
    url = provider.authorize_redirect(
        client_id=_client_id(provider, settings) or "",
        redirect_uri=redirect_uri,
        state=state,
    )
    response = RedirectResponse(url=url, status_code=302)
    _set_state_cookie(response, state, settings)
    _set_next_cookie(response, next_path, settings)
    return response


async def _handle_callback(
    provider: OAuthProvider,
    request: Request,
    settings: Settings,
    code: str | None,
    state: str | None,
) -> Response:
    if not _enabled(provider, settings):
        raise api_error(
            404, f"{provider.slug.title()} sign-in is not configured", ErrorType.NOT_FOUND,
        )
    if not code or not state:
        raise api_error(400, "Missing OAuth code or state", ErrorType.BAD_REQUEST)
    cookie_state = request.cookies.get(OAUTH_STATE_COOKIE)
    if not cookie_state or cookie_state != state:
        raise api_error(400, "Invalid OAuth state", ErrorType.BAD_REQUEST)

    access_token = await exchange_code(
        provider=provider,
        code=code,
        client_id=_client_id(provider, settings) or "",
        client_secret=_client_secret(provider, settings) or "",
        redirect_uri=_redirect_uri(provider, request, settings),
    )
    info = await fetch_user(provider=provider, access_token=access_token)
    if not info.email_verified:
        raise api_error(
            400,
            f"{provider.slug.title()} did not return a verified email",
            ErrorType.BAD_REQUEST,
        )

    existing_user = STORE.find_user_by_email(info.email)
    first_time = existing_user is None
    if existing_user is None:
        # signup() returns None only on a TOCTOU race; fall back to a
        # fresh lookup, surface a real 500 if even that fails so we
        # don't deref None silently.
        result = STORE.signup(email=info.email)
        if result is not None:
            user_id = result.user.id
        else:
            concurrent = STORE.find_user_by_email(info.email)
            if concurrent is None:
                raise api_error(
                    500,
                    "Could not create or find user account; please retry sign-in",
                    ErrorType.INTERNAL_ERROR,
                )
            user_id = concurrent.id
    else:
        user_id = existing_user.id
    STORE.mark_user_email_verified(user_id)

    raw_token, _ = STORE.create_auth_session(
        user_id=user_id,
        provider=provider.slug,
        label=info.email,
        ttl_seconds=settings.auth_session_ttl_seconds,
        state="active",
    )

    target = _next_target(request) or (
        "/console/welcome?first=1" if first_time else "/console/api-keys"
    )
    response = RedirectResponse(url=target, status_code=302)
    set_session_cookie(response, raw_token, settings)
    _clear_state_and_next_cookies(response, settings)
    return response


def _enabled(provider: OAuthProvider, settings: Settings) -> bool:
    if provider.slug == "google":
        return settings.google_oauth_enabled
    if provider.slug == "github":
        return settings.github_oauth_enabled
    return False


def _client_id(provider: OAuthProvider, settings: Settings) -> str | None:
    return getattr(settings, f"{provider.slug}_client_id", None)


def _client_secret(provider: OAuthProvider, settings: Settings) -> str | None:
    return getattr(settings, f"{provider.slug}_client_secret", None)


def _redirect_uri(provider: OAuthProvider, request: Request, settings: Settings) -> str:
    configured = getattr(settings, f"{provider.slug}_oauth_redirect_url", None)
    if configured:
        return configured
    scheme = "https" if settings.environment.lower() == "production" else request.url.scheme
    host = request.headers.get("host", request.url.netloc)
    return f"{scheme}://{host}/{provider.slug}_oauth_callback"


def _safe_next_path(value: str | None) -> str | None:
    if not value or len(value) > 2048:
        return None
    if not value.startswith("/") or value.startswith("//"):
        return None
    return value


def _set_state_cookie(response: Response, state: str, settings: Settings) -> None:
    response.set_cookie(
        key=OAUTH_STATE_COOKIE,
        value=state,
        max_age=OAUTH_STATE_COOKIE_MAX_AGE,
        httponly=True,
        secure=settings.environment.lower() == "production",
        samesite="lax",
        path="/",
    )


def _set_next_cookie(response: Response, next_path: str | None, settings: Settings) -> None:
    safe = _safe_next_path(next_path)
    if safe is None:
        return
    response.set_cookie(
        key=OAUTH_NEXT_COOKIE,
        value=safe,
        max_age=OAUTH_STATE_COOKIE_MAX_AGE,
        httponly=True,
        secure=settings.environment.lower() == "production",
        samesite="lax",
        path="/",
    )


def _next_target(request: Request) -> str | None:
    return _safe_next_path(request.cookies.get(OAUTH_NEXT_COOKIE))


def _clear_state_and_next_cookies(response: Response, settings: Settings) -> None:
    secure = settings.environment.lower() == "production"
    response.delete_cookie(key=OAUTH_STATE_COOKIE, path="/", secure=secure, samesite="lax")
    response.delete_cookie(key=OAUTH_NEXT_COOKIE, path="/", secure=secure, samesite="lax")
