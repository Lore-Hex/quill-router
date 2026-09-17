import os
from unittest.mock import Mock

import pytest
from lightning_router import launch


def test_sidecar_cannot_inherit_root_spending_or_checkout_credentials(monkeypatch):
    for name in ["LEXE_ROOT_SEED", "LEXE_ROOT_SEED_PATH", "LEXE_CLIENT_CREDENTIALS", "LEXE_WEBHOOK_URL",
                 "LR_CHECKOUT_SECRET", "LR_DATABASE_URL", "HTTP_PROXY", "RUST_LOG"]:
        monkeypatch.setenv(name, "secret-do-not-inherit")
    env = launch.sidecar_environment()
    assert set(env) == {"PATH", "HOME", "RUST_LOG"}
    assert env["RUST_LOG"] == "off"
    command = launch.sidecar_command()
    assert command[2] == "127.0.0.1:5393"
    assert "--root-seed" not in command and "--root-seed-path" not in command
    assert "mainnet" in command


def test_without_lexe_launches_unchanged_web_only(monkeypatch):
    monkeypatch.delenv("LR_LEXE_WALLET_ID", raising=False)
    def execute(executable, args):
        assert args[:3] == [executable, "-m", "uvicorn"]
        raise SystemExit(0)
    monkeypatch.setattr(os, "execv", execute)
    with pytest.raises(SystemExit):
        launch.main()


def test_early_sidecar_exit_cannot_start_web(monkeypatch):
    monkeypatch.setenv("LR_LEXE_WALLET_ID", "a" * 64)
    process = Mock()
    process.poll.return_value = 1
    spawn = Mock(return_value=process)
    monkeypatch.setattr(launch.subprocess, "Popen", spawn)
    monkeypatch.setattr(launch.signal, "signal", lambda *args: None)
    assert launch.main() == 1
    assert spawn.call_count == 1
    process.wait.assert_called_once()


def test_web_exit_terminates_sidecar(monkeypatch):
    monkeypatch.setenv("LR_LEXE_WALLET_ID", "a" * 64)
    sidecar, web = Mock(), Mock()
    sidecar.poll.return_value = None
    web.poll.return_value = 1
    spawn = Mock(side_effect=[sidecar, web])
    monkeypatch.setattr(launch.subprocess, "Popen", spawn)
    monkeypatch.setattr(launch.signal, "signal", lambda *args: None)
    monkeypatch.setattr(launch.httpx.Client, "get", lambda *args: Mock(status_code=200, json=lambda: {"status": "ok"}))
    assert launch.main() == 1
    sidecar.terminate.assert_called_once()
    assert spawn.call_count == 2
