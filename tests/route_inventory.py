"""Enumerate the paths an app actually serves, across FastAPI versions.

Every route-inventory test used to do ``{route.path for route in app.routes}``,
which was exact while ``include_router(prefix=...)`` flattened its children into
the parent's route list. FastAPI 0.141 stopped doing that: an include now
appears as a single private ``_IncludedRouter`` object holding the sub-router,
so the old comprehension sees one opaque entry where it used to see 192 routes
and reports the rest as missing. The app serves them exactly as before -- this
is a change in what introspection shows, not in what is routed, confirmed by
requesting the paths.

That distinction is why this lives in one file. These inventories are the guard
on which surface mounts which route -- the public/control/internal split -- so
they have to be right, and when FastAPI next changes shape only this file should
need editing.

Works on both layouts: the ``_IncludedRouter`` branch simply never matches on a
version that still flattens.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

_INCLUDED_ROUTER = "_IncludedRouter"


def _path_of(route: Any) -> str:
    """``route.path``, which is what every caller here compared before.

    NOT ``path_format``: the two differ wherever a converter is declared --
    ``/models/{model_id:path}`` vs ``/models/{model_id}`` -- and mixing them
    makes a route look unmounted on one side of a set difference. That is a
    false violation in exactly the tests that guard which surface serves what.
    """
    return str(getattr(route, "path", "") or getattr(route, "path_format", "") or "")


def effective_routes(app_or_router: Any, _prefix: str = "") -> Iterator[tuple[str, Any]]:
    """``(served_path, route)`` for every leaf route, prefixes applied.

    RECURSIVE, because an include can contain another include: the core
    inference routes reach their served paths through two levels
    (``/v1`` + a nested router), and a single-level walk reported
    ``/v1/chat/completions`` as unregistered while the app answered it.
    """
    for route in getattr(app_or_router, "routes", []):
        if type(route).__name__ != _INCLUDED_ROUTER:
            yield _prefix + _path_of(route), route
            continue
        # FastAPI >=0.141: the include is an opaque wrapper holding the router
        # it mounted. Recurse into it carrying the accumulated prefix.
        context = getattr(route, "include_context", None)
        inner = _prefix + str(getattr(context, "prefix", "") or "")
        yield from effective_routes(route.original_router, inner)


def route_paths(app_or_router: Any) -> set[str]:
    return {path for path, _route in effective_routes(app_or_router)}


def route_methods(app_or_router: Any) -> set[tuple[str, str]]:
    """``(path, METHOD)`` pairs, one per method a route declares."""
    return {
        (path, method)
        for path, route in effective_routes(app_or_router)
        for method in getattr(route, "methods", set()) or set()
    }


class EffectiveRoute:
    """A route whose ``.path`` is the path actually served.

    Needed because a child of an include carries its path RELATIVE to that
    include -- ``/gateway/validate``, not ``/internal/gateway/validate``. Code
    that used to read ``route.path`` off a flattened list got the served path
    for free; after FastAPI 0.141 it silently gets the relative one, which
    compares equal to nothing and empties a route inventory without erroring.

    Everything other than ``path`` proxies straight through, so existing
    ``route.methods`` / ``route.endpoint`` / ``isinstance`` checks are unchanged.
    """

    __slots__ = ("path", "_route")

    def __init__(self, path: str, route: Any) -> None:
        self.path = path
        self._route = route

    def __getattr__(self, name: str) -> Any:
        return getattr(self._route, name)

    @property
    def unwrapped(self) -> Any:
        return self._route

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"EffectiveRoute({self.path!r}, {self._route!r})"


def effective_route_objects(app_or_router: Any) -> list[EffectiveRoute]:
    """Every leaf route, each reporting the path it is actually served at."""
    return [EffectiveRoute(path, route) for path, route in effective_routes(app_or_router)]
