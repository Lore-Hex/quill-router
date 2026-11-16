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
    key (way too broad). Every chat-browser key is non-management and
    carries a lifetime spend limit (`limit_microdollars`), never a daily
    one, so a key that leaks via XSS, devtools sharing, or browser-storage
    exfil can spend at most what is left of that limit.

The rule, by role in the selected workspace:

  * Owners and admins (who can create ordinary API keys anyway) get a key
    with a lifetime $5 limit that expires after 30 days. Each call mints
    another; older keys stay live.
  * Everyone else can spend at most $5 per rolling 24 hours, in total,
    through chat-browser keys in the workspace, however often they reissue.
    See `_issue_member_key`: a new key's lifetime limit is whatever is left
    of the $5 after everything the member's other chat keys may have spent
    in the last 24 hours, at most one of their chat keys is live at a time,
    and their keys expire after 24 hours.

For everyone, issuance is also capped at CHAT_BROWSER_KEY_ISSUES_PER_DAY per
user per workspace per UTC day, and the POST must carry this site's Origin,
like the other sensitive console POSTs, so another site cannot mint keys
with a signed-in visitor's cookie.

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
import logging
import secrets
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from trusted_router.auth import SettingsDep
from trusted_router.errors import assert_workspace_billing_active
from trusted_router.request_limits import fingerprint_subject, rate_limit_headers
from trusted_router.routes.console._shared import ConsoleDep
from trusted_router.routes.console.social_rewards import _same_origin
from trusted_router.storage import STORE, ApiKey
from trusted_router.storage_models import ApiKeyUsageSnapshot

log = logging.getLogger(__name__)

# Every chat-browser key is named chat-browser-YYYYMMDD-{6hex}; the member
# allowance finds a member's chat keys by this prefix and their creator.
CHAT_BROWSER_KEY_NAME_PREFIX = "chat-browser-"

# Lifetime spend limit of an owner's or admin's chat key: $5 in
# microdollars, the key's total limit, not a daily one. Plenty for
# prototyping in the chat playground but bounded enough that an
# exfiltrated key isn't a financial disaster.
CHAT_BROWSER_KEY_LIMIT_MICRODOLLARS = 5_000_000

# What a member may spend through all their chat keys in a workspace in any
# rolling 24 hours, and how long a member's chat key lives. The short life
# bounds how long a key's spend keeps counting: its whole lifetime spend
# counts while it may have spent within the window, so a member's spend
# stops counting at most 48 hours after it happened (24 hours after the key
# was superseded or expired).
CHAT_BROWSER_MEMBER_ALLOWANCE_MICRODOLLARS = 5_000_000
CHAT_BROWSER_MEMBER_WINDOW = dt.timedelta(hours=24)
CHAT_BROWSER_MEMBER_KEY_TTL = dt.timedelta(hours=24)

# Issuance cap per (workspace, user) per UTC day, for everyone, as defence in
# depth. The client reuses a key across reloads, so a user needs a new one
# only when the cached key is missing, expired or rejected.
CHAT_BROWSER_KEY_ISSUES_PER_DAY = 5
CHAT_BROWSER_KEY_ISSUE_WINDOW_SECONDS = 24 * 60 * 60
CHAT_BROWSER_KEY_RATE_LIMIT_NAMESPACE = "chat_browser_key_issue_v1"

# Owners' and admins' chat keys expire after 30 days. The chat client sees
# this via browser storage; when a key expires the next Send gets a 401 and
# pops a fresh `/internal/chat/issue-browser-key` call.
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

_ALLOWANCE_USED_UP = (
    "You have used this workspace's $5 chat allowance for the last 24 hours. "
    "Try again later, or ask a workspace owner or admin for an API key."
)


def register(router: APIRouter) -> None:
    @router.post("/internal/chat/issue-browser-key")
    def issue_chat_browser_key(
        request: Request,
        response: Response,
        ctx: ConsoleDep,
        settings: SettingsDep,
    ) -> dict[str, Any]:
        _same_origin(request, settings)
        # Naming convention: chat-browser-YYYYMMDD-{6hex}. The date
        # prefix makes it obvious in /console/api-keys that this is an
        # auto-issued chat key from a specific day; the hex suffix
        # disambiguates multiple keys created the same day across
        # tab-close/reopen cycles. Listing the workspace's keys in
        # creation-time order shows them as the chat user's "session
        # log."
        now = _utcnow()
        name = f"{CHAT_BROWSER_KEY_NAME_PREFIX}{now.strftime('%Y%m%d')}-{secrets.token_hex(3)}"

        assert_workspace_billing_active(ctx.workspace)  # quiesce: no new keys while paused
        _enforce_issue_rate_limit(ctx.workspace.id, ctx.user.id)
        if ctx.can_manage:
            raw_key, api_key = STORE.create_api_key(
                workspace_id=ctx.workspace.id,
                name=name,
                creator_user_id=ctx.user.id,
                management=False,  # never grant management on a browser key
                limit_microdollars=CHAT_BROWSER_KEY_LIMIT_MICRODOLLARS,
                expires_at=_iso(now + dt.timedelta(days=CHAT_BROWSER_KEY_TTL_DAYS)),
                include_byok_in_limit=True,
            )
        else:
            raw_key, api_key = _issue_member_key(
                ctx.workspace.id, ctx.user.id, name=name, now=now
            )

        # Set the one-shot cookie so the chat client's page-load
        # bootstrap can pick up the key without an extra request. JS
        # reads it, copies to sessionStorage, then clears the cookie.
        secure = settings.environment.lower() not in {"local", "test"}
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
                "expires_at": api_key.expires_at,
                "limit_microdollars": api_key.limit_microdollars,
            }
        }


def _issue_member_key(
    workspace_id: str, user_id: str, *, name: str, now: dt.datetime
) -> tuple[str, ApiKey]:
    """Mint a member's chat key so that, with every other chat key of theirs
    in this workspace, it cannot take their spend past $5 in any rolling 24
    hours.

    Three facts carry the bound:

    1. The new key's lifetime limit is the allowance left after the lifetime
       spend (used plus reserved, BYOK included) of each of the member's chat
       keys that may have spent in the last 24 hours: one not expired and not
       disabled before the window began. A key's whole lifetime spend counts,
       because the counters do not say when it was spent; that over-counts,
       never under-counts.
    2. Every other live chat key of the member is disabled here, so at most
       one is live, and a superseded key cannot take new spend.
    3. The allowance is counted again after (2). A key that a concurrent
       request created after the first count is disabled by then, and its
       spend is in the second count, so the new key's limit only goes down.

    For any 24-hour window, the keys that spent in it were each live in it
    and, being superseded one by one, were all live within 24 hours before
    the last of them was issued; that issuance counted their spend. Each
    key's limit is enforced at authorize, like every lifetime key limit.
    """
    allowance = _member_allowance_left(workspace_id, user_id, now=now)
    if allowance <= 0:
        raise _allowance_used_up()
    raw_key, api_key = STORE.create_api_key(
        workspace_id=workspace_id,
        name=name,
        creator_user_id=user_id,
        management=False,  # never grant management on a browser key
        limit_microdollars=allowance,
        expires_at=_iso(now + CHAT_BROWSER_MEMBER_KEY_TTL),
        include_byok_in_limit=True,
    )
    for snapshot in _member_chat_keys(workspace_id, user_id):
        key = snapshot.api_key
        if key.hash != api_key.hash and not key.disabled and not _expired(key, now):
            STORE.update_key(key.hash, {"disabled": True})
    recount = _member_allowance_left(workspace_id, user_id, now=now, excluding=api_key.hash)
    if recount <= 0:
        STORE.update_key(api_key.hash, {"disabled": True})
        raise _allowance_used_up()
    if recount < allowance:
        STORE.update_key(api_key.hash, {"limit_microdollars": recount})
    current = STORE.get_key_by_hash(api_key.hash)
    if current is None or current.disabled:
        # A concurrent issuance for the same member superseded this key.
        raise HTTPException(409, "Another chat key was issued at the same moment. Please retry.")
    return raw_key, current


def _member_chat_keys(workspace_id: str, user_id: str) -> list[ApiKeyUsageSnapshot]:
    """The member's chat keys in the workspace, disabled ones included, with
    live usage and holds. One per-workspace key listing; no scan."""
    return [
        snapshot
        for snapshot in STORE.list_api_keys_with_usage(workspace_id, include_disabled=True)
        if snapshot.api_key.creator_user_id == user_id
        and snapshot.api_key.name.startswith(CHAT_BROWSER_KEY_NAME_PREFIX)
        and not snapshot.api_key.management
    ]


def _member_allowance_left(
    workspace_id: str, user_id: str, *, now: dt.datetime, excluding: str | None = None
) -> int:
    window_start = now - CHAT_BROWSER_MEMBER_WINDOW
    spent = sum(
        snapshot.usage_microdollars
        + snapshot.byok_usage_microdollars
        + snapshot.reserved_microdollars
        for snapshot in _member_chat_keys(workspace_id, user_id)
        if snapshot.api_key.hash != excluding
        and _may_have_spent_since(snapshot.api_key, window_start)
    )
    return CHAT_BROWSER_MEMBER_ALLOWANCE_MICRODOLLARS - spent


def _may_have_spent_since(key: ApiKey, window_start: dt.datetime) -> bool:
    """False only when the key provably could not spend after window_start:
    it expired before then, or it was disabled before then. Every key update,
    disabling included, stamps updated_at, so a disabled key whose updated_at
    is older than the window was disabled before it. Unparseable times count
    as "may have spent"."""
    expires_at = _parse_instant(key.expires_at)
    if expires_at is not None and expires_at <= window_start:
        return False
    if key.disabled:
        updated_at = _parse_instant(key.updated_at)
        if updated_at is not None and updated_at <= window_start:
            return False
    return True


def _expired(key: ApiKey, now: dt.datetime) -> bool:
    expires_at = _parse_instant(key.expires_at)
    return expires_at is not None and expires_at <= now


def _allowance_used_up() -> HTTPException:
    log.info("chat_browser_key.member_allowance_used_up")
    return HTTPException(429, _ALLOWANCE_USED_UP)


def _enforce_issue_rate_limit(workspace_id: str, user_id: str) -> None:
    """Admit at most CHAT_BROWSER_KEY_ISSUES_PER_DAY keys per user per
    workspace per UTC day. The counter lives in the store, so a new session
    or another serving instance does not reset it. A storage failure refuses
    the key rather than issuing it unmetered."""
    try:
        hit = STORE.hit_rate_limit(
            namespace=CHAT_BROWSER_KEY_RATE_LIMIT_NAMESPACE,
            subject=fingerprint_subject(f"{workspace_id}:{user_id}"),
            limit=CHAT_BROWSER_KEY_ISSUES_PER_DAY,
            window_seconds=CHAT_BROWSER_KEY_ISSUE_WINDOW_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 - fail closed without exposing storage details.
        log.warning(
            "chat_browser_key.admission_unavailable",
            extra={"error_type": type(exc).__name__},
        )
        raise HTTPException(
            503,
            "Chat keys are temporarily unavailable. Please retry shortly.",
            headers={"Retry-After": "5"},
        ) from exc
    if not hit.allowed:
        log.info("chat_browser_key.issue_throttled")
        raise HTTPException(
            429,
            "Too many chat keys issued today. Reuse the key in this browser or try again tomorrow.",
            headers=rate_limit_headers(hit),
        )


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _iso(moment: dt.datetime) -> str:
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_instant(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.UTC)
