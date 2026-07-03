"""POST /internal/chat/issue-browser-key

Issues a scoped API key for the public chat playground at /chat to use
from the browser. Auto-created on first signed-in Send.

Why a dedicated endpoint instead of asking the user to copy a key
manually:

  * Cross-origin: /chat is on trustedrouter.com; /v1/chat/completions
    is on api.trustedrouter.com (different registrable domain).
    `tr_session` cookie is host-only on trustedrouter.com so cross-
    origin session auth is impossible. Browser MUST send
    Authorization: Bearer sk-tr-… .
  * Scoped: we don't want the chat page to use the user's management
    key (way too broad). The chat-browser key is bounded:
      - `limit_microdollars = 5_000_000` ($5/day) — if the key leaks
        via XSS, devtools sharing, or browser-storage exfil, the blast
        radius is bounded to $5/day until the next rotation.
      - `expires_at = NOW + 30d` — auto-rotates; long-term exposure
        bounded.

The endpoint is session-gated by `require_console_context` (same
session cookie that backs /console/*). Returns 302 → /?reason=signin
when unauth (the existing console-shaped gate; the chat JS catches
the redirect and pops the sign-in modal).

Each call creates a NEW key — we do not cache raw keys server-side
(would violate the "we only ever store hashes" invariant). Client
preserves continuity across page refreshes and browser restarts via a
scoped localStorage key plus a one-shot `tr_chat_key` cookie that JS
reads + clears on page load. So this endpoint is only called when the
current user/workspace has no reusable browser key or when a cached key
is rejected and must rotate.
"""

from __future__ import annotations

import datetime as dt
import secrets
from typing import Any

from fastapi import APIRouter, Response

from trusted_router.auth import SettingsDep
from trusted_router.errors import assert_workspace_billing_active
from trusted_router.routes.console._shared import ConsoleDep
from trusted_router.storage import STORE

# Soft cap per browser-issued key. $5/day in microdollars. This caps the
# blast radius if the key leaks via XSS or shared DevTools session. The
# user's actual chat usage on a $5/day key is plenty for prototyping but
# bounded enough that an exfiltrated key isn't a financial disaster.
CHAT_BROWSER_KEY_LIMIT_MICRODOLLARS = 5_000_000

# 30 days. The chat client sees this via browser storage; when it expires
# the next Send pops a fresh `/internal/chat/issue-browser-key` call.
CHAT_BROWSER_KEY_TTL_DAYS = 30

# Short-lived cookie that hands the raw key from server → JS. HttpOnly
# would defeat the point (JS needs to read it). Mitigations:
#   * Path is /chat — cookie is not sent on any other request
#   * SameSite=Lax + Secure-in-prod
#   * 24h max-age — covers the gap between cookie set and JS reading
#     it (in practice, milliseconds), but bounded if the user closes
#     the tab and reopens later
#   * Client clears the cookie immediately after reading + copies the
#     value into scoped browser storage
CHAT_BROWSER_KEY_COOKIE_NAME = "tr_chat_key"
CHAT_BROWSER_KEY_COOKIE_MAX_AGE = 60 * 60 * 24  # 24h


def register(router: APIRouter) -> None:
    @router.post("/internal/chat/issue-browser-key")
    async def issue_chat_browser_key(
        response: Response,
        ctx: ConsoleDep,
        settings: SettingsDep,
    ) -> dict[str, Any]:
        # Naming convention: chat-browser-YYYYMMDD-{6hex}. The date
        # prefix makes it obvious in /console/api-keys that this is an
        # auto-issued chat key from a specific day; the hex suffix
        # disambiguates multiple keys created the same day across
        # tab-close/reopen cycles. Listing the workspace's keys in
        # creation-time order shows them as the chat user's "session
        # log."
        now = dt.datetime.now(dt.UTC)
        name = f"chat-browser-{now.strftime('%Y%m%d')}-{secrets.token_hex(3)}"
        expires_at = (now + dt.timedelta(days=CHAT_BROWSER_KEY_TTL_DAYS)).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z")

        assert_workspace_billing_active(ctx.workspace)  # quiesce: no new keys while paused
        raw_key, api_key = STORE.create_api_key(
            workspace_id=ctx.workspace.id,
            name=name,
            creator_user_id=ctx.user.id,
            management=False,  # never grant management on a browser key
            limit_microdollars=CHAT_BROWSER_KEY_LIMIT_MICRODOLLARS,
            expires_at=expires_at,
            include_byok_in_limit=True,
        )

        # Set the one-shot cookie so the chat client's page-load
        # bootstrap can pick up the key without an extra request. JS
        # reads it, copies to sessionStorage, then clears the cookie.
        secure = settings.environment.lower() == "production"
        response.set_cookie(
            key=CHAT_BROWSER_KEY_COOKIE_NAME,
            value=raw_key,
            max_age=CHAT_BROWSER_KEY_COOKIE_MAX_AGE,
            httponly=False,  # JS must read this — that's the point
            secure=secure,
            samesite="lax",
            path="/chat",  # narrow path → cookie not sent elsewhere
        )

        return {
            "data": {
                "raw_key": raw_key,
                "key_hash": api_key.hash,
                "name": name,
                "expires_at": expires_at,
                "limit_microdollars": CHAT_BROWSER_KEY_LIMIT_MICRODOLLARS,
            }
        }
