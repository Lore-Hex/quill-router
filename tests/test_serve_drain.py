from __future__ import annotations

import asyncio
import logging
import signal
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


def test_second_sigterm_during_the_drain_exits_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(serve, "monotonic", lambda: 50.0)
    server = _server(monkeypatch)
    server.handle_exit(signal.SIGTERM, None)
    assert server.should_exit is False

    server.handle_exit(signal.SIGTERM, None)

    assert server.should_exit is True
    assert asyncio.run(server.on_tick(1)) is True


def test_sigint_is_immediate_so_local_runs_stop_on_ctrl_c(monkeypatch: pytest.MonkeyPatch) -> None:
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
    assert serve.drain_seconds_from_env({serve.DRAIN_SECONDS_ENV: "5"}) == 5.0
    assert serve.drain_seconds_from_env({serve.DRAIN_SECONDS_ENV: "30"}) == 8.0
    assert serve.drain_seconds_from_env({serve.DRAIN_SECONDS_ENV: "-1"}) == 0.0
    assert serve.drain_seconds_from_env({serve.DRAIN_SECONDS_ENV: "soon"}) == 3.0
    assert serve.drain_seconds_from_env({serve.DRAIN_SECONDS_ENV: " "}) == 3.0
    server = _server(monkeypatch, drain=30.0)
    assert server.drain_seconds == 8.0


def test_drain_logs_start_and_completion(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
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

