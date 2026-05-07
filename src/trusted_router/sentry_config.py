from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass
from threading import Lock
from typing import Any, cast

from trusted_router.config import Settings

SENSITIVE_KEYS = {
    "authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "api_key",
    "apikey",
    "key",
    "byok",
    "secret",
    "token",
    "password",
    "messages",
    "prompt",
    "completion",
    "content",
    "output",
    "input",
    "raw_body",
    "body",
}

# Substring matches: any string field containing one of these is scrubbed.
# Keep here as the single source of truth — `_scrub_string` and the
# regression test in test_security_contracts.py both consume this set so
# adding a new format means editing one list, not two.
SENSITIVE_STRING_FRAGMENTS: tuple[str, ...] = (
    "sk-tr-v1-",
    "sk-or-v1-",
    "anthropic_api_key",
    "openai_api_key",
    "gemini_api_key",
    "cerebras_api_key",
    "deepseek_api_key",
    "mistral_api_key",
    "kimi_api_key",
    "moonshot_api_key",
    "vertex_api_key",
    "vertex_access_token",
    "zai_api_key",
    "zhipu_api_key",
    "together_api_key",
    "togetherai_api_key",
)

# Prefix matches: scrub the entire string if it starts with one of these.
# Used for tokens whose envelope is the secret itself (not embedded in a
# longer URL or key=value form). Most are OAuth client / personal-access
# token prefixes that don't carry the literal "key" or "secret" word so
# the key-name blocklist above wouldn't catch them.
SENSITIVE_STRING_PREFIXES: tuple[str, ...] = (
    "GOCSPX-",  # Google OAuth client secret
    "gho_",     # GitHub OAuth-app token
    "ghp_",     # GitHub personal access token
    "ghu_",     # GitHub user-to-server token
    "ghs_",     # GitHub server token
    "ghr_",     # GitHub refresh token
)


@dataclass(frozen=True)
class SentryFloodgateConfig:
    enabled: bool = True
    window_seconds: int = 60 * 60
    max_events_per_fingerprint: int = 3
    max_events_per_window: int = 50
    max_fingerprints: int = 2048


@dataclass
class _FloodBucket:
    window_started: float
    count: int = 0


class _SentryFloodgate:
    def __init__(
        self,
        config: SentryFloodgateConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._clock = clock
        self._lock = Lock()
        self._fingerprints: dict[str, _FloodBucket] = {}
        self._global = _FloodBucket(window_started=clock())

    def allow(self, event: dict[str, Any]) -> bool:
        config = self._config
        if not config.enabled:
            return True
        if config.max_events_per_fingerprint <= 0 or config.max_events_per_window <= 0:
            return False

        now = self._clock()
        window_seconds = max(config.window_seconds, 1)
        fingerprint = _event_fingerprint(event)

        with self._lock:
            if now - self._global.window_started >= window_seconds:
                self._global = _FloodBucket(window_started=now)
                self._fingerprints.clear()

            bucket = self._fingerprints.get(fingerprint)
            is_first_for_fingerprint = bucket is None or now - bucket.window_started >= window_seconds
            if is_first_for_fingerprint:
                bucket = _FloodBucket(window_started=now)
                self._fingerprints[fingerprint] = bucket
            else:
                assert bucket is not None
                if bucket.count >= config.max_events_per_fingerprint:
                    return False
                if self._global.count >= config.max_events_per_window:
                    return False

            bucket.count += 1
            self._global.count += 1
            self._prune_if_needed(now, window_seconds)
            return True

    def _prune_if_needed(self, now: float, window_seconds: int) -> None:
        max_fingerprints = max(self._config.max_fingerprints, 1)
        if len(self._fingerprints) <= max_fingerprints:
            return
        stale = [
            fingerprint
            for fingerprint, bucket in self._fingerprints.items()
            if now - bucket.window_started >= window_seconds
        ]
        for fingerprint in stale:
            self._fingerprints.pop(fingerprint, None)
        if len(self._fingerprints) <= max_fingerprints:
            return
        oldest = sorted(
            self._fingerprints.items(),
            key=lambda item: item[1].window_started,
        )
        for fingerprint, _bucket in oldest[: len(self._fingerprints) - max_fingerprints]:
            self._fingerprints.pop(fingerprint, None)


_floodgate = _SentryFloodgate(SentryFloodgateConfig())


def init_sentry(settings: Settings) -> None:
    if not settings.sentry_dsn:
        return
    if _running_under_pytest(settings):
        return
    configure_sentry_floodgate(settings)
    import sentry_sdk
    from sentry_sdk.integrations.fastapi import FastApiIntegration
    from sentry_sdk.integrations.logging import LoggingIntegration
    from sentry_sdk.integrations.starlette import StarletteIntegration

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.environment,
        release=settings.release,
        send_default_pii=False,
        max_request_body_size="never",
        include_local_variables=False,
        enable_logs=True,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        integrations=[
            LoggingIntegration(level=None, event_level=None),
            StarletteIntegration(transaction_style="endpoint"),
            FastApiIntegration(transaction_style="endpoint"),
        ],
        # cast over Sentry's TypedDict event/log signatures — the scrubbers
        # operate on whatever shape Sentry hands them, and we don't want to
        # depend on private TypedDict imports to stay in sync.
        before_send=cast(Any, before_send),
        before_send_log=cast(Any, before_send_log),
        before_breadcrumb=cast(Any, before_breadcrumb),
    )


def _running_under_pytest(settings: Settings) -> bool:
    return (
        settings.environment.lower() == "test"
        or "pytest" in sys.modules
        or bool(os.environ.get("PYTEST_CURRENT_TEST"))
    )


def before_send(event: dict[str, Any], hint: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if _is_dropped_noise(event):
        return None
    event = _scrub(event)
    request = event.get("request")
    if isinstance(request, MutableMapping):
        request.pop("data", None)
        request.pop("cookies", None)
    if not _floodgate.allow(event):
        return None
    return event


def before_send_log(event: dict[str, Any], hint: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if _is_dropped_noise(event):
        return None
    event = _scrub(event)
    if not _floodgate.allow(event):
        return None
    return event


def before_breadcrumb(crumb: dict[str, Any], hint: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if _is_dropped_noise(crumb):
        return None
    return _scrub(crumb)


def _is_dropped_noise(event: dict[str, Any]) -> bool:
    text = json.dumps(event, default=str)
    return (
        "Failed to export metrics to Cloud Monitoring" in text
        and "spanner.googleapis.com/internal/client/" in text
        and "missing (instance_id)" in text
    )


def configure_sentry_floodgate(settings: Settings) -> None:
    global _floodgate
    _floodgate = _SentryFloodgate(
        SentryFloodgateConfig(
            enabled=settings.sentry_floodgate_enabled,
            window_seconds=settings.sentry_floodgate_window_seconds,
            max_events_per_fingerprint=settings.sentry_floodgate_max_events_per_fingerprint,
            max_events_per_window=settings.sentry_floodgate_max_events_per_window,
            max_fingerprints=settings.sentry_floodgate_max_fingerprints,
        )
    )


def reset_sentry_floodgate_for_tests(
    *,
    settings: Settings | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    global _floodgate
    if settings is None:
        _floodgate = _SentryFloodgate(SentryFloodgateConfig(), clock=clock)
        return
    _floodgate = _SentryFloodgate(
        SentryFloodgateConfig(
            enabled=settings.sentry_floodgate_enabled,
            window_seconds=settings.sentry_floodgate_window_seconds,
            max_events_per_fingerprint=settings.sentry_floodgate_max_events_per_fingerprint,
            max_events_per_window=settings.sentry_floodgate_max_events_per_window,
            max_fingerprints=settings.sentry_floodgate_max_fingerprints,
        ),
        clock=clock,
    )


def _event_fingerprint(event: dict[str, Any]) -> str:
    explicit = event.get("fingerprint")
    if isinstance(explicit, list) and explicit:
        return _hash_identity("fingerprint:" + "|".join(str(item) for item in explicit))

    exception = event.get("exception")
    if isinstance(exception, dict):
        values = exception.get("values")
        if isinstance(values, list) and values:
            value = values[-1]
            if isinstance(value, dict):
                return _hash_identity("exception:" + _exception_identity(value))

    message = _message_from_event(event)
    logger = event.get("logger")
    level = event.get("level")
    return _hash_identity(f"message:{logger}:{level}:{message}")


def _exception_identity(value: dict[str, Any]) -> str:
    exc_type = str(value.get("type") or "")
    exc_value = str(value.get("value") or "")
    stacktrace = value.get("stacktrace")
    frame_identity = ""
    if isinstance(stacktrace, dict):
        frames = stacktrace.get("frames")
        if isinstance(frames, list) and frames:
            frame = frames[-1]
            if isinstance(frame, dict):
                filename = frame.get("filename") or frame.get("abs_path") or ""
                function = frame.get("function") or ""
                lineno = frame.get("lineno") or ""
                frame_identity = f"{filename}:{function}:{lineno}"
    return f"{exc_type}:{exc_value}:{frame_identity}"


def _message_from_event(event: dict[str, Any]) -> str:
    message = event.get("message")
    if isinstance(message, str) and message:
        return message
    logentry = event.get("logentry")
    if isinstance(logentry, dict):
        formatted = logentry.get("formatted")
        if isinstance(formatted, str) and formatted:
            return formatted
        log_message = logentry.get("message")
        if isinstance(log_message, str) and log_message:
            return log_message
    return json.dumps(event, sort_keys=True, default=str)[:2048]


def _hash_identity(identity: str) -> str:
    return hashlib.sha256(identity.encode()).hexdigest()


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if _is_sensitive_key(str(key)):
                out[key] = "[Filtered]"
            else:
                out[key] = _scrub(item)
        return out
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if isinstance(value, str):
        return _scrub_string(value)
    return value


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower().replace("-", "_")
    return any(part in lowered for part in SENSITIVE_KEYS)


def _scrub_string(value: str) -> str:
    """Substring match across all known secret formats — both
    case-insensitive (for things like `OPENAI_API_KEY=...` env-var dumps)
    and case-sensitive prefix occurrences anywhere in the string (for
    OAuth tokens whose prefix is distinctive enough that even mid-string
    occurrences are suspicious — `GOCSPX-` and the GitHub `gh*_` family).
    Substring rather than prefix-only catches the cases where the secret
    is embedded in a longer log line or breadcrumb message."""
    lowered = value.lower()
    if any(fragment in lowered for fragment in SENSITIVE_STRING_FRAGMENTS):
        return "[Filtered]"
    if any(prefix in value for prefix in SENSITIVE_STRING_PREFIXES):
        return "[Filtered]"
    return value
