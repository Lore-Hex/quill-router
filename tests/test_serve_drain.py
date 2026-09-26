from __future__ import annotations

import asyncio
import errno
import http.client
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from trusted_router import serve

ROOT = Path(__file__).parents[1]


def _server(monkeypatch: pytest.MonkeyPatch, drain: float = 3.0) -> serve.DrainingServer:
    server = serve.DrainingServer(serve.build_config({"PORT": "8080"}), drain_seconds=drain)
    # Keep uvicorn's once-per-second header refresh and callbacks out of the way.
    monkeypatch.setattr(server.config, "callback_notify", None, raising=False)
    return server


def test_first_sigterm_keeps_the_listener_open_until_the_drain_elapses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(serve, "monotonic", lambda: clock[0])
    server = _server(monkeypatch, drain=3.0)

    server.handle_exit(signal.SIGTERM, None)

    # Still accepting: uvicorn only closes the socket once should_exit flips.
    assert server.should_exit is False
    assert server.drain_deadline == 1003.0
    assert asyncio.run(server.on_tick(1)) is False
    clock[0] = 1002.9
    assert asyncio.run(server.on_tick(2)) is False
    assert server.should_exit is False
    clock[0] = 1003.0
    assert asyncio.run(server.on_tick(3)) is True
    assert server.should_exit is True


def test_second_sigterm_during_the_drain_starts_graceful_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(serve, "monotonic", lambda: 50.0)
    server = _server(monkeypatch)
    server.handle_exit(signal.SIGTERM, None)
    assert server.should_exit is False

    server.handle_exit(signal.SIGTERM, None)

    assert server.should_exit is True
    assert asyncio.run(server.on_tick(1)) is True


def test_sigint_starts_graceful_shutdown_without_draining(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _server(monkeypatch)
    server.handle_exit(signal.SIGINT, None)
    assert server.should_exit is True
    assert server.drain_deadline is None


def test_zero_drain_behaves_like_stock_uvicorn(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _server(monkeypatch, drain=0.0)
    server.handle_exit(signal.SIGTERM, None)
    assert server.should_exit is True
    assert server.drain_deadline is None


def test_drain_is_capped_below_cloud_run_sigkill(monkeypatch: pytest.MonkeyPatch) -> None:
    assert serve.drain_seconds_from_env({}) == 3.0
    assert serve.drain_seconds_from_env({serve.DRAIN_SECONDS_ENV: "1.5"}) == 1.5
    assert serve.drain_seconds_from_env({serve.DRAIN_SECONDS_ENV: "5"}) == 3.0
    assert serve.drain_seconds_from_env({serve.DRAIN_SECONDS_ENV: "30"}) == 3.0
    assert serve.drain_seconds_from_env({serve.DRAIN_SECONDS_ENV: "-1"}) == 0.0
    assert serve.drain_seconds_from_env({serve.DRAIN_SECONDS_ENV: "soon"}) == 3.0
    assert serve.drain_seconds_from_env({serve.DRAIN_SECONDS_ENV: " "}) == 3.0
    server = _server(monkeypatch, drain=30.0)
    assert server.drain_seconds == 3.0


@pytest.mark.parametrize("value", ["NaN", "inf", "-inf"])
def test_nonfinite_drain_uses_default_and_warns(
    value: str, caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="trusted_router.serve"):
        assert serve.drain_seconds_from_env({serve.DRAIN_SECONDS_ENV: value}) == 3.0
    assert f"serve.invalid_drain_seconds value={value!r} using default" in caplog.text


def test_drain_logs_start_and_completion(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    clock = [10.0]
    monkeypatch.setattr(serve, "monotonic", lambda: clock[0])
    server = _server(monkeypatch, drain=1.0)
    with caplog.at_level(logging.INFO, logger="trusted_router.serve"):
        server.handle_exit(signal.SIGTERM, None)
        clock[0] = 11.0
        asyncio.run(server.on_tick(1))
    messages = [r.getMessage() for r in caplog.records]
    assert any("serve.sigterm_drain_started drain_seconds=1.0" in m for m in messages)
    assert any("serve.sigterm_drain_complete" in m for m in messages)


def test_config_binds_the_container_port_and_the_app() -> None:
    config = serve.build_config({"PORT": "9090"})
    assert config.app == "trusted_router.main:app"
    assert config.host == "0.0.0.0"  # noqa: S104 - container listener
    assert config.port == 9090
    assert serve.build_config({}).port == 8080
    assert serve.build_config({"PORT": "eight"}).port == 8080
    assert config.timeout_keep_alive == 5
    assert config.proxy_headers is True
    assert config.forwarded_allow_ips == "127.0.0.1"
    assert config.workers == 1


def test_config_honours_keep_alive_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UVICORN_TIMEOUT_KEEP_ALIVE", "17")
    assert serve.build_config().timeout_keep_alive == 17


@pytest.mark.parametrize("value", ["false", "0", "off", "no"])
def test_config_honours_proxy_headers_env(value: str) -> None:
    assert serve.build_config({"UVICORN_PROXY_HEADERS": value}).proxy_headers is False


def test_config_rejects_invalid_proxy_headers_env() -> None:
    with pytest.raises(ValueError, match="UVICORN_PROXY_HEADERS.*expected a boolean"):
        serve.build_config({"UVICORN_PROXY_HEADERS": "maybe"})


def test_config_honours_forwarded_allow_ips_env() -> None:
    assert serve.build_config({"FORWARDED_ALLOW_IPS": "10.0.0.1"}).forwarded_allow_ips == "10.0.0.1"
    config = serve.build_config({
        "FORWARDED_ALLOW_IPS": "10.0.0.1",
        "UVICORN_FORWARDED_ALLOW_IPS": "10.0.0.2,10.0.0.3",
    })
    assert config.forwarded_allow_ips == "10.0.0.2,10.0.0.3"


@pytest.mark.parametrize("name", ["WEB_CONCURRENCY", "UVICORN_WORKERS"])
def test_main_rejects_multiple_workers_before_running(
    name: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(name, "2")
    monkeypatch.setattr(serve.DrainingServer, "run", lambda self: pytest.fail("server ran"))
    with pytest.raises(ValueError, match=rf"{name}.*one worker.*no supervisor"):
        serve.main()


def test_config_accepts_one_worker() -> None:
    # Positive compatibility control; the rejection tests prove worker parsing.
    assert serve.build_config({"WEB_CONCURRENCY": "1", "UVICORN_WORKERS": "1"}).workers == 1


def test_image_runs_the_draining_server() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert 'CMD ["/app/.venv/bin/python", "-m", "trusted_router.serve"]' in dockerfile
    assert "uvicorn trusted_router.main:app" not in dockerfile.replace("\n", " ")
    assert (ROOT / "src/trusted_router/serve.py").is_file()


def test_drain_lines_reach_the_package_logger_when_run_as_main(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The image runs `python -m trusted_router.serve`, so the module executes
    # as "__main__". Its records must still carry the package logger name or
    # the console handler installed on "trusted_router" never sees them.
    import runpy

    import uvicorn

    clock = [500.0]
    monkeypatch.setattr("time.monotonic", lambda: clock[0])

    def fake_run(self: uvicorn.Server, sockets: object = None) -> None:
        self.handle_exit(signal.SIGTERM, None)
        clock[0] += 60.0
        asyncio.run(self.on_tick(1))

    monkeypatch.setattr(uvicorn.Server, "run", fake_run)
    monkeypatch.setenv("PORT", "18099")
    with caplog.at_level(logging.INFO), pytest.raises(SystemExit) as exit_info:
        runpy.run_module("trusted_router.serve", run_name="__main__", alter_sys=True)
    assert exit_info.value.code == 0
    names = {r.name for r in caplog.records if "serve.sigterm_drain" in r.getMessage()}
    assert names == {"trusted_router.serve"}, names
    messages = [r.getMessage() for r in caplog.records if r.name == "trusted_router.serve"]
    assert any("serve.sigterm_drain_started" in m for m in messages)
    assert any("serve.sigterm_drain_complete" in m for m in messages)


@pytest.mark.skipif(sys.platform == "win32", reason="requires POSIX SIGTERM")
@pytest.mark.parametrize("shutdown_signal", [signal.SIGTERM, signal.SIGINT], ids=["sigterm", "sigint"])
def test_real_shutdown_completes_slow_settlement(
    tmp_path: Path, shutdown_signal: signal.Signals,
) -> None:
    # Equivalent to settle_gateway's await run_in_threadpool(sync_body), without
    # authentication or database writes. Exercise real Uvicorn cancellation,
    # HTTP response handling, and OS signals in an isolated process.
    script = """
import asyncio
import logging
import socket
import sys
import time

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from trusted_router import serve

logging.basicConfig(level=logging.INFO)

def settle_sync():
    print("settlement_started", flush=True)
    time.sleep(6)
    print("settlement_completed", flush=True)
    return "settled"

async def settle(request):
    print("settlement_request_started", flush=True)
    if server.drain_deadline is not None:
        # Accept the HTTP request during the drain, then begin the six-second
        # worker near its end. Scheduling here avoids a cross-process race
        # between the parent's sleep and Uvicorn closing the listener.
        assert not server.should_exit
        await asyncio.sleep(max(0, server.drain_deadline - time.monotonic() - 0.1))
    return PlainTextResponse(await run_in_threadpool(settle_sync))

async def health(request):
    return PlainTextResponse("ok")

config = serve.build_config({})
config.app = Starlette(routes=[Route("/settle", settle, methods=["POST"]), Route("/health", health)])
server = serve.DrainingServer(config, drain_seconds=serve.drain_seconds_from_env({}))
server.run(sockets=[socket.socket(fileno=int(sys.argv[1]))])
"""
    log_path = tmp_path / "settlement.log"
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    with socket.socket() as listener, log_path.open("w") as log:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        process = subprocess.Popen(  # noqa: S603 - fixed local test program
            [sys.executable, "-c", script, str(listener.fileno())],
            cwd=tmp_path, env=env, pass_fds=(listener.fileno(),), stdout=log, stderr=log,
        )
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=12)

        def wait_for(message: str) -> None:
            deadline = time.monotonic() + 10
            while message not in log_path.read_text():
                assert process.poll() is None, log_path.read_text()
                assert time.monotonic() < deadline, log_path.read_text()
                time.sleep(0.01)

        try:
            wait_for("Application startup complete")
            # Startup logs precede listen(), so probe until a fresh connection works.
            deadline = time.monotonic() + 10
            while True:
                health = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
                try:
                    health.request("GET", "/health")
                    response = health.getresponse()
                    response.read()
                    assert response.status == 200
                    break
                except OSError:
                    assert time.monotonic() < deadline, log_path.read_text()
                    time.sleep(0.01)
                finally:
                    health.close()

            if shutdown_signal == signal.SIGTERM:
                process.send_signal(shutdown_signal)
                wait_for("serve.sigterm_drain_started")
            connection.request("POST", "/settle", headers={"Connection": "close"})
            wait_for("settlement_request_started")
            if shutdown_signal == signal.SIGTERM:
                logs = log_path.read_text()
                assert logs.index("serve.sigterm_drain_started") < logs.index(
                    "settlement_request_started"
                )
            else:
                wait_for("settlement_started")
                process.send_signal(shutdown_signal)

            response = connection.getresponse()
            body = response.read()
            assert response.status == 200, (body, log_path.read_text())
            assert body == b"settled"
            assert "settlement_completed" in log_path.read_text()
            process.wait(timeout=5)
            assert process.returncode in {0, -shutdown_signal}, log_path.read_text()
            assert "Application shutdown complete" in log_path.read_text()
        finally:
            connection.close()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


@pytest.mark.skipif(sys.platform == "win32", reason="requires POSIX SIGTERM")
def test_real_sigterm_serves_fresh_connection_during_drain(tmp_path: Path) -> None:
    with socket.socket() as probe:
        try:
            probe.bind(("0.0.0.0", 0))  # noqa: S104 - match the container listener
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EPERM}:
                pytest.skip(f"local port binding is not permitted: {exc}")
            raise
        port = probe.getsockname()[1]

    # Isolate from operator settings and .env files, while retaining the
    # production package logging configuration (in particular, no root INFO).
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("TR_", "UVICORN_", "AXIOM_", "SENTRY_"))
        and key not in {"WEB_CONCURRENCY", "FORWARDED_ALLOW_IPS"}
    }
    env.update({
        "PYTHONPATH": str(ROOT / "src"),
        "PORT": str(port),
        "TR_STORAGE_BACKEND": "memory",
        "TR_ENVIRONMENT": "test",
        "TR_SHUTDOWN_DRAIN_SECONDS": "3",
        "TR_SYNTHETIC_SCHEDULER_INTERVAL_SECONDS": "0",
        "TR_ACTIVATION_REMINDER_INTERVAL_SECONDS": "0",
        "TR_REMEDIATOR_IN_PROCESS_ENABLED": "false",
        "TR_SETTLE_OUTBOX_ENABLED": "false",
        "TR_FEDERATION_DEFERRED_SETTLEMENT_ENABLED": "false",
    })
    stderr_path = tmp_path / "stderr.log"

    def get_health() -> int:
        # Each call creates and closes its own TCP connection.
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
        try:
            connection.request("GET", "/health", headers={"Connection": "close"})
            response = connection.getresponse()
            response.read()
            return response.status
        finally:
            connection.close()

    with stderr_path.open("w") as stderr:
        process = subprocess.Popen(  # noqa: S603 - fixed local module, isolated test environment
            [sys.executable, "-m", "trusted_router.serve"],
            cwd=tmp_path, env=env, stdout=subprocess.DEVNULL, stderr=stderr,
        )
        try:
            deadline = time.monotonic() + 30
            while True:
                assert process.poll() is None, stderr_path.read_text()
                try:
                    if get_health() == 200:
                        break
                except (OSError, http.client.HTTPException):
                    pass
                assert time.monotonic() < deadline, stderr_path.read_text()
                time.sleep(0.05)

            process.send_signal(signal.SIGTERM)
            deadline = time.monotonic() + 2
            while "serve.sigterm_drain_started" not in stderr_path.read_text():
                assert process.poll() is None, stderr_path.read_text()
                assert time.monotonic() < deadline, stderr_path.read_text()
                time.sleep(0.01)
            assert get_health() == 200
            assert "serve.sigterm_drain_complete" not in stderr_path.read_text()
            process.wait(timeout=12)
            logs = stderr_path.read_text()
            assert "serve.sigterm_drain_started drain_seconds=3.0" in logs
            assert "serve.sigterm_drain_complete" in logs
            assert logs.index("serve.sigterm_drain_complete") < logs.index("Shutting down")
            assert "Application shutdown complete" in logs
            assert "Finished server process" in logs
            # Uvicorn re-raises the captured signal after orderly shutdown.
            assert process.returncode in {0, -signal.SIGTERM}, logs
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
