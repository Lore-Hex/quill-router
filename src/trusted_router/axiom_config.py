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

import atexit
import copy
import importlib
import logging
import logging.handlers
import os
import queue
import re
import sys
import threading
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from urllib3.util.retry import Retry

from trusted_router.config import Settings
from trusted_router.sentry_config import _is_sensitive_key, _scrub, _scrub_string

log = logging.getLogger(__name__)
HTTPAdapter: Any = importlib.import_module("requests.adapters").HTTPAdapter

# Key-based `_scrub` cannot see positional-arg VALUES; collapsing + regex is
# the args-safe complement (PR #124 review P2).
_AXIOM_SECRET_VALUE_RE = re.compile(r"(?i)(token|secret|key|password|authorization)=([^&\s\"']+)")
_AXIOM_EMAIL_VALUE_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _scrub_axiom_string(value: str) -> str:
    """Apply Axiom's message redactions to any string in the payload."""
    return _scrub_string(
        _AXIOM_EMAIL_VALUE_RE.sub(
            "[Filtered-email]",
            _AXIOM_SECRET_VALUE_RE.sub(r"\1=[Filtered]", value),
        )
    )


def _scrub_axiom_value(value: Any) -> Any:
    """Run the shared safe-value scrubber, then redact Axiom-wide PII.

    ``sentry_config._scrub`` makes arbitrary values finite and serializable,
    but its string policy intentionally targets known secret formats. Axiom's
    payload is a flattened ``LogRecord.__dict__``, so its e-mail and
    ``key=value`` rules must also apply recursively to every custom extra.
    """

    def redact_strings(scrubbed: Any) -> Any:
        if isinstance(scrubbed, str):
            return _scrub_axiom_string(scrubbed)
        if isinstance(scrubbed, dict):
            out: dict[Any, Any] = {}
            for key, item in scrubbed.items():
                if isinstance(key, str):
                    safe_key: Any = _scrub_axiom_string(key)
                elif key is None or isinstance(key, (bool, int, float)):
                    safe_key = key
                else:
                    safe_key = f"[Filtered-{type(key).__name__}-key]"
                out[safe_key] = redact_strings(item)
            return out
        if isinstance(scrubbed, list):
            return [redact_strings(item) for item in scrubbed]
        if isinstance(scrubbed, tuple):
            return tuple(redact_strings(item) for item in scrubbed)
        return scrubbed

    return redact_strings(_scrub(value))


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
    raw_handler = AxiomHandler(client, dataset)
    handler, listener = build_axiom_pipeline(
        raw_handler,
        resolved_level=resolved_level,
    )
    listener.start()

    # The callback owns shutdown ordering: drain the queue into axiom-py's
    # buffer, then flush that buffer. axiom-py registers its own flush before
    # this callback, so relying on its FIFO callback order would strand records
    # that the listener delivers afterward. The same idempotent callback is an
    # atexit backstop for paths that never reach the client's shutdown hook.
    shutdown_pipeline = _make_axiom_shutdown(listener, raw_handler)

    try:
        client.before_shutdown(shutdown_pipeline)
    except Exception as exc:  # noqa: BLE001 - hook registration is best effort.
        log.warning("axiom.listener_shutdown_hook_failed err=%s", exc)
    atexit.register(shutdown_pipeline)

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


def build_axiom_pipeline(
    raw_handler: Any,
    *,
    resolved_level: int,
    max_queued: int = 10_000,
) -> tuple[_DroppingQueueHandler, logging.handlers.QueueListener]:
    """Wire the record path: logging thread -> bounded queue -> shipper thread.

    Extracted from `init_axiom` so the wiring is testable. `init_axiom` returns
    early under pytest by design, so a test that only exercised `init_axiom`
    could not assert on the wiring at all — and a test that builds its own
    handlers asserts a stdlib property rather than this function's choices. The
    security-relevant choice here is that the queue handler scrubs a private
    copy during ``prepare()``, before that copy can enter the queue.

    Returns the handler to attach to the root logger and the not-yet-started
    listener; the caller starts it and registers shutdown.
    """
    raw_handler.setLevel(logging.NOTSET)
    # axiom-py's emit() recreates each threading.Timer with `self.flush` at
    # Timer-creation time. Assigning the bound flush on this instance shadows
    # the class method, so timer-thread flushes go through the safe wrapper too.
    raw_handler.flush = _safe_flush_wrapper(raw_handler.flush)
    shipper: logging.Handler = _SafeAxiomHandler(raw_handler)
    shipper.setLevel(resolved_level)

    # The record crosses to the shipper thread here, so the blocking HTTPS POST
    # inside axiom-py's emit() no longer happens on the request thread. The
    # queue is bounded: log shipping may lose records under backpressure, and
    # may never slow a response.
    handler = _DroppingQueueHandler(queue.Queue(maxsize=max_queued))
    handler.setLevel(resolved_level)
    # Scrubbing is owned by `_DroppingQueueHandler.prepare()`, not a handler
    # filter. A filter would mutate the caller's shared LogRecord before a
    # sibling stdout or Sentry handler sees it. `prepare()` first copies, then
    # sanitizes, and only its private copy may cross the queue boundary.
    # Drop third-party transport chatter before it ships. Measured 2026-07-04:
    # urllib3.connectionpool (Sentry's envelope uploads) was 235 of 238 events
    # in a 2h window — observability traffic generating observability traffic.
    handler.addFilter(_AxiomNoiseFilter())

    # respect_handler_level=True so the shipper's level still governs what is
    # sent if either level is changed later.
    listener = _IdempotentQueueListener(
        handler.queue,
        shipper,
        respect_handler_level=True,
    )
    # No daemon flag to set: QueueListener.start() creates its monitor thread
    # with daemon=True itself, so the process is never held open by shipping.
    return handler, listener


def _make_axiom_shutdown(
    listener: logging.handlers.QueueListener,
    raw_handler: logging.Handler,
) -> Callable[[], None]:
    """Return a once-only queue-drain-then-buffer-flush callback."""
    lock = threading.Lock()
    complete = False

    def shutdown() -> None:
        nonlocal complete
        with lock:
            if complete:
                return
            try:
                listener.stop()
            except Exception as exc:  # noqa: BLE001 - shutdown must not raise.
                # Leave `complete` false so the atexit backstop can retry.
                sys.stderr.write(f"axiom.listener_stop_failed err={exc!r}\n")
                return
            try:
                raw_handler.flush()
            except Exception as exc:  # noqa: BLE001 - defensive for test/custom handlers.
                sys.stderr.write(f"axiom.flush_failed dropped=true err={exc!r}\n")
            # axiom-py's emit starts a new timer after adding each drained
            # record. Its earlier FIFO shutdown callback canceled the old
            # timer, so cancel the replacement created during this drain.
            timer = getattr(raw_handler, "timer", None)
            if timer is not None:
                try:
                    timer.cancel()
                except Exception:  # noqa: BLE001 - shutdown remains best effort.
                    sys.stderr.write("axiom.timer_cancel_failed dropped=true\n")
            complete = True

    return shutdown


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
    # `_DroppingQueueHandler` is what actually lands on root now that shipping
    # happens on a listener thread; the other two names are what earlier
    # versions attached, and are kept so a mixed-version process still
    # short-circuits. Missing a name here does not merely double-log: it
    # starts a second listener thread and a second bounded queue.
    return any(
        type(handler).__name__ in {"AxiomHandler", "_SafeAxiomHandler", "_DroppingQueueHandler"}
        for handler in root.handlers
    )


def _running_under_pytest(settings: Settings) -> bool:
    return (
        settings.environment.lower() == "test"
        or "pytest" in sys.modules
        or bool(os.environ.get("PYTEST_CURRENT_TEST"))
    )


class _AxiomScrubFilter(logging.Filter):
    """Scrub PII fields out of a private ``LogRecord`` copy.

    The AxiomHandler reads ``record.__dict__`` to build the event payload, so
    every outgoing key and value must be covered. The production queue handler
    invokes this only after copying the caller's record; using it directly as
    a handler filter would mutate records seen by sibling handlers.

    Reuses `sentry_config._scrub`, which walks the value recursively
    and replaces keys matching SENSITIVE_KEYS (prompt, content, key,
    authorization, ...) with '[Filtered]'. Same rules that protect
    Sentry breadcrumbs apply here."""

    # These fields require structural handling rather than the generic value
    # scrub below. Exception and stack data are removed in ``prepare()`` before
    # a formatter can fold their arbitrary free text into the Axiom message.
    _SKIP_FIELDS = frozenset(
        {
            "msg",
            "args",
            "exc_info",
            "exc_text",
            "stack_info",
        }
    )
    _MAX_MESSAGE_CHARS = 2_000
    _TRUNCATION_SUFFIX = "…[truncated]"

    def filter(self, record: logging.LogRecord) -> bool:
        # Never invoke arbitrary ``__str__`` code from a custom message object.
        # The shared scrubber reduces unknown objects to a safe type marker.
        if type(record.msg) is not str:
            record.msg = _scrub_axiom_value(record.msg)
            record.args = None
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
            record.msg = _scrub_axiom_string(collapsed)
            if len(record.msg) > self._MAX_MESSAGE_CHARS:
                record.msg = (
                    record.msg[: self._MAX_MESSAGE_CHARS - len(self._TRUNCATION_SUFFIX)]
                    + self._TRUNCATION_SUFFIX
                )
            record.args = None
            # QueueHandler.prepare() adds ``message`` alongside ``msg``. Keep
            # both payload fields identical so a downstream handler cannot
            # read the pre-scrub formatted value from the former.
            if "message" in record.__dict__:
                record.message = record.msg

        for key, value in list(record.__dict__.items()):
            if key in self._SKIP_FIELDS:
                continue
            safe_key = _scrub_axiom_string(key)
            scrubbed = "[Filtered]" if _is_sensitive_key(key) else _scrub_axiom_value(value)
            if safe_key != key:
                del record.__dict__[key]
            record.__dict__[safe_key] = scrubbed
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
        "urllib3",  # Sentry transport + assorted HTTP chatter
        "sentry_sdk",  # the SDK's own internal logging
        "google",  # spanner/bigtable/auth client libraries
        "grpc",  # gRPC channel state churn
        "httpx",  # per-request INFO lines for provider calls
        "httpcore",  # httpx's transport layer
        "hpack",  # HTTP/2 header codec debug noise
    )

    def filter(self, record: logging.LogRecord) -> bool:
        name = record.name
        for prefix in self._NOISY_PREFIXES:
            if name == prefix or name.startswith(prefix + "."):
                return False
        return True


class _IdempotentQueueListener(logging.handlers.QueueListener):
    """QueueListener with stable start/stop semantics across Python 3.11+."""

    def __init__(self, *handlers: Any, **kwargs: Any) -> None:
        super().__init__(*handlers, **kwargs)
        self._lifecycle_lock = threading.Lock()
        self._running = False

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._running:
                return
            super().start()
            self._running = True

    def stop(self) -> None:
        with self._lifecycle_lock:
            if not self._running:
                return
            super().stop()
            self._running = False


class _DroppingQueueHandler(logging.handlers.QueueHandler):
    """Enqueue for the shipper thread, and drop rather than block when full.

    WHY THIS EXISTS. axiom-py's `AxiomHandler.emit()` flushes SYNCHRONOUSLY in
    the calling thread whenever more than its interval (1s) has elapsed since
    the last flush. At low request volume that is nearly every request, so a
    request that logs pays a blocking HTTPS POST to Axiom before it can
    respond. Measured 2026-08-17 from this machine: 551 ms for a cold POST to
    api.axiom.co. Production console pages served from europe-west4 measured
    p50 1540 ms; this was one of the summands.

    `logging.handlers.QueueHandler.enqueue` uses `put_nowait`, which raises
    `queue.Full` on a bounded queue. The base class routes that through
    `handleError`, whose behaviour depends on `logging.raiseExceptions`. Being
    explicit instead: on a full queue the record is dropped and a throttled
    stderr breadcrumb is written. Dropping observability under backpressure is
    the correct trade; blocking a request on log shipping is not.

    SCRUBBING RUNS BEFORE THE QUEUE, DELIBERATELY. ``prepare()`` copies the
    caller's record, scrubs its ordinary message and all custom extras, removes
    arbitrary exception/stack free text, then formats and scrubs the final
    payload again. Thus neither the shared caller record nor anything that
    reaches the queue contains a newly exposed traceback or unsanitized extra.
    """

    def __init__(self, queue: Any) -> None:
        super().__init__(queue)
        self._dropped = 0
        self._last_drop_log_at: float | None = None
        self._queue_scrubber = _AxiomScrubFilter()

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        """Format, then scrub the exact copy that will cross the queue.

        Axiom is the structured-event tier; Sentry owns exception detail.
        ``QueueHandler.prepare`` would otherwise format arbitrary exception and
        stack text into ``msg``. Remove it from the copied record before stdlib
        formatting, then scrub the fully prepared payload a second time for
        custom formatter output. The caller's record remains untouched for
        sibling stdout and Sentry handlers.
        """
        prepared = copy.copy(record)
        self._queue_scrubber.filter(prepared)
        prepared.exc_info = None
        prepared.exc_text = None
        prepared.stack_info = None
        prepared = super().prepare(prepared)
        self._queue_scrubber.filter(prepared)
        return prepared

    def emit(self, record: logging.LogRecord) -> None:
        """Prepare without letting stdlib print the raw record on failure.

        ``QueueHandler.emit`` delegates preparation errors to
        ``handleError(record)``. With ``logging.raiseExceptions`` enabled that
        fallback writes the *original*, unsanitized message and arguments to
        stderr. A pathological custom extra must cost one log record, never
        create a second PII channel.
        """
        try:
            prepared = self.prepare(record)
        except Exception:  # noqa: BLE001 - malformed extras must fail closed.
            self._note_drop(event="record_dropped", reason="scrub_failed")
            return
        self.enqueue(prepared)

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except Exception:  # noqa: BLE001 - a full queue must not break a request.
            self._note_drop(event="queue_full", reason="shipper_thread_behind")

    def _note_drop(self, *, event: str, reason: str) -> None:
        self._dropped += 1
        now = time.monotonic()
        if self._last_drop_log_at is None or now - self._last_drop_log_at > 60:
            self._last_drop_log_at = now
            sys.stderr.write(f"axiom.{event} dropped_total={self._dropped} reason={reason}\n")


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
