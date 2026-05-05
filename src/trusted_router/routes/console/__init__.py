"""Server-rendered console pages.

Each page lives in its own module under this package; this __init__
just imports them and exposes a single `register_console_routes(app)`
that wires them all up. Adding a new page is a one-file change plus
one import here.

Auth: every console route requires an active session cookie via
`require_console_context` in `_shared`. Without one, the dep raises a
302 to `/?reason=signin` so the marketing page can pop the sign-in
modal.
"""

from __future__ import annotations

from fastapi import FastAPI

from trusted_router.routes.console import (
    activity,
    api_keys,
    broadcast,
    byok,
    credits,
    preferences,
    root,
    routing_page,
    settings,
    welcome,
)
from trusted_router.routes.console._shared import (
    ConsoleContext,
    ConsoleDep,
    require_console_context,
)


def register_console_routes(app: FastAPI) -> None:
    root.register(app)
    welcome.register(app)
    api_keys.register(app)
    credits.register(app)
    activity.register(app)
    broadcast.register(app)
    byok.register(app)
    routing_page.register(app)
    settings.register(app)
    preferences.register(app)


__all__ = [
    "ConsoleContext",
    "ConsoleDep",
    "register_console_routes",
    "require_console_context",
]
