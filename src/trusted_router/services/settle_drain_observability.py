"""Worker-only output boundaries: content-free logs and existing money pages."""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, cast

from trusted_router.config import Settings

WORKER_LOGGER = "trusted_router.services.settle_outbox_worker"
_ALERT_CLASSES = frozenset({"activity-repair-expired", "lost-charge", "reservation-missing",
                            "exhausted-retries"})


class WorkerLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> logging.LogRecord:
        if record.name == WORKER_LOGGER:
            return record
        error_class = record.exc_info[0].__name__ if record.exc_info and record.exc_info[0] else "none"
        # Python 3.12 handler filters may replace records. Construct directly,
        # bypassing factories and discarding ALL extras added after a factory.
        return logging.LogRecord(record.name, record.levelno, "", 0,
            "logger=%s level=%s exception_class=%s", (record.name, record.levelname, error_class), None)


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
