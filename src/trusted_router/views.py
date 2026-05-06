"""Shared Jinja2 environment for the route modules.

Every route module that renders HTML imports `render_template` from
here so we have one Jinja env, one search path, and consistent
autoescape behaviour. Adding a new auth/console/marketing page is just
dropping a `.html` under templates/ — no per-module Jinja boilerplate.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATES_DIR = Path(__file__).parent / "templates"


def _format_uptime(value: float | None, decimals: int = 4) -> str:
    """Render an uptime percentage. Caps display at "99.99%" — claiming
    a literal 100.0000% with a few hundred probe samples behind it is
    overconfident; "99.99%+" reads honest, matches what
    status.anthropic.com / status.github.com surface, and stops the eye
    from interpreting "100%" as a guarantee.

    Threshold is `>= 99.995` so values that would round to 100 at 4
    decimals of precision get the cap; anything that actually rounds
    below that shows its real number."""
    if value is None:
        return "n/a"
    if value >= 99.995:
        return ">99.99%"
    return f"{value:.{decimals}f}%"


@lru_cache(maxsize=1)
def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(["html"]),
        keep_trailing_newline=True,
    )
    env.filters["uptime_pct"] = _format_uptime
    return env


def render_template(name: str, **context: Any) -> str:
    return _env().get_template(name).render(**context)
