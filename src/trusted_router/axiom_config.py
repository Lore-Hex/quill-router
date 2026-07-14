"""Axiom log shipping — non-blocking handler installed alongside Sentry.

Goal: every structured log line that lands in stdout from
`logging.getLogger(...)` also flows to the Axiom dataset
`trusted-router-logs` (override via TR_AXIOM_DATASET) so we can slice by
request_id, provider, error_class, etc. Sentry handles the
exception-tier; Axiom handles the structured-event tier.

What this gives us that Cloud Logging alone doesn't:
  * Server-side APL queries faster than Cloud Logging's UI.
  * Joins across request_id between rate-limit middleware, inference
    services, and storage_gcp_generations swallowed-error logs.
  * The Axiom MCP server (https://mcp.axiom.co/mcp) can query the same
    dataset, so AI agents can answer "what request_ids saw a Bigtable
    write failure in the last hour?"

Design choices:
  * Token + org id come from environment, not Settings, because the
    axiom-py SDK reads them itself and we don't want to fight it.
    AXIOM_API_TOKEN is what we secret-mount; Settings only holds
    `axiom_dataset` (config, not secret) and the log level.
  * Empty AXIOM_API_TOKEN at startup → silently skip registration.
    Local dev should not need an Axiom account.
  * Reuse `sentry_config._scrub` so prompt/completion/key material
    never reaches Axiom either. Single source of truth for PII rules.
  * Skip registration under pytest unless explicitly enabled — same
    pattern as `init_sentry`.
"""
from __future__ import annotations

import importlib
import logging
import os
import re
import sys
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from urllib3.util.retry import Retry

from trusted_router.config import Settings
from trusted_router.sentry_config import _scrub

log = logging.getLogger(__name__)
HTTPAdapter: Any = importlib.import_module("requests.adapters").HTTPAdapter

# Key-based `_scrub` cannot see positional-arg VALUES; collapsing + regex is
# the args-safe complement (PR #124 review P2).
_AXIOM_SECRET_VALUE_RE = re.compile(r"(?i)(token|secret|key|password|authorization)=([^&\s\"']+)")
_AXIOM_EMAIL_VALUE_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def init_axiom(settings: Settings) -> None:
    """Wire an AxiomHandler onto the root logger if AXIOM_API_TOKEN is
    set in the environment. Idempotent — calling twice is safe but only
    the first call has effect."""
    if _running_under_pytest(settings):
        return
    token = os.environ.get("AXIOM_API_TOKEN") or os.environ.get("AXIOM_TOKEN")
    if not token:
        log.info("axiom.disabled reason=no_token_in_env")
        return
    org_id = os.environ.get("AXIOM_ORG_ID")
    dataset = settings.axiom_dataset
    if not dataset:
        log.warning("axiom.disabled reason=no_dataset_configured")
        return
    if _handler_already_installed():
        return

    try:
        import axiom_py
        from axiom_py.logging import AxiomHandler
    except ImportError as exc:
        log.warning("axiom.disabled reason=import_failed err=%s", exc)
        return

    try:
        client_kwargs = _client_kwargs(
            token=token,
            org_id=org_id,
            axiom_url=settings.axiom_url,
        )
        client = axiom_py.Client(**client_kwargs)
        _mount_axiom_retry_adapter(client)
    except Exception as exc:  # noqa: BLE001
        log.warning("axiom.disabled reason=client_init_failed err=%s", exc)
        return

    resolved_level = _resolve_level(settings.axiom_log_level)
    raw_handler: Any = AxiomHandler(client, dataset)
    raw_handler.setLevel(logging.NOTSET)
    # axiom-py's emit() recreates each threading.Timer with `self.flush` at
    # Timer-creation time. Assigning the bound flush on this instance shadows
    # the class method, so timer-thread flushes go through the safe wrapper too.
    raw_handler.flush = _safe_flush_wrapper(raw_handler.flush)
    handler: logging.Handler = _SafeAxiomHandler(raw_handler)
    handler.setLevel(resolved_level)
    # Attach a filter that scrubs PII before the handler ships the
    # record. Reuses sentry_config's `_scrub` so the rules are defined
    # in one place.
    handler.addFilter(_AxiomScrubFilter())
    # Drop third-party transport chatter before it ships. Measured
    # 2026-07-04: urllib3.connectionpool (Sentry's envelope uploads)
    # was 235 of 238 events in a 2h window — a feedback loop where
    # observability traffic generates observability traffic. Name-based
    # so it composes with any handler level.
    handler.addFilter(_AxiomNoiseFilter())

    root = logging.getLogger()
    root.addHandler(handler)
    # The handler's level alone is not enough: uvicorn leaves the root
    # logger at WARNING, which filters app INFO records before any handler
    # sees them. Lower the level on OUR package logger only, but never
    # raise it above WARNING. TR_AXIOM_LOG_LEVEL is the Axiom handler
    # threshold; if set above WARNING it must not suppress app warnings
    # from other integrations such as Sentry. The handler's own level still
    # filters what ships to Axiom.
    logging.getLogger("trusted_router").setLevel(min(resolved_level, logging.WARNING))
    log.info(
        "axiom.enabled dataset=%s url=%s level=%s org_id=%s",
        dataset,
        settings.axiom_url,
        settings.axiom_log_level,
        "<set>" if org_id else "<unset>",
    )


def _client_kwargs(*, token: str, org_id: str | None, axiom_url: str) -> dict[str, Any]:
    client_kwargs: dict[str, Any] = {"token": token}
    if org_id:
        client_kwargs["org_id"] = org_id
    if axiom_url:
        parsed = urlparse(axiom_url)
        if parsed.hostname and parsed.hostname.endswith(".edge.axiom.co"):
            client_kwargs["edge_url"] = axiom_url
        else:
            client_kwargs["url"] = axiom_url
    return client_kwargs


def _mount_axiom_retry_adapter(client: Any) -> None:
    session = getattr(client, "session", None)
    if session is None:
        return

    # Retrying log ingest can at worst duplicate a log batch; the common failure
    # here is RemoteDisconnected on an idle keepalive socket.
    session.mount(
        "https://",
        HTTPAdapter(
            max_retries=Retry(
                total=2,
                connect=2,
                read=1,
                backoff_factor=0.2,
                allowed_methods=frozenset({"POST"}),
                raise_on_status=False,
            )
        ),
    )


def _safe_flush_wrapper(bound_flush: Callable[[], None]) -> Callable[[], None]:
    last_error_log_at: float | None = None

    def safe_flush() -> None:
        nonlocal last_error_log_at
        try:
            bound_flush()
        except Exception as exc:  # noqa: BLE001 - logging flushes must not break requests.
            now = time.monotonic()
            if last_error_log_at is None or now - last_error_log_at > 60:
                last_error_log_at = now
                sys.stderr.write(f"axiom.flush_failed dropped=true err={exc!r}\n")

    return safe_flush


def _resolve_level(name: str) -> int:
    return getattr(logging, name.upper(), logging.INFO)


def _handler_already_installed() -> bool:
    """Don't double-attach if init_axiom() is called twice in the same
    process (e.g. tests that re-create the FastAPI app)."""
    root = logging.getLogger()
    return any(
        type(handler).__name__ in {"AxiomHandler", "_SafeAxiomHandler"} for handler in root.handlers
    )


def _running_under_pytest(settings: Settings) -> bool:
    return (
        settings.environment.lower() == "test"
        or "pytest" in sys.modules
        or bool(os.environ.get("PYTEST_CURRENT_TEST"))
    )


class _AxiomScrubFilter(logging.Filter):
    """Scrub PII fields out of LogRecord.__dict__ before it leaves the
    process. The AxiomHandler reads `record.__dict__` to build the
    event payload, so mutating it here is the right hook.

    Reuses `sentry_config._scrub`, which walks the value recursively
    and replaces keys matching SENSITIVE_KEYS (prompt, content, key,
    authorization, ...) with '[Filtered]'. Same rules that protect
    Sentry breadcrumbs apply here."""

    # Standard LogRecord fields we don't want to scrub (their values
    # are usually filename/line numbers/etc., never secrets, and
    # passing them through `_scrub` would needlessly walk them).
    _SKIP_FIELDS = frozenset(
        {
            "name", "msg", "args", "levelname", "levelno", "pathname",
            "filename", "module", "exc_info", "exc_text", "stack_info",
            "lineno", "funcName", "created", "msecs", "relativeCreated",
            "thread", "threadName", "processName", "process",
            "taskName", "asctime",
        }
    )
    _MAX_MESSAGE_CHARS = 2_000
    _TRUNCATION_SUFFIX = "…[truncated]"

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            collapsed = record.getMessage()
        except Exception:  # noqa: BLE001 - logging filters must not break logging.
            collapsed = None
            # If formatting fails, keep the unformatted template but drop raw
            # positional values so axiom-py cannot ship them from record.args.
            record.args = None
        if collapsed is not None:
            # Collapsing args means Axiom loses structured args fields and gets
            # the final formatted message only. That is the point: nothing
            # unscrubbed can leave the process.
            record.msg = _AXIOM_EMAIL_VALUE_RE.sub(
                "[Filtered-email]",
                _AXIOM_SECRET_VALUE_RE.sub(r"\1=[Filtered]", collapsed),
            )
            if len(record.msg) > self._MAX_MESSAGE_CHARS:
                record.msg = (
                    record.msg[: self._MAX_MESSAGE_CHARS - len(self._TRUNCATION_SUFFIX)]
                    + self._TRUNCATION_SUFFIX
                )
            record.args = None

        for key, value in list(record.__dict__.items()):
            if key in self._SKIP_FIELDS:
                continue
            if key.startswith("_"):
                continue
            scrubbed = _scrub(value)
            if scrubbed is not value:
                record.__dict__[key] = scrubbed
        return True


class _AxiomNoiseFilter(logging.Filter):
    """Drop third-party transport/client chatter before it ships to
    Axiom. The dataset exists for the app's structured events
    (request_id, provider, error_class joins) — not for the HTTP
    plumbing underneath our own observability stack.

    Measured 2026-07-04: `urllib3.connectionpool` alone (Sentry's
    envelope uploads) was 235 of 238 events in a 2h window. Shipping
    those burns ingest quota to record that we recorded something.

    Prefix match on the logger name, so child loggers
    (`urllib3.connectionpool`, `google.auth.transport`, ...) are
    covered by their root entry. App loggers (`trusted_router.*`) and
    uvicorn error logs are unaffected."""

    _NOISY_PREFIXES = (
        "urllib3",             # Sentry transport + assorted HTTP chatter
        "sentry_sdk",          # the SDK's own internal logging
        "google",              # spanner/bigtable/auth client libraries
        "grpc",                # gRPC channel state churn
        "httpx",               # per-request INFO lines for provider calls
        "httpcore",            # httpx's transport layer
        "hpack",               # HTTP/2 header codec debug noise
    )

    def filter(self, record: logging.LogRecord) -> bool:
        name = record.name
        for prefix in self._NOISY_PREFIXES:
            if name == prefix or name.startswith(prefix + "."):
                return False
        return True


class _SafeAxiomHandler(logging.Handler):
    """Axiom is observability, not request serving infrastructure.

    The upstream Axiom handler can raise during `emit()` when the token,
    org, dataset type, or ingestion endpoint is wrong. Logging handlers run
    inline with application code, so an uncaught Axiom exception can turn a
    normal 4xx path into a 500. Drop failed Axiom emits and write a throttled
    stderr breadcrumb instead of raising.
    """

    def __init__(self, inner: logging.Handler) -> None:
        super().__init__()
        self.inner = inner
        self._last_error_log_at: float | None = None

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.inner.handle(record)
        except Exception as exc:  # noqa: BLE001 - logging must never break requests.
            now = time.monotonic()
            if self._last_error_log_at is None or now - self._last_error_log_at > 60:
                self._last_error_log_at = now
                sys.stderr.write(f"axiom.emit_failed dropped=true err={exc!r}\n")
