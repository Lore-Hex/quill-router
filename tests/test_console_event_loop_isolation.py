"""Regression coverage for control-plane event-loop isolation.

Console pages are dominated by synchronous storage, KMS, payment-provider, and
template work.  FastAPI only moves that work to AnyIO's worker pool when the
decorated endpoint is a plain ``def``.  The four console endpoints that really
await network I/O or coordinate concurrent provider calls keep ``async def`` and
place each synchronous segment behind ``run_in_threadpool`` explicitly.

The AST checks make the 59/4 split non-vacuous; the thread-id and heartbeat
checks prove the framework/runtime behavior instead of merely matching source
text.  Storage methods are patched on ``InMemoryStore`` rather than the global
``STORE`` proxy so pytest restores them without poisoning later tests.
"""

from __future__ import annotations

import ast
import asyncio
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.routes.console import credits as console_credits
from trusted_router.storage import STORE, InMemoryStore
from trusted_router.storage_rate_limits import InMemoryRateLimits

_ROOT = Path(__file__).resolve().parents[1]
_CONSOLE_DIR = _ROOT / "src" / "trusted_router" / "routes" / "console"
_ASYNC_CONSOLE_HANDLERS = {
    "console_credit_stripe_details",
}


def _console_route_functions() -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    functions: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for path in sorted(_CONSOLE_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for decorator in node.decorator_list:
                if not (
                    isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and decorator.args
                    and isinstance(decorator.args[0], ast.Constant)
                    and isinstance(decorator.args[0].value, str)
                    and decorator.args[0].value.startswith("/console")
                ):
                    continue
                functions.append(node)
                break
    return functions


def _walk_without_nested_functions(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[ast.AST]:
    nodes: list[ast.AST] = []
    stack: list[ast.AST] = list(function.body)
    while stack:
        node = stack.pop()
        nodes.append(node)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            continue
        stack.extend(ast.iter_child_nodes(node))
    return nodes


def _offload_targets(function: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    targets: list[str] = []
    for node in _walk_without_nested_functions(function):
        if not (
            isinstance(node, ast.Await)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "run_in_threadpool"
            and node.value.args
        ):
            continue
        targets.append(ast.unparse(node.value.args[0]))
    return targets


def _threadpool_targets(function: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """Include workers passed to an awaited ``asyncio.gather`` call."""
    return [
        ast.unparse(node.args[0])
        for node in _walk_without_nested_functions(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "run_in_threadpool"
        and node.args
    ]


def _nested_function(
    function: ast.FunctionDef | ast.AsyncFunctionDef, name: str
) -> ast.FunctionDef | ast.AsyncFunctionDef:
    match = next(
        (
            node
            for node in ast.walk(function)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node is not function
            and node.name == name
        ),
        None,
    )
    assert match is not None, f"{function.name} lost its {name} worker callback"
    return match


def test_console_route_async_split_and_worker_boundaries_are_total() -> None:
    routes = _console_route_functions()
    assert len(routes) == 52, "guard must inspect every decorated console route"
    async_routes = {node.name for node in routes if isinstance(node, ast.AsyncFunctionDef)}
    assert async_routes == _ASYNC_CONSOLE_HANDLERS
    assert sum(isinstance(node, ast.FunctionDef) for node in routes) == 51

    by_name = {node.name: node for node in routes}
    for name in ("confirm_account_deletion", "review_account_deletion", "delete_account"):
        assert isinstance(by_name[name], ast.FunctionDef)
    assert "STORE.close_user_account" in ast.unparse(by_name["delete_account"])
    assert set(_threadpool_targets(by_name["console_credit_stripe_details"])) == {
        "STORE.get_credit_account",
        "_load_saved_payment_method",
        "_load_workspace_payments",
        "render_stripe_details",
    }
    assert not any("user_model" in name or "custom_model" in name for name in by_name)

    stripe_render_callback = ast.unparse(
        _nested_function(by_name["console_credit_stripe_details"], "render_stripe_details")
    )
    assert "render_template" in stripe_render_callback
    assert "is_wallet_only_user" in stripe_render_callback


def test_limiter_stays_storage_free() -> None:
    path = _ROOT / "src" / "trusted_router" / "middleware.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    limiter = next(node for node in ast.walk(tree)
                   if isinstance(node, ast.AsyncFunctionDef) and node.name == "_rate_limit_request")
    assert _offload_targets(limiter) == []
    source = ast.unparse(limiter)
    assert "ingress_rate_limits.hit" in source
    assert "STORE.hit_rate_limit" not in source
    assert "run_in_threadpool" not in source


async def _wait_until_started(started: threading.Event) -> None:
    for _ in range(400):
        if started.is_set():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("blocked operation never started")


async def _five_heartbeats() -> None:
    for _ in range(5):
        await asyncio.sleep(0.005)


def _active_console_session(app: Any, email: str) -> str:
    del app
    user = STORE.ensure_user(email)
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    raw_session, _ = STORE.create_auth_session(
        user_id=user.id,
        provider="test",
        label=email,
        ttl_seconds=3600,
        state="active",
        workspace_id=workspace.id,
    )
    return raw_session


def test_sync_console_handler_runs_storage_off_loop_and_keeps_heartbeat_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(Settings(environment="test"), init_observability=False)
    raw_session = _active_console_session(app, "console-loop@example.com")
    started = threading.Event()
    release = threading.Event()
    storage_thread: dict[str, int] = {}
    original = InMemoryStore.list_api_keys_with_usage

    def blocking_list_api_keys_with_usage(self: InMemoryStore, workspace_id: str) -> Any:
        storage_thread["id"] = threading.get_ident()
        started.set()
        assert release.wait(timeout=2.0), "console storage ran on the blocked event loop"
        return original(self, workspace_id)

    monkeypatch.setattr(
        InMemoryStore,
        "list_api_keys_with_usage",
        blocking_list_api_keys_with_usage,
    )

    async def scenario() -> None:
        loop_thread = threading.get_ident()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            cookies={"tr_session": raw_session},
        ) as client:
            request = asyncio.create_task(client.get("/console/api-keys"))
            try:
                await _wait_until_started(started)
                await _five_heartbeats()
                assert storage_thread["id"] != loop_thread
            finally:
                release.set()
            response = await asyncio.wait_for(request, timeout=5.0)
            assert response.status_code == 200

    asyncio.run(asyncio.wait_for(scenario(), timeout=10.0))


def test_stripe_fragment_template_render_runs_off_loop_and_keeps_heartbeat_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(Settings(environment="test"), init_observability=False)
    raw_session = _active_console_session(app, "stripe-render-loop@example.com")
    started = threading.Event()
    release = threading.Event()
    render_thread: dict[str, int] = {}
    original_render = console_credits.render_template

    monkeypatch.setattr(
        console_credits,
        "_load_saved_payment_method",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        console_credits,
        "_load_workspace_payments",
        lambda *_args, **_kwargs: ([], True),
    )

    def blocking_render(*args: Any, **kwargs: Any) -> str:
        render_thread["id"] = threading.get_ident()
        started.set()
        assert release.wait(timeout=2.0), "Stripe template blocked the event loop"
        return original_render(*args, **kwargs)

    monkeypatch.setattr(console_credits, "render_template", blocking_render)

    async def scenario() -> None:
        loop_thread = threading.get_ident()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            cookies={"tr_session": raw_session},
        ) as client:
            request = asyncio.create_task(client.get("/console/credits/stripe-details"))
            try:
                await _wait_until_started(started)
                await _five_heartbeats()
                assert render_thread["id"] != loop_thread
            finally:
                release.set()
            response = await asyncio.wait_for(request, timeout=5.0)
            assert response.status_code == 200

    asyncio.run(asyncio.wait_for(scenario(), timeout=10.0))


def test_http_rate_limit_never_schedules_durable_store_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(
        Settings(environment="test", rate_limit_enabled=True),
        init_observability=False,
    )
    store_calls = 0

    def reject_store_hit(self: InMemoryStore, **_kwargs: Any) -> Any:
        del self
        nonlocal store_calls
        store_calls += 1
        raise AssertionError("HTTP limiter touched durable storage")

    monkeypatch.setattr(InMemoryStore, "hit_rate_limit", reject_store_hit)

    async def scenario() -> None:
        loop_thread = threading.get_ident()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/v1/signup",
                headers={"x-forwarded-for": "203.0.113.111"},
                json={},
            )
            assert response.status_code == 400
            assert threading.get_ident() == loop_thread
            assert store_calls == 0

    asyncio.run(scenario())


def test_public_read_rate_limit_stays_inline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_threads: list[int] = []
    original = InMemoryRateLimits.hit

    def spy_hit(self: InMemoryRateLimits, **kwargs: Any) -> Any:
        seen_threads.append(threading.get_ident())
        return original(self, **kwargs)

    monkeypatch.setattr(InMemoryRateLimits, "hit", spy_hit)
    app = create_app(
        Settings(environment="test", rate_limit_enabled=True),
        init_observability=False,
    )

    async def scenario() -> None:
        loop_thread = threading.get_ident()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            response = await client.get("/v1/models")
        assert response.status_code == 200
        assert seen_threads == [loop_thread]

    asyncio.run(scenario())
