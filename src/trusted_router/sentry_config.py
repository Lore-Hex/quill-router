from __future__ import annotations

import json
import os
import sys
from collections.abc import MutableMapping
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


def init_sentry(settings: Settings) -> None:
    if not settings.sentry_dsn:
        return
    if _running_under_pytest(settings):
        return
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
    return event


def before_send_log(event: dict[str, Any], hint: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if _is_dropped_noise(event):
        return None
    return _scrub(event)


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
