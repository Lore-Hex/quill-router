"""Worker-only output boundaries: content-free logs and existing money pages."""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, cast

from trusted_router.config import Settings

WORKER_LOGGER = "trusted_router.services.settle_outbox_worker"
# The Spanner channel failover and transport watch lines tell a dead
# connection from a silent or a slow Spanner during a drain incident, and the
# scrubbed form below keeps none of that. A record from one of those two
# loggers (exact names, no children) keeps its text only when its format is
# one of the exact strings below and each argument fits its position:
#   * a word from that position's fixed set;
#   * the __name__ of a class that exists in this process: an exception class
#     (a subclass of BaseException) or a google-auth credentials class. A
#     string that only looks like a class name (a key, a password) is no
#     class's name, so it does not pass;
#   * an exact int inside that position's range, or an exact bool. A range
#     still admits any number inside it, at most nine digits.
# Any other record is scrubbed as before.
# tests/test_settle_drain_observability.py checks that every logging call in
# the two modules uses one of these formats, with type(x).__name__ in exactly
# the class-name positions and only reviewed expressions elsewhere.
_CLIENT = frozenset({"primary"})
_REASON = frozenset({"waiting", "deadline_streak"})
_ANSWER = frozenset({"answered", "silent"})
_STATE = frozenset({"IDLE", "CONNECTING", "READY", "TRANSIENT_FAILURE", "SHUTDOWN"})
_PREVIOUS = _STATE | {"none"}
_EXCEPTION_CLASS = object()
_CREDENTIALS_CLASS = object()
# Durations up to seven days, counts up to a million, a token lifetime within
# seven days either way (-1 is "no expiry").
_MS = range(0, 7 * 86_400_000 + 1)
_COUNT = range(0, 1_000_001)
_TTL_SECONDS = range(-7 * 86_400, 7 * 86_400 + 1)
_FAILOVER_LOGGER = "trusted_router.storage_gcp_channel_failover"
_WATCH_LOGGER = "trusted_router.storage_gcp_transport_watch"
FIXED_FORMATS: dict[tuple[str, str], tuple[Any, ...]] = {
    (_FAILOVER_LOGGER, "spanner channel probe unavailable client=%s error_class=%s"):
        (_CLIENT, _EXCEPTION_CLASS),
    (_FAILOVER_LOGGER, "spanner channel probe kept client=%s reason=%s standby=%s active=%s "
                       "stalled_ms=%d probe_ms=%d"):
        (_CLIENT, _REASON, _ANSWER, _ANSWER, _MS, _MS),
    (_FAILOVER_LOGGER, "spanner channel failover client=%s reason=%s stalled_ms=%d waiting=%d "
                       "deadline_streak=%d probe_ms=%d failovers=%d"):
        (_CLIENT, _REASON, _MS, _COUNT, _COUNT, _MS, _COUNT),
    (_FAILOVER_LOGGER, "spanner channel standby unavailable client=%s error_class=%s"):
        (_CLIENT, _EXCEPTION_CLASS),
    (_FAILOVER_LOGGER, "spanner channel failover off client=%s"): (_CLIENT,),
    (_FAILOVER_LOGGER, "spanner channel failover unavailable client=%s error_class=%s"):
        (_CLIENT, _EXCEPTION_CLASS),
    (_FAILOVER_LOGGER, "spanner channel failover armed client=%s"): (_CLIENT,),
    (_FAILOVER_LOGGER, "spanner channel failover dropped client=%s error_class=%s"):
        (_CLIENT, _EXCEPTION_CLASS),
    (_FAILOVER_LOGGER, "spanner channel failover skipped client=%s reason=built"): (_CLIENT,),
    (_FAILOVER_LOGGER, "spanner channel failover skipped client=%s reason=omni"): (_CLIENT,),
    (_WATCH_LOGGER, "spanner transport watch unavailable client=%s error_class=%s"):
        (_CLIENT, _EXCEPTION_CLASS),
    (_WATCH_LOGGER, "spanner channel state=%s previous=%s previous_ms=%d client=%s"):
        (_STATE, _PREVIOUS, _MS, _CLIENT),
    (_WATCH_LOGGER, "spanner credential refresh unwatched client=%s"): (_CLIENT,),
    (_WATCH_LOGGER, "spanner credential refresh watched client=%s kind=%s"):
        (_CLIENT, _CREDENTIALS_CLASS),
    (_WATCH_LOGGER, "spanner credential refresh ok elapsed_ms=%d inline=%s ttl_s=%d "
                    "suppressed=%d client=%s"):
        (_MS, bool, _TTL_SECONDS, _COUNT, _CLIENT),
    (_WATCH_LOGGER, "spanner credential refresh failed error_class=%s elapsed_ms=%d "
                    "inline=%s suppressed=%d client=%s"):
        (_EXCEPTION_CLASS, _MS, bool, _COUNT, _CLIENT),
}
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
    """The record rebuilt from its listed format and checked arguments, or None.

    Only exact types are read, as above. The rebuilt record carries no
    exception, stack or extras.
    """
    if type(record.name) is not str or type(record.msg) is not str:
        return None
    positions = FIXED_FORMATS.get((record.name, record.msg))
    args = record.args
    if positions is None or type(args) is not tuple or len(args) != len(positions):
        return None
    if not all(map(_fits, args, positions)):
        return None
    return logging.LogRecord(record.name, record.levelno, "", 0, record.msg, args, None)


def _fits(arg: Any, position: Any) -> bool:
    if position is bool:
        return type(arg) is bool
    if isinstance(position, range):
        return type(arg) is int and arg in position
    if type(arg) is not str:
        return False
    if position is _EXCEPTION_CLASS:
        return arg in _class_names(BaseException)
    if position is _CREDENTIALS_CLASS:
        try:
            from google.auth import credentials
        except ImportError:
            return False
        return arg in _class_names(credentials.Credentials)
    return bool(arg in position)


def _class_names(base: type) -> set[str]:
    """The names of base and every class below it, as of now.

    Walked per record, not cached: a module imported later can add a class.
    These lines are rare (a probe round, a failed refresh, startup).
    """
    names: set[str] = set()
    seen: set[type] = set()
    stack = [base]
    while stack:
        cls = stack.pop()
        if cls in seen:
            continue
        seen.add(cls)
        names.add(cls.__name__)
        stack.extend(type.__subclasses__(cls))
    return names


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
