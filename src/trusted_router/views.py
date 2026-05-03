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


@lru_cache(maxsize=1)
def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(["html"]),
        keep_trailing_newline=True,
    )


def render_template(name: str, **context: Any) -> str:
    return _env().get_template(name).render(**context)
