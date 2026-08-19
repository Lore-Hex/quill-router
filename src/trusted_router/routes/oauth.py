"""Generic OAuth sign-in routes for Google + GitHub.

For each `OAuthProvider` in `OAUTH_PROVIDERS` we register two routes:

- `GET /auth/{slug}/login` — mints a CSRF state cookie (and a
  `?next=…` cookie if supplied) and 302s to the provider's consent page.
- `GET /{slug}_oauth_callback?code&state` — verifies state, exchanges
  the code, fetches the user profile, finds-or-creates the local user,
  mints an active session cookie, and 302s to either the one-shot welcome
  page (new user) or the requested `next` / `/console/api-keys`.

The callback path is `/{slug}_oauth_callback` (not `/auth/{slug}/callback`)
because providers require an exact registered redirect URI. Every first-party
domain uses its own provider client and same-origin callback, so a backup
domain never depends on the canonical domain to complete login.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from urllib.parse import parse_qsl, urlsplit

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import RedirectResponse

from trusted_router.acquisition import record_signup_attribution
from trusted_router.auth import SettingsDep, set_session_cookie
from trusted_router.config import Settings
from trusted_router.domains import configured_control_domains, request_hostname
from trusted_router.errors import api_error
from trusted_router.oauth_provider import (
    OAUTH_PROVIDERS,
    OAuthProvider,
    exchange_code,
    fetch_user,
)
from trusted_router.signup_gate import require_new_account_creation
from trusted_router.storage import STORE
from trusted_router.types import ErrorType

OAUTH_STATE_COOKIE = "tr_oauth_state"
OAUTH_NEXT_COOKIE = "tr_oauth_next"
OAUTH_STATE_COOKIE_MAX_AGE = 600  # 10 minutes
OAUTH_STATE_VERSION = "v1"

# Short-lived one-shot cookie that carries the raw API key from the OAuth
# signup callback to the /console/welcome reveal page. Without this the
# `result.raw_key` minted in STORE.signup() falls out of scope between
# the OAuth callback and the welcome render, and the welcome page shows
# the "this key has already been displayed" fallback — which is what
# Gabriella saw on 2026-05-23. 5-minute TTL bounds the window where the
# key is in any HTTP header; HttpOnly prevents JS read; SameSite=Lax so
# the cookie survives the OAuth 302; Secure in prod so it's never sent
# over plain HTTP. Path is /console/welcome so the cookie isn't echoed
# on any other request (limits log-exposure surface).
PENDING_REVEAL_COOKIE = "tr_pending_reveal"
PENDING_REVEAL_COOKIE_MAX_AGE = 300  # 5 minutes — bounded reveal window


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
    request_domain = _request_oauth_domain(request, settings)
    if request_domain is None:
        raise api_error(400, "Invalid OAuth host", ErrorType.BAD_REQUEST)
    if not _enabled(provider, settings, request_domain):
        raise api_error(
            404,
            f"{provider.slug.title()} sign-in is not configured",
            ErrorType.NOT_FOUND,
        )
    redirect_uri = _provider_redirect_uri(
        provider,
        request,
        settings,
        request_domain,
    )
    state = _new_state(
        provider,
        request_domain,
        settings,
    )
    url = provider.authorize_redirect(
        client_id=_client_id(provider, settings, request_domain) or "",
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
    if not code or not state:
        raise api_error(400, "Missing OAuth code or state", ErrorType.BAD_REQUEST)
    state_domain = _state_domain(provider, state, settings)
    if state_domain is None:
        raise api_error(400, "Invalid OAuth state", ErrorType.BAD_REQUEST)
    if not _enabled(provider, settings, state_domain):
        raise api_error(
            404,
            f"{provider.slug.title()} sign-in is not configured",
            ErrorType.NOT_FOUND,
        )

    request_domain = _request_oauth_domain(request, settings)
    if request_domain is None or request_domain != state_domain:
        raise api_error(400, "Invalid OAuth callback host", ErrorType.BAD_REQUEST)

    cookie_state = request.cookies.get(OAUTH_STATE_COOKIE)
    if not cookie_state or cookie_state != state:
        raise api_error(400, "Invalid OAuth state", ErrorType.BAD_REQUEST)

    access_token = await exchange_code(
        provider=provider,
        code=code,
        client_id=_client_id(provider, settings, state_domain) or "",
        client_secret=_client_secret(provider, settings, state_domain) or "",
        redirect_uri=_provider_redirect_uri(
            provider,
            request,
            settings,
            state_domain,
        ),
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
    next_target = _next_target(request)
    delegated_signup = first_time and _is_credit_delegation_target(next_target)
    # `pending_reveal_raw_key` is the raw API key minted during signup; it
    # must survive the 302 to /console/welcome so the user can copy it.
    # Without this hand-off, the welcome page shows "key already been
    # displayed" on every first login. See PENDING_REVEAL_COOKIE above.
    pending_reveal_raw_key: str | None = None
    if existing_user is None:
        # Creation-only global brake: provider login/state/profile validation
        # still runs, and returning users never enter this branch.
        require_new_account_creation(settings)
        if delegated_signup:
            # An app-originated signup receives no promotional credit and no
            # management API key. The app gets an inference-only key after
            # explicit consent, and the user can fund the workspace in that
            # same consent flow. Direct TrustedRouter signups keep the normal
            # starter-credit and one-time management-key experience below.
            user = STORE.ensure_user(
                info.email,
                email=info.email,
                trial_credit_microdollars=0,
            )
            workspace = STORE.list_workspaces_for_user(user.id)[0]
            user_id = user.id
            record_signup_attribution(
                request,
                workspace_id=workspace.id,
                signup_provider=provider.slug,
                starter_credit_microdollars=0,
            )
        else:
            # signup() returns None only on a TOCTOU race; fall back to a
            # fresh lookup, surface a real 500 if even that fails so we
            # don't deref None silently.
            result = STORE.signup(
                email=info.email,
                trial_credit_microdollars=settings.signup_trial_credit_microdollars,
            )
            if result is not None:
                user_id = result.user.id
                pending_reveal_raw_key = result.raw_key
                record_signup_attribution(
                    request,
                    workspace_id=result.workspace.id,
                    signup_provider=provider.slug,
                    starter_credit_microdollars=result.trial_credit_microdollars,
                )
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

    target = next_target or ("/console/welcome?first=1" if first_time else "/console/api-keys")
    response = RedirectResponse(url=target, status_code=302)
    set_session_cookie(response, raw_token, settings)
    _clear_state_and_next_cookies(response, settings)
    if pending_reveal_raw_key is not None and first_time:
        # One-shot hand-off to /console/welcome. Scoped to that path so
        # this cookie is never sent on any other request, which keeps
        # the raw key out of every other access log line.
        response.set_cookie(
            key=PENDING_REVEAL_COOKIE,
            value=pending_reveal_raw_key,
            max_age=PENDING_REVEAL_COOKIE_MAX_AGE,
            httponly=True,
            secure=settings.environment.lower() == "production",
            samesite="lax",
            path="/console/welcome",
        )
    return response


def _enabled(provider: OAuthProvider, settings: Settings, domain: str) -> bool:
    return bool(
        _client_id(provider, settings, domain) and _client_secret(provider, settings, domain)
    )


def _request_oauth_domain(request: Request, settings: Settings) -> str | None:
    """Return an exact configured apex, never the generic Host fallback.

    Public rendering intentionally falls back to the canonical domain when an
    unknown Host arrives so untrusted values are never reflected. OAuth must be
    stricter: treating an unknown or prefixed host as canonical would weaken
    the same-origin binding carried in signed state.
    """
    hostname = request_hostname(request)
    domains = configured_control_domains(settings)
    if hostname in domains:
        return hostname
    local_hostname = request.headers.get("host", "").split(":", 1)[0].lower()
    if settings.environment.lower() in {"local", "test"} and local_hostname in {
        "127.0.0.1",
        "localhost",
        "testserver",
    }:
        return domains[0]
    return None


def _client_id(
    provider: OAuthProvider,
    settings: Settings,
    domain: str,
) -> str | None:
    if domain != configured_control_domains(settings)[0]:
        credentials = _alias_credentials(provider, settings).get(domain)
        return credentials[0] if credentials else None
    return getattr(settings, f"{provider.slug}_client_id", None)


def _client_secret(
    provider: OAuthProvider,
    settings: Settings,
    domain: str,
) -> str | None:
    if domain != configured_control_domains(settings)[0]:
        credentials = _alias_credentials(provider, settings).get(domain)
        return credentials[1] if credentials else None
    return getattr(settings, f"{provider.slug}_client_secret", None)


def _alias_credentials(
    provider: OAuthProvider,
    settings: Settings,
) -> dict[str, tuple[str, str]]:
    credentials = getattr(settings, f"{provider.slug}_alias_credentials", None)
    if isinstance(credentials, dict):
        return credentials
    return {}


def _provider_redirect_uri(
    provider: OAuthProvider,
    request: Request,
    settings: Settings,
    domain: str,
) -> str:
    """Return the provider-registered callback URI for this domain.

    Every production domain completes OAuth on its own origin and uses an
    independent OAuth client. GitHub requires this because an OAuth App permits
    one callback URL; using the same isolation for Google preserves login when
    a canonical-domain OAuth client is disabled or misconfigured.
    """
    if domain != configured_control_domains(settings)[0]:
        return f"https://{domain}/{provider.slug}_oauth_callback"
    configured = getattr(settings, f"{provider.slug}_oauth_redirect_url", None)
    if configured:
        return configured
    scheme = "https" if settings.environment.lower() == "production" else request.url.scheme
    host = request.headers.get("host", request.url.netloc)
    return f"{scheme}://{host}/{provider.slug}_oauth_callback"


def _new_state(
    provider: OAuthProvider,
    domain: str,
    settings: Settings,
) -> str:
    nonce = secrets.token_urlsafe(24)
    encoded_domain = _b64encode(domain.encode("ascii"))
    unsigned = f"{OAUTH_STATE_VERSION}.{encoded_domain}.{nonce}"
    signature = _state_signature(provider, unsigned, settings, domain)
    return f"{unsigned}.{signature}"


def _state_domain(
    provider: OAuthProvider,
    state: str,
    settings: Settings,
) -> str | None:
    parts = state.split(".")
    if len(parts) != 4 or parts[0] != OAUTH_STATE_VERSION:
        return None
    try:
        domain = _b64decode(parts[1]).decode("ascii")
    except (ValueError, UnicodeDecodeError):
        return None
    if domain not in configured_control_domains(settings):
        return None
    unsigned = ".".join(parts[:3])
    try:
        expected = _state_signature(provider, unsigned, settings, domain)
    except RuntimeError:
        return None
    if not hmac.compare_digest(parts[3], expected):
        return None
    return domain


def _state_signature(
    provider: OAuthProvider,
    unsigned_state: str,
    settings: Settings,
    domain: str,
) -> str:
    secret = _client_secret(provider, settings, domain)
    if not secret:
        # Callers already reject disabled providers. Keeping this fail-closed
        # guard makes the helper safe if it is reused independently.
        raise RuntimeError(f"{provider.slug} OAuth client secret is unavailable")
    digest = hmac.new(
        secret.encode("utf-8"),
        f"{provider.slug}:{unsigned_state}".encode(),
        hashlib.sha256,
    ).digest()
    return _b64encode(digest)


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, altchars=b"-_", validate=True)


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


def _is_credit_delegation_target(value: str | None) -> bool:
    """Identify an app-originated delegated-key flow without trusting hosts.

    The target has already passed `_safe_next_path`; requiring the exact auth
    route plus a callback URL prevents an unrelated `next=/auth/...` link from
    changing signup-credit behavior.
    """
    safe = _safe_next_path(value)
    if safe is None:
        return False
    parsed = urlsplit(safe)
    if parsed.path not in {"/auth", "/v1/auth"}:
        return False
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    return bool(params.get("callback_url"))


def _clear_state_and_next_cookies(response: Response, settings: Settings) -> None:
    secure = settings.environment.lower() == "production"
    response.delete_cookie(key=OAUTH_STATE_COOKIE, path="/", secure=secure, samesite="lax")
    response.delete_cookie(key=OAUTH_NEXT_COOKIE, path="/", secure=secure, samesite="lax")
