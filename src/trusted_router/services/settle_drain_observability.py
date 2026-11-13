"""Worker-only output boundaries: content-free logs and existing money pages."""
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any, cast

from trusted_router.config import Settings

WORKER_LOGGER = "trusted_router.services.settle_outbox_worker"
# Storage modules whose every line is a literal format filled with fixed words
# and numbers, by design (their module docstrings say so, and
# tests/test_settle_drain_observability.py checks each call). Their lines tell
# a dead connection from a silent or a slow Spanner during a drain incident,
# and the scrubbed form below keeps none of that. A line from one of them keeps
# its literal format when every argument is an exact int, float or bool, or an
# exact str that is one short word; any other line is scrubbed as before.
_FIXED_VOCABULARY_LOGGERS = frozenset({
    "trusted_router.storage_gcp_channel_failover",
    "trusted_router.storage_gcp_transport_watch",
})
_FIXED_WORD = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_ALERT_CLASSES = frozenset({"activity-repair-expired", "lost-charge", "reservation-missing",
                            "exhausted-retries"})
# Counter events the storage layer writes while the worker applies a settle
# (storage_gcp_authorize._log_missing_key_releases). The worker's line keeps
# the counter's name, a fixed word from this set, in the place the full line
# has it, so one log filter counts both; nothing else of the record is kept.
# Only an exact str is read: a subclass may render, hash or compare as it likes.
_COUNTED_METRICS = frozenset({"key_usage_unbooked_total", "key_usage_shard_fallback_total"})


class WorkerLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> logging.LogRecord | bool:
        # The worker only serves /health. Drop access records before scrubbing:
        # Uvicorn's AccessFormatter requires its original five request fields,
        # which must never reach the worker's output boundary.
        if record.name == "uvicorn.access":
            return False
        if record.name == WORKER_LOGGER:
            return record
        fixed = _fixed_vocabulary(record)
        if fixed is not None:
            return fixed
        error_class = record.exc_info[0].__name__ if record.exc_info and record.exc_info[0] else "none"
        # Python 3.12 handler filters may replace records. Construct directly,
        # bypassing factories and discarding ALL extras added after a factory.
        metric = getattr(record, "metric", None)
        if type(metric) is str and metric in _COUNTED_METRICS:
            return logging.LogRecord(record.name, record.levelno, "", 0,
                "metric=%s logger=%s level=%s exception_class=%s",
                (metric, record.name, record.levelname, error_class), None)
        return logging.LogRecord(record.name, record.levelno, "", 0,
            "logger=%s level=%s exception_class=%s", (record.name, record.levelname, error_class), None)


def _fixed_vocabulary(record: logging.LogRecord) -> logging.LogRecord | None:
    """The record rebuilt from its format and checked arguments, or None to scrub it.

    Only exact types are read, as above. The rebuilt record carries no
    exception, stack or extras; these modules attach none.
    """
    if record.name not in _FIXED_VOCABULARY_LOGGERS or type(record.msg) is not str:
        return None
    args = record.args
    if type(args) is not tuple:
        return None
    for arg in args:
        if type(arg) in (int, float, bool):
            continue
        if type(arg) is str and _FIXED_WORD.fullmatch(arg):
            continue
        return None
    try:
        if args:  # as LogRecord.getMessage formats
            record.msg % args  # noqa: B018 - a format that does not fit its arguments is scrubbed
    except (TypeError, ValueError, KeyError):
        return None
    return logging.LogRecord(record.name, record.levelno, "", 0, record.msg, args, None)


def install_logging() -> Callable[[], None]:
    """Only the worker lifespan installs these filters; other surfaces are unchanged."""
    loggers = [logging.getLogger(), *(value for value in logging.Logger.manager.loggerDict.values()
                                     if isinstance(value, logging.Logger))]
    handlers = {handler for logger in loggers for handler in logger.handlers}
    if logging.lastResort is not None:
        handlers.add(logging.lastResort)
    scrubber = WorkerLogFilter()
    for handler in handlers:
        handler.addFilter(scrubber)

    def remove() -> None:
        for handler in handlers:
            handler.removeFilter(scrubber)
    return remove


def alert_event(event: dict[str, Any], hint: Any) -> dict[str, Any]:
    """ops_alert's direct capture bypasses log handlers: allowlist its page class."""
    fingerprint = event.get("fingerprint")
    category = "other"
    if (isinstance(fingerprint, list) and len(fingerprint) == 3
            and fingerprint[:2] == ["ops-alert", "settle-outbox"]
            and fingerprint[2] in _ALERT_CLASSES):
        category = fingerprint[2]
    return {"event_id": event["event_id"], "level": "error", "message": f"ops-alert settle-outbox {category}",
            "fingerprint": ["ops-alert", "settle-outbox", category]}


def init_alerting(settings: Settings) -> Callable[[], None]:
    """Use ops_alert's Sentry destination, with no logging/request integrations."""
    from trusted_router.sentry_config import sentry_should_init

    if not sentry_should_init(settings):
        return lambda: None
    import sentry_sdk

    previous = sentry_sdk.get_global_scope().client
    client = sentry_sdk.Client(dsn=settings.sentry_dsn, environment=settings.environment,
        release=settings.release, default_integrations=False, integrations=[],
        send_default_pii=False, include_local_variables=False, max_request_body_size="never",
        before_send=cast(Any, alert_event), shutdown_timeout=1)
    sentry_sdk.get_global_scope().set_client(client)

    def close() -> None:
        client.close(timeout=1)
        sentry_sdk.get_global_scope().set_client(previous)
    return close
