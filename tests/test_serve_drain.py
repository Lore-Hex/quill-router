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

    # This in-process entry supplies no launch input. Restore all bridge state
    # afterward so it cannot claim the runner's fd 3 or retain an earlier owner.
    monkeypatch.setattr(serve.launch_descriptor_state, "LAUNCH_FD_PRESENT", False)
    monkeypatch.setattr(serve.launch_descriptor_state, "INHERITED_FD", None)
    monkeypatch.setattr(serve.launch_descriptor_state, "_claimed", False)

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



def _stall_server(monkeypatch: pytest.MonkeyPatch, stall: float | None) -> serve.DrainingServer:
    server = serve.DrainingServer(
        serve.build_config({"PORT": "8080"}), drain_seconds=3.0, stall_seconds=stall,
    )
    monkeypatch.setattr(server.config, "callback_notify", None, raising=False)
    return server


def _record_arming(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, object]]:
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(serve, "LoopStallDiagnostics", lambda *args: None)
    monkeypatch.setattr(serve, "stall_dump_fd", lambda fd: calls.append(("dump_fd", fd)) or 99)
    monkeypatch.setattr(serve.faulthandler, "dump_traceback_later",
                        lambda timeout, exit=False, file=None: calls.append(("dump", (timeout, exit, file))))
    monkeypatch.setattr(serve.faulthandler, "cancel_dump_traceback_later",
                        lambda: calls.append(("cancel", None)))
    return calls


def test_each_second_of_ticks_rearms_the_stall_watchdog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_arming(monkeypatch)
    clock = [1000.0]
    monkeypatch.setattr(serve, "monotonic", lambda: clock[0])
    server = _stall_server(monkeypatch, 30.0)

    for counter in range(21):
        asyncio.run(server.on_tick(counter))
        clock[0] += 0.1

    arm = ("dump", (30.0, True, 99))
    assert calls == [("dump_fd", sys.stderr.fileno()), arm, arm, arm]


def test_slow_ticks_rearm_the_stall_watchdog_on_every_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    # Ten ticks 3.1 s apart would outlast a 30 s budget; each one re-arms.
    calls = _record_arming(monkeypatch)
    clock = [1000.0]
    monkeypatch.setattr(serve, "monotonic", lambda: clock[0])
    server = _stall_server(monkeypatch, 30.0)

    for counter in range(10):
        asyncio.run(server.on_tick(counter))
        clock[0] += 3.1

    assert calls.count(("dump", (30.0, True, 99))) == 10


def test_without_a_stall_budget_ticks_arm_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record_arming(monkeypatch)
    server = _stall_server(monkeypatch, None)

    for counter in range(21):
        asyncio.run(server.on_tick(counter))

    assert calls == []


def test_shutdown_disarms_the_stall_watchdog(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record_arming(monkeypatch)

    async def stock_shutdown(self: object, sockets: object = None) -> None:
        calls.append(("stock_shutdown", None))

    monkeypatch.setattr(serve.uvicorn.Server, "shutdown", stock_shutdown)
    server = _stall_server(monkeypatch, 30.0)

    asyncio.run(server.shutdown())

    assert calls == [("cancel", None), ("stock_shutdown", None)]


@pytest.mark.parametrize(("raw", "expected"), [
    (None, None), ("", None), (" ", None), ("30", 30.0), ("2", 2.0),
    ("1.9", None), ("0", None), ("-5", None), ("nan", None), ("inf", None), ("thirty", None),
])
def test_stall_budget_comes_from_env(raw: str | None, expected: float | None) -> None:
    env = {} if raw is None else {serve.LOOP_STALL_ENV: raw}
    assert serve.loop_stall_seconds_from_env(env) == expected


def test_trusted_router_rollout_sets_the_stall_budget_with_continuous_cpu() -> None:
    rollout = (ROOT / "scripts/deploy/rollout.sh").read_text()
    assert f'"{serve.LOOP_STALL_ENV}=30"' in rollout
    assert "--no-cpu-throttling" in rollout


_STALL_SCRIPT = """
import os
import socket
import sys
import time

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from trusted_router import serve

FILL_STDERR = sys.argv[2] == "fill"

def wedged_call():
    time.sleep(30)

async def block(request):
    print("blocking", flush=True)
    if FILL_STDERR:
        os.set_blocking(2, False)
        try:
            while True:
                os.write(2, b"x" * 4096)
        except BlockingIOError:
            pass
        os.set_blocking(2, True)
    wedged_call()
    return PlainTextResponse("unreachable")

async def health(request):
    return PlainTextResponse("ok")

async def slow(request):
    # Holds the loop for a while on each request: ticks slow, never stop.
    time.sleep(0.4)
    return PlainTextResponse("ok")

config = serve.build_config({})
config.app = Starlette(routes=[Route("/block", block), Route("/health", health), Route("/slow", slow)])
server = serve.DrainingServer(config, drain_seconds=0, stall_seconds=2.5)
server.run(sockets=[socket.socket(fileno=int(sys.argv[1]))])
"""


def _run_stalled_server(tmp_path: Path, *, fill_stderr: bool) -> tuple[int, str]:
    # A loop whose ticks are slowed by every request outlives several stall
    # budgets; a loop blocked in a call then ends the process within one.
    out_path = tmp_path / "stdout.log"
    err_path = tmp_path / "stderr.log"
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    read_end = write_end = None
    if fill_stderr:
        # A pipe nobody reads stands for a backed-up log collector.
        read_end, write_end = os.pipe()
    with socket.socket() as listener, out_path.open("w") as out, err_path.open("w") as err:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        process = subprocess.Popen(  # noqa: S603 - fixed local test program
            [sys.executable, "-c", _STALL_SCRIPT, str(listener.fileno()), "fill" if fill_stderr else "drain"],
            cwd=tmp_path, env=env, pass_fds=(listener.fileno(),), stdout=out,
            stderr=write_end if write_end is not None else err,
        )
        if write_end is not None:
            os.close(write_end)

        def get(path: str) -> int:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            try:
                connection.request("GET", path)
                response = connection.getresponse()
                response.read()
                return response.status
            finally:
                connection.close()

        try:
            deadline = time.monotonic() + 10
            while True:
                assert process.poll() is None, out_path.read_text()
                try:
                    if get("/health") == 200:
                        break
                except (OSError, http.client.HTTPException):
                    time.sleep(0.05)
                assert time.monotonic() < deadline, out_path.read_text()

            healthy_until = time.monotonic() + 6
            while time.monotonic() < healthy_until:
                assert get("/slow") == 200
            assert process.poll() is None, out_path.read_text()

            with pytest.raises((OSError, http.client.HTTPException)):
                get("/block")
            returncode = process.wait(timeout=8)
            assert "blocking" in out_path.read_text()
            return returncode, err_path.read_text()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            if read_end is not None:
                os.close(read_end)


def test_real_stalled_loop_exits_with_every_stack(tmp_path: Path) -> None:
    returncode, logs = _run_stalled_server(tmp_path, fill_stderr=False)
    assert returncode == 1, logs
    assert "Timeout (0:00:02.500000)!" in logs
    assert "in wedged_call" in logs


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="the dump descriptor reopens stderr through /proc")
def test_real_stalled_loop_exits_when_stderr_is_backed_up(tmp_path: Path) -> None:
    # The dump cannot be written, but the exit is not held behind it.
    returncode, _ = _run_stalled_server(tmp_path, fill_stderr=True)
    assert returncode == 1


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="the dump descriptor reopens stderr through /proc")
def test_dump_descriptor_is_nonblocking_without_changing_stderr() -> None:
    read_end, write_end = os.pipe()
    try:
        fd = serve.stall_dump_fd(write_end)
        assert fd != write_end
        assert os.get_blocking(write_end) is True
        assert os.get_blocking(fd) is False
        os.write(fd, b"stack")
        assert os.read(read_end, 16) == b"stack"
        os.close(fd)
    finally:
        os.close(read_end)
        os.close(write_end)


def test_dump_descriptor_falls_back_to_stderr_when_it_cannot_reopen(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    def refuse(*args: object, **kwargs: object) -> int:
        raise FileNotFoundError("no /proc")

    monkeypatch.setattr(serve.os, "open", refuse)
    read_end, write_end = os.pipe()
    try:
        with caplog.at_level(logging.WARNING, logger="trusted_router.serve"):
            assert serve.stall_dump_fd(write_end) == write_end
        assert os.get_blocking(write_end) is True
        assert "serve.stall_dump_blocking reason=FileNotFoundError" in caplog.text
    finally:
        os.close(read_end)
        os.close(write_end)


def test_drain_immediate_dump_preserves_the_live_event_loop_watchdog() -> None:
    # Run both real dump mechanisms in one process. Only the drain's immediate
    # exit is intercepted so the existing C timer can prove it is still armed.
    code = """
import asyncio
import faulthandler
import time
from trusted_router import serve
from trusted_router.services import settle_outbox_worker as worker
assert serve.stall_dump_fd is worker.stall_dump_fd
original_arm = faulthandler.dump_traceback_later
original_cancel = faulthandler.cancel_dump_traceback_later
def arm(*args, **kwargs):
    print('arm', flush=True)
    return original_arm(*args, **kwargs)
def cancel():
    print('cancel', flush=True)
    return original_cancel()
faulthandler.dump_traceback_later = arm
faulthandler.cancel_dump_traceback_later = cancel
server = serve.DrainingServer(serve.build_config({}), drain_seconds=0, stall_seconds=2)
asyncio.run(server.on_tick(0))
worker.os._exit = lambda code: print(f'exit {code}', flush=True)
worker._exit_stalled_worker()
time.sleep(10)
"""
    process = subprocess.run(  # noqa: S603 - fixed test program, no external inputs
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    assert process.returncode == 1, process.stderr
    assert process.stdout.splitlines() == ["arm", "exit 70"]
    assert "in _exit_stalled_worker" in process.stderr  # immediate dump happened
    assert "Timeout (0:00:02)!" in process.stderr  # original delayed timer fired


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="the dump descriptor reopens stderr through /proc")
def test_real_drain_stall_exits_when_stderr_is_backed_up() -> None:
    # No socket required. Fill stderr after imports, then measure only dump+exit.
    code = """
import os
import sys
import time
from trusted_router.services import settle_outbox_worker as worker
fd = sys.stderr.fileno()
os.set_blocking(fd, False)
try:
    while True:
        os.write(fd, b'x' * 4096)
except BlockingIOError:
    pass
os.set_blocking(fd, True)
print(time.monotonic(), flush=True)
worker._exit_stalled_worker()
"""
    read_end, write_end = os.pipe()
    try:
        process = subprocess.run(  # noqa: S603 - fixed test program, no external inputs
            [sys.executable, "-c", code], cwd=ROOT, stdout=subprocess.PIPE,
            stderr=write_end, text=True, timeout=30,
        )
        elapsed = time.monotonic() - float(process.stdout.strip())
        assert process.returncode == 70
        assert elapsed < 1, f"dump+exit exceeded the 1 s operational budget: {elapsed}"
    finally:
        os.close(read_end)
        os.close(write_end)


@pytest.mark.parametrize("block", ["loop", "gil", "rearm"])
def test_early_stall_samples_survive_loop_and_gil_blocks(tmp_path: Path, block: str) -> None:
    # This reproduces diagnostic blind spots, NOT the unproven Spanner incident.
    # PyDLL deliberately retains the GIL in a sleeping native worker; a Python
    # sampler thread cannot take a stack sample in that case. No sockets needed.
    code = r'''
import asyncio
import ctypes
import os
import sys
import threading
import time
from trusted_router import serve, stall_dump
stall_dump.SAMPLE_SECONDS = (0.3, 0.8)
stall_dump.PULSE_SECONDS = 0.05
fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NONBLOCK, 0o600)
serve.stall_dump_fd = lambda _: fd
async def stock_shutdown(self, sockets=None):
    pass
serve.uvicorn.Server.shutdown = stock_shutdown
server = serve.DrainingServer(serve.build_config({}), drain_seconds=0, stall_seconds=5)
asyncio.run(server.on_tick(0))
time.sleep(0.1)
if sys.argv[2] == 'gil':
    def native_worker():
        ctypes.PyDLL(None).sleep(2)
    worker = threading.Thread(target=native_worker)
    worker.start()
    worker.join()
elif sys.argv[2] == 'rearm':
    original = serve.faulthandler.dump_traceback_later
    def blocked_rearm(*args, **kwargs):
        time.sleep(1.4)
        original(*args, **kwargs)
    serve.faulthandler.dump_traceback_later = blocked_rearm
    server._stall_armed_at = None
    asyncio.run(server.on_tick(1))
else:
    def blocked_loop():
        time.sleep(1.4)
    blocked_loop()
asyncio.run(server.shutdown())
time.sleep(0.2)
os.close(fd)
'''
    dump = tmp_path / "early-stacks.log"
    process = subprocess.run(  # noqa: S603 - fixed isolated diagnostic scenario
        [sys.executable, "-c", code, str(dump), block], cwd=ROOT,
        capture_output=True, text=True, timeout=15,
    )
    assert process.returncode == 0, process.stderr
    logs = dump.read_text()
    samples = [line for line in logs.splitlines() if "serve.loop_stall_sample " in line]
    assert len(samples) == 2, logs
    assert "threshold_seconds=0.3" in samples[0]
    assert "threshold_seconds=0.8" in samples[1]
    progress = "stalled" if block == "gil" else "running"
    assert all(f"python_progress={progress}" in line for line in samples), logs
    frame = {"loop": "blocked_loop", "gil": "native_worker", "rearm": "blocked_rearm"}[block]
    assert f"in {frame}" in logs, logs
    if block == "rearm":
        assert all("loop_phase=rearm" in line for line in samples), logs
    assert "Timeout (" not in logs  # Early samples did not replace/fire the exit timer.


@pytest.mark.parametrize("mode", [
    "healthy", "recover", "exit", "helper_exit", "full_pipe", "backed_up",
])
def test_stall_diagnostic_lifecycle(tmp_path: Path, mode: str) -> None:
    code = r'''
import asyncio
import os
import sys
import threading
import time
from trusted_router import serve, stall_dump
stall_dump.SAMPLE_SECONDS = (0.3, 0.8)
stall_dump.PULSE_SECONDS = 0.05
fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NONBLOCK, 0o600)
if sys.argv[2] == 'backed_up':
    os.close(fd)
    read_end, fd = os.pipe()
    os.set_blocking(fd, False)
    try:
        while True:
            os.write(fd, b'x' * 4096)
    except BlockingIOError:
        pass
serve.stall_dump_fd = lambda _: fd
async def stock_shutdown(self, sockets=None):
    pass
serve.uvicorn.Server.shutdown = stock_shutdown
server = serve.DrainingServer(serve.build_config({}), drain_seconds=0, stall_seconds=2)
asyncio.run(server.on_tick(0))
diagnostics = server._stall_diagnostics
assert diagnostics is not None
mode = sys.argv[2]
if mode in {'exit', 'backed_up'}:
    time.sleep(10)  # C exit timer must still kill this process after early dumps.
elif mode == 'recover':
    time.sleep(1.1)
    asyncio.run(server.on_tick(1))
    time.sleep(1.1)
elif mode == 'helper_exit':
    diagnostics._process.kill()
    diagnostics._thread.join(timeout=3)
    assert not diagnostics._thread.is_alive()
    asyncio.run(server.on_tick(1))  # EPIPE cannot fail a serving tick.
elif mode == 'full_pipe':
    # Replace only the loop's writer with a pipe nobody consumes. Its writes
    # must drop, not block. The original helper sees EOF after stop.
    read_fd, write_fd = os.pipe()
    os.set_blocking(write_fd, False)
    try:
        while True:
            os.write(write_fd, b'x' * 4096)
    except BlockingIOError:
        pass
    original_fd = diagnostics._fd
    diagnostics._fd = write_fd
    before = time.monotonic()
    asyncio.run(server.on_tick(1))
    assert time.monotonic() - before < 0.5
    diagnostics._fd = original_fd
    os.close(read_fd)
    os.close(write_fd)
else:
    # A blocked worker alone must neither trigger samples nor stop serving
    # ticks; this control is green on main and does not prove an RPC bug.
    release = threading.Event()
    worker = threading.Thread(target=release.wait)
    worker.start()
    async def healthy():
        for i in range(15):
            await asyncio.sleep(0.1)
            await server.on_tick(i + 1)
    asyncio.run(healthy())
    assert worker.is_alive()
    release.set()
    worker.join()
before = time.monotonic()
asyncio.run(server.shutdown())
assert time.monotonic() - before < 0.5
diagnostics._thread.join(timeout=3)
assert not diagnostics._thread.is_alive()
assert diagnostics._process.poll() is not None
time.sleep(0.9)  # No late samples after shutdown, even though ticks have stopped.
os.close(fd)
'''
    dump = tmp_path / "lifecycle-stacks.log"
    process = subprocess.run(  # noqa: S603 - fixed isolated diagnostic scenario
        [sys.executable, "-c", code, str(dump), mode], cwd=ROOT,
        capture_output=True, text=True, timeout=15,
    )
    logs = dump.read_text()
    assert process.returncode == (1 if mode in {"exit", "backed_up"} else 0), process.stderr + logs
    count = logs.count("serve.loop_stall_sample ")
    assert count == {"recover": 4, "exit": 2}.get(mode, 0), logs
    assert ("Timeout (0:00:02)!" in logs) == (mode == "exit"), logs


def test_stall_diagnostic_setup_failure_keeps_exit_watchdog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_arming(monkeypatch)

    def fail(*args: object) -> None:
        raise OSError("helper unavailable")

    monkeypatch.setattr(serve, "LoopStallDiagnostics", fail)
    server = _stall_server(monkeypatch, 30)
    asyncio.run(server.on_tick(0))
    assert calls[-1] == ("dump", (30, True, 99))


def test_stall_diagnostic_fd_exhaustion_closes_partial_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trusted_router.stall_dump import LoopStallDiagnostics

    fd = os.open(tmp_path / "dump", os.O_WRONLY | os.O_CREAT | os.O_NONBLOCK, 0o600)
    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(os, "pipe", lambda: (read_fd, write_fd))

    def exhausted(*args: object) -> int:
        raise OSError(errno.EMFILE, "no descriptors")

    monkeypatch.setattr(os, "dup", exhausted)
    try:
        with pytest.raises(OSError, match="no descriptors"):
            LoopStallDiagnostics(fd, 30)
        for closed in (read_fd, write_fd):
            with pytest.raises(OSError) as exc:
                os.fstat(closed)
            assert exc.value.errno == errno.EBADF
    finally:
        os.close(fd)
