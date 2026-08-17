from __future__ import annotations

import asyncio
import datetime as dt
import html
import json
import logging
import re
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Query, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response
from starlette.types import Scope

from trusted_router.ai_iq import ai_iq_catalog_payload
from trusted_router.apps import aggregate_apps
from trusted_router.benchmark_reports import monthly_benchmark_report
from trusted_router.benchmark_samples import (
    PUBLIC_BENCHMARK_RECENT_MINUTES,
    PUBLIC_BENCHMARK_SAMPLE_LIMIT,
    public_benchmark_samples,
    public_video_benchmark_samples,
)
from trusted_router.catalog import (
    META_MODEL_IDS,
    MODELS,
    endpoints_for_model,
    provider_to_openrouter_shape,
    providers_for_display,
)
from trusted_router.choose_catalog import choose_catalog_payload
from trusted_router.client_reliability import client_observed_status_section
from trusted_router.config import Settings
from trusted_router.dashboard import (
    MODEL_SEO_SECTIONS,
    STATIC_DIR,
    canonical_model_comparison_path,
    dashboard_html,
    docs_llms_full_txt,
    docs_llms_txt,
    hipaa_readiness_json,
    llms_txt,
    procurement_json,
    public_apps_html,
    public_baa_html,
    public_benchmark_report_html,
    public_benchmark_reports_index_html,
    public_benchmarks_html,
    public_blog_index_html,
    public_blog_post_html,
    public_chat_html,
    public_competitor_compare_html,
    public_competitor_compare_index_html,
    public_dpa_html,
    public_fusion_html,
    public_hipaa_readiness_html,
    public_leaderboard_html,
    public_legal_html,
    public_model_compare_html,
    public_model_compare_index_html,
    public_model_detail_html,
    public_model_not_found_html,
    public_model_section_html,
    public_models_html,
    public_not_found_html,
    public_page_html,
    public_privacy_html,
    public_provider_detail_html,
    public_provider_performance_html,
    public_providers_html,
    public_rankings_html,
    public_sms_html,
    public_soc2_readiness_html,
    public_subprocessors_html,
    public_support_html,
    public_terms_html,
    public_video_leaderboard_html,
    robots_txt,
    sitemap_comparisons_xml,
    sitemap_core_xml,
    sitemap_models_xml,
    sitemap_providers_xml,
    sitemap_xml,
    soc2_readiness_json,
    subprocessors_json,
)
from trusted_router.domains import (
    canonical_public_url,
    control_domain_for_hostname,
    is_status_hostname,
    is_trust_hostname,
    request_api_base_url,
    request_control_domain,
    request_hostname,
    status_hostname_for_domain,
)
from trusted_router.og import OG_PNG_PATH
from trusted_router.operational_analytics_freshness import (
    ANALYTICS_STATUS_KEY,
    analytics_status_from_reading,
)
from trusted_router.provider_contract import (
    PROVIDER_CATALOG_SCHEMA,
    PROVIDER_CATALOG_V2_SCHEMA,
)
from trusted_router.public_analytics_snapshots import current_public_analytics_snapshot
from trusted_router.serialization import user_model_public_shape
from trusted_router.services.email import EmailMessage, get_email_service
from trusted_router.services.ops_chat import OpsChatSupportMessage, fanout_support_message
from trusted_router.services.trust_release import (
    ResolvedTrustRelease,
    TrustReleaseResolver,
    TrustReleaseUnavailable,
    embedded_aws_metadata,
    embedded_azure_metadata,
    unavailable_trust_release,
    validated_aws_metadata,
    validated_azure_metadata,
)
from trusted_router.storage import STORE
from trusted_router.storage_custom_models import normalize_custom_model_id
from trusted_router.storage_models import SyntheticProbeSample, SyntheticRollup, utcnow
from trusted_router.synthetic.fleet import fleet_snapshot
from trusted_router.synthetic.leaderboard import aggregate_leaderboard
from trusted_router.synthetic.status import history_payload, status_snapshot
from trusted_router.synthetic.video_leaderboard import aggregate_video_leaderboard
from trusted_router.trust import aws_release, azure_release, gcp_release, trust_html
from trusted_router.views import render_template

STATUS_SNAPSHOT_CACHE_SECONDS = 15
LEGACY_MODEL_PAGE_REDIRECTS: dict[str, str] = {
    "deepseek/deepseek-chat-v3.1": "/models/deepseek/deepseek-v3.1",
    "google/gemini-3-pro-image": "/models/google/gemini-3.1-flash-image-preview",
    # Muse Spark was removed after the only configured route failed every
    # health probe. There is no like-for-like successor, so retain the old
    # backlink by sending readers to the current open-weight catalog.
    "meta/muse-spark-1.1": "/models?filter=open",
}
STATUS_RAW_SAMPLE_LIMIT_PER_DAY = 35_000
STATUS_LIVE_SAMPLE_LIMIT = 500
STATUS_HOUR_ROLLUP_LIMIT = 5_000
STATUS_DAY_ROLLUP_LIMIT = 25_000
STATUS_MONTH_ROLLUP_LIMIT = 50
STATUS_ROLLUP_RETENTION_MONTHS = 24
STATUS_RESPONSE_CACHE_SECONDS = 60
STATUS_RESPONSE_STALE_SECONDS = 600
PUBLIC_RESPONSE_CACHE_MAX_ENTRIES = 32
PUBLIC_RESPONSE_REFRESH_MAX_THREADS = 2
STATUS_HISTORY_CACHE_SECONDS = 300
STATUS_HISTORY_STALE_SECONDS = 1_800
LEADERBOARD_SAMPLE_LIMIT = PUBLIC_BENCHMARK_SAMPLE_LIMIT
LEADERBOARD_MIN_SAMPLES = 1
LEADERBOARD_MODEL_RANK_MIN_SAMPLES = 10
LEADERBOARD_PROVIDER_RANK_MIN_SAMPLES = 30
LEADERBOARD_RANK_MIN_TTFT_SAMPLES = 3
LEADERBOARD_RECENT_WINDOW_MINUTES = PUBLIC_BENCHMARK_RECENT_MINUTES
LEADERBOARD_SNAPSHOT_CACHE_SECONDS = 300
LEADERBOARD_RESPONSE_CACHE_SECONDS = 60
LEADERBOARD_RESPONSE_STALE_SECONDS = 600
VIDEO_LEADERBOARD_SAMPLE_LIMIT = 5_000
VIDEO_LEADERBOARD_RECENT_WINDOW_MINUTES = 30 * 24 * 60
CHOOSE_PAGE_CACHE_SECONDS = 300
CHOOSE_PAGE_STALE_SECONDS = 86_400
CHOOSE_CATALOG_CACHE_SECONDS = 300
CHOOSE_CATALOG_STALE_SECONDS = 86_400
INDEXNOW_KEY = "360a02e48445d297f9612a4c3fef878b"
_STATUS_CACHE: tuple[float, dict[str, Any]] | None = None
# /fleet fans out to every peer's status.json, so its cache TTL is what keeps
# a page-refresh storm from turning into a cross-cloud fetch storm.
_FLEET_CACHE: tuple[float, dict[str, Any]] | None = None
_FLEET_CACHE_LOCK = asyncio.Lock()
FLEET_SNAPSHOT_CACHE_SECONDS = 30
_LEADERBOARD_CACHE: tuple[float, dict[str, Any]] | None = None
_VIDEO_LEADERBOARD_CACHE: tuple[float, dict[str, Any]] | None = None
_APPS_CACHE: tuple[float, dict[str, Any]] | None = None
_STATUS_RESPONSE_CACHE: OrderedDict[str, _CachedPublicBody] = OrderedDict()
_STATUS_RESPONSE_REFRESHING: set[str] = set()
_STATUS_RESPONSE_CACHE_LOCK = threading.RLock()
_STATUS_RESPONSE_REFRESH_SLOTS = threading.BoundedSemaphore(PUBLIC_RESPONSE_REFRESH_MAX_THREADS)


@dataclass(frozen=True)
class _CachedPublicBody:
    cached_at: float
    body: bytes
    media_type: str
    cache_control: str


class _CachedStaticFiles(StaticFiles):
    """StaticFiles + a public 1-day Cache-Control header.

    The default StaticFiles ships no cache directive, which means every
    visit to the marketing page re-fetches every CSS/JS/SVG asset on
    cold-load. We hash-bust nothing today, so the conservative play is
    a 24-hour public cache — long enough to take the edge off Cloud Run
    bandwidth, short enough that a deploy reaches users within a day."""

    def __init__(self, *args: Any, max_age: int = 86_400, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._max_age = max_age

    def file_response(
        self,
        full_path: Any,
        stat_result: Any,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code=status_code)
        if str(full_path).casefold().endswith(".woff2"):
            response.headers["content-type"] = "font/woff2"
        response.headers.setdefault("cache-control", f"public, max-age={self._max_age}")
        return response


log = logging.getLogger(__name__)
_leads_log = logging.getLogger("tr_leads.trustedos_inquiry")
_leads_log.propagate = False
if not _leads_log.handlers:
    _leads_handler = logging.StreamHandler()
    _leads_handler.setFormatter(logging.Formatter("%(message)s"))
    _leads_log.addHandler(_leads_handler)

# Simple in-process sliding-window limiter for the public TrustedOS inquiry
# form. Not a substitute for an edge WAF, but enough to blunt casual abuse of
# an unauthenticated POST that fans out to email. Keyed by client IP.
_INQUIRY_RATE_LOCK = threading.Lock()
_INQUIRY_MAX_CLIENTS = 4096
_INQUIRY_HITS: OrderedDict[str, list[float]] = OrderedDict()
_INQUIRY_WINDOW_SECONDS = 3600.0
_INQUIRY_MAX_PER_WINDOW = 5
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_SUPPORT_CATEGORIES = {
    "api": "API and routing",
    "account": "Account access",
    "billing": "Billing and credits",
    "provider": "Provider or model",
    "other": "Other",
}


def _inquiry_rate_ok(client_ip: str, *, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    cutoff = now - _INQUIRY_WINDOW_SECONDS
    with _INQUIRY_RATE_LOCK:
        hits = [t for t in _INQUIRY_HITS.get(client_ip, ()) if t > cutoff]
        if len(hits) >= _INQUIRY_MAX_PER_WINDOW:
            _INQUIRY_HITS[client_ip] = hits
            _INQUIRY_HITS.move_to_end(client_ip)
            _bound_inquiry_clients(cutoff)
            return False
        hits.append(now)
        _INQUIRY_HITS[client_ip] = hits
        _INQUIRY_HITS.move_to_end(client_ip)
        _bound_inquiry_clients(cutoff)
    return True


def _bound_inquiry_clients(cutoff: float) -> None:
    if len(_INQUIRY_HITS) <= _INQUIRY_MAX_CLIENTS:
        return
    stale = [key for key, hits in _INQUIRY_HITS.items() if not any(hit > cutoff for hit in hits)]
    for key in stale:
        _INQUIRY_HITS.pop(key, None)
    while len(_INQUIRY_HITS) > _INQUIRY_MAX_CLIENTS:
        _INQUIRY_HITS.popitem(last=False)


async def _handle_trustedos_inquiry(settings: Settings, request: Request) -> JSONResponse:
    """Receive a TrustedOS partner-inquiry submission and email it to the
    configured recipient. Returns an opaque {"ok": true} on accept so the
    endpoint never leaks whether email delivery or suppression happened."""
    ok = JSONResponse({"ok": True})

    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"ok": False, "error": "invalid_request"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "error": "invalid_request"}, status_code=400)

    # Honeypot: a hidden field real users never fill. Silently accept so bots
    # get a success and move on, without an email being sent.
    if str(payload.get("website", "")).strip():
        return ok

    name = str(payload.get("name", "")).strip()[:200]
    email = str(payload.get("email", "")).strip()[:320]
    company = str(payload.get("company", "")).strip()[:200]
    message = str(payload.get("message", "")).strip()[:5000]

    if not name or not message or not _EMAIL_RE.match(email):
        return JSONResponse({"ok": False, "error": "missing_fields"}, status_code=422)

    client_ip = request.client.host if request.client else "unknown"
    if not _inquiry_rate_ok(client_ip):
        return JSONResponse({"ok": False, "error": "rate_limited"}, status_code=429)

    recipient = settings.partner_inquiry_email or settings.ses_from_email
    if not recipient:
        # No inbox configured — emit a metadata-only diagnostic and still
        # report success to the sender.
        log.error(
            "trustedos_inquiry.no_recipient name=%r email=%r company_len=%d message_len=%d",
            name,
            email,
            len(company or ""),
            len(message or ""),
        )
        # Full lead goes to first-party stderr logs only. This logger is outside
        # the trusted_router namespace and propagate=False keeps it off the
        # root-attached third-party Axiom handler.
        _leads_log.error(
            "trustedos_inquiry.lead recipient=%r name=%r email=%r company=%r message=%r",
            recipient,
            name,
            email,
            company,
            message,
        )
        return ok

    log.info(
        "trustedos_inquiry.received name=%r email=%r company_len=%d message_len=%d",
        name,
        email,
        len(company or ""),
        len(message or ""),
    )

    text_body = (
        "New TrustedOS partner inquiry\n\n"
        f"Name:    {name}\n"
        f"Company: {company or '(not given)'}\n"
        f"Email:   {email}\n"
        f"IP:      {client_ip}\n\n"
        f"Message:\n{message}\n"
    )
    subject = f"TrustedOS inquiry: {company or name}"
    try:
        sent = get_email_service(settings).send(
            EmailMessage(
                to=recipient,
                subject=subject,
                text_body=text_body,
                mail_class="partner_inquiry",
            )
        )
    except Exception:  # noqa: BLE001 - never surface mailer errors to the form
        sent = False
        log.exception(
            "trustedos_inquiry.send_failed name=%r email=%r company_len=%d message_len=%d",
            name,
            email,
            len(company or ""),
            len(message or ""),
        )
    if not sent:
        # send() returns False when SES is unconfigured or the recipient is
        # suppressed. Surface a metadata-only diagnostic so alerting sees the
        # delivery issue without logging submitted free text.
        log.error(
            "trustedos_inquiry.delivery_failed recipient=%r name=%r email=%r company_len=%d message_len=%d",
            recipient,
            name,
            email,
            len(company or ""),
            len(message or ""),
        )
        # Full lead goes to first-party stderr logs only. This logger is outside
        # the trusted_router namespace and propagate=False keeps it off the
        # root-attached third-party Axiom handler.
        _leads_log.error(
            "trustedos_inquiry.lead recipient=%r name=%r email=%r company=%r message=%r",
            recipient,
            name,
            email,
            company,
            message,
        )
    return ok


async def _handle_support_inquiry(settings: Settings, request: Request) -> JSONResponse:
    """Send support mail without logging submitted free text or identity fields."""
    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"ok": False, "error": "invalid_request"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "error": "invalid_request"}, status_code=400)

    # Silently accept honeypot submissions so automated senders do not learn
    # which field caused the drop.
    if str(payload.get("website", "")).strip():
        return JSONResponse({"ok": True})

    name = str(payload.get("name", "")).strip()[:200]
    email = str(payload.get("email", "")).strip()[:320]
    subject = " ".join(str(payload.get("subject", "")).splitlines()).strip()[:200]
    message = str(payload.get("message", "")).strip()[:5000]
    request_id = str(payload.get("request_id", "")).strip()[:200]
    category = str(payload.get("category", "other")).strip().lower()
    category_label = _SUPPORT_CATEGORIES.get(category)

    if (
        not name
        or not subject
        or not message
        or not _EMAIL_RE.fullmatch(email)
        or category_label is None
    ):
        return JSONResponse({"ok": False, "error": "missing_fields"}, status_code=422)

    client_ip = request.client.host if request.client else "unknown"
    if not _inquiry_rate_ok(f"support:{client_ip}"):
        return JSONResponse({"ok": False, "error": "rate_limited"}, status_code=429)

    text_body = (
        "New TrustedRouter support request\n\n"
        f"Category:   {category_label}\n"
        f"Name:       {name}\n"
        f"Email:      {email}\n"
        f"Request ID: {request_id or '(not given)'}\n\n"
        f"Subject: {subject}\n\n"
        f"Message:\n{message}\n"
    )
    support_message = EmailMessage(
        to=settings.support_email,
        reply_to=email,
        subject=f"TrustedRouter support: {category_label}: {subject}",
        text_body=text_body,
        mail_class="support_inquiry",
    )
    try:
        sent = await run_in_threadpool(
            get_email_service(settings).send,
            support_message,
        )
    except Exception:  # noqa: BLE001 - return a stable support fallback
        sent = False
        log.exception(
            "support_inquiry.send_failed category=%s has_request_id=%s "
            "subject_len=%d message_len=%d",
            category,
            bool(request_id),
            len(subject),
            len(message),
        )
    if not sent:
        log.error(
            "support_inquiry.delivery_failed category=%s has_request_id=%s "
            "subject_len=%d message_len=%d",
            category,
            bool(request_id),
            len(subject),
            len(message),
        )
        return JSONResponse(
            {"ok": False, "error": "delivery_unavailable"},
            status_code=503,
        )

    ops_message = OpsChatSupportMessage(
        message_id=f"support:{uuid.uuid4().hex}",
        name=name,
        email=email,
        subject=f"{category_label}: {subject}",
        message=(f"Request ID: {request_id}\n\n{message}" if request_id else message),
    )
    try:
        fanout = await fanout_support_message(settings, ops_message)
    except Exception:  # noqa: BLE001 - email delivery remains authoritative
        fanout = None
        log.exception(
            "support_inquiry.ops_fanout_exception category=%s",
            category,
        )
    if fanout is not None and fanout.configured and fanout.accepted == 0:
        log.error(
            "support_inquiry.ops_fanout_failed configured=%d category=%s",
            fanout.configured,
            category,
        )
    elif fanout is not None and fanout.accepted < fanout.configured:
        log.warning(
            "support_inquiry.ops_fanout_partial accepted=%d configured=%d category=%s",
            fanout.accepted,
            fanout.configured,
            category,
        )

    log.info(
        "support_inquiry.sent category=%s has_request_id=%s subject_len=%d message_len=%d",
        category,
        bool(request_id),
        len(subject),
        len(message),
    )
    return JSONResponse({"ok": True})


def register_public_routes(app: FastAPI, settings: Settings) -> None:
    app.mount("/static", _CachedStaticFiles(directory=STATIC_DIR), name="static")
    trust_release_resolver = TrustReleaseResolver(settings)
    # One resolver per plane. The control plane mirrors three independently
    # published records rather than authoring two of them from its own config.
    aws_release_resolver = TrustReleaseResolver(
        settings,
        urls=[settings.trust_aws_release_url],
        validator=validated_aws_metadata,
        embedded=embedded_aws_metadata,
    )
    azure_release_resolver = TrustReleaseResolver(
        settings,
        urls=[settings.trust_azure_release_url],
        validator=validated_azure_metadata,
        embedded=embedded_azure_metadata,
    )

    async def _mirrored(
        resolver: TrustReleaseResolver, embedded: Callable[[Settings], Mapping[str, Any]]
    ) -> tuple[Mapping[str, Any], str]:
        """Authoritative record if reachable, else the offline fallback.

        Falling back to config keeps a verified measurement available during an
        upstream outage. It is labelled 'embedded' rather than 'live' so the
        response never implies it was just confirmed from the source.
        """
        try:
            resolved = await resolver.resolve()
            return resolved.metadata, resolved.status
        except TrustReleaseUnavailable:
            return embedded(settings), "embedded"

    async def resolved_trust_release() -> ResolvedTrustRelease:
        try:
            return await trust_release_resolver.resolve()
        except TrustReleaseUnavailable:
            return unavailable_trust_release()

    def trust_response_headers(status: str) -> dict[str, str]:
        return {
            "cache-control": (
                "max-age=60, public" if status in {"live", "embedded"} else "no-store"
            ),
            "x-trustedrouter-release-status": status,
        }

    def trust_response_status(status: str) -> int:
        return 200 if status in {"live", "embedded"} else 503

    def public_document_headers(path: str) -> dict[str, str]:
        return {
            "cache-control": "public, max-age=300, s-maxage=3600",
            "link": f'<{canonical_public_url(settings, path)}>; rel="canonical"',
        }

    def public_html_route(
        path: str, *, include_slash: bool = True
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            app.api_route(path, methods=["GET", "HEAD"], response_class=HTMLResponse)(func)
            if include_slash and not path.endswith("/"):
                app.api_route(
                    f"{path}/",
                    methods=["GET", "HEAD"],
                    response_class=HTMLResponse,
                    include_in_schema=False,
                )(func)
            return func

        return decorator

    @app.api_route(
        "/api/reference",
        methods=["GET", "HEAD"],
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    @app.api_route(
        "/api/reference/",
        methods=["GET", "HEAD"],
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    async def api_reference() -> HTMLResponse:
        response = get_swagger_ui_html(
            openapi_url=app.openapi_url or "/openapi.json",
            title=f"{app.title} API reference",
        )
        canonical = html.escape(
            f"https://{settings.trusted_domain}/api/reference",
            quote=True,
        )
        title = "TrustedRouter API Reference: Endpoints and Schemas"
        description = (
            "Explore TrustedRouter's OpenAI-compatible API endpoints, request schemas, "
            "authentication, models, keys, billing, observability, and stable compatibility errors."
        )
        escaped_description = html.escape(description, quote=True)
        metadata = (
            f'<meta name="description" content="{escaped_description}">\n'
            f'<link rel="canonical" href="{canonical}">\n'
            '<meta property="og:type" content="website">\n'
            f'<meta property="og:title" content="{html.escape(title, quote=True)}">\n'
            f'<meta property="og:description" content="{escaped_description}">\n'
            f'<meta property="og:url" content="{canonical}">\n'
            '<meta property="og:image" content="https://trustedrouter.com/og.png">\n'
            '<meta name="twitter:card" content="summary_large_image">\n'
        )
        reference_header = (
            '<header style="padding:16px 24px;background:#101820;color:#fff;font-family:'
            'ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif">'
            '<h1 style="font-size:18px;line-height:1.2;margin:0 0 6px;color:#fff">'
            "TrustedRouter API reference</h1>"
            '<p style="max-width:900px;margin:0 0 10px;color:#d7e6f5;font-size:14px;line-height:1.45">'
            "Explore the OpenAI-compatible endpoints, request schemas, authentication, model "
            "routing, billing, observability, and stable compatibility errors used by "
            "TrustedRouter clients. Start with the integration guide, browse the live model "
            "catalog, and review how the attested gateway keeps prompt traffic separate from "
            "the control plane. Requests use one base URL and standard bearer authentication."
            "</p>"
            '<nav aria-label="TrustedRouter API reference links" '
            'style="display:flex;align-items:center;gap:18px;flex-wrap:wrap">'
            '<a href="/docs" style="color:#fff;font-weight:700;text-decoration:none">Docs</a>'
            '<a href="/models" style="color:#d7e6f5;text-decoration:none">Models</a>'
            '<a href="/security" style="color:#d7e6f5;text-decoration:none">Security</a>'
            '<a href="/" style="color:#d7e6f5;text-decoration:none">TrustedRouter</a>'
            "</nav></header>"
        )
        body = (
            bytes(response.body)
            .decode()
            .replace("<html>", '<html lang="en">', 1)
            .replace(
                f"<title>{html.escape(app.title)} API reference</title>",
                f"<title>{html.escape(title)}</title>",
                1,
            )
            .replace(
                "</head>",
                f"{metadata}</head>",
                1,
            )
            .replace("<body>", f"<body>{reference_header}", 1)
        )
        return HTMLResponse(body)

    @app.api_route(
        "/redoc",
        methods=["GET", "HEAD"],
        include_in_schema=False,
    )
    @app.api_route(
        "/docs/oauth2-redirect",
        methods=["GET", "HEAD"],
        include_in_schema=False,
    )
    async def legacy_api_reference() -> RedirectResponse:
        return RedirectResponse(url="/api/reference", status_code=301)

    @public_html_route("/", include_slash=False)
    async def dashboard(request: Request, background_tasks: BackgroundTasks) -> Any:
        hostname = request_hostname(request)
        domain = request_control_domain(request, settings)
        api_base_url = request_api_base_url(request, settings)
        if is_trust_hostname(settings, hostname):
            release = await resolved_trust_release()
            return HTMLResponse(
                trust_html(
                    settings,
                    public_domain=domain,
                    api_base_url=api_base_url,
                    release_metadata=release.metadata,
                    release_metadata_status=release.status,
                ),
                status_code=trust_response_status(release.status),
                headers=trust_response_headers(release.status),
            )
        if is_status_hostname(settings, hostname):
            return _cached_status_page_response(
                settings,
                host=hostname,
                background_tasks=background_tasks,
            )
        if hostname == "eu.trustedrouter.com":
            return public_page_html(settings, "eu", site_url="https://eu.trustedrouter.com/")
        alternate_brand = {
            "uptimerouter.com": "UptimeRouter",
            "www.uptimerouter.com": "UptimeRouter",
            "allyrouter.com": "AllyRouter",
            "www.allyrouter.com": "AllyRouter",
        }.get(hostname)
        if alternate_brand:
            canonical_host = hostname.removeprefix("www.")
            return dashboard_html(
                settings,
                api_base_url=api_base_url,
                brand_name=alternate_brand,
                site_url=f"https://{canonical_host}/",
            )
        return dashboard_html(settings, api_base_url=api_base_url)

    @public_html_route("/trust")
    async def trust_page(request: Request) -> HTMLResponse:
        release = await resolved_trust_release()
        return HTMLResponse(
            trust_html(
                settings,
                public_domain=request_control_domain(request, settings),
                api_base_url=request_api_base_url(request, settings),
                release_metadata=release.metadata,
                release_metadata_status=release.status,
            ),
            status_code=trust_response_status(release.status),
            headers=trust_response_headers(release.status),
        )

    @public_html_route("/compare/openrouter")
    async def compare_openrouter() -> HTMLResponse:
        body = public_competitor_compare_html(settings, "openrouter")
        assert body is not None
        return HTMLResponse(body)

    @public_html_route("/compare/vercel-ai-gateway")
    async def compare_vercel_ai_gateway() -> HTMLResponse:
        body = public_competitor_compare_html(settings, "vercel-ai-gateway")
        assert body is not None
        return HTMLResponse(body)

    @public_html_route("/compare/litellm")
    async def compare_litellm() -> HTMLResponse:
        body = public_competitor_compare_html(settings, "litellm")
        assert body is not None
        return HTMLResponse(body)

    @public_html_route("/compare")
    async def competitor_compare_index() -> HTMLResponse:
        return HTMLResponse(public_competitor_compare_index_html(settings))

    @public_html_route("/docs/migrate-from-openrouter")
    async def migrate_from_openrouter() -> str:
        return public_page_html(settings, "docs/migrate-from-openrouter")

    @public_html_route("/docs/agent-setup")
    async def agent_setup() -> str:
        return public_page_html(settings, "docs/agent-setup")

    @public_html_route("/docs/tagging")
    async def tagging_docs() -> str:
        return public_page_html(settings, "docs/tagging")

    @public_html_route("/docs/telemetry")
    async def telemetry_docs() -> str:
        return public_page_html(settings, "docs/telemetry")

    @public_html_route("/docs/prompt-caching")
    async def prompt_caching_docs() -> str:
        return public_page_html(settings, "docs/prompt-caching")

    @public_html_route("/docs/batch")
    async def batch_docs() -> str:
        return public_page_html(settings, "docs/batch")

    @public_html_route("/docs/web-search")
    async def web_search_docs() -> str:
        return public_page_html(settings, "docs/web-search")

    @public_html_route("/docs/video")
    async def video_docs() -> str:
        return public_page_html(settings, "docs/video")

    @public_html_route("/docs/mcp")
    async def mcp_docs() -> str:
        return public_page_html(settings, "docs/mcp")

    @public_html_route("/docs/notify")
    async def notify_docs() -> str:
        return public_page_html(settings, "docs/notify")

    @public_html_route("/docs/evals")
    async def evals() -> str:
        return public_page_html(settings, "docs/evals")

    @public_html_route("/docs/provider-conformance")
    async def provider_conformance_docs() -> str:
        return public_page_html(settings, "docs/provider-conformance")

    @public_html_route("/docs/synth")
    async def synth_docs() -> str:
        return public_page_html(settings, "docs/synth")

    @public_html_route("/docs/fusion")
    async def fusion_docs() -> RedirectResponse:
        return RedirectResponse(url="/docs/synth", status_code=301)

    @public_html_route("/docs/x402")
    async def x402_docs() -> str:
        return public_page_html(settings, "docs/x402")

    @public_html_route("/docs/user-models")
    async def user_models_docs() -> str:
        return public_page_html(settings, "docs/user-models")

    @public_html_route("/eu")
    async def eu() -> str:
        return public_page_html(settings, "eu")

    @public_html_route("/trustedos")
    async def trustedos() -> str:
        return public_page_html(settings, "trustedos")

    @app.post("/trustedos/inquiry", include_in_schema=False)
    async def trustedos_inquiry(request: Request) -> JSONResponse:
        return await _handle_trustedos_inquiry(settings, request)

    # ── SEO landing pages ────────────────────────────────────────────
    # Top-level slugs targeting high-intent buyer queries. Each is a
    # self-contained sales surface (see PUBLIC_PAGES in dashboard.py).
    # Keep these top-level (not under /seo or /landing) — that hurts
    # ranking and looks defensive.
    @public_html_route("/azure-openai-alternative")
    async def seo_azure_openai_alternative() -> str:
        return public_page_html(settings, "azure-openai-alternative")

    @public_html_route("/deepseek-api-privacy")
    async def seo_deepseek_api_privacy() -> str:
        return public_page_html(settings, "deepseek-api-privacy")

    @public_html_route("/glm-5-api")
    async def seo_glm_5_api() -> str:
        return public_page_html(settings, "glm-5-api")

    @public_html_route("/gdpr-compliant-llm-api")
    async def seo_gdpr_compliant_llm_api() -> str:
        return public_page_html(settings, "gdpr-compliant-llm-api")

    @public_html_route("/chinese-ai-models-us-hosted")
    async def seo_chinese_ai_models_us_hosted() -> str:
        return public_page_html(settings, "chinese-ai-models-us-hosted")

    @public_html_route("/minimax-m3-api")
    async def seo_minimax_m3_api() -> str:
        return public_page_html(settings, "minimax-m3-api")

    @public_html_route("/best-llm-router")
    async def seo_best_llm_router() -> str:
        return public_page_html(settings, "best-llm-router")

    @public_html_route("/llm-failover")
    async def seo_llm_failover() -> str:
        return public_page_html(settings, "llm-failover")

    @public_html_route("/groq-alternative")
    async def seo_groq_alternative() -> str:
        return public_page_html(settings, "groq-alternative")

    @public_html_route("/vertex-ai-alternative")
    async def seo_vertex_ai_alternative() -> str:
        return public_page_html(settings, "vertex-ai-alternative")

    @public_html_route("/llm-api-for-financial-services")
    async def seo_llm_api_for_financial_services() -> str:
        return public_page_html(settings, "llm-api-for-financial-services")

    @public_html_route("/llm-api-for-law-firms")
    async def seo_llm_api_for_law_firms() -> str:
        return public_page_html(settings, "llm-api-for-law-firms")

    @public_html_route("/llm-data-residency")
    async def seo_llm_data_residency() -> str:
        return public_page_html(settings, "llm-data-residency")

    @public_html_route("/no-log-llm-api")
    async def seo_no_log_llm_api() -> str:
        return public_page_html(settings, "no-log-llm-api")

    @public_html_route("/anonymous-llm-api")
    async def seo_anonymous_llm_api() -> str:
        return public_page_html(settings, "anonymous-llm-api")

    @public_html_route("/cline-api-provider")
    async def seo_cline_api_provider() -> str:
        return public_page_html(settings, "cline-api-provider")

    @public_html_route("/sillytavern-api")
    async def seo_sillytavern_api() -> str:
        return public_page_html(settings, "sillytavern-api")

    @public_html_route("/aws-bedrock-alternative")
    async def seo_aws_bedrock_alternative() -> str:
        return public_page_html(settings, "aws-bedrock-alternative")

    @public_html_route("/llm-document-processing")
    async def seo_llm_document_processing() -> str:
        return public_page_html(settings, "llm-document-processing")

    @public_html_route("/gpt-oss-120b-api")
    async def seo_gpt_oss_120b_api() -> str:
        return public_page_html(settings, "gpt-oss-120b-api")

    @public_html_route("/latest-model-apis")
    async def seo_latest_model_apis() -> str:
        return public_page_html(settings, "latest-model-apis")

    @public_html_route("/eu-ai-act-llm-compliance")
    async def seo_eu_ai_act_llm_compliance() -> str:
        return public_page_html(settings, "eu-ai-act-llm-compliance")

    @public_html_route("/x402-llm-api")
    async def seo_x402_llm_api() -> str:
        return public_page_html(settings, "x402-llm-api")

    @public_html_route("/openrouter-alternative")
    async def seo_openrouter_alternative() -> str:
        return public_page_html(settings, "openrouter-alternative")

    @public_html_route("/private-llm-api")
    async def seo_private_llm_api() -> str:
        return public_page_html(settings, "private-llm-api")

    @public_html_route("/hipaa-llm-api")
    async def seo_hipaa_llm_api() -> str:
        return public_page_html(settings, "hipaa-llm-api")

    @public_html_route("/llm-zero-data-retention")
    async def seo_llm_zero_data_retention() -> str:
        return public_page_html(settings, "llm-zero-data-retention")

    @public_html_route("/claude-api-privacy")
    async def seo_claude_api_privacy() -> str:
        return public_page_html(settings, "claude-api-privacy")

    @public_html_route("/litellm-alternative")
    async def seo_litellm_alternative() -> str:
        return public_page_html(settings, "litellm-alternative")

    @public_html_route("/portkey-alternative")
    async def seo_portkey_alternative() -> str:
        return public_page_html(settings, "portkey-alternative")

    @public_html_route("/confidential-computing-llm")
    async def seo_confidential_computing_llm() -> str:
        return public_page_html(settings, "confidential-computing-llm")

    @public_html_route("/badge")
    async def confidential_ai_badge() -> str:
        return public_page_html(settings, "badge")

    @public_html_route("/tinfoil-alternative")
    async def seo_tinfoil_alternative() -> str:
        return public_page_html(settings, "tinfoil-alternative")

    @public_html_route("/sign-in-with-trustedrouter")
    async def seo_sign_in_with_trustedrouter() -> str:
        return public_page_html(settings, "sign-in-with-trustedrouter")

    @public_html_route("/openai-compatible-llm-api")
    async def seo_openai_compatible_llm_api() -> str:
        return public_page_html(settings, "openai-compatible-llm-api")

    @public_html_route("/kimi-k2-api")
    async def seo_kimi_k2_api() -> str:
        return public_page_html(settings, "kimi-k2-api")

    @public_html_route("/gemini-flash-alternative")
    async def seo_gemini_flash_alternative() -> str:
        return public_page_html(settings, "gemini-flash-alternative")

    @public_html_route("/llm-provider-latency-benchmarks")
    async def seo_llm_provider_latency_benchmarks() -> str:
        return public_page_html(settings, "llm-provider-latency-benchmarks")

    @public_html_route("/pricing")
    async def pricing() -> str:
        return public_page_html(settings, "pricing")

    @public_html_route("/choose")
    async def choose(background_tasks: BackgroundTasks) -> Response:
        return _cached_public_response(
            settings,
            key=f"choose:page:{settings.release}",
            media_type="text/html",
            ttl_seconds=CHOOSE_PAGE_CACHE_SECONDS,
            stale_seconds=CHOOSE_PAGE_STALE_SECONDS,
            background_tasks=background_tasks,
            build=lambda: public_page_html(settings, "choose").encode(),
        )

    @app.get("/choose/catalog.json")
    async def choose_catalog(background_tasks: BackgroundTasks) -> Response:
        return _cached_public_response(
            settings,
            key=f"choose:catalog:{settings.release}",
            media_type="application/json",
            ttl_seconds=CHOOSE_CATALOG_CACHE_SECONDS,
            stale_seconds=CHOOSE_CATALOG_STALE_SECONDS,
            background_tasks=background_tasks,
            build=lambda: _json_body(
                choose_catalog_payload(test_mode=settings.environment == "test")
            ),
        )

    @app.get("/ai-iq/models.json")
    async def ai_iq_models() -> JSONResponse:
        payload = ai_iq_catalog_payload(
            (model_id for model_id in MODELS if model_id not in META_MODEL_IDS),
            test_mode=settings.environment == "test",
        )
        return JSONResponse(
            payload,
            headers={"cache-control": "public, max-age=3600, stale-while-revalidate=86400"},
        )

    @public_html_route("/docs")
    async def docs_hub() -> str:
        return public_page_html(settings, "docs")

    @public_html_route("/vibe-coders")
    async def vibe_coders() -> str:
        return public_page_html(settings, "vibe-coders")

    @app.get("/claude-code", include_in_schema=False)
    async def claude_code_redirect() -> RedirectResponse:
        return RedirectResponse(url="/vibe-coders", status_code=301)

    @public_html_route("/for-developers")
    async def for_developers() -> str:
        return public_page_html(settings, "for-developers")

    @public_html_route("/providers/marketplace")
    async def provider_marketplace() -> str:
        return public_page_html(settings, "providers/marketplace")

    @app.get(
        "/providers/marketplace/catalog.schema.json",
        include_in_schema=False,
    )
    async def provider_catalog_schema() -> JSONResponse:
        return JSONResponse(
            PROVIDER_CATALOG_SCHEMA,
            media_type="application/schema+json",
            headers={"cache-control": "public, max-age=3600, stale-while-revalidate=86400"},
        )

    @app.get(
        "/providers/marketplace/catalog.v2.schema.json",
        include_in_schema=False,
    )
    async def provider_catalog_v2_schema() -> JSONResponse:
        return JSONResponse(
            PROVIDER_CATALOG_V2_SCHEMA,
            media_type="application/schema+json",
            headers={"cache-control": "public, max-age=3600, stale-while-revalidate=86400"},
        )

    @app.api_route(
        "/providers/apply",
        methods=["GET", "HEAD"],
        include_in_schema=False,
    )
    @app.api_route(
        "/providers/apply/",
        methods=["GET", "HEAD"],
        include_in_schema=False,
    )
    async def legacy_provider_apply() -> RedirectResponse:
        return RedirectResponse(url="/providers/marketplace", status_code=301)

    @app.api_route(
        "/providers/apply/catalog.schema.json",
        methods=["GET", "HEAD"],
        include_in_schema=False,
    )
    async def legacy_provider_catalog_schema() -> RedirectResponse:
        return RedirectResponse(
            url="/providers/marketplace/catalog.schema.json",
            status_code=301,
        )

    @public_html_route("/apps")
    async def apps() -> str:
        return public_apps_html(settings, apps=_apps_snapshot(settings))

    @public_html_route("/resources")
    async def resources() -> str:
        return public_page_html(settings, "resources")

    @public_html_route("/customers/robot-robot-human")
    async def customer_robot_robot_human() -> str:
        return public_page_html(settings, "customers/robot-robot-human")

    @public_html_route("/careers")
    async def careers() -> str:
        return public_page_html(settings, "careers")

    @public_html_route("/blog")
    async def blog() -> str:
        return public_blog_index_html(settings)

    @public_html_route("/blog/{slug}")
    async def blog_post(slug: str) -> Any:
        if slug in {"zeus-terminal-bench-hard-72", "socrates-pro-plus-terminal-bench-hard-72"}:
            return RedirectResponse(
                url="/blog/socrates-1.1-terminal-bench-hard-72", status_code=301
            )
        html = public_blog_post_html(settings, slug)
        if html is None:
            return HTMLResponse(
                public_not_found_html(settings, f"/blog/{slug}"),
                status_code=404,
            )
        return html

    @public_html_route("/security")
    async def security() -> str:
        return public_page_html(settings, "security")

    @public_html_route("/legal")
    async def legal() -> str:
        return public_legal_html(settings)

    @public_html_route("/privacy")
    async def privacy() -> str:
        return public_privacy_html(settings)

    @public_html_route("/sms")
    async def sms_program() -> str:
        return public_sms_html(settings)

    @public_html_route("/terms")
    async def terms() -> str:
        return public_terms_html(settings)

    @public_html_route("/support")
    async def support() -> str:
        return public_support_html(settings)

    @app.post("/support/inquiry", include_in_schema=False)
    async def support_inquiry(request: Request) -> JSONResponse:
        return await _handle_support_inquiry(settings, request)

    @public_html_route("/legal/dpa")
    async def legal_dpa() -> str:
        return public_dpa_html(settings)

    @public_html_route("/legal/baa")
    async def legal_baa() -> str:
        return public_baa_html(settings)

    @public_html_route("/legal/soc2-readiness")
    async def legal_soc2_readiness() -> str:
        return public_soc2_readiness_html(settings)

    @public_html_route("/legal/hipaa-readiness")
    async def legal_hipaa_readiness() -> str:
        return public_hipaa_readiness_html(settings)

    @public_html_route("/legal/subprocessors")
    async def legal_subprocessors() -> str:
        return public_subprocessors_html(settings)

    @app.api_route(
        "/legal/procurement.json",
        methods=["GET", "HEAD"],
        response_class=PlainTextResponse,
    )
    async def legal_procurement_json() -> PlainTextResponse:
        return PlainTextResponse(
            procurement_json(settings),
            media_type="application/json",
            headers={"cache-control": "public, max-age=300, s-maxage=3600"},
        )

    @app.api_route(
        "/legal/soc2-readiness.json",
        methods=["GET", "HEAD"],
        response_class=PlainTextResponse,
    )
    async def legal_soc2_readiness_json() -> PlainTextResponse:
        return PlainTextResponse(
            soc2_readiness_json(settings),
            media_type="application/json",
            headers={"cache-control": "public, max-age=300, s-maxage=3600"},
        )

    @app.api_route(
        "/legal/hipaa-readiness.json",
        methods=["GET", "HEAD"],
        response_class=PlainTextResponse,
    )
    async def legal_hipaa_readiness_json() -> PlainTextResponse:
        return PlainTextResponse(
            hipaa_readiness_json(settings),
            media_type="application/json",
            headers={"cache-control": "public, max-age=300, s-maxage=3600"},
        )

    @app.api_route(
        "/legal/subprocessors.json",
        methods=["GET", "HEAD"],
        response_class=PlainTextResponse,
    )
    async def legal_subprocessors_json() -> PlainTextResponse:
        return PlainTextResponse(
            subprocessors_json(settings),
            media_type="application/json",
            headers={"cache-control": "public, max-age=300, s-maxage=3600"},
        )

    @public_html_route("/benchmarks")
    async def benchmarks() -> str:
        return public_benchmarks_html(settings)

    @public_html_route("/benchmarks/reports")
    async def benchmark_reports() -> str:
        return public_benchmark_reports_index_html(settings)

    @app.get("/benchmarks/reports/{period}.json", include_in_schema=False)
    async def benchmark_report_json(period: str) -> JSONResponse:
        report = monthly_benchmark_report(period)
        if report is None:
            return JSONResponse(
                {"error": {"message": "benchmark report not found", "type": "not_found"}},
                status_code=404,
            )
        return JSONResponse(
            {"data": report},
            headers={"cache-control": "public, max-age=300, s-maxage=86400"},
        )

    @public_html_route("/benchmarks/reports/{period}")
    async def benchmark_report(period: str) -> HTMLResponse:
        body = public_benchmark_report_html(settings, period)
        if body is None:
            return HTMLResponse(public_not_found_html(settings, "/benchmarks/reports"), 404)
        return HTMLResponse(body)

    @public_html_route("/rankings")
    async def rankings() -> str:
        return public_rankings_html(settings)

    @app.api_route("/robots.txt", methods=["GET", "HEAD"], response_class=PlainTextResponse)
    async def robots() -> PlainTextResponse:
        return PlainTextResponse(
            robots_txt(settings),
            headers={"cache-control": "public, max-age=300, s-maxage=3600"},
        )

    @app.api_route(
        f"/{INDEXNOW_KEY}.txt",
        methods=["GET", "HEAD"],
        response_class=PlainTextResponse,
        include_in_schema=False,
    )
    async def indexnow_key() -> PlainTextResponse:
        return PlainTextResponse(
            f"{INDEXNOW_KEY}\n",
            headers={"cache-control": "public, max-age=86400, s-maxage=86400"},
        )

    @app.api_route("/sitemap.xml", methods=["GET", "HEAD"])
    async def sitemap() -> Response:
        return Response(
            sitemap_xml(settings),
            media_type="application/xml",
            headers={"cache-control": "public, max-age=300, s-maxage=3600"},
        )

    @app.api_route("/sitemap-core.xml", methods=["GET", "HEAD"])
    async def sitemap_core() -> Response:
        return Response(
            sitemap_core_xml(settings),
            media_type="application/xml",
            headers={"cache-control": "public, max-age=300, s-maxage=3600"},
        )

    @app.api_route("/sitemap-providers.xml", methods=["GET", "HEAD"])
    async def sitemap_providers() -> Response:
        return Response(
            sitemap_providers_xml(settings),
            media_type="application/xml",
            headers={"cache-control": "public, max-age=300, s-maxage=3600"},
        )

    @app.api_route("/sitemap-models.xml", methods=["GET", "HEAD"])
    async def sitemap_models() -> Response:
        return Response(
            sitemap_models_xml(settings),
            media_type="application/xml",
            headers={"cache-control": "public, max-age=300, s-maxage=3600"},
        )

    @app.api_route("/sitemap-comparisons.xml", methods=["GET", "HEAD"])
    async def sitemap_comparisons() -> Response:
        return Response(
            sitemap_comparisons_xml(settings),
            media_type="application/xml",
            headers={"cache-control": "public, max-age=300, s-maxage=3600"},
        )

    @app.api_route("/llms.txt", methods=["GET", "HEAD"], response_class=PlainTextResponse)
    async def llms() -> PlainTextResponse:
        return PlainTextResponse(
            llms_txt(settings),
            headers=public_document_headers("/llms.txt"),
        )

    @app.api_route("/docs/llms.txt", methods=["GET", "HEAD"], response_class=PlainTextResponse)
    async def docs_llms() -> PlainTextResponse:
        return PlainTextResponse(
            docs_llms_txt(settings),
            headers=public_document_headers("/docs/llms.txt"),
        )

    @app.api_route(
        "/docs/llms-full.txt",
        methods=["GET", "HEAD"],
        response_class=PlainTextResponse,
    )
    async def docs_llms_full() -> PlainTextResponse:
        return PlainTextResponse(
            docs_llms_full_txt(settings),
            headers=public_document_headers("/docs/llms-full.txt"),
        )

    @public_html_route("/status")
    async def status_page(request: Request, background_tasks: BackgroundTasks) -> Response:
        return _cached_status_page_response(
            settings,
            host=request.headers.get("host", ""),
            background_tasks=background_tasks,
        )

    @public_html_route("/leaderboard")
    async def leaderboard_page(request: Request, background_tasks: BackgroundTasks) -> Response:
        _ = request
        return _cached_public_response(
            settings,
            key="leaderboard:page",
            media_type="text/html",
            ttl_seconds=LEADERBOARD_RESPONSE_CACHE_SECONDS,
            stale_seconds=LEADERBOARD_RESPONSE_STALE_SECONDS,
            background_tasks=background_tasks,
            build=lambda: public_leaderboard_html(
                settings, _leaderboard_snapshot(settings)
            ).encode(),
        )

    @public_html_route("/leaderboard/video")
    async def video_leaderboard_page(
        request: Request, background_tasks: BackgroundTasks
    ) -> Response:
        _ = request
        return _cached_public_response(
            settings,
            key="leaderboard:video:page",
            media_type="text/html",
            ttl_seconds=LEADERBOARD_RESPONSE_CACHE_SECONDS,
            stale_seconds=LEADERBOARD_RESPONSE_STALE_SECONDS,
            background_tasks=background_tasks,
            build=lambda: public_video_leaderboard_html(
                settings, _video_leaderboard_snapshot(settings)
            ).encode(),
        )

    @app.get("/leaderboard/video.json")
    async def video_leaderboard_json(background_tasks: BackgroundTasks) -> Response:
        return _cached_public_response(
            settings,
            key="leaderboard:video:json",
            media_type="application/json",
            ttl_seconds=LEADERBOARD_RESPONSE_CACHE_SECONDS,
            stale_seconds=LEADERBOARD_RESPONSE_STALE_SECONDS,
            background_tasks=background_tasks,
            build=lambda: _json_body({"data": _video_leaderboard_snapshot(settings)}),
        )

    @app.get("/fleet.json")
    async def fleet_json() -> Response:
        # Served by EVERY control plane so the fleet view is as multi-cloud
        # as the service: any healthy deployment can be the command center.
        return Response(
            content=_json_body({"data": await _fleet_snapshot_cached(settings)}),
            media_type="application/json",
        )

    @public_html_route("/fleet")
    async def fleet_page(request: Request) -> HTMLResponse:
        _ = request
        return HTMLResponse(_fleet_page_html(await _fleet_snapshot_cached(settings)))

    @app.get("/status.json")
    async def status_json(background_tasks: BackgroundTasks) -> Response:
        return _cached_public_response(
            settings,
            key="status:json",
            media_type="application/json",
            ttl_seconds=STATUS_RESPONSE_CACHE_SECONDS,
            stale_seconds=STATUS_RESPONSE_STALE_SECONDS,
            background_tasks=background_tasks,
            build=lambda: _json_body({"data": _compact_status_json(_status_snapshot(settings))}),
        )

    @app.get("/status/history")
    async def status_history(
        request: Request,
        background_tasks: BackgroundTasks,
        window: str = "48h",
        response_format: str | None = Query(default=None, alias="format"),
    ) -> Response:
        if window not in {"5m", "24h", "48h", "daily", "monthly"}:
            return JSONResponse(
                {
                    "error": {
                        "message": "window must be 5m, 24h, 48h, daily, or monthly",
                        "type": "bad_request",
                    }
                },
                status_code=400,
            )
        if not _wants_history_html(request, explicit_format=response_format):
            return _cached_public_response(
                settings,
                key=f"status:history:{window}:json",
                media_type="application/json",
                ttl_seconds=STATUS_HISTORY_CACHE_SECONDS,
                stale_seconds=STATUS_HISTORY_STALE_SECONDS,
                background_tasks=background_tasks,
                build=lambda: _json_body({"data": _status_history_payload(window)}),
            )
        render_host = _status_render_host(settings, request.headers.get("host", ""))
        return _cached_public_response(
            settings,
            key=f"status:history:{window}:html:{render_host}",
            media_type="text/html",
            ttl_seconds=STATUS_HISTORY_CACHE_SECONDS,
            stale_seconds=STATUS_HISTORY_STALE_SECONDS,
            background_tasks=background_tasks,
            build=lambda: _status_history_page_html(
                settings,
                host=render_host,
                window=window,
                history=_status_history_payload(window),
            ).encode(),
        )

    @public_html_route("/models")
    async def models(request: Request) -> str:
        return public_models_html(settings, model_filter=request.query_params.get("filter", "all"))

    @public_html_route("/providers")
    async def providers(request: Request) -> Response:
        if _wants_html(request):
            return HTMLResponse(public_providers_html(settings))
        return JSONResponse(
            {
                "data": [
                    provider_to_openrouter_shape(provider) for provider in providers_for_display()
                ]
            }
        )

    @public_html_route("/providers/{provider_slug}/performance")
    async def provider_performance(provider_slug: str) -> HTMLResponse:
        body = public_provider_performance_html(settings, provider_slug.strip())
        if body is None:
            return HTMLResponse(
                public_page_html(settings, "security"),
                status_code=404,
            )
        return HTMLResponse(body)

    @public_html_route("/providers/{provider_slug}")
    async def provider_detail(provider_slug: str) -> HTMLResponse:
        body = public_provider_detail_html(settings, provider_slug.strip())
        if body is None:
            return HTMLResponse(
                public_page_html(settings, "security"),
                status_code=404,
            )
        return HTMLResponse(body)

    @public_html_route("/compare/models")
    async def model_compare_index() -> HTMLResponse:
        body = public_model_compare_index_html(settings)
        assert body is not None
        return HTMLResponse(body)

    @public_html_route("/compare/models/page/{page}")
    async def model_compare_index_page(page: int) -> Response:
        if page == 1:
            return RedirectResponse(url="/compare/models", status_code=301)
        body = public_model_compare_index_html(settings, page=page)
        if body is None:
            return HTMLResponse(
                public_model_not_found_html(settings, f"comparison page {page}"),
                status_code=404,
            )
        return HTMLResponse(body)

    @public_html_route("/compare/models/{left_author}/{left_slug}/vs/{right_author}/{right_slug}")
    async def model_compare(
        left_author: str,
        left_slug: str,
        right_author: str,
        right_slug: str,
    ) -> Response:
        left_id = f"{left_author.strip()}/{left_slug.strip()}"
        right_id = f"{right_author.strip()}/{right_slug.strip()}"
        canonical_path = canonical_model_comparison_path(left_id, right_id)
        if canonical_path is not None:
            requested_path = f"/compare/models/{left_id}/vs/{right_id}"
            if requested_path != canonical_path:
                return RedirectResponse(url=canonical_path, status_code=301)
        body = public_model_compare_html(settings, left_id, right_id)
        if body is None:
            return HTMLResponse(
                public_model_not_found_html(settings, f"{left_id}/vs/{right_id}"),
                status_code=404,
            )
        return HTMLResponse(body)

    @public_html_route("/compare/{competitor_slug}")
    async def competitor_compare(competitor_slug: str) -> HTMLResponse:
        body = public_competitor_compare_html(settings, competitor_slug.strip())
        if body is None:
            return HTMLResponse(
                public_not_found_html(settings, f"/compare/{competitor_slug}"),
                status_code=404,
            )
        return HTMLResponse(body)

    @public_html_route("/chat")
    async def chat() -> str:
        return public_chat_html(settings)

    @public_html_route("/user-chat")
    async def user_chat(model: str = Query(..., min_length=1)) -> str:
        locked_model_id = normalize_custom_model_id(model)
        user_model = STORE.get_user_model(locked_model_id)
        return public_chat_html(
            settings,
            locked_model_id=locked_model_id,
            locked_model_label=(
                "User-provided model" if user_model is not None else "Custom model"
            ),
        )

    @public_html_route("/synth")
    async def synth() -> str:
        return public_fusion_html(settings)

    @public_html_route("/fusion")
    async def fusion() -> RedirectResponse:
        return RedirectResponse(url="/synth", status_code=301)

    # Per-model detail page. Path captures `{author}/{slug}` (e.g.
    # `z-ai/glm-4.6`, `moonshotai/kimi-k2.6`) so the URL exactly mirrors
    # the OpenRouter model id. The `:path` converter lets the slash
    # through. Unknown ids render a styled 404 page (HTML, same chrome
    # as the rest of the marketing site) instead of FastAPI's default
    # JSON error body.
    @app.api_route(
        "/models/{model_id:path}",
        methods=["GET", "HEAD"],
        response_class=HTMLResponse,
    )
    async def model_detail(model_id: str) -> Response:
        cleaned = model_id.strip()
        maybe_base_model_id, separator, maybe_section = cleaned.rpartition("/")
        legacy_model_id = (
            maybe_base_model_id if separator and maybe_section in MODEL_SEO_SECTIONS else cleaned
        )
        legacy_target = LEGACY_MODEL_PAGE_REDIRECTS.get(legacy_model_id)
        if legacy_target:
            return RedirectResponse(url=legacy_target, status_code=301)
        if separator and maybe_section in MODEL_SEO_SECTIONS:
            body = public_model_section_html(settings, maybe_base_model_id, maybe_section)
            if body is None:
                return HTMLResponse(
                    public_model_not_found_html(settings, cleaned),
                    status_code=404,
                )
            return HTMLResponse(body)
        body = public_model_detail_html(settings, cleaned)
        if body is None:
            user_model = STORE.get_user_model(normalize_custom_model_id(cleaned))
            if (
                user_model is not None
                and user_model.enabled
                and user_model.status == "active"
            ):
                shape = user_model_public_shape(user_model)
                body = render_template(
                    "public/user_model_detail.html",
                    api_base_url=settings.api_base_url,
                    site_url=(
                        f"https://{settings.trusted_domain}/models/{user_model.id}"
                    ),
                    title=f"{user_model.name} | User-provided model",
                    heading=user_model.name,
                    description=shape["privacy_notice"],
                    robots_meta="noindex",
                    model=shape,
                    google_enabled=settings.google_oauth_enabled,
                    github_enabled=settings.github_oauth_enabled,
                    static_version=settings.release,
                )
        if body is None:
            return HTMLResponse(
                public_model_not_found_html(settings, cleaned),
                status_code=404,
            )
        return HTMLResponse(body)

    @app.get("/og.png")
    async def og_image() -> FileResponse:
        return FileResponse(
            path=OG_PNG_PATH,
            media_type="image/png",
            headers={"cache-control": "max-age=3600, public"},
        )

    @app.get("/favicon.ico")
    @app.head("/favicon.ico")
    async def favicon() -> FileResponse:
        return FileResponse(
            path=STATIC_DIR / "favicon.ico",
            media_type="image/x-icon",
            headers={"cache-control": "max-age=86400, public"},
        )

    @app.get("/trust/gcp-release.json")
    async def trust_release() -> JSONResponse:
        release = await resolved_trust_release()
        return JSONResponse(
            gcp_release(
                settings,
                release_metadata=release.metadata,
                release_metadata_status=release.status,
            ),
            status_code=trust_response_status(release.status),
            headers=trust_response_headers(release.status),
        )

    # AWS and Azure are deploy-time configured, so unlike the GCP record there
    # is nothing to resolve and no stale/live distinction to report. What there
    # IS is an unconfigured state, and that must not render as a measurement:
    # serve 503 so a verifier treats it as "no answer" rather than reading
    # "not-configured" as the value it should expect.
    def _static_release_response(payload: dict[str, Any], status: str = "embedded") -> JSONResponse:
        configured = payload["release_metadata_status"] != "not-configured"
        return JSONResponse(
            payload,
            status_code=200 if configured else 503,
            headers=trust_response_headers(status if configured else "unavailable"),
        )

    @app.get("/trust/aws-release.json")
    async def trust_release_aws() -> JSONResponse:
        metadata, status = await _mirrored(aws_release_resolver, embedded_aws_metadata)
        return _static_release_response(aws_release(settings, metadata=metadata), status)

    @app.get("/trust/azure-release.json")
    async def trust_release_azure() -> JSONResponse:
        metadata, status = await _mirrored(azure_release_resolver, embedded_azure_metadata)
        return _static_release_response(azure_release(settings, metadata=metadata), status)

    @app.get("/trust/pcr0-aws.txt")
    async def trust_pcr0_aws() -> PlainTextResponse:
        metadata, _ = await _mirrored(aws_release_resolver, embedded_aws_metadata)
        payload = aws_release(settings, metadata=metadata)
        configured = payload["release_metadata_status"] != "not-configured"
        return PlainTextResponse(
            "".join(f"{value}\n" for value in payload["accepted_pcr0s"]),
            status_code=200 if configured else 503,
            headers=trust_response_headers("embedded" if configured else "unavailable"),
        )

    @app.get("/trust/hostdata-azure.txt")
    async def trust_hostdata_azure() -> PlainTextResponse:
        metadata, _ = await _mirrored(azure_release_resolver, embedded_azure_metadata)
        payload = azure_release(settings, metadata=metadata)
        configured = payload["release_metadata_status"] != "not-configured"
        return PlainTextResponse(
            "".join(f"{value}\n" for value in payload["accepted_hostdata"]),
            status_code=200 if configured else 503,
            headers=trust_response_headers("embedded" if configured else "unavailable"),
        )

    @app.get("/trust/image-digest-gcp.txt")
    async def trust_digest() -> PlainTextResponse:
        release = await resolved_trust_release()
        return PlainTextResponse(
            f"{release.metadata['image_digest']}\n",
            status_code=trust_response_status(release.status),
            headers=trust_response_headers(release.status),
        )

    @app.get("/trust/image-reference-gcp.txt")
    async def trust_image_reference() -> PlainTextResponse:
        release = await resolved_trust_release()
        return PlainTextResponse(
            f"{release.metadata['image_reference']}\n",
            status_code=trust_response_status(release.status),
            headers=trust_response_headers(release.status),
        )


def _cached_status_page_response(
    settings: Settings,
    *,
    host: str,
    background_tasks: BackgroundTasks,
) -> Response:
    render_host = _status_render_host(settings, host)
    return _cached_public_response(
        settings,
        key=f"status:page:{render_host}",
        media_type="text/html",
        ttl_seconds=STATUS_RESPONSE_CACHE_SECONDS,
        stale_seconds=STATUS_RESPONSE_STALE_SECONDS,
        background_tasks=background_tasks,
        build=lambda: _status_page_html(settings, host=render_host).encode(),
    )


def _status_render_host(settings: Settings, host: str) -> str:
    """Collapse arbitrary Host headers to configured status/apex variants."""
    hostname = host.partition(":")[0].strip().lower()
    if is_status_hostname(settings, hostname):
        return hostname
    return settings.trusted_domain


def _cached_public_response(
    settings: Settings,
    *,
    key: str,
    media_type: str,
    ttl_seconds: int,
    stale_seconds: int,
    background_tasks: BackgroundTasks,
    build: Callable[[], bytes],
) -> Response:
    cache_control = _public_cache_control(ttl_seconds=ttl_seconds, stale_seconds=stale_seconds)
    if settings.environment == "test":
        return Response(
            content=build(),
            media_type=media_type,
            headers={"cache-control": cache_control, "x-tr-cache": "bypass"},
        )

    now = time.monotonic()
    with _STATUS_RESPONSE_CACHE_LOCK:
        cached = _STATUS_RESPONSE_CACHE.get(key)
        if cached is not None:
            age = now - cached.cached_at
            if age < ttl_seconds:
                _STATUS_RESPONSE_CACHE.move_to_end(key)
                return _cached_body_response(cached, cache_state="hit")
            if age < ttl_seconds + stale_seconds:
                _STATUS_RESPONSE_CACHE.move_to_end(key)
                _schedule_cached_response_refresh(
                    key=key,
                    media_type=media_type,
                    cache_control=cache_control,
                    build=build,
                    background_tasks=background_tasks,
                )
                return _cached_body_response(cached, cache_state="stale")

    body = build()
    cached = _CachedPublicBody(
        cached_at=time.monotonic(),
        body=body,
        media_type=media_type,
        cache_control=cache_control,
    )
    with _STATUS_RESPONSE_CACHE_LOCK:
        _STATUS_RESPONSE_CACHE[key] = cached
        _STATUS_RESPONSE_CACHE.move_to_end(key)
        while len(_STATUS_RESPONSE_CACHE) > PUBLIC_RESPONSE_CACHE_MAX_ENTRIES:
            _STATUS_RESPONSE_CACHE.popitem(last=False)
    return _cached_body_response(cached, cache_state="miss")


def _schedule_cached_response_refresh(
    *,
    key: str,
    media_type: str,
    cache_control: str,
    build: Callable[[], bytes],
    background_tasks: BackgroundTasks,
) -> None:
    _ = background_tasks
    with _STATUS_RESPONSE_CACHE_LOCK:
        if key in _STATUS_RESPONSE_REFRESHING:
            return
        if not _STATUS_RESPONSE_REFRESH_SLOTS.acquire(blocking=False):
            return
        _STATUS_RESPONSE_REFRESHING.add(key)
    refresh_thread = threading.Thread(
        target=_refresh_cached_response,
        args=(key, media_type, cache_control, build),
        daemon=True,
    )
    try:
        refresh_thread.start()
    except Exception:
        with _STATUS_RESPONSE_CACHE_LOCK:
            _STATUS_RESPONSE_REFRESHING.discard(key)
        _STATUS_RESPONSE_REFRESH_SLOTS.release()
        raise


def _refresh_cached_response(
    key: str,
    media_type: str,
    cache_control: str,
    build: Callable[[], bytes],
) -> None:
    try:
        body = build()
        with _STATUS_RESPONSE_CACHE_LOCK:
            _STATUS_RESPONSE_CACHE[key] = _CachedPublicBody(
                cached_at=time.monotonic(),
                body=body,
                media_type=media_type,
                cache_control=cache_control,
            )
            _STATUS_RESPONSE_CACHE.move_to_end(key)
            while len(_STATUS_RESPONSE_CACHE) > PUBLIC_RESPONSE_CACHE_MAX_ENTRIES:
                _STATUS_RESPONSE_CACHE.popitem(last=False)
    except Exception:
        log.exception("public_cache_refresh_failed key=%s", key)
    finally:
        with _STATUS_RESPONSE_CACHE_LOCK:
            _STATUS_RESPONSE_REFRESHING.discard(key)
        _STATUS_RESPONSE_REFRESH_SLOTS.release()


def _cached_body_response(cached: _CachedPublicBody, *, cache_state: str) -> Response:
    return Response(
        content=cached.body,
        media_type=cached.media_type,
        headers={
            "cache-control": cached.cache_control,
            "x-tr-cache": cache_state,
        },
    )


def _public_cache_control(*, ttl_seconds: int, stale_seconds: int) -> str:
    browser_ttl = min(ttl_seconds, 15)
    return (
        f"public, max-age={browser_ttl}, s-maxage={ttl_seconds}, "
        f"stale-while-revalidate={stale_seconds}"
    )


def _json_body(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()


def _status_history_payload(window: str) -> dict[str, Any]:
    return history_payload(_status_samples(hours=1), window, rollups=_status_rollups(window))


def _wants_history_html(request: Request, *, explicit_format: str | None) -> bool:
    if explicit_format == "html":
        return True
    if explicit_format == "json":
        return False
    accept = request.headers.get("accept", "")
    return "text/html" in accept


def _wants_html(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    if not accept or accept == "*/*":
        return True
    if "text/html" in accept:
        return True
    return "application/json" not in accept


def _status_history_page_html(
    settings: Settings,
    *,
    host: str,
    window: str,
    history: dict[str, Any],
) -> str:
    hostname = host.split(":", 1)[0].lower()
    domain = control_domain_for_hostname(settings, hostname)
    status_hostname = status_hostname_for_domain(domain)
    site_url = (
        f"https://{status_hostname}/status/history?window={window}"
        if is_status_hostname(settings, hostname)
        else f"https://{settings.trusted_domain}/status/history?window={window}"
    )
    title = {
        "48h": "48 hour Status History | TrustedRouter",
        "monthly": "Monthly Status History | TrustedRouter",
        "daily": "Daily Status History | TrustedRouter",
        "24h": "24 hour Status History | TrustedRouter",
        "5m": "Current Status History | TrustedRouter",
    }[window]
    heading = {
        "48h": "48 hour status history",
        "monthly": "Monthly status history",
        "daily": "Daily status history",
        "24h": "24 hour status history",
        "5m": "Current status history",
    }[window]
    return render_template(
        "public/status_history.html",
        api_base_url=settings.api_base_url,
        site_url=site_url,
        canonical_url=canonical_public_url(
            settings,
            "/status/history",
            query=f"window={window}",
        ),
        title=title,
        heading=heading,
        description=(
            "Explore TrustedRouter uptime history from metadata-only synthetic checks, with visual "
            "rollups for gateway health, attestation, SDK requests, billing, fallback, and regions."
        ),
        google_enabled=settings.google_oauth_enabled,
        github_enabled=settings.github_oauth_enabled,
        static_version=settings.release,
        snapshot=_status_snapshot(settings),
        history=history,
        window=window,
        json_url=f"/status/history?window={window}&format=json",
    )


def _leaderboard_snapshot(settings: Settings) -> dict[str, Any]:
    global _LEADERBOARD_CACHE
    now = time.monotonic()
    if settings.environment != "test" and _LEADERBOARD_CACHE is not None:
        cached_at, payload = _LEADERBOARD_CACHE
        if now - cached_at < LEADERBOARD_SNAPSHOT_CACHE_SECONDS:
            return payload
    if settings.environment != "test":
        try:
            precomputed = _precomputed_public_analytics_snapshot("leaderboard")
        except Exception:
            log.exception("public_analytics_snapshot_read_failed name=leaderboard")
            if _LEADERBOARD_CACHE is not None:
                return _LEADERBOARD_CACHE[1]
        else:
            if precomputed is not None:
                _LEADERBOARD_CACHE = (now, precomputed)
                return precomputed
    try:
        samples = public_benchmark_samples(
            limit=LEADERBOARD_SAMPLE_LIMIT,
            recent_minutes=LEADERBOARD_RECENT_WINDOW_MINUTES,
        )
        payload = aggregate_leaderboard(
            samples,
            min_samples=LEADERBOARD_MIN_SAMPLES,
            model_rank_min_samples=LEADERBOARD_MODEL_RANK_MIN_SAMPLES,
            provider_rank_min_samples=LEADERBOARD_PROVIDER_RANK_MIN_SAMPLES,
            rank_min_ttft_samples=LEADERBOARD_RANK_MIN_TTFT_SAMPLES,
        )
        payload["rank_minimums"] = {
            "model_availability_samples": LEADERBOARD_MODEL_RANK_MIN_SAMPLES,
            "provider_availability_samples": LEADERBOARD_PROVIDER_RANK_MIN_SAMPLES,
            "ttft_samples": LEADERBOARD_RANK_MIN_TTFT_SAMPLES,
        }
        payload["generated_at"] = utcnow().isoformat().replace("+00:00", "Z")
        payload["sample_window_count"] = len(samples)
        payload["sample_limit"] = LEADERBOARD_SAMPLE_LIMIT
        payload["window_label"] = (
            f"rolling benchmark set of up to {LEADERBOARD_SAMPLE_LIMIT:,} samples"
        )
    except Exception:
        if settings.environment != "test" and _LEADERBOARD_CACHE is not None:
            log.exception("leaderboard_live_fallback_failed_serving_stale")
            return _LEADERBOARD_CACHE[1]
        raise
    if settings.environment != "test":
        _LEADERBOARD_CACHE = (now, payload)
    return payload


def _video_leaderboard_snapshot(settings: Settings) -> dict[str, Any]:
    global _VIDEO_LEADERBOARD_CACHE
    now = time.monotonic()
    if settings.environment != "test" and _VIDEO_LEADERBOARD_CACHE is not None:
        cached_at, payload = _VIDEO_LEADERBOARD_CACHE
        if now - cached_at < LEADERBOARD_SNAPSHOT_CACHE_SECONDS:
            return payload
    if settings.environment != "test":
        try:
            precomputed = _precomputed_public_analytics_snapshot("video_leaderboard")
        except Exception:
            log.exception("public_analytics_snapshot_read_failed name=video_leaderboard")
            if _VIDEO_LEADERBOARD_CACHE is not None:
                return _VIDEO_LEADERBOARD_CACHE[1]
        else:
            if precomputed is not None:
                _VIDEO_LEADERBOARD_CACHE = (now, precomputed)
                return precomputed
    try:
        samples = public_video_benchmark_samples(
            limit=VIDEO_LEADERBOARD_SAMPLE_LIMIT,
            recent_minutes=VIDEO_LEADERBOARD_RECENT_WINDOW_MINUTES,
        )
        configured_routes = {
            (endpoint.provider, model.id)
            for model in MODELS.values()
            if model.supports_video
            for endpoint in endpoints_for_model(model.id)
        }
        payload = aggregate_video_leaderboard(
            samples,
            configured_routes=configured_routes,
        )
        payload["generated_at"] = utcnow().isoformat().replace("+00:00", "Z")
        payload["sample_window_count"] = len(samples)
        payload["sample_limit"] = VIDEO_LEADERBOARD_SAMPLE_LIMIT
        payload["window_label"] = (
            f"rolling video benchmark set of up to {VIDEO_LEADERBOARD_SAMPLE_LIMIT:,} jobs"
        )
    except Exception:
        if settings.environment != "test" and _VIDEO_LEADERBOARD_CACHE is not None:
            log.exception("video_leaderboard_live_fallback_failed_serving_stale")
            return _VIDEO_LEADERBOARD_CACHE[1]
        raise
    if settings.environment != "test":
        _VIDEO_LEADERBOARD_CACHE = (now, payload)
    return payload


def _apps_snapshot(settings: Settings) -> dict[str, Any]:
    """Cached self-reported app directory, aggregated from the same recent
    benchmark sample set as the leaderboard (no per-view live reads)."""
    global _APPS_CACHE
    now = time.monotonic()
    if settings.environment != "test" and _APPS_CACHE is not None:
        cached_at, payload = _APPS_CACHE
        if now - cached_at < STATUS_SNAPSHOT_CACHE_SECONDS:
            return payload
    if settings.environment != "test":
        try:
            precomputed = _precomputed_public_analytics_snapshot("apps")
        except Exception:
            log.exception("public_analytics_snapshot_read_failed name=apps")
            if _APPS_CACHE is not None:
                return _APPS_CACHE[1]
        else:
            if precomputed is not None:
                _APPS_CACHE = (now, precomputed)
                return precomputed
    samples = public_benchmark_samples(
        limit=LEADERBOARD_SAMPLE_LIMIT,
        recent_minutes=LEADERBOARD_RECENT_WINDOW_MINUTES,
    )
    payload = aggregate_apps(samples)
    payload["generated_at"] = utcnow().isoformat().replace("+00:00", "Z")
    if settings.environment != "test":
        _APPS_CACHE = (now, payload)
    return payload


def _precomputed_public_analytics_snapshot(name: str) -> dict[str, Any] | None:
    reader = getattr(STORE, "public_analytics_snapshot", None)
    if not callable(reader):
        return None
    return current_public_analytics_snapshot(
        name,
        reader=reader,
        max_age_seconds=(2_147_483_647 if name == "client_reliability" else 600),
    )


def _merge_client_observed_status(
    payload: dict[str, Any],
    *,
    settings: Settings,
) -> dict[str, Any]:
    snapshot = None
    if settings.environment != "test":
        try:
            snapshot = _precomputed_public_analytics_snapshot("client_reliability")
        except Exception:
            log.exception("public_analytics_snapshot_read_failed name=client_reliability")
    result = dict(payload)
    result["client_observed"] = client_observed_status_section(
        snapshot,
        now=dt.datetime.now(dt.UTC),
    )
    return result


def _merge_analytics_status(payload: dict[str, Any]) -> dict[str, Any]:
    """Publish this cloud's operational-analytics drain lag.

    One wiring covers every deployment because every deployment runs this
    codebase; that is the property the fleet check in
    :mod:`clickhouse.check_fleet_analytics_freshness` relies on, and the reason
    the registry can insist that no cloud is missing the section.

    The key is written unconditionally. Omitting it on failure would be the
    original bug in a new place: the checker reads a missing section as "this
    deployment does not publish drain lag", and a section that disappears
    whenever the database is unhappy is a signal that is quietest exactly when
    it matters. A read failure publishes `available: false` with a reason
    instead, and never the last good number -- a stale-but-plausible lag is
    indistinguishable from a healthy one.

    Runs inside `_status_snapshot`, so it is behind `STATUS_SNAPSHOT_CACHE_SECONDS`
    and costs one index seek per cache miss rather than one per request.
    """
    reading = None
    try:
        # Called through the declared `Store` surface rather than a getattr
        # probe, so a backend that stops implementing it fails mypy instead of
        # silently degrading every cloud's status page to "unreachable".
        reading = STORE.operational_analytics_outbox_freshness()
    except Exception:
        log.exception("operational_analytics_outbox_freshness_read_failed")
    result = dict(payload)
    result[ANALYTICS_STATUS_KEY] = analytics_status_from_reading(
        reading,
        now=dt.datetime.now(dt.UTC),
    )
    return result


def _status_snapshot(settings: Settings) -> dict[str, Any]:
    global _STATUS_CACHE
    now = time.monotonic()
    if settings.environment != "test" and _STATUS_CACHE is not None:
        cached_at, payload = _STATUS_CACHE
        if now - cached_at < STATUS_SNAPSHOT_CACHE_SECONDS:
            return payload
    if settings.environment != "test":
        try:
            precomputed = _precomputed_public_analytics_snapshot("status_inputs")
        except Exception:
            log.exception("public_analytics_snapshot_read_failed name=status_inputs")
            if _STATUS_CACHE is not None:
                return _STATUS_CACHE[1]
        else:
            if precomputed is not None:
                try:
                    samples = [
                        SyntheticProbeSample(**row)
                        for row in precomputed.get("samples", [])
                        if isinstance(row, dict)
                    ]
                    rollups = [
                        SyntheticRollup(**row)
                        for row in precomputed.get("rollups", [])
                        if isinstance(row, dict)
                    ]
                    payload = status_snapshot(samples, rollups=rollups, settings=settings)
                except (TypeError, ValueError):
                    log.exception("public_analytics_snapshot_invalid name=status_inputs")
                else:
                    payload = _merge_client_observed_status(payload, settings=settings)
                    payload = _merge_analytics_status(payload)
                    _STATUS_CACHE = (now, payload)
                    return payload
    # Keep the fallback bounded. Current state and headline latency come from
    # a small live sample window; 24h/48h history comes from compact rollups.
    try:
        payload = status_snapshot(
            _status_samples(hours=1),
            rollups=_status_rollups("snapshot"),
            settings=settings,
        )
    except Exception:
        if settings.environment != "test" and _STATUS_CACHE is not None:
            log.exception("status_live_fallback_failed_serving_stale")
            return _STATUS_CACHE[1]
        raise
    payload = _merge_client_observed_status(payload, settings=settings)
    payload = _merge_analytics_status(payload)
    if settings.environment != "test":
        _STATUS_CACHE = (now, payload)
    return payload


async def _fleet_snapshot_cached(settings: Settings) -> dict[str, Any]:
    global _FLEET_CACHE

    def fresh() -> dict[str, Any] | None:
        if settings.environment != "test" and _FLEET_CACHE is not None:
            cached_at, payload = _FLEET_CACHE
            if time.monotonic() - cached_at < FLEET_SNAPSHOT_CACHE_SECONDS:
                return payload
        return None

    cached = fresh()
    if cached is not None:
        return cached
    # Single-flight: /fleet.json is public and its miss path fans out to
    # every peer cloud, so concurrent misses must coalesce into one rebuild
    # rather than one cross-cloud fetch storm per request.
    async with _FLEET_CACHE_LOCK:
        cached = fresh()
        if cached is not None:
            return cached
        payload = await fleet_snapshot(settings)
        if settings.environment != "test":
            _FLEET_CACHE = (time.monotonic(), payload)
        return payload


_FLEET_STATUS_COLORS = {
    "up": "#2e9e5b",
    "degraded": "#d99a2b",
    "routing_degraded": "#d99a2b",
    "trust_degraded": "#c2542e",
    "down": "#c43d3d",
    "unreachable": "#c43d3d",
    "unknown": "#8a8f98",
}


def _fleet_page_html(snapshot: dict[str, Any]) -> str:
    """Minimal server-rendered fleet page: one row per deployment, one row
    per scheduler heartbeat. Deliberately dependency-free and compact — this
    is the operator's "where do I look next" page, not a second status page.
    """

    def color(status: str) -> str:
        return _FLEET_STATUS_COLORS.get(status, "#8a8f98")

    def pill(status: str) -> str:
        label = html.escape(status.replace("_", " "))
        return (
            f'<span style="background:{color(status)};color:#fff;'
            'border-radius:9px;padding:2px 10px;font-size:13px;">'
            f"{label}</span>"
        )

    rows = []
    for row in snapshot.get("deployments", []):
        status = str(row.get("overall_status") or "unknown")
        components = row.get("components") or {}
        bad = {key: value for key, value in components.items() if value not in ("up", "unknown")}
        detail = (
            ", ".join(f"{key}: {value}" for key, value in sorted(bad.items()))
            if bad
            else "all components up"
        )
        stale = row.get("monitor_stale")
        monitor = "stale" if stale else ("fresh" if stale is not None else "n/a")
        rows.append(
            "<tr>"
            f'<td><a href="{html.escape(str(row.get("url") or ""))}">'
            f"{html.escape(str(row.get('name') or ''))}</a></td>"
            f"<td>{pill(status)}</td>"
            f"<td>{html.escape(str(row.get('headline') or ''))}</td>"
            f"<td>{html.escape(monitor)}</td>"
            f"<td>{html.escape(detail)}</td>"
            "</tr>"
        )
    beats = []
    for beat in snapshot.get("heartbeats", []):
        age = beat.get("age_seconds")
        beats.append(
            "<tr>"
            f"<td>{html.escape(str(beat.get('name') or ''))}</td>"
            f"<td>{pill('down' if beat.get('stale') else 'up')}</td>"
            f"<td>{html.escape(str(age) + 's' if age is not None else 'unknown')}</td>"
            f"<td>{html.escape(str(beat.get('last_beat_at') or ''))}</td>"
            "</tr>"
        )
    remediator = snapshot.get("remediator") or {}
    decisions = []
    for row in remediator.get("decisions") or []:
        decisions.append(
            "<tr>"
            f"<td>{html.escape(str(row.get('at') or ''))}</td>"
            f"<td>{html.escape(str(row.get('decision') or ''))}</td>"
            f"<td>{html.escape(str(row.get('detail') or ''))}</td>"
            "</tr>"
        )
    overall = str(snapshot.get("fleet_overall_status") or "unknown")
    table_css = "width:100%;border-collapse:collapse;margin:12px 0 28px"
    cell_css = (
        "th,td{text-align:left;padding:8px 12px;border-bottom:1px solid #2a2f3a;"
        "font-size:14px}th{color:#8a8f98;font-weight:600}"
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>TrustedRouter Fleet</title>
<style>
body{{background:#0f1218;color:#e6e9ef;font:15px/1.5 -apple-system,'Segoe UI',sans-serif;
margin:0;padding:32px;max-width:1080px;margin-inline:auto}}
a{{color:#7ab7ff;text-decoration:none}} {cell_css}
h1{{font-size:20px;margin:0 0 4px}} h2{{font-size:16px;color:#8a8f98;margin:28px 0 0}}
</style></head><body>
<h1>TrustedRouter Fleet {pill(overall)}</h1>
<div style="color:#8a8f98;font-size:13px">generated {html.escape(str(snapshot.get("generated_at") or ""))}
 &middot; served by every control plane &middot; <a href="/fleet.json">fleet.json</a></div>
<h2>Deployments</h2>
<table style="{table_css}"><tr><th>deployment</th><th>status</th><th>headline</th>
<th>monitor</th><th>attention</th></tr>{"".join(rows)}</table>
<h2>Scheduler heartbeats (this deployment)</h2>
<table style="{table_css}"><tr><th>job</th><th>liveness</th><th>age</th><th>last beat</th></tr>
{"".join(beats) or '<tr><td colspan="4">no heartbeats recorded yet</td></tr>'}</table>
<h2>Remediator (mode: {html.escape(str(remediator.get("mode") or "off"))})</h2>
<table style="{table_css}"><tr><th>at</th><th>decision</th><th>detail</th></tr>
{"".join(decisions) or '<tr><td colspan="3">no decisions recorded — nothing needed fixing</td></tr>'}</table>
</body></html>"""


def _compact_status_json(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Remove tooltip-only duplication from the machine-readable status feed."""
    payload = dict(snapshot)
    compact_components: list[dict[str, Any]] = []
    for component in snapshot.get("components", []):
        compact_component = dict(component)
        compact_component["history"] = [
            {key: value for key, value in bucket.items() if key != "latency_breakdown"}
            for bucket in component.get("history", [])
        ]
        compact_components.append(compact_component)
    payload["components"] = compact_components
    if "client_observed" in snapshot:
        payload["client_observed"] = snapshot["client_observed"]
    # Carried through explicitly. This function is what /status.json actually
    # serves, and the fleet freshness check fails a cloud whose payload has no
    # `analytics` key -- so dropping it here would look exactly like a cloud
    # running code too old to publish drain lag.
    if ANALYTICS_STATUS_KEY in snapshot:
        payload[ANALYTICS_STATUS_KEY] = snapshot[ANALYTICS_STATUS_KEY]
    return payload


def _status_samples(*, hours: int = 48) -> list[Any]:
    if hours <= 1:
        return STORE.synthetic_probe_samples(limit=STATUS_LIVE_SAMPLE_LIMIT)
    samples = []
    for date in _dates_covering_recent_hours(hours=hours):
        samples.extend(
            STORE.synthetic_probe_samples(date=date, limit=STATUS_RAW_SAMPLE_LIMIT_PER_DAY)
        )
    deduped = {sample.id: sample for sample in samples}
    return sorted(deduped.values(), key=lambda sample: sample.created_at, reverse=True)


def _status_rollups(window: str) -> list[Any]:
    now = utcnow()
    if window == "snapshot":
        return [
            *STORE.synthetic_rollups(
                period="hour",
                since=_hour_rollup_since(now, hours=48),
                limit=STATUS_HOUR_ROLLUP_LIMIT,
            ),
        ]
    if window in {"24h", "48h"}:
        return STORE.synthetic_rollups(
            period="hour",
            since=_hour_rollup_since(now, hours=24 if window == "24h" else 48),
            limit=STATUS_HOUR_ROLLUP_LIMIT,
        )
    if window == "daily":
        return STORE.synthetic_rollups(
            period="day",
            since=_day_rollup_since(now, months=STATUS_ROLLUP_RETENTION_MONTHS),
            limit=STATUS_DAY_ROLLUP_LIMIT,
        )
    if window == "monthly":
        return STORE.synthetic_rollups(
            period="day",
            since=_day_rollup_since(now, months=STATUS_ROLLUP_RETENTION_MONTHS),
            include_histograms=False,
            limit=STATUS_MONTH_ROLLUP_LIMIT,
        )
    return []


def _hour_rollup_since(now: dt.datetime, *, hours: int) -> str:
    base = now.astimezone(dt.UTC).replace(minute=0, second=0, microsecond=0)
    return _iso_utc(base - dt.timedelta(hours=max(hours - 1, 0)))


def _day_rollup_since(now: dt.datetime, *, months: int) -> str:
    return _iso_utc(_month_floor(now, months=months))


def _month_rollup_since(now: dt.datetime, *, months: int) -> str:
    return _iso_utc(_month_floor(now, months=months))


def _month_floor(now: dt.datetime, *, months: int) -> dt.datetime:
    current = now.astimezone(dt.UTC)
    month_index = current.year * 12 + current.month - 1
    cutoff_index = month_index - max(months - 1, 0)
    year, zero_based_month = divmod(cutoff_index, 12)
    return dt.datetime(year, zero_based_month + 1, 1, tzinfo=dt.UTC)


def _iso_utc(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _dates_covering_recent_hours(*, hours: int) -> list[str]:
    now = utcnow()
    cutoff = now - dt.timedelta(hours=hours)
    dates = []
    current = cutoff.date()
    while current <= now.date():
        dates.append(current.isoformat())
        current += dt.timedelta(days=1)
    return dates


def _status_page_html(settings: Settings, *, host: str) -> str:
    hostname = host.split(":", 1)[0].lower()
    domain = control_domain_for_hostname(settings, hostname)
    site_url = (
        f"https://{status_hostname_for_domain(domain)}/"
        if is_status_hostname(settings, hostname)
        else f"https://{settings.trusted_domain}/status"
    )
    snapshot = _status_snapshot(settings)
    # Measured upstream-provider health from the rotation-probe / organic
    # benchmark samples. Informational provider watch — intentionally NOT part
    # of the router-core paging SLO above (a flaky upstream model must not page
    # router health), but surfaced here so provider errors are visible.
    leaderboard = _leaderboard_snapshot(settings)
    provider_health = sorted(
        (
            provider
            for provider in leaderboard.get("providers", [])
            if provider.get("sample_count", 0) > 0
        ),
        key=lambda p: (
            p.get("error_rate", 0.0),
            0 if p.get("p50_ttft_ms") is not None else 1,
            p.get("p50_ttft_ms") or 0,
            p.get("provider", ""),
        ),
    )
    return render_template(
        "public/status.html",
        api_base_url=(
            f"https://api.{domain}/v1"
            if domain != settings.trusted_domain
            else settings.api_base_url
        ),
        site_url=site_url,
        canonical_url=canonical_public_url(settings, "/status"),
        title="Status | TrustedRouter",
        heading="TrustedRouter Status",
        description=(
            "Check TrustedRouter availability across regions with live gateway, attestation, SDK, "
            "billing, provider fallback, latency, incident history, and router-core uptime signals."
        ),
        google_enabled=settings.google_oauth_enabled,
        github_enabled=settings.github_oauth_enabled,
        static_version=settings.release,
        snapshot=snapshot,
        provider_health=provider_health,
        provider_health_window=leaderboard.get("window_label"),
    )
