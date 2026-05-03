"""Shared OAuth provider plumbing — Google and GitHub diverge only on
the authorize-URL params and on how they hand back the user's email, so
we describe each as a `OAuthProvider` dataclass and share everything else.

The route module ([routes/oauth.py](src/trusted_router/routes/oauth.py)) loops
over `OAUTH_PROVIDERS` and registers one `/auth/{slug}/login` +
`/{slug}_oauth_callback` pair per provider. Adding a third provider is
one new singleton in this file plus its `fetch_user` async function.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import urlencode

import httpx


@dataclass(frozen=True)
class OAuthUserInfo:
    """Provider-agnostic shape returned to the route after sign-in."""

    sub: str  # provider-side user id
    email: str
    email_verified: bool
    display_name: str | None = None


@dataclass(frozen=True)
class OAuthProvider:
    """Static description of an OAuth identity provider.

    `fetch_user` does the userinfo dance — Google's is one /userinfo
    call, GitHub needs /user + /user/emails — so we keep that as the
    only thing each provider has to implement.
    """

    slug: str  # "google", "github"
    authorize_url: str
    token_url: str
    fetch_user: Callable[[str, httpx.AsyncClient], Awaitable[OAuthUserInfo]] = field(repr=False)
    authorize_params: dict[str, str] = field(default_factory=dict)

    def authorize_redirect(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        state: str,
    ) -> str:
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            **self.authorize_params,
        }
        return f"{self.authorize_url}?{urlencode(params)}"


async def exchange_code(
    *,
    provider: OAuthProvider,
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    client: httpx.AsyncClient | None = None,
) -> str:
    """Exchange the authorization code for a bearer access token.

    Both Google and GitHub accept the same form-encoded body; GitHub
    differs only in needing `Accept: application/json` so the response
    isn't form-encoded. We send that header for both — it's a no-op
    for Google."""
    payload = {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=10.0)
    try:
        response = await client.post(
            provider.token_url,
            data=payload,
            headers={"accept": "application/json"},
        )
        response.raise_for_status()
        data = response.json()
    finally:
        if owns_client:
            await client.aclose()
    access_token = data.get("access_token")
    if not isinstance(access_token, str):
        raise RuntimeError(f"{provider.slug} token endpoint returned no access_token")
    return access_token


async def fetch_user(
    *,
    provider: OAuthProvider,
    access_token: str,
    client: httpx.AsyncClient | None = None,
) -> OAuthUserInfo:
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=10.0)
    try:
        return await provider.fetch_user(access_token, client)
    finally:
        if owns_client:
            await client.aclose()


# ── Google ──────────────────────────────────────────────────────────────────

_GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


async def _fetch_google_user(access_token: str, client: httpx.AsyncClient) -> OAuthUserInfo:
    response = await client.get(
        _GOOGLE_USERINFO_URL,
        headers={"authorization": f"Bearer {access_token}"},
    )
    response.raise_for_status()
    data = response.json()
    sub = data.get("sub")
    email = data.get("email")
    if not isinstance(sub, str) or not isinstance(email, str):
        raise RuntimeError("google userinfo response missing sub or email")
    return OAuthUserInfo(
        sub=sub,
        email=email,
        email_verified=bool(data.get("email_verified")),
        display_name=data.get("name") if isinstance(data.get("name"), str) else None,
    )


GOOGLE = OAuthProvider(
    slug="google",
    authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
    token_url="https://oauth2.googleapis.com/token",  # noqa: S106 - URL, not a secret
    authorize_params={
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "online",
        "prompt": "select_account",
    },
    fetch_user=_fetch_google_user,
)


# ── GitHub ──────────────────────────────────────────────────────────────────

_GITHUB_USER_URL = "https://api.github.com/user"
_GITHUB_EMAILS_URL = "https://api.github.com/user/emails"


def _pick_github_primary_verified(
    emails: list[dict[str, object]],
    *,
    fallback: object,
) -> tuple[str | None, bool]:
    """GitHub may return [] for /user/emails if the token lacks scope, or
    a list with one verified-primary entry. Prefer the verified primary;
    fall back to any verified email; finally, return whatever /user
    surfaced as `email` and let the caller's verified-only gate reject."""
    for entry in emails:
        if entry.get("primary") and entry.get("verified") and isinstance(entry.get("email"), str):
            return str(entry["email"]), True
    for entry in emails:
        if entry.get("verified") and isinstance(entry.get("email"), str):
            return str(entry["email"]), True
    if isinstance(fallback, str) and fallback:
        return fallback, False
    return None, False


async def _fetch_github_user(access_token: str, client: httpx.AsyncClient) -> OAuthUserInfo:
    headers = {
        "authorization": f"Bearer {access_token}",
        "accept": "application/vnd.github+json",
        "user-agent": "TrustedRouter",
    }
    user_resp = await client.get(_GITHUB_USER_URL, headers=headers)
    user_resp.raise_for_status()
    user = user_resp.json()
    emails_resp = await client.get(_GITHUB_EMAILS_URL, headers=headers)
    emails = emails_resp.json() if emails_resp.status_code == 200 else []
    primary_email, verified = _pick_github_primary_verified(emails, fallback=user.get("email"))
    if not primary_email:
        raise RuntimeError("github did not return a verified primary email")
    user_id = user.get("id")
    login = user.get("login")
    if not isinstance(user_id, int) or not isinstance(login, str):
        raise RuntimeError("github user response missing id or login")
    return OAuthUserInfo(
        sub=str(user_id),
        email=primary_email,
        email_verified=verified,
        display_name=user.get("name") if isinstance(user.get("name"), str) else login,
    )


GITHUB = OAuthProvider(
    slug="github",
    authorize_url="https://github.com/login/oauth/authorize",
    token_url="https://github.com/login/oauth/access_token",  # noqa: S106 - URL, not a secret
    authorize_params={"scope": "user:email"},
    fetch_user=_fetch_github_user,
)


OAUTH_PROVIDERS: dict[str, OAuthProvider] = {GOOGLE.slug: GOOGLE, GITHUB.slug: GITHUB}
