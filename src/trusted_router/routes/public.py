from __future__ import annotations

import datetime as dt
import json
import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Query, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.types import Scope

from trusted_router.ai_iq import ai_iq_catalog_payload
from trusted_router.apps import aggregate_apps
from trusted_router.benchmark_samples import public_benchmark_samples
from trusted_router.catalog import (
    META_MODEL_IDS,
    MODELS,
    provider_to_openrouter_shape,
    providers_for_display,
)
from trusted_router.choose_catalog import choose_catalog_payload
from trusted_router.config import Settings
from trusted_router.dashboard import (
    MODEL_SEO_SECTIONS,
    STATIC_DIR,
    dashboard_html,
    docs_llms_full_txt,
    docs_llms_txt,
    hipaa_readiness_json,
    llms_txt,
    procurement_json,
    public_apps_html,
    public_baa_html,
    public_benchmarks_html,
    public_blog_index_html,
    public_blog_post_html,
    public_chat_html,
    public_dpa_html,
    public_fusion_html,
    public_hipaa_readiness_html,
    public_leaderboard_html,
    public_legal_html,
    public_model_compare_html,
    public_model_detail_html,
    public_model_not_found_html,
    public_model_section_html,
    public_models_html,
    public_page_html,
    public_privacy_html,
    public_provider_detail_html,
    public_provider_performance_html,
    public_providers_html,
    public_rankings_html,
    public_soc2_readiness_html,
    public_subprocessors_html,
    public_support_html,
    public_terms_html,
    robots_txt,
    sitemap_comparisons_xml,
    sitemap_core_xml,
    sitemap_models_xml,
    sitemap_providers_xml,
    sitemap_xml,
    soc2_readiness_json,
    subprocessors_json,
)
from trusted_router.og import OG_PNG_PATH
from trusted_router.services.email import EmailMessage, get_email_service
from trusted_router.storage import STORE
from trusted_router.storage_custom_models import normalize_custom_model_id
from trusted_router.storage_models import utcnow
from trusted_router.synthetic.leaderboard import aggregate_leaderboard
from trusted_router.synthetic.status import history_payload, status_snapshot
from trusted_router.trust import gcp_release, trust_html
from trusted_router.views import render_template

STATUS_SNAPSHOT_CACHE_SECONDS = 15
STATUS_RAW_SAMPLE_LIMIT_PER_DAY = 35_000
STATUS_LIVE_SAMPLE_LIMIT = 500
STATUS_HOUR_ROLLUP_LIMIT = 5_000
STATUS_DAY_ROLLUP_LIMIT = 25_000
STATUS_MONTH_ROLLUP_LIMIT = 50
STATUS_ROLLUP_RETENTION_MONTHS = 24
STATUS_RESPONSE_CACHE_SECONDS = 60
STATUS_RESPONSE_STALE_SECONDS = 600
STATUS_HISTORY_CACHE_SECONDS = 300
STATUS_HISTORY_STALE_SECONDS = 1_800
LEADERBOARD_SAMPLE_LIMIT = 5_000
LEADERBOARD_MIN_SAMPLES = 1
LEADERBOARD_RECENT_WINDOW_MINUTES = 180
LEADERBOARD_RESPONSE_CACHE_SECONDS = 60
LEADERBOARD_RESPONSE_STALE_SECONDS = 0
CHOOSE_PAGE_CACHE_SECONDS = 300
CHOOSE_PAGE_STALE_SECONDS = 86_400
CHOOSE_CATALOG_CACHE_SECONDS = 300
CHOOSE_CATALOG_STALE_SECONDS = 86_400
INDEXNOW_KEY = "360a02e48445d297f9612a4c3fef878b"
_STATUS_CACHE: tuple[float, dict[str, Any]] | None = None
_LEADERBOARD_CACHE: tuple[float, dict[str, Any]] | None = None
_APPS_CACHE: tuple[float, dict[str, Any]] | None = None
_STATUS_RESPONSE_CACHE: dict[str, _CachedPublicBody] = {}
_STATUS_RESPONSE_REFRESHING: set[str] = set()
_STATUS_RESPONSE_CACHE_LOCK = threading.RLock()


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
_INQUIRY_HITS: dict[str, list[float]] = {}
_INQUIRY_WINDOW_SECONDS = 3600.0
_INQUIRY_MAX_PER_WINDOW = 5
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _inquiry_rate_ok(client_ip: str, *, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    cutoff = now - _INQUIRY_WINDOW_SECONDS
    with _INQUIRY_RATE_LOCK:
        hits = [t for t in _INQUIRY_HITS.get(client_ip, ()) if t > cutoff]
        if len(hits) >= _INQUIRY_MAX_PER_WINDOW:
            _INQUIRY_HITS[client_ip] = hits
            return False
        hits.append(now)
        _INQUIRY_HITS[client_ip] = hits
        # Opportunistic cleanup so the dict can't grow unbounded.
        if len(_INQUIRY_HITS) > 4096:
            for key in [k for k, v in _INQUIRY_HITS.items() if not any(t > cutoff for t in v)]:
                _INQUIRY_HITS.pop(key, None)
    return True


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
            name, email, len(company or ""), len(message or ""),
        )
        # Full lead goes to first-party stderr logs only. This logger is outside
        # the trusted_router namespace and propagate=False keeps it off the
        # root-attached third-party Axiom handler.
        _leads_log.error(
            "trustedos_inquiry.lead recipient=%r name=%r email=%r company=%r message=%r",
            recipient, name, email, company, message,
        )
        return ok

    log.info(
        "trustedos_inquiry.received name=%r email=%r company_len=%d message_len=%d",
        name, email, len(company or ""), len(message or ""),
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
            EmailMessage(to=recipient, subject=subject, text_body=text_body)
        )
    except Exception:  # noqa: BLE001 - never surface mailer errors to the form
        sent = False
        log.exception(
            "trustedos_inquiry.send_failed name=%r email=%r company_len=%d message_len=%d",
            name, email, len(company or ""), len(message or ""),
        )
    if not sent:
        # send() returns False when SES is unconfigured or the recipient is
        # suppressed. Surface a metadata-only diagnostic so alerting sees the
        # delivery issue without logging submitted free text.
        log.error(
            "trustedos_inquiry.delivery_failed recipient=%r name=%r email=%r company_len=%d message_len=%d",
            recipient, name, email, len(company or ""), len(message or ""),
        )
        # Full lead goes to first-party stderr logs only. This logger is outside
        # the trusted_router namespace and propagate=False keeps it off the
        # root-attached third-party Axiom handler.
        _leads_log.error(
            "trustedos_inquiry.lead recipient=%r name=%r email=%r company=%r message=%r",
            recipient, name, email, company, message,
        )
    return ok


def register_public_routes(app: FastAPI, settings: Settings) -> None:
    app.mount("/static", _CachedStaticFiles(directory=STATIC_DIR), name="static")

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

    @public_html_route("/", include_slash=False)
    async def dashboard(request: Request, background_tasks: BackgroundTasks) -> Any:
        host = request.headers.get("host", "")
        hostname = host.split(":", 1)[0].lower()
        if hostname == "trust.trustedrouter.com":
            return trust_html(settings)
        if hostname == "status.trustedrouter.com":
            return _cached_status_page_response(
                settings,
                host=hostname,
                background_tasks=background_tasks,
            )
        if hostname == "eu.trustedrouter.com":
            return public_page_html(settings, "eu", site_url="https://eu.trustedrouter.com/")
        return dashboard_html(settings)

    @public_html_route("/trust")
    async def trust_page() -> str:
        return trust_html(settings)

    @public_html_route("/compare/openrouter")
    async def compare_openrouter() -> str:
        return public_page_html(settings, "compare/openrouter")

    @public_html_route("/compare/vercel-ai-gateway")
    async def compare_vercel_ai_gateway() -> str:
        return public_page_html(settings, "compare/vercel-ai-gateway")

    @public_html_route("/compare/litellm")
    async def compare_litellm() -> str:
        return public_page_html(settings, "compare/litellm")

    @public_html_route("/docs/migrate-from-openrouter")
    async def migrate_from_openrouter() -> str:
        return public_page_html(settings, "docs/migrate-from-openrouter")

    @public_html_route("/docs/agent-setup")
    async def agent_setup() -> str:
        return public_page_html(settings, "docs/agent-setup")

    @public_html_route("/docs/tagging")
    async def tagging_docs() -> str:
        return public_page_html(settings, "docs/tagging")

    @public_html_route("/docs/mcp")
    async def mcp_docs() -> str:
        return public_page_html(settings, "docs/mcp")

    @public_html_route("/docs/evals")
    async def evals() -> str:
        return public_page_html(settings, "docs/evals")

    @public_html_route("/docs/synth")
    async def synth_docs() -> str:
        return public_page_html(settings, "docs/synth")

    @public_html_route("/docs/fusion")
    async def fusion_docs() -> str:
        return public_page_html(settings, "docs/synth")

    @public_html_route("/docs/x402")
    async def x402_docs() -> str:
        return public_page_html(settings, "docs/x402")

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

    @public_html_route("/apps")
    async def apps() -> str:
        return public_apps_html(settings, apps=_apps_snapshot(settings))

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
            return HTMLResponse(public_page_html(settings, "docs"), status_code=404)
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

    @public_html_route("/terms")
    async def terms() -> str:
        return public_terms_html(settings)

    @public_html_route("/support")
    async def support() -> str:
        return public_support_html(settings)

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
            headers={"cache-control": "public, max-age=300, s-maxage=3600"},
        )

    @app.api_route("/docs/llms.txt", methods=["GET", "HEAD"], response_class=PlainTextResponse)
    async def docs_llms() -> PlainTextResponse:
        return PlainTextResponse(
            docs_llms_txt(settings),
            headers={"cache-control": "public, max-age=300, s-maxage=3600"},
        )

    @app.api_route(
        "/docs/llms-full.txt",
        methods=["GET", "HEAD"],
        response_class=PlainTextResponse,
    )
    async def docs_llms_full() -> PlainTextResponse:
        return PlainTextResponse(
            docs_llms_full_txt(settings),
            headers={"cache-control": "public, max-age=300, s-maxage=3600"},
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
        return _cached_public_response(
            settings,
            key=f"leaderboard:page:{request.headers.get('host', '')}",
            media_type="text/html",
            ttl_seconds=LEADERBOARD_RESPONSE_CACHE_SECONDS,
            stale_seconds=LEADERBOARD_RESPONSE_STALE_SECONDS,
            background_tasks=background_tasks,
            build=lambda: public_leaderboard_html(
                settings, _leaderboard_snapshot(settings)
            ).encode(),
        )

    @app.get("/status.json")
    async def status_json(background_tasks: BackgroundTasks) -> Response:
        return _cached_public_response(
            settings,
            key="status:json",
            media_type="application/json",
            ttl_seconds=STATUS_RESPONSE_CACHE_SECONDS,
            stale_seconds=STATUS_RESPONSE_STALE_SECONDS,
            background_tasks=background_tasks,
            build=lambda: _json_body({"data": _status_snapshot(settings)}),
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
        return _cached_public_response(
            settings,
            key=f"status:history:{window}:html:{request.headers.get('host', '')}",
            media_type="text/html",
            ttl_seconds=STATUS_HISTORY_CACHE_SECONDS,
            stale_seconds=STATUS_HISTORY_STALE_SECONDS,
            background_tasks=background_tasks,
            build=lambda: _status_history_page_html(
                settings,
                host=request.headers.get("host", ""),
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

    @public_html_route("/compare/models/{left_author}/{left_slug}/vs/{right_author}/{right_slug}")
    async def model_compare(
        left_author: str,
        left_slug: str,
        right_author: str,
        right_slug: str,
    ) -> HTMLResponse:
        left_id = f"{left_author.strip()}/{left_slug.strip()}"
        right_id = f"{right_author.strip()}/{right_slug.strip()}"
        body = public_model_compare_html(settings, left_id, right_id)
        if body is None:
            return HTMLResponse(
                public_model_not_found_html(settings, f"{left_id}/vs/{right_id}"),
                status_code=404,
            )
        return HTMLResponse(body)

    @public_html_route("/chat")
    async def chat() -> str:
        return public_chat_html(settings)

    @public_html_route("/user-chat")
    async def user_chat(model: str = Query(..., min_length=1)) -> str:
        locked_model_id = normalize_custom_model_id(model)
        return public_chat_html(
            settings,
            locked_model_id=locked_model_id,
            locked_model_label="Custom model",
        )

    @public_html_route("/synth")
    async def synth() -> str:
        return public_fusion_html(settings)

    @public_html_route("/fusion")
    async def fusion() -> str:
        return public_fusion_html(settings)

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
    async def model_detail(model_id: str) -> HTMLResponse:
        cleaned = model_id.strip()
        maybe_base_model_id, separator, maybe_section = cleaned.rpartition("/")
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
        return JSONResponse(gcp_release(settings), headers={"cache-control": "max-age=60, public"})

    @app.get("/trust/image-digest-gcp.txt")
    async def trust_digest() -> PlainTextResponse:
        return PlainTextResponse(
            f"{settings.trust_gcp_image_digest or 'not-configured'}\n",
            headers={"cache-control": "max-age=60, public"},
        )

    @app.get("/trust/image-reference-gcp.txt")
    async def trust_image_reference() -> PlainTextResponse:
        return PlainTextResponse(
            f"{settings.trust_gcp_image_reference or 'not-configured'}\n",
            headers={"cache-control": "max-age=60, public"},
        )


def _cached_status_page_response(
    settings: Settings,
    *,
    host: str,
    background_tasks: BackgroundTasks,
) -> Response:
    return _cached_public_response(
        settings,
        key=f"status:page:{host}",
        media_type="text/html",
        ttl_seconds=STATUS_RESPONSE_CACHE_SECONDS,
        stale_seconds=STATUS_RESPONSE_STALE_SECONDS,
        background_tasks=background_tasks,
        build=lambda: _status_page_html(settings, host=host).encode(),
    )


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
                return _cached_body_response(cached, cache_state="hit")
            if age < ttl_seconds + stale_seconds:
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
        _STATUS_RESPONSE_REFRESHING.add(key)
    refresh_thread = threading.Thread(
        target=_refresh_cached_response,
        args=(key, media_type, cache_control, build),
        daemon=True,
    )
    refresh_thread.start()


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
    finally:
        with _STATUS_RESPONSE_CACHE_LOCK:
            _STATUS_RESPONSE_REFRESHING.discard(key)


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
    site_url = (
        f"https://status.trustedrouter.com/status/history?window={window}"
        if hostname == "status.trustedrouter.com"
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
        title=title,
        heading=heading,
        description="Visual rollups from metadata synthetic checks.",
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
        if now - cached_at < STATUS_SNAPSHOT_CACHE_SECONDS:
            return payload
    samples = public_benchmark_samples(
        limit=LEADERBOARD_SAMPLE_LIMIT,
        recent_minutes=LEADERBOARD_RECENT_WINDOW_MINUTES,
    )
    payload = aggregate_leaderboard(samples, min_samples=LEADERBOARD_MIN_SAMPLES)
    payload["generated_at"] = utcnow().isoformat().replace("+00:00", "Z")
    payload["window_label"] = f"{LEADERBOARD_SAMPLE_LIMIT:,}-sample benchmark set"
    if settings.environment != "test":
        _LEADERBOARD_CACHE = (now, payload)
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
    samples = public_benchmark_samples(
        limit=LEADERBOARD_SAMPLE_LIMIT,
        recent_minutes=LEADERBOARD_RECENT_WINDOW_MINUTES,
    )
    payload = aggregate_apps(samples)
    payload["generated_at"] = utcnow().isoformat().replace("+00:00", "Z")
    if settings.environment != "test":
        _APPS_CACHE = (now, payload)
    return payload


def _status_snapshot(settings: Settings) -> dict[str, Any]:
    global _STATUS_CACHE
    now = time.monotonic()
    if settings.environment != "test" and _STATUS_CACHE is not None:
        cached_at, payload = _STATUS_CACHE
        if now - cached_at < STATUS_SNAPSHOT_CACHE_SECONDS:
            return payload
    # Keep the public status hot path bounded: current state and headline
    # latency come from a small live sample window, while 24h/48h/monthly
    # history comes from compact rollups precomputed when the monitor writes
    # each sample. Do not scan raw 48h/day Bigtable rows on page load.
    payload = status_snapshot(_status_samples(hours=1), rollups=_status_rollups("snapshot"))
    if settings.environment != "test":
        _STATUS_CACHE = (now, payload)
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
    site_url = (
        "https://status.trustedrouter.com/"
        if hostname == "status.trustedrouter.com"
        else f"https://{settings.trusted_domain}/status"
    )
    snapshot = _status_snapshot(settings)
    # Measured upstream-provider health from the rotation-probe / organic
    # benchmark samples. Informational provider watch — intentionally NOT part
    # of the router-core paging SLO above (a flaky upstream model must not page
    # router health), but surfaced here so provider errors are visible.
    leaderboard = _leaderboard_snapshot(settings)
    provider_health = sorted(
        leaderboard.get("providers", []),
        key=lambda p: (-p.get("error_rate", 0.0), p.get("provider", "")),
    )
    return render_template(
        "public/status.html",
        api_base_url=settings.api_base_url,
        site_url=site_url,
        title="Status | TrustedRouter",
        heading="TrustedRouter Status",
        description="Regional uptime, attestation, SDK, billing, and fallback checks.",
        google_enabled=settings.google_oauth_enabled,
        github_enabled=settings.github_oauth_enabled,
        static_version=settings.release,
        snapshot=snapshot,
        provider_health=provider_health,
        provider_health_window=leaderboard.get("window_label"),
    )
