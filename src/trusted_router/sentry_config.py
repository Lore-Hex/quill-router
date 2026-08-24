from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import sys
import time
from collections.abc import Callable, Iterable, Mapping, MutableMapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as _dt_time
from decimal import Decimal
from threading import Lock
from typing import Any, cast
from uuid import UUID

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
    "fireworks_api_key",
    "fireworks_ai_api_key",
    "cohere_api_key",
)

# Prefix matches: scrub the entire string if it starts with one of these.
# Used for tokens whose envelope is the secret itself (not embedded in a
# longer URL or key=value form). Most are OAuth client / personal-access
# token prefixes that don't carry the literal "key" or "secret" word so
# the key-name blocklist above wouldn't catch them.
SENSITIVE_STRING_PREFIXES: tuple[str, ...] = (
    "GOCSPX-",  # Google OAuth client secret
    "gho_",  # GitHub OAuth-app token
    "ghp_",  # GitHub personal access token
    "ghu_",  # GitHub user-to-server token
    "ghs_",  # GitHub server token
    "ghr_",  # GitHub refresh token
)

# An HTTP 501 is an explicit compatibility response, not an unhandled server
# failure. In particular, the gateway returns it before creating a billing hold
# when a route/endpoint combination is unsupported. Keep real 5xx failures and
# our high-signal 405 contract signal in Sentry without paging on that expected
# fail-closed response.
SENTRY_FAILED_REQUEST_STATUS_CODES = {
    405,
    *range(500, 501),
    *range(502, 600),
}


@dataclass(frozen=True)
class SentryFloodgateConfig:
    enabled: bool = True
    window_seconds: int = 60 * 60
    max_events_per_fingerprint: int = 3
    max_events_per_window: int = 50
    max_fingerprints: int = 2048
    trusted_internal_token_digests: frozenset[bytes] = frozenset()


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
        with self._lock:
            if now - self._global.window_started >= window_seconds:
                self._global = _FloodBucket(window_started=now)
                self._fingerprints.clear()

            # This is a hard process-wide shipping cap, not merely a cap for
            # already-seen issues. Checking it before fingerprint calculation
            # and bucket creation means a stream of unique attacker-controlled
            # errors cannot keep winning a "first event" exception, churn the
            # bounded map, or spend hashing work after the budget is exhausted.
            if self._global.count >= config.max_events_per_window:
                return False

            fingerprint = _event_fingerprint(event)
            bucket = self._fingerprints.get(fingerprint)
            is_first_for_fingerprint = (
                bucket is None or now - bucket.window_started >= window_seconds
            )
            if is_first_for_fingerprint:
                self._make_room_for_fingerprint(now, window_seconds)
                bucket = _FloodBucket(window_started=now)
                self._fingerprints[fingerprint] = bucket
            else:
                assert bucket is not None
                if bucket.count >= config.max_events_per_fingerprint:
                    return False

            bucket.count += 1
            self._global.count += 1
            return True

    def trusts_internal_token(self, value: str | None) -> bool:
        if not value:
            return False
        candidate = hashlib.sha256(value.encode("utf-8")).digest()
        return any(
            hmac.compare_digest(candidate, expected)
            for expected in self._config.trusted_internal_token_digests
        )

    def _make_room_for_fingerprint(self, now: float, window_seconds: int) -> None:
        max_fingerprints = max(self._config.max_fingerprints, 1)
        stale = [
            fingerprint
            for fingerprint, bucket in self._fingerprints.items()
            if now - bucket.window_started >= window_seconds
        ]
        for fingerprint in stale:
            self._fingerprints.pop(fingerprint, None)
        if len(self._fingerprints) < max_fingerprints:
            return
        oldest = min(
            self._fingerprints,
            key=lambda fingerprint: self._fingerprints[fingerprint].window_started,
        )
        self._fingerprints.pop(oldest, None)


_floodgate = _SentryFloodgate(SentryFloodgateConfig())


def init_sentry(settings: Settings) -> None:
    if not sentry_should_init(settings):
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
            # INFO is Axiom-only by design; Sentry logs stay WARNING+ so chatty
            # INFO can never crowd out error events in the floodgate.
            LoggingIntegration(level=None, event_level=None, sentry_logs_level=logging.WARNING),
            # 405 is reported alongside 5xx. The SDK default is 5xx only, while
            # Starlette answers a wrong-method internal probe at the router
            # without raising an exception for Sentry to catch.
            #
            # 405 is retained only for an internal worker presenting the exact
            # configured token because that indicates a route contract we can
            # fix. before_send drops public wrong-method traffic, including
            # spoofable same-origin headers, before it can consume the Sentry
            # budget. Other 4xx stay unreported:
            # 401/402/404 are routine and would drown the signal.
            StarletteIntegration(
                transaction_style="endpoint",
                failed_request_status_codes=SENTRY_FAILED_REQUEST_STATUS_CODES,
            ),
            FastApiIntegration(
                transaction_style="endpoint",
                failed_request_status_codes=SENTRY_FAILED_REQUEST_STATUS_CODES,
            ),
        ],
        # cast over Sentry's TypedDict event/log signatures — the scrubbers
        # operate on whatever shape Sentry hands them, and we don't want to
        # depend on private TypedDict imports to stay in sync.
        before_send=cast(Any, before_send),
        before_send_log=cast(Any, before_send_log),
        before_breadcrumb=cast(Any, before_breadcrumb),
    )


def sentry_should_init(
    settings: Settings,
    *,
    running_under_pytest: bool | None = None,
) -> bool:
    if not settings.sentry_dsn:
        return False
    if settings.environment.lower() == "local" and not settings.sentry_local_enabled:
        return False
    if running_under_pytest is None:
        running_under_pytest = _running_under_pytest(settings)
    if running_under_pytest:
        return False
    return True


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
    _fingerprint_method_not_allowed(event)
    request = event.get("request")
    if isinstance(request, MutableMapping):
        request.pop("data", None)
        request.pop("cookies", None)
    if not _floodgate.allow(event):
        return None
    return event


def before_send_log(
    event: dict[str, Any], hint: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    if _is_dropped_noise(event):
        return None
    event = _scrub(event)
    if not _floodgate.allow(event):
        return None
    return event


def before_breadcrumb(
    crumb: dict[str, Any], hint: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    if _is_dropped_noise(crumb):
        return None
    return _scrub(crumb)


def _is_dropped_noise(event: dict[str, Any]) -> bool:
    text = json.dumps(event, default=str)
    if (
        "Failed to export metrics to Cloud Monitoring" in text
        and "spanner.googleapis.com/internal/client/" in text
        and "missing (instance_id)" in text
    ):
        return True
    return _is_untrusted_405(event)


def _is_untrusted_405(event: dict[str, Any]) -> bool:
    """Keep authenticated internal 405s without reporting public probes.

    Public endpoints receive wrong-method requests continuously. A 405 is a
    product signal only when it came from an authenticated internal worker.
    Origin and Referer are caller-controlled request headers, so even an exact
    same-origin value is not evidence that a browser rendered one of our pages.
    SDK/API callers can choose arbitrary HTTP methods, so every other 405 from
    the public internet is not a server regression.
    """
    if not _is_method_not_allowed_event(event):
        return False

    request = event.get("request")
    if not isinstance(request, Mapping):
        return True
    headers = _request_headers(request)
    authorization = headers.get("authorization", "")
    bearer = ""
    if authorization.casefold().startswith("bearer "):
        bearer = authorization.split(" ", 1)[1].strip()
    supplied = bearer or headers.get("x-trustedrouter-internal-token")
    return not _floodgate.trusts_internal_token(supplied)


def _is_method_not_allowed_event(event: Mapping[str, Any]) -> bool:
    contexts = event.get("contexts")
    if isinstance(contexts, Mapping):
        response = contexts.get("response")
        if isinstance(response, Mapping) and str(response.get("status_code")) == "405":
            return True

    exception = event.get("exception")
    values = exception.get("values") if isinstance(exception, Mapping) else None
    if not isinstance(values, list):
        return False
    for value in values:
        if not isinstance(value, Mapping):
            continue
        exception_type = str(value.get("type") or "").lower()
        exception_value = str(value.get("value") or "").lower()
        if "httpexception" in exception_type and "method not allowed" in exception_value:
            return True
    return False


def _request_headers(request: Mapping[str, Any]) -> dict[str, str]:
    raw_headers = request.get("headers")
    if isinstance(raw_headers, Mapping):
        return {str(name).lower(): str(value) for name, value in raw_headers.items()}
    if isinstance(raw_headers, list):
        headers: dict[str, str] = {}
        for item in raw_headers:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                headers[str(item[0]).lower()] = str(item[1])
        return headers
    return {}


def _fingerprint_method_not_allowed(event: dict[str, Any]) -> None:
    if not _is_method_not_allowed_event(event) or event.get("fingerprint"):
        return
    request = event.get("request")
    if not isinstance(request, Mapping):
        return
    method = str(request.get("method") or "UNKNOWN").upper()
    if method not in {
        "CONNECT",
        "DELETE",
        "GET",
        "HEAD",
        "OPTIONS",
        "PATCH",
        "POST",
        "PUT",
        "TRACE",
    }:
        method = "OTHER"
    event["fingerprint"] = ["http-405", method, _safe_route_identity(event)]


def _safe_route_identity(event: Mapping[str, Any]) -> str:
    """Return only an SDK endpoint/route identity, never the raw target.

    The Sentry Starlette/FastAPI integrations set ``component`` for our
    configured endpoint-style transaction names and ``route`` for route-table
    templates/fallbacks. If that server-derived provenance is absent, a
    constant fallback deliberately coalesces all unmatched paths. Request
    URLs, Origin, and Referer are attacker controlled and therefore cannot
    contribute to Sentry issue cardinality.
    """
    transaction_info = event.get("transaction_info")
    if not isinstance(transaction_info, Mapping):
        return "unresolved-route"
    if str(transaction_info.get("source") or "").casefold() not in {
        "component",
        "route",
    }:
        return "unresolved-route"
    transaction = event.get("transaction")
    if not isinstance(transaction, str):
        return "unresolved-route"
    normalized = " ".join(transaction.split())
    if not normalized:
        return "unresolved-route"
    return normalized[:256]


def configure_sentry_floodgate(settings: Settings) -> None:
    global _floodgate
    _floodgate = _SentryFloodgate(_floodgate_config(settings))


def reset_sentry_floodgate_for_tests(
    *,
    settings: Settings | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    global _floodgate
    if settings is None:
        _floodgate = _SentryFloodgate(SentryFloodgateConfig(), clock=clock)
        return
    _floodgate = _SentryFloodgate(_floodgate_config(settings), clock=clock)


def _floodgate_config(settings: Settings) -> SentryFloodgateConfig:
    internal_tokens = {
        value
        for value in (
            settings.internal_gateway_token,
            settings.observer_internal_token,
        )
        if value
    }
    return SentryFloodgateConfig(
        enabled=settings.sentry_floodgate_enabled,
        window_seconds=settings.sentry_floodgate_window_seconds,
        max_events_per_fingerprint=settings.sentry_floodgate_max_events_per_fingerprint,
        max_events_per_window=settings.sentry_floodgate_max_events_per_window,
        max_fingerprints=settings.sentry_floodgate_max_fingerprints,
        trusted_internal_token_digests=frozenset(
            hashlib.sha256(value.encode("utf-8")).digest() for value in internal_tokens
        ),
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


# Walk limits. A log extra is not a data structure we control, so the scrubber
# has to terminate on a cyclic or pathological one rather than recursing until
# the interpreter gives up *inside a logging filter*.
_SCRUB_MAX_DEPTH = 12
_SCRUB_MAX_ITEMS = 500

# Types whose str() is structurally incapable of carrying a secret (hex and
# dashes, digits, ISO timestamps) and which appear constantly in log extras.
# Everything outside this set is replaced by its type name rather than
# stringified: calling repr() on an arbitrary object can execute application
# code, raise, recurse, be enormous, or expose a value in a format the
# blocklist does not recognise.
_SAFE_REPR_TYPES = (UUID, datetime, date, _dt_time, timedelta, Decimal)


def _scrub(value: Any, *, _depth: int = 0, _seen: frozenset[int] = frozenset()) -> Any:
    """Redact secrets from an arbitrary logging value.

    Totality matters here because this is the last barrier on the Axiom path:
    `_AxiomScrubFilter` runs every log-record attribute through it and hands
    the record straight to the shipper. It previously recursed into dict, list
    and str only and returned everything else untouched, so a secret inside a
    tuple extra was serialised and shipped verbatim.

    The contract is now a whitelist rather than a blacklist: a value is only
    returned unchanged if it is a scalar that cannot hold a string.
    """
    if _depth > _SCRUB_MAX_DEPTH:
        return "[Filtered-depth]"

    if isinstance(value, str):
        return _scrub_string(value)

    # Scalars that cannot carry a string secret. bool is checked implicitly —
    # it is an int subclass and equally safe.
    if value is None or isinstance(value, (bool, int, float)):
        return value

    # Bytes are redacted wholesale rather than decoded and blocklist-scrubbed:
    # a key can reach a log as base64, hex, or some framing the fragment list
    # does not know, and there is no diagnostic value in the payload anyway.
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "[Filtered-bytes]"

    # Cycle guard, keyed on the path rather than globally so that a value
    # legitimately repeated across sibling branches is still scrubbed in each.
    marker = id(value)
    if marker in _seen:
        return "[Filtered-cycle]"
    seen = _seen | {marker}
    child_depth = _depth + 1

    if isinstance(value, Mapping):
        out: dict[Any, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= _SCRUB_MAX_ITEMS:
                out["[truncated]"] = f"{len(value) - _SCRUB_MAX_ITEMS} more"
                break
            # The KEY is a string that leaves the process too. A secret can be
            # the key rather than the value — {"sk-tr-v1-...": 3} is what a
            # per-key counter or cache-hit tally looks like — and scrubbing only
            # values left it intact. Scrub the key with the same string rules
            # before deciding anything about the value under it.
            safe_key = _scrub_string(key) if isinstance(key, str) else key
            if _is_sensitive_key(str(key)):
                out[safe_key] = "[Filtered]"
            else:
                out[safe_key] = _scrub(item, _depth=child_depth, _seen=seen)
        return out

    if isinstance(value, (set, frozenset)):
        # Deterministically ordered so two runs of the same event produce the
        # same record; sorted on the scrubbed text since the members may not be
        # mutually comparable.
        return sorted(
            (str(_scrub(item, _depth=child_depth, _seen=seen)) for item in _limited(value)),
        )

    if isinstance(value, (list, tuple)):
        scrubbed = [_scrub(item, _depth=child_depth, _seen=seen) for item in _limited(value)]
        if len(value) > _SCRUB_MAX_ITEMS:
            scrubbed.append(f"[truncated {len(value) - _SCRUB_MAX_ITEMS} more]")
        return tuple(scrubbed) if isinstance(value, tuple) else scrubbed

    if isinstance(value, _SAFE_REPR_TYPES):
        return _scrub_string(str(value))

    # Unknown object: name the type so the log still says what was there, but
    # never call repr() on it.
    return f"[Filtered-{type(value).__name__}]"


def _limited(items: Iterable[Any]) -> list[Any]:
    """First _SCRUB_MAX_ITEMS elements, so one enormous extra cannot stall the
    logging filter it is being scrubbed inside."""
    out: list[Any] = []
    for item in items:
        if len(out) >= _SCRUB_MAX_ITEMS:
            break
        out.append(item)
    return out


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
