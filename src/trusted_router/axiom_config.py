"""Axiom log shipping — non-blocking handler installed alongside Sentry.

Goal: every structured log line that lands in stdout from
`logging.getLogger(...)` also flows to the Axiom dataset
`trusted-router` (override via TR_AXIOM_DATASET) so we can slice by
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

import logging
import os
import sys
import time
from typing import Any

from trusted_router.config import Settings
from trusted_router.sentry_config import _scrub

log = logging.getLogger(__name__)


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
        client_kwargs: dict[str, Any] = {"token": token}
        if org_id:
            client_kwargs["org_id"] = org_id
        if settings.axiom_url:
            client_kwargs["url"] = settings.axiom_url
        client = axiom_py.Client(**client_kwargs)
    except Exception as exc:  # noqa: BLE001
        log.warning("axiom.disabled reason=client_init_failed err=%s", exc)
        return

    raw_handler: logging.Handler = AxiomHandler(client, dataset)
    raw_handler.setLevel(logging.NOTSET)
    handler: logging.Handler = _SafeAxiomHandler(raw_handler)
    handler.setLevel(_resolve_level(settings.axiom_log_level))
    # Attach a filter that scrubs PII before the handler ships the
    # record. Reuses sentry_config's `_scrub` so the rules are defined
    # in one place.
    handler.addFilter(_AxiomScrubFilter())

    root = logging.getLogger()
    root.addHandler(handler)
    log.info(
        "axiom.enabled dataset=%s url=%s level=%s org_id=%s",
        dataset,
        settings.axiom_url,
        settings.axiom_log_level,
        "<set>" if org_id else "<unset>",
    )


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

    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in list(record.__dict__.items()):
            if key in self._SKIP_FIELDS:
                continue
            if key.startswith("_"):
                continue
            scrubbed = _scrub(value)
            if scrubbed is not value:
                record.__dict__[key] = scrubbed
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
        self._last_error_log_at = 0.0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.inner.handle(record)
        except Exception as exc:  # noqa: BLE001 - logging must never break requests.
            now = time.monotonic()
            if now - self._last_error_log_at > 60:
                self._last_error_log_at = now
                sys.stderr.write(f"axiom.emit_failed dropped=true err={exc!r}\n")
