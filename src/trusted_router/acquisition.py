"""First-party, privacy-bounded acquisition attribution.

This module never receives prompt or output content. It only handles public
landing metadata, account/workspace conversion milestones, and integer money.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import logging
import re
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from fastapi import Request, Response

from trusted_router.config import Settings
from trusted_router.storage import STORE
from trusted_router.storage_models import AcquisitionAttribution, iso_now

log = logging.getLogger(__name__)

ATTRIBUTION_COOKIE_NAME = "tr_attribution"
ATTRIBUTION_COOKIE_MAX_AGE = 60 * 60 * 24 * 90
RETENTION_MILESTONE_SECONDS = 60 * 60 * 24 * 7
_COOKIE_VERSION = 1
_MAX_COOKIE_BYTES = 3_800
_ANONYMOUS_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_CLICK_ID_RE = re.compile(r"^[A-Za-z0-9._~-]{1,256}$")
_TOUCH_FIELDS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "gclid",
        "gbraid",
        "wbraid",
        "twclid",
        "landing_path",
        "referer_host",
        "captured_at",
    }
)
_USAGE_CHECK_CACHE_MAX = 50_000
_usage_check_after: OrderedDict[str, float] = OrderedDict()
_usage_check_lock = threading.Lock()


@dataclass(frozen=True)
class AttributionContext:
    anonymous_id: str
    first_touch: dict[str, str]
    last_touch: dict[str, str]
    created_at: str


def prepare_request_attribution(
    request: Request,
    settings: Settings,
) -> tuple[AttributionContext | None, bool]:
    """Decode the current cookie and optionally capture a new public touch."""
    if _privacy_signal_enabled(request):
        request.state.acquisition_attribution = None
        return None, False
    context = decode_attribution_cookie(
        request.cookies.get(ATTRIBUTION_COOKIE_NAME),
        settings,
    )
    request.state.acquisition_attribution = context
    if not _should_capture_request(request):
        return context, False

    touch = _touch_from_request(request)
    now = iso_now()
    if context is None:
        context = AttributionContext(
            anonymous_id=uuid.uuid4().hex,
            first_touch=touch,
            last_touch=touch,
            created_at=now,
        )
        changed = True
    elif _has_explicit_campaign_touch(request):
        context = AttributionContext(
            anonymous_id=context.anonymous_id,
            first_touch=context.first_touch,
            last_touch=touch,
            created_at=context.created_at,
        )
        changed = touch != request.state.acquisition_attribution.last_touch
    else:
        changed = False
    request.state.acquisition_attribution = context
    return context, changed


def set_attribution_cookie(
    response: Response,
    context: AttributionContext,
    settings: Settings,
) -> None:
    response.set_cookie(
        key=ATTRIBUTION_COOKIE_NAME,
        value=encode_attribution_cookie(context, settings),
        max_age=ATTRIBUTION_COOKIE_MAX_AGE,
        httponly=True,
        secure=settings.environment.lower() == "production",
        samesite="lax",
        path="/",
    )


def encode_attribution_cookie(context: AttributionContext, settings: Settings) -> str:
    payload = {
        "v": _COOKIE_VERSION,
        "anonymous_id": context.anonymous_id,
        "first_touch": context.first_touch,
        "last_touch": context.last_touch,
        "created_at": context.created_at,
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).rstrip(b"=")
    signature = hmac.new(_cookie_signing_key(settings), encoded, hashlib.sha256).digest()
    return f"{encoded.decode('ascii')}.{_b64encode(signature)}"


def decode_attribution_cookie(
    value: str | None,
    settings: Settings,
) -> AttributionContext | None:
    if not value or len(value.encode("utf-8")) > _MAX_COOKIE_BYTES:
        return None
    try:
        encoded, supplied_signature = value.split(".", 1)
        expected_signature = hmac.new(
            _cookie_signing_key(settings),
            encoded.encode("ascii"),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(_b64decode(supplied_signature), expected_signature):
            return None
        payload = json.loads(_b64decode(encoded))
        if payload.get("v") != _COOKIE_VERSION:
            return None
        anonymous_id = str(payload.get("anonymous_id") or "")
        created_at = str(payload.get("created_at") or "")
        if not _ANONYMOUS_ID_RE.fullmatch(anonymous_id) or _cookie_expired(created_at):
            return None
        first_touch = _validated_touch(payload.get("first_touch"))
        last_touch = _validated_touch(payload.get("last_touch"))
        if not first_touch or not last_touch:
            return None
        return AttributionContext(
            anonymous_id=anonymous_id,
            first_touch=first_touch,
            last_touch=last_touch,
            created_at=created_at,
        )
    except (UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError):
        return None


def request_attribution(request: Request) -> AttributionContext | None:
    value = getattr(request.state, "acquisition_attribution", None)
    return value if isinstance(value, AttributionContext) else None


def record_signup_attribution(
    request: Request,
    *,
    workspace_id: str,
    signup_provider: str,
) -> None:
    context = request_attribution(request) or _direct_context(request)
    occurred_at = iso_now()
    record = AcquisitionAttribution(
        workspace_id=workspace_id,
        anonymous_id=context.anonymous_id,
        first_touch=dict(context.first_touch),
        last_touch=dict(context.last_touch),
        signup_provider=signup_provider,
        signup_at=occurred_at,
        milestones={
            "signup_completed": occurred_at,
            "api_key_created": occurred_at,
        },
    )
    try:
        created = STORE.create_acquisition_attribution(record)
    except Exception as exc:  # noqa: BLE001 - analytics must never block signup.
        log.warning(
            "acquisition.signup_write_failed",
            extra={
                "workspace_fingerprint": _fingerprint(workspace_id),
                "error": type(exc).__name__,
            },
        )
        return
    if not created:
        return
    _clear_usage_check(workspace_id)
    _log_conversion("acquisition.signup_completed", record)
    _log_conversion("acquisition.api_key_created", record)


def record_successful_api_call(
    workspace_id: str,
    *,
    model: str,
    provider: str,
    occurred_at: str | None = None,
) -> AcquisitionAttribution | None:
    """Claim first-use and seven-day retained-use milestones atomically."""
    occurred_at = occurred_at or iso_now()
    record = STORE.get_acquisition_attribution(workspace_id)
    if record is None:
        return None
    milestones = ["first_successful_api_call"]
    if _age_seconds(record.signup_at, occurred_at) >= RETENTION_MILESTONE_SECONDS:
        milestones.append("retained_api_usage_7d")
    record, claimed = STORE.claim_acquisition_milestones(
        workspace_id,
        milestones,
        occurred_at=occurred_at,
    )
    if record is None:
        return None
    extra: dict[str, object] = {"model": model[:200], "provider": provider[:100]}
    for milestone in claimed:
        _log_conversion(f"acquisition.{milestone}", record, extra=extra)
    return record


def record_successful_api_call_safely(
    workspace_id: str,
    *,
    model: str,
    provider: str,
) -> None:
    if not _usage_check_due(workspace_id):
        return
    try:
        record = record_successful_api_call(
            workspace_id,
            model=model,
            provider=provider,
        )
    except Exception as exc:  # noqa: BLE001 - analytics must never affect inference.
        log.warning(
            "acquisition.milestone_write_failed",
            extra={
                "workspace_fingerprint": _fingerprint(workspace_id),
                "error": type(exc).__name__,
            },
        )
        _defer_usage_check(workspace_id, 60)
        return
    if record is None:
        _defer_usage_check(workspace_id, 60 * 60)
        return
    if "retained_api_usage_7d" in record.milestones:
        _defer_usage_check(workspace_id, 60 * 60 * 24 * 7)
        return
    remaining = RETENTION_MILESTONE_SECONDS - _age_seconds(record.signup_at, iso_now())
    _defer_usage_check(workspace_id, max(60, int(remaining)))


def record_credit_purchase(
    workspace_id: str,
    *,
    amount_microdollars: int,
    payment_method: str,
) -> None:
    """Record a credited purchase. Call only after payment-ledger dedupe wins."""
    occurred_at = iso_now()
    try:
        record = STORE.record_acquisition_purchase(
            workspace_id,
            amount_microdollars=amount_microdollars,
            occurred_at=occurred_at,
        )
    except Exception as exc:  # noqa: BLE001 - payment already committed; never fail webhook.
        log.warning(
            "acquisition.purchase_write_failed",
            extra={
                "workspace_fingerprint": _fingerprint(workspace_id),
                "error": type(exc).__name__,
            },
        )
        return
    if record is None:
        return
    _log_conversion(
        "acquisition.credit_purchase_completed",
        record,
        extra={
            "amount_microdollars": amount_microdollars,
            "payment_method": payment_method[:32],
            "purchase_number": record.purchase_count,
        },
    )


def log_browser_funnel_event(request: Request, event: str) -> None:
    context = request_attribution(request)
    if context is None:
        return
    touch = context.last_touch
    log.info(
        f"acquisition.{event}",
        extra={
            "event": f"acquisition.{event}",
            "anonymous_fingerprint": _fingerprint(context.anonymous_id),
            **_safe_touch_log_fields(touch),
        },
    )


def pageview_attribution_fields(request: Request) -> dict[str, object]:
    context = request_attribution(request)
    if context is None:
        return {}
    return {
        "anonymous_fingerprint": _fingerprint(context.anonymous_id),
        **_safe_touch_log_fields(context.last_touch),
    }


def _log_conversion(
    event: str,
    record: AcquisitionAttribution,
    *,
    extra: dict[str, object] | None = None,
) -> None:
    fields: dict[str, object] = {
        "event": event,
        "workspace_fingerprint": _fingerprint(record.workspace_id),
        "anonymous_fingerprint": _fingerprint(record.anonymous_id),
        "signup_provider": record.signup_provider,
        **_safe_touch_log_fields(record.last_touch),
        "first_utm_source": record.first_touch.get("utm_source"),
        "first_utm_medium": record.first_touch.get("utm_medium"),
        "first_utm_campaign": record.first_touch.get("utm_campaign"),
        "first_landing_path": record.first_touch.get("landing_path"),
    }
    if extra:
        fields.update(extra)
    log.info(event, extra=fields)


def _safe_touch_log_fields(touch: dict[str, str]) -> dict[str, object]:
    return {
        "utm_source": touch.get("utm_source"),
        "utm_medium": touch.get("utm_medium"),
        "utm_campaign": touch.get("utm_campaign"),
        "utm_term": touch.get("utm_term"),
        "utm_content": touch.get("utm_content"),
        "landing_path": touch.get("landing_path"),
        "referer_host": touch.get("referer_host"),
        "has_gclid": bool(touch.get("gclid")),
        "has_gbraid": bool(touch.get("gbraid")),
        "has_wbraid": bool(touch.get("wbraid")),
        "has_twclid": bool(touch.get("twclid")),
    }


def _touch_from_request(request: Request) -> dict[str, str]:
    touch: dict[str, str] = {}
    for name in ("utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content"):
        value = _safe_text(request.query_params.get(name), 128)
        if value:
            touch[name] = value
    for name in ("gclid", "gbraid", "wbraid", "twclid"):
        value = str(request.query_params.get(name) or "").strip()
        if _CLICK_ID_RE.fullmatch(value):
            touch[name] = value
    if "gclid" in touch or "gbraid" in touch or "wbraid" in touch:
        touch.setdefault("utm_source", "google")
        touch.setdefault("utm_medium", "paid_search")
    if "twclid" in touch:
        touch.setdefault("utm_source", "x")
        touch.setdefault("utm_medium", "paid_social")
    touch.setdefault("utm_source", "direct")
    touch.setdefault("utm_medium", "none")
    touch["landing_path"] = request.url.path[:256]
    referer_host = _external_referer_host(request)
    if referer_host:
        touch["referer_host"] = referer_host
        if touch["utm_source"] == "direct":
            touch["utm_source"] = referer_host
            touch["utm_medium"] = "referral"
    touch["captured_at"] = iso_now()
    return touch


def _validated_touch(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    touch: dict[str, str] = {}
    for key, raw_value in value.items():
        if key not in _TOUCH_FIELDS or not isinstance(raw_value, str):
            continue
        if key in {"gclid", "gbraid", "wbraid", "twclid"}:
            if _CLICK_ID_RE.fullmatch(raw_value):
                touch[key] = raw_value
            continue
        limit = 256 if key == "landing_path" else 128
        safe = _safe_text(raw_value, limit)
        if safe:
            touch[key] = safe
    return touch


def _direct_context(request: Request) -> AttributionContext:
    now = iso_now()
    touch = {
        "utm_source": "direct",
        "utm_medium": "none",
        "landing_path": request.url.path[:256],
        "captured_at": now,
    }
    return AttributionContext(uuid.uuid4().hex, touch, touch, now)


def _should_capture_request(request: Request) -> bool:
    if request.method.upper() != "GET" or _privacy_signal_enabled(request):
        return False
    path = request.url.path
    excluded = (
        "/auth",
        "/console",
        "/internal",
        "/v1",
        "/static",
        "/health",
        "/openapi",
    )
    if path.startswith(excluded) or path.endswith("_oauth_callback"):
        return False
    user_agent = request.headers.get("user-agent", "").lower()
    return not any(token in user_agent for token in ("bot", "crawler", "spider"))


def _has_explicit_campaign_touch(request: Request) -> bool:
    return any(
        request.query_params.get(name)
        for name in (
            "utm_source",
            "utm_medium",
            "utm_campaign",
            "utm_term",
            "utm_content",
            "gclid",
            "gbraid",
            "wbraid",
            "twclid",
        )
    )


def _privacy_signal_enabled(request: Request) -> bool:
    return request.headers.get("sec-gpc") == "1" or request.headers.get("dnt") == "1"


def _external_referer_host(request: Request) -> str | None:
    raw = request.headers.get("referer", "").strip()
    if not raw:
        return None
    try:
        host = urlsplit(raw).hostname or ""
    except ValueError:
        return None
    request_host = (request.url.hostname or "").lower()
    host = host.lower()
    return host[:128] if host and host != request_host else None


def _safe_text(value: str | None, limit: int) -> str:
    if not value:
        return ""
    return "".join(character for character in value.strip() if character.isprintable())[:limit]


def _cookie_signing_key(settings: Settings) -> bytes:
    root = settings.internal_gateway_token or f"local:{settings.service_name}"
    return hmac.new(
        root.encode("utf-8"),
        b"trustedrouter-attribution-cookie-v1",
        hashlib.sha256,
    ).digest()


def _cookie_expired(created_at: str) -> bool:
    try:
        created = dt.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    if created.tzinfo is None:
        created = created.replace(tzinfo=dt.UTC)
    age = (dt.datetime.now(dt.UTC) - created).total_seconds()
    return age < 0 or age > ATTRIBUTION_COOKIE_MAX_AGE


def _age_seconds(earlier: str, later: str) -> float:
    try:
        start = dt.datetime.fromisoformat(earlier.replace("Z", "+00:00"))
        end = dt.datetime.fromisoformat(later.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if start.tzinfo is None:
        start = start.replace(tzinfo=dt.UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=dt.UTC)
    return max(0.0, (end - start).total_seconds())


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _usage_check_due(workspace_id: str) -> bool:
    now = time.monotonic()
    with _usage_check_lock:
        check_after = _usage_check_after.get(workspace_id)
        if check_after is not None and check_after > now:
            _usage_check_after.move_to_end(workspace_id)
            return False
        _usage_check_after.pop(workspace_id, None)
        return True


def _defer_usage_check(workspace_id: str, seconds: int) -> None:
    with _usage_check_lock:
        _usage_check_after[workspace_id] = time.monotonic() + max(seconds, 1)
        _usage_check_after.move_to_end(workspace_id)
        while len(_usage_check_after) > _USAGE_CHECK_CACHE_MAX:
            _usage_check_after.popitem(last=False)


def _clear_usage_check(workspace_id: str) -> None:
    with _usage_check_lock:
        _usage_check_after.pop(workspace_id, None)
