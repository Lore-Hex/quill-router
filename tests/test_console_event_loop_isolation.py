"""Regression coverage for control-plane event-loop isolation.

Console pages are dominated by synchronous storage, KMS, payment-provider, and
template work.  FastAPI only moves that work to AnyIO's worker pool when the
decorated endpoint is a plain ``def``.  The three console endpoints that really
await network I/O keep ``async def`` and place each synchronous segment behind
``run_in_threadpool`` explicitly.

The AST checks make the 45/3 split non-vacuous; the thread-id and heartbeat
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
from trusted_router.routes.console import user_models as console_user_models
from trusted_router.services import user_model_probe
from trusted_router.storage import STORE, InMemoryStore
from trusted_router.storage_models import UserProvidedModel
from trusted_router.storage_rate_limits import InMemoryRateLimits

_ROOT = Path(__file__).resolve().parents[1]
_CONSOLE_DIR = _ROOT / "src" / "trusted_router" / "routes" / "console"
_ASYNC_CONSOLE_HANDLERS = {
    "console_create_user_model",
    "console_update_user_model",
    "console_clock_in_user_model",
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
    assert len(routes) == 48, "guard must inspect every decorated console route"
    async_routes = {node.name for node in routes if isinstance(node, ast.AsyncFunctionDef)}
    assert async_routes == _ASYNC_CONSOLE_HANDLERS
    assert sum(isinstance(node, ast.FunctionDef) for node in routes) == 45

    by_name = {node.name: node for node in routes}
    assert _offload_targets(by_name["console_create_user_model"]) == ["create_and_render"]
    assert set(_offload_targets(by_name["console_update_user_model"])) == {
        "_require_owner_model",
        "update_model",
    }
    assert (
        _offload_targets(by_name["console_clock_in_user_model"]).count("_require_owner_model") == 1
    )
    assert (
        _offload_targets(by_name["console_clock_in_user_model"]).count(
            "STORE.set_user_model_online"
        )
        == 2
    )

    create_callback = ast.unparse(
        _nested_function(by_name["console_create_user_model"], "create_and_render")
    )
    for blocking_call in (
        "STORE.create_user_model",
        "encrypt_user_model_endpoint_key",
        "encrypt_user_model_signing_secret",
        "_render_page",
    ):
        assert blocking_call in create_callback

    update_callback = ast.unparse(
        _nested_function(by_name["console_update_user_model"], "update_model")
    )
    assert "STORE.update_user_model" in update_callback
    assert "encrypt_user_model_endpoint_key" in update_callback


def test_probe_and_durable_limiter_keep_explicit_worker_boundaries() -> None:
    probe_path = _ROOT / "src" / "trusted_router" / "services" / "user_model_probe.py"
    probe_tree = ast.parse(probe_path.read_text(encoding="utf-8"), filename=str(probe_path))
    probe = next(
        node
        for node in ast.walk(probe_tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "probe_user_model"
    )
    assert "_decrypt_probe_secrets" in _offload_targets(probe)
    recorded = _nested_function(probe, "recorded_result")
    assert _offload_targets(recorded) == ["_recorded_result"]

    middleware_path = _ROOT / "src" / "trusted_router" / "middleware.py"
    middleware_tree = ast.parse(
        middleware_path.read_text(encoding="utf-8"), filename=str(middleware_path)
    )
    limiter = next(
        node
        for node in ast.walk(middleware_tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_rate_limit_request"
    )
    assert _offload_targets(limiter) == ["hit_rate_limit"]
    source = ast.unparse(limiter)
    assert "if durable:" in source
    assert "else:\n            hit = hit_rate_limit" in source


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
    original = InMemoryStore.list_keys

    def blocking_list_keys(self: InMemoryStore, workspace_id: str) -> Any:
        storage_thread["id"] = threading.get_ident()
        started.set()
        assert release.wait(timeout=2.0), "console storage ran on the blocked event loop"
        return original(self, workspace_id)

    monkeypatch.setattr(InMemoryStore, "list_keys", blocking_list_keys)

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


def test_async_console_kms_store_and_template_segment_runs_off_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        environment="test",
        custom_models_require_verification=False,
    )
    app = create_app(settings, init_observability=False)
    raw_session = _active_console_session(app, "async-console-loop@example.com")
    started = threading.Event()
    release = threading.Event()
    threads: dict[str, list[int]] = {"kms": [], "store": [], "template": []}
    original_create = InMemoryStore.create_user_model
    original_endpoint_encrypt = console_user_models.encrypt_user_model_endpoint_key
    original_signing_encrypt = console_user_models.encrypt_user_model_signing_secret
    original_render = console_user_models.render

    async def validated_endpoint(url: str, _settings: Settings) -> str:
        await asyncio.sleep(0)
        return url.rstrip("/")

    def endpoint_encrypt(*args: Any, **kwargs: Any) -> Any:
        threads["kms"].append(threading.get_ident())
        return original_endpoint_encrypt(*args, **kwargs)

    def signing_encrypt(*args: Any, **kwargs: Any) -> Any:
        threads["kms"].append(threading.get_ident())
        return original_signing_encrypt(*args, **kwargs)

    def blocking_create(self: InMemoryStore, **kwargs: Any) -> UserProvidedModel:
        threads["store"].append(threading.get_ident())
        started.set()
        assert release.wait(timeout=2.0), "async console storage blocked the event loop"
        return original_create(self, **kwargs)

    def render_in_worker(*args: Any, **kwargs: Any) -> str:
        threads["template"].append(threading.get_ident())
        return original_render(*args, **kwargs)

    monkeypatch.setattr(console_user_models, "validate_endpoint_url", validated_endpoint)
    monkeypatch.setattr(console_user_models, "encrypt_user_model_endpoint_key", endpoint_encrypt)
    monkeypatch.setattr(console_user_models, "encrypt_user_model_signing_secret", signing_encrypt)
    monkeypatch.setattr(console_user_models, "render", render_in_worker)
    monkeypatch.setattr(InMemoryStore, "create_user_model", blocking_create)

    async def scenario() -> None:
        loop_thread = threading.get_ident()
        transport = httpx.ASGITransport(app=app)
        form = {
            "name": "Worker model",
            "slug": "worker-model",
            "kind": "machine",
            "display_name": "worker",
            "endpoint_url": "https://owner.example/v1",
            "endpoint_api_key": "owner-secret",
            "max_concurrency": "4",
            "prompt_price_microdollars_per_million_tokens": "100",
            "completion_price_microdollars_per_million_tokens": "200",
        }
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            cookies={"tr_session": raw_session},
        ) as client:
            request = asyncio.create_task(client.post("/console/user-models", data=form))
            try:
                await _wait_until_started(started)
                await _five_heartbeats()
                assert threads["store"] == [threads["store"][0]]
                assert threads["store"][0] != loop_thread
                assert threads["kms"] and all(tid != loop_thread for tid in threads["kms"])
            finally:
                release.set()
            response = await asyncio.wait_for(request, timeout=5.0)
            assert response.status_code == 201, response.text
            assert threads["template"] and all(tid != loop_thread for tid in threads["template"])

    asyncio.run(asyncio.wait_for(scenario(), timeout=10.0))


def test_durable_rate_limit_runs_off_loop_and_keeps_heartbeat_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(
        Settings(environment="test", rate_limit_enabled=True),
        init_observability=False,
    )
    started = threading.Event()
    release = threading.Event()
    storage_thread: dict[str, int] = {}
    original = InMemoryStore.hit_rate_limit

    def blocking_hit(self: InMemoryStore, **kwargs: Any) -> Any:
        storage_thread["id"] = threading.get_ident()
        started.set()
        assert release.wait(timeout=2.0), "durable limiter blocked the event loop"
        return original(self, **kwargs)

    monkeypatch.setattr(InMemoryStore, "hit_rate_limit", blocking_hit)

    async def scenario() -> None:
        loop_thread = threading.get_ident()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            request = asyncio.create_task(
                client.post(
                    "/v1/signup",
                    headers={"x-forwarded-for": "203.0.113.111"},
                    json={},
                )
            )
            try:
                await _wait_until_started(started)
                await _five_heartbeats()
                assert storage_thread["id"] != loop_thread
            finally:
                release.set()
            response = await asyncio.wait_for(request, timeout=5.0)
            assert response.status_code == 400

    asyncio.run(asyncio.wait_for(scenario(), timeout=10.0))


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


def test_probe_kms_and_result_recording_run_off_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(environment="test")
    user = STORE.ensure_user("probe-loop@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    model = STORE.create_user_model(
        owner_user_id=user.id,
        owner_workspace_id=workspace.id,
        name="Probe worker",
        slug="probe-worker",
        kind="machine",
        display_name="worker",
        endpoint_url="https://owner.example/v1",
        supports_streaming=False,
    )
    started = threading.Event()
    release = threading.Event()
    threads: dict[str, int] = {}
    original_record = InMemoryStore.record_user_model_probe

    def blocking_decrypt(_model: UserProvidedModel, _settings: Settings) -> Any:
        threads["decrypt"] = threading.get_ident()
        started.set()
        assert release.wait(timeout=2.0), "probe KMS work blocked the event loop"
        return "signing-secret", None

    async def successful_probe(*_args: Any, **_kwargs: Any) -> bytes:
        return b'{"choices":[{"message":{"content":"pong"}}]}'

    def record_result(self: InMemoryStore, model_id: str, **kwargs: Any) -> Any:
        threads["record"] = threading.get_ident()
        return original_record(self, model_id, **kwargs)

    monkeypatch.setattr(user_model_probe, "_decrypt_probe_secrets", blocking_decrypt)
    monkeypatch.setattr(user_model_probe, "_probe_once", successful_probe)
    monkeypatch.setattr(InMemoryStore, "record_user_model_probe", record_result)

    async def scenario() -> None:
        loop_thread = threading.get_ident()
        task = asyncio.create_task(user_model_probe.probe_user_model(model, settings))
        try:
            await _wait_until_started(started)
            await _five_heartbeats()
            assert threads["decrypt"] != loop_thread
        finally:
            release.set()
        result = await asyncio.wait_for(task, timeout=5.0)
        assert result.ok is True
        assert threads["record"] != loop_thread

    asyncio.run(asyncio.wait_for(scenario(), timeout=10.0))
