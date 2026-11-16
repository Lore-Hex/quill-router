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
    in the last 24 hours, every other chat key of theirs is frozen (disabled
    and capped at what it has spent and holds) so at most one can take new
    holds, and their keys expire after 24 hours.
    The cap covers chat keys issued by this code. A member's chat keys issued
    before it (each a lifetime $5, 30-day key, any number of them) are not
    indexed, so they are neither counted nor frozen; they stay usable up to
    their own limits until they expire, at most 30 days after issue.

Every chat key is indexed under its user and workspace, so issuance reads
only that user's chat keys, never the rest of the workspace. For everyone,
issuances are capped at CHAT_BROWSER_KEY_ISSUES_PER_DAY per user per
workspace per UTC day, and the POST must carry this site's Origin, like the
other sensitive console POSTs, so another site cannot mint keys with a
signed-in visitor's cookie.

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

# Every chat-browser key is named chat-browser-YYYYMMDD-{6hex}.
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
# depth; the spend allowance above is what bounds money. A slot is taken just
# before a key is created, so refusals before that (cross-origin, the member's
# allowance used up, this cap) are free. The two refusals after creation do
# use a slot: 409 when a concurrent issuance for the same user froze the new
# key, and 429 when the recount after a concurrent issuance leaves nothing.
# Both need two issuances for one user at the same moment; the counter has no
# decrement, so giving the slot back would not be race-safe. The client
# reuses a key across reloads and needs a
# new one only when the cached key is missing, expired or rejected, but a
# member using two browsers re-issues on every switch (each issuance freezes
# the other browser's key, whose next request gets a 401). Thirty a day is a
# switch every half hour through a long working day; a script churning keys
# is stopped within seconds.
CHAT_BROWSER_KEY_ISSUES_PER_DAY = 30
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
        if ctx.can_manage:
            _enforce_issue_rate_limit(ctx.workspace.id, ctx.user.id)
            raw_key, api_key = STORE.create_api_key(
                workspace_id=ctx.workspace.id,
                name=name,
                creator_user_id=ctx.user.id,
                management=False,  # never grant management on a browser key
                limit_microdollars=CHAT_BROWSER_KEY_LIMIT_MICRODOLLARS,
                expires_at=_iso(now + dt.timedelta(days=CHAT_BROWSER_KEY_TTL_DAYS)),
                include_byok_in_limit=True,
            )
            # Indexed too: if this user is later made a plain member, their
            # live owner-era chat keys are counted and frozen like any other.
            STORE.record_chat_browser_key(
                ctx.workspace.id, ctx.user.id, api_key.hash, expires_at=api_key.expires_at,
            )
            # Keys that expired before the member window began can no longer
            # count; drop their rows (read from the index alone).
            STORE.forget_expired_chat_browser_keys(
                ctx.workspace.id,
                ctx.user.id,
                expired_before=_iso(now - CHAT_BROWSER_MEMBER_WINDOW),
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

    Four facts carry the bound:

    1. The new key's lifetime limit is the allowance left after the lifetime
       spend (used plus reserved, BYOK included) of each of the member's chat
       keys that may have spent in the last 24 hours: one not expired and not
       disabled before the window began. A key's whole lifetime spend counts,
       because the counters do not say when it was spent; that over-counts,
       never under-counts.
    2. Every other chat key of theirs that could still take a hold is frozen:
       disabled, and its lifetime limit cut to what it has used and holds. The
       cut is a key-limit write (update_key with limit_microdollars), which
       every backend serializes with the reserve that checks that limit
       (Spanner: the typed transaction over tr_key_limit; Postgres: the
       locked tr_key_limit row; memory: the store lock). A request that read
       the key before it was disabled and reserves afterwards is refused by
       the cut. The cut uses the spend read earlier; a hold placed in between
       is still in the store's counters, so the cut leaves no headroom and
       the recount below sees that hold.
    3. The member's index is read again after the new key is recorded, so a
       key a concurrent request created is found, frozen and counted. Of two
       concurrent issuances, at least one records its key before the other
       reads the index.
    4. Whenever anything was frozen or found, the allowance is counted again
       from a fresh read. There every other key that has not expired counts
       at the larger of its spend and its lifetime limit: a frozen key's
       limit is the spend read before the freeze, and if one of its holds
       settles below its estimate it gets that headroom back, usable by a
       request that read it before the freeze. The new key's limit only goes
       down, by a conditional lower that never raises a lower limit set
       meanwhile. Every freeze is the same conditional lower. If a concurrent
       issuance froze this request's key, it answers 409.

    For any 24-hour window, the keys that spent in it were each able to take
    holds in it and were frozen one by one, so all of them could take holds
    within 24 hours before the last of them was issued; that issuance counted
    their spend. Each key's limit is enforced at authorize, like every
    lifetime key limit.
    """
    window_start = now - CHAT_BROWSER_MEMBER_WINDOW
    before = STORE.list_chat_browser_keys(workspace_id, user_id)
    allowance = CHAT_BROWSER_MEMBER_ALLOWANCE_MICRODOLLARS - _spent_since(before, window_start)
    if allowance <= 0:
        _forget_finished(workspace_id, user_id, before, window_start)
        raise _allowance_used_up()
    _enforce_issue_rate_limit(workspace_id, user_id)
    raw_key, api_key = STORE.create_api_key(
        workspace_id=workspace_id,
        name=name,
        creator_user_id=user_id,
        management=False,  # never grant management on a browser key
        limit_microdollars=allowance,
        expires_at=_iso(now + CHAT_BROWSER_MEMBER_KEY_TTL),
        include_byok_in_limit=True,
    )
    STORE.record_chat_browser_key(
        workspace_id, user_id, api_key.hash, expires_at=api_key.expires_at,
    )

    known = {snapshot.api_key.hash for snapshot in before}
    found = [
        key_hash
        for key_hash in STORE.chat_browser_key_hashes(workspace_id, user_id)
        if key_hash not in known and key_hash != api_key.hash
    ]
    found_keys = (
        STORE.list_chat_browser_keys(workspace_id, user_id, key_hashes=found) if found else []
    )
    dangling = set(found) - {snapshot.api_key.hash for snapshot in found_keys}
    if dangling:  # index rows of deleted keys
        STORE.forget_chat_browser_keys(workspace_id, user_id, sorted(dangling))
    others = before + found_keys
    to_freeze = [snapshot for snapshot in others if _can_take_holds(snapshot, now)]
    for snapshot in to_freeze:
        _freeze(snapshot)
    if not (to_freeze or found):
        # No other key of theirs could take a hold and none appeared, so
        # their spend cannot have moved since the first count.
        _forget_finished(workspace_id, user_id, before, window_start)
        return raw_key, api_key

    after = STORE.list_chat_browser_keys(workspace_id, user_id)
    _forget_finished(workspace_id, user_id, after, window_start)
    own = next((s for s in after if s.api_key.hash == api_key.hash), None)
    if own is None or own.api_key.disabled:
        raise HTTPException(409, "Another chat key was issued at the same moment. Please retry.")
    recount = CHAT_BROWSER_MEMBER_ALLOWANCE_MICRODOLLARS - _committed_since(
        [s for s in after if s.api_key.hash != api_key.hash], window_start, now
    )
    if recount <= 0:
        _freeze(own)
        raise _allowance_used_up()
    if recount < allowance:
        lowered = STORE.update_key(api_key.hash, {"limit_microdollars_at_most": recount})
        if lowered is None or lowered.disabled:
            # A concurrent issuance froze this key between the recount and
            # the lower; the lower kept that freeze.
            raise HTTPException(409, "Another chat key was issued at the same moment. Please retry.")
        api_key = lowered
    return raw_key, api_key


def _consumed(snapshot: ApiKeyUsageSnapshot) -> int:
    """What the key's lifetime limit is measured against."""
    byok = snapshot.byok_usage_microdollars if snapshot.api_key.include_byok_in_limit else 0
    return snapshot.usage_microdollars + byok + snapshot.reserved_microdollars


def _spent_since(snapshots: list[ApiKeyUsageSnapshot], window_start: dt.datetime) -> int:
    return sum(
        snapshot.usage_microdollars
        + snapshot.byok_usage_microdollars
        + snapshot.reserved_microdollars
        for snapshot in snapshots
        if _may_have_spent_since(snapshot.api_key, window_start)
    )


def _committed_since(
    snapshots: list[ApiKeyUsageSnapshot], window_start: dt.datetime, now: dt.datetime,
) -> int:
    """For the recount, after the freezes: what each key may have spent in the
    window or can still spend. A key that has not expired can reach its
    lifetime limit (a frozen one through a request that read it before the
    freeze once a hold settles below its estimate, an unfrozen one at its cap
    the same way), so it counts at the larger of its spend and its limit. A
    key with no limit left after the freezes counts as the whole allowance."""
    total = 0
    for snapshot in snapshots:
        key = snapshot.api_key
        if not _may_have_spent_since(key, window_start):
            continue
        spent = (
            snapshot.usage_microdollars
            + snapshot.byok_usage_microdollars
            + snapshot.reserved_microdollars
        )
        if not _expired(key, now):
            if key.limit_microdollars is None:
                spent = max(spent, CHAT_BROWSER_MEMBER_ALLOWANCE_MICRODOLLARS)
            else:
                spent = max(spent, key.limit_microdollars)
        total += spent
    return total


def _can_take_holds(snapshot: ApiKeyUsageSnapshot, now: dt.datetime) -> bool:
    """Not expired and with headroom under its lifetime limit, disabled or
    not: a key disabled any other way (an owner in the console) is a
    metadata write, which a request that already read the key can outrun."""
    key = snapshot.api_key
    if _expired(key, now):
        return False
    return key.limit_microdollars is None or key.limit_microdollars > _consumed(snapshot)


def _freeze(snapshot: ApiKeyUsageSnapshot) -> None:
    # A conditional lower: two requests freezing the same key from reads
    # taken at different moments leave the lower cut in place.
    STORE.update_key(
        snapshot.api_key.hash,
        {"disabled": True, "limit_microdollars_at_most": _consumed(snapshot)},
    )


def _forget_finished(
    workspace_id: str,
    user_id: str,
    snapshots: list[ApiKeyUsageSnapshot],
    window_start: dt.datetime,
) -> None:
    """Remove the index rows of keys whose spend can no longer count, so the
    member's index stays as small as their recent keys."""
    finished = [
        snapshot.api_key.hash
        for snapshot in snapshots
        if not _may_have_spent_since(snapshot.api_key, window_start)
    ]
    if finished:
        STORE.forget_chat_browser_keys(workspace_id, user_id, finished)


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
