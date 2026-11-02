"""Repo-wide guard: route handlers never run synchronous IO on the event loop.

FastAPI runs a plain ``def`` endpoint on AnyIO's worker thread pool but runs an
``async def`` endpoint directly on the event loop. An ``async def`` handler that
never awaits gains nothing from being a coroutine, and if it calls the Store it
freezes every other request on the instance for the duration of that call.

That was the 2026-10-30 europe-west4 freeze: the loop-stall watchdog caught the
main thread inside ``list_destinations`` (an ``async def`` route) waiting on a
Spanner streaming read, while /internal/gateway/settle and /validate on the same
instance timed out behind it.

The console (tests/test_console_event_loop_isolation.py) and gateway
(tests/test_gateway_event_loop_isolation.py) guards cover their own modules.
This one covers every endpoint the app can serve on any surface:

* the inventory is enumerated from the built apps (every service surface, with
  the optional route groups switched on), so routes registered through helpers
  such as ``public_html_route`` or ``add_api_route`` are included, plus a
  static AST pass over the route modules for anything registered behind other
  settings;
* no endpoint may be ``async def`` without an ``await``;
* an ``async def`` endpoint that does await may not reach the Store on the loop:
  no ``STORE.<method>`` reference, no call to a same-module helper that reads the
  Store, and no call to the Store-backed auth helpers, except as the callable
  handed to ``run_in_threadpool``;
* a behavioural test proves /health keeps answering while a broadcast Store read
  is blocked, and that the read runs on a worker thread.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import re
import textwrap
import threading
from collections.abc import Callable
from functools import cache
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest

from tests.route_inventory import effective_routes
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.storage import STORE, InMemoryStore

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src" / "trusted_router"
_SURFACES = ("combined", "public", "control", "internal", "observer", "actions", "settle-drain")

# Endpoints that stay ``async def`` without awaiting, each with its reason.
# Keep this list short: anything that touches the Store, KMS, a template, or
# a provider API belongs in a plain ``def``.
_ASYNC_WITHOUT_AWAIT_ALLOWED = {
    "trusted_router.main:_make_api_router.<locals>.health": (
        "Cloud Run liveness: answered on the loop so it never queues behind "
        "worker-pool saturation; no IO."
    ),
    "trusted_router.services.settle_outbox_worker:create_worker_app.<locals>.health": (
        "Settle-drain worker liveness, same reasoning as the main /health."
    ),
    "trusted_router.main:create_app.<locals>.public_openapi": (
        "Serves bytes pre-serialized at startup (a Starlette Route, outside "
        "the application inventory); no IO."
    ),
}

# Async endpoints that still reach the Store on the loop, each with its reason.
_ASYNC_STORE_ON_LOOP_ALLOWED: dict[str, str] = {}

# Synchronous auth helpers that resolve a principal from the Store.
_STORE_BACKED_AUTH_HELPERS = frozenset(
    {
        "principal_from_request",
        "principal_user",
        "require_authenticated",
        "require_console_context",
        "require_inference_key",
        "require_management",
    }
)
_ROUTE_METHODS = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options", "api_route", "websocket"}
)


def _settings(surface: str) -> Settings:
    return Settings(
        environment="test",
        service_surface=surface,
        # Mount the optional internal route groups too.
        operator_token="operator-secret",  # noqa: S106 - test credential.
        operator_identities="ops@example.com",
        routing_snapshot_publish_enabled=True,
        edge_policy_publish_enabled=True,
    )


@cache
def _served_endpoints() -> dict[str, tuple[Callable[..., Any], frozenset[str]]]:
    endpoints: dict[str, tuple[Callable[..., Any], set[str]]] = {}
    for surface in _SURFACES:
        app = create_app(_settings(surface), configure_store_arg=False, init_observability=False)
        for _path, route in effective_routes(app):
            endpoint = getattr(route, "endpoint", None)
            if endpoint is None or not endpoint.__module__.startswith("trusted_router"):
                continue
            key = f"{endpoint.__module__}:{endpoint.__qualname__}"
            endpoints.setdefault(key, (endpoint, set()))[1].add(surface)
    return {key: (fn, frozenset(surfaces)) for key, (fn, surfaces) in endpoints.items()}


def _function_node(fn: Callable[..., Any]) -> ast.FunctionDef | ast.AsyncFunctionDef:
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    node = tree.body[0]
    assert isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    return node


def _walk_own_body(function: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.AST]:
    """The function's own statements; nested defs/lambdas are worker callbacks."""
    nodes: list[ast.AST] = []
    stack: list[ast.AST] = list(function.body)
    while stack:
        node = stack.pop()
        nodes.append(node)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            continue
        stack.extend(ast.iter_child_nodes(node))
    return nodes


def _awaits(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(
        isinstance(node, ast.Await | ast.AsyncFor | ast.AsyncWith)
        for node in _walk_own_body(function)
    )


@cache
def _store_helpers(module_path: str) -> frozenset[str]:
    """Module-level sync functions that reach the Store, transitively: they
    reference STORE or call one of the Store-backed auth helpers."""
    tree = ast.parse(Path(module_path).read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    names = {
        name: {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
        for name, fn in functions.items()
    }
    seeds = {"STORE"} | _STORE_BACKED_AUTH_HELPERS
    touched = {name for name, used in names.items() if used & seeds}
    changed = True
    while changed:
        changed = False
        for name, used in names.items():
            if name not in touched and used & touched:
                touched.add(name)
                changed = True
    return frozenset(touched)


def _store_on_loop(fn: Callable[..., Any]) -> set[str]:
    node = _function_node(fn)
    own = _walk_own_body(node)
    offloaded: set[int] = set()
    for item in own:
        if (
            isinstance(item, ast.Call)
            and isinstance(item.func, ast.Name)
            and item.func.id == "run_in_threadpool"
            and item.args
        ):
            offloaded.update(id(child) for child in ast.walk(item.args[0]))
    source_file = inspect.getsourcefile(fn)
    assert source_file is not None
    helpers = _store_helpers(source_file) | _STORE_BACKED_AUTH_HELPERS
    # A nested sync worker that reaches the Store is only safe when it is the
    # callable handed to run_in_threadpool; calling it inline runs it on the loop.
    for item in own:
        if isinstance(item, ast.FunctionDef) and any(
            (isinstance(n, ast.Name) and (n.id == "STORE" or n.id in helpers))
            for n in ast.walk(item)
        ):
            helpers = helpers | {item.name}
    hits: set[str] = set()
    for item in own:
        if id(item) in offloaded:
            continue
        if (
            isinstance(item, ast.Attribute)
            and isinstance(item.value, ast.Name)
            and item.value.id == "STORE"
        ):
            hits.add(f"STORE.{item.attr}")
        elif isinstance(item, ast.Name) and item.id in helpers:
            hits.add(item.id)
    return hits


def _is_route_decorator(decorator: ast.expr) -> bool:
    if not isinstance(decorator, ast.Call):
        return False
    func = decorator.func
    if isinstance(func, ast.Attribute):
        return func.attr in _ROUTE_METHODS
    return isinstance(func, ast.Name) and func.id.endswith("_route")


def _static_async_routes() -> dict[str, ast.AsyncFunctionDef]:
    found: dict[str, ast.AsyncFunctionDef] = {}
    paths = [*sorted((_SRC / "routes").rglob("*.py")), _SRC / "main.py"]
    paths += [_SRC / "services" / "settle_outbox_worker.py"]
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and any(
                _is_route_decorator(d) for d in node.decorator_list
            ):
                found[f"{path.relative_to(_ROOT)}:{node.lineno}:{node.name}"] = node
    return found


def test_inventory_is_non_vacuous() -> None:
    endpoints = _served_endpoints()
    assert len(endpoints) > 400, "guard must enumerate the whole route inventory"
    surfaces = set().union(*(s for _fn, s in endpoints.values()))
    assert surfaces == set(_SURFACES)
    assert "trusted_router.routes.broadcast:register_broadcast_routes.<locals>.list_destinations" in endpoints
    # Registered through the public_html_route helper, not a router decorator.
    assert "trusted_router.routes.public:register_public_routes.<locals>.apps" in endpoints
    assert len(_static_async_routes()) > 50


def test_no_served_endpoint_is_async_without_await() -> None:
    offenders = sorted(
        key
        for key, (fn, _surfaces) in _served_endpoints().items()
        if inspect.iscoroutinefunction(fn)
        and not _awaits(_function_node(fn))
        and key not in _ASYNC_WITHOUT_AWAIT_ALLOWED
    )
    assert offenders == [], (
        "These endpoints are async def but never await, so FastAPI runs them on "
        "the event loop. Make them plain def (worker thread):\n" + "\n".join(offenders)
    )


def test_async_without_await_allowlist_is_current() -> None:
    endpoints = _served_endpoints()
    for key in _ASYNC_WITHOUT_AWAIT_ALLOWED:
        assert key in endpoints, f"stale allowlist entry {key}"
        fn = endpoints[key][0]
        assert inspect.iscoroutinefunction(fn) and not _awaits(_function_node(fn)), key


def test_no_route_module_declares_async_route_without_await() -> None:
    """Static pass: also covers routes mounted only under other settings."""
    allowed_names = {key.rsplit(".", 1)[-1] for key in _ASYNC_WITHOUT_AWAIT_ALLOWED}
    offenders = sorted(
        key
        for key, node in _static_async_routes().items()
        if not _awaits(node) and node.name not in allowed_names
    )
    assert offenders == [], "\n".join(offenders)


def test_async_endpoints_keep_store_access_off_the_loop() -> None:
    offenders = {}
    for key, (fn, _surfaces) in sorted(_served_endpoints().items()):
        if not inspect.iscoroutinefunction(fn) or key in _ASYNC_STORE_ON_LOOP_ALLOWED:
            continue
        hits = _store_on_loop(fn)
        if hits:
            offenders[key] = sorted(hits)
    assert offenders == {}, (
        "These async endpoints reach the Store on the event loop. Make them plain "
        "def, or pass the synchronous segment to run_in_threadpool:\n"
        + "\n".join(f"{k}: {v}" for k, v in offenders.items())
    )


def test_store_on_loop_detector_is_not_vacuous() -> None:
    async def direct(request: Any) -> Any:
        await asyncio.sleep(0)
        return STORE.list_broadcast_destinations("ws")

    async def via_auth(request: Any, settings: Any) -> Any:
        from trusted_router.auth import principal_from_request

        await asyncio.sleep(0)
        return principal_from_request(request, settings)

    async def offloaded(request: Any) -> Any:
        from starlette.concurrency import run_in_threadpool

        def worker() -> Any:
            return STORE.list_broadcast_destinations("ws")

        await run_in_threadpool(STORE.get_workspace, "ws")
        return await run_in_threadpool(worker)

    async def inline_worker(request: Any) -> Any:
        await asyncio.sleep(0)

        def worker() -> Any:
            return STORE.list_broadcast_destinations("ws")

        return worker()

    assert _store_on_loop(direct) == {"STORE.list_broadcast_destinations"}
    assert _store_on_loop(inline_worker) == {"worker"}
    assert _store_on_loop(via_auth) == {"principal_from_request"}
    assert _store_on_loop(offloaded) == set()


async def _wait_until_started(started: threading.Event) -> None:
    for _ in range(400):
        if started.is_set():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("blocked Store call never started")


def test_blocked_broadcast_store_read_does_not_freeze_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The production incident, reproduced: block the Spanner read under
    GET /v1/broadcast/destinations and require /health to answer meanwhile."""
    app = create_app(Settings(environment="test"), init_observability=False)
    user = STORE.ensure_user("broadcast-loop@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    raw_key, _key = STORE.create_api_key(
        workspace_id=workspace.id,
        name="mgmt",
        creator_user_id=user.id,
        management=True,
    )
    started = threading.Event()
    release = threading.Event()
    observed: dict[str, Any] = {}
    original = InMemoryStore.list_broadcast_destinations

    def blocking_list(self: InMemoryStore, workspace_id: str) -> Any:
        observed["thread"] = threading.get_ident()
        started.set()
        # On a regression this runs ON the loop: /health cannot be served, so
        # nobody sets release and the wait times out (recorded, not hung).
        observed["released_while_blocked"] = release.wait(timeout=2.0)
        return original(self, workspace_id)

    monkeypatch.setattr(InMemoryStore, "list_broadcast_destinations", blocking_list)

    async def scenario() -> None:
        loop_thread = threading.get_ident()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            listing = asyncio.create_task(
                client.get(
                    "/v1/broadcast/destinations",
                    headers={"Authorization": f"Bearer {raw_key}"},
                )
            )
            try:
                await _wait_until_started(started)
                health = await asyncio.wait_for(client.get("/health"), timeout=1.0)
                assert health.status_code == 200
            finally:
                release.set()
            response = await asyncio.wait_for(listing, timeout=5.0)
            assert response.status_code == 200, response.text
            assert response.json() == {"data": []}
        assert observed["thread"] != loop_thread, "Store read ran on the event loop"
        assert observed["released_while_blocked"], "/health waited for the Store read"

    asyncio.run(asyncio.wait_for(scenario(), timeout=10.0))


_CONCURRENCY_SOURCES = (
    ("scripts/deploy/_lib.sh", r'TR_CLOUD_RUN_CONCURRENCY="\$\{TR_CLOUD_RUN_CONCURRENCY:-(\d+)\}"'),
    ("scripts/deploy/public_surface.sh", r'PUBLIC_CONCURRENCY="\$\{TR_PUBLIC_CONCURRENCY:-(\d+)\}"'),
    ("scripts/deploy/internal_surface.sh", r"--concurrency (\d+) "),
)


def test_worker_pool_covers_configured_request_concurrency() -> None:
    """Sizing assumption behind running handlers on AnyIO's default pool.

    A request holds at most one worker token at a time: FastAPI resolves each
    sync dependency and then the sync endpoint one after another. So the hard
    per-instance Cloud Run concurrency bounds request-held tokens. Keep it at
    or below half of the default 40-token limiter, leaving the rest for
    explicit run_in_threadpool fan-out (e.g. the console's gathered reads) and
    calls still finishing after a client disconnect. Raise the limiter at
    startup before raising any of these.
    """
    configured = {}
    for relative, pattern in _CONCURRENCY_SOURCES:
        text = (_ROOT / relative).read_text(encoding="utf-8")
        values = [int(v) for v in re.findall(pattern, text)]
        assert values, f"{relative}: concurrency setting not found"
        configured[relative] = max(values)

    async def limiter_tokens() -> float:
        create_app(Settings(environment="test"), init_observability=False)
        return anyio.to_thread.current_default_thread_limiter().total_tokens

    total = asyncio.run(limiter_tokens())
    assert total >= 40
    assert max(configured.values()) * 2 <= total, configured
