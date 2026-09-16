"""Security boundaries for the optional, isolated BTCPay dashboard."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
import yaml

from scripts.lightning.btcpay import bootstrap, deploy

ROOT = Path(__file__).parents[1] / "scripts/lightning/btcpay"


def test_images_are_immutable_and_no_node_is_created() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    assert set(compose["services"]) == {"postgres", "btcpay", "caddy"}
    for service in compose["services"].values():
        assert "@sha256:" in service["image"]
        assert service["mem_limit"]
        assert "no-new-privileges:true" in service["security_opt"]
        assert service["logging"]["options"]["max-size"] == "10m"
        assert not service.get("privileged")
        assert not any("docker.sock" in volume for volume in service["volumes"])
    assert "ports" not in compose["services"]["postgres"]
    assert compose["services"]["btcpay"]["ports"] == ["127.0.0.1:49392:49392"]
    assert compose["networks"]["database"]["internal"]
    assert not any("admin.macaroon" in line for line in (ROOT / "compose.yaml").read_text().splitlines())


def test_lnd_permissions_cannot_spend_or_manage_channels() -> None:
    assert set(deploy.RPCS) == {
        "/lnrpc.Lightning/GetInfo", "/lnrpc.Lightning/ListChannels",
        "/lnrpc.Lightning/WalletBalance", "/lnrpc.Lightning/ChannelBalance",
        "/lnrpc.Lightning/ListInvoices", "/lnrpc.Lightning/LookupInvoice",
        "/lnrpc.Lightning/SubscribeInvoices", "/lnrpc.Lightning/AddInvoice",
    }
    assert "restart" not in deploy.NODE_SCRIPT
    assert "stop" not in deploy.NODE_SCRIPT
    assert "--root_key_id=31" in deploy.NODE_SCRIPT
    assert "funding.macaroon" not in deploy.NODE_SCRIPT
    assert "printmacaroon" in deploy.NODE_SCRIPT
    assert "permissions !=" in deploy.NODE_SCRIPT
    assert "create_default_context(cafile=" in deploy.NODE_SCRIPT


def test_private_state_atomic_and_private(tmp_path: Path) -> None:
    path = tmp_path / "private/state.json"
    bootstrap.private_json(path, {"secret": "first"})
    bootstrap.private_json(path, {"secret": "second"})
    assert json.loads(path.read_text()) == {"secret": "second"}
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert list(path.parent.iterdir()) == [path]


def test_private_state_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("preserve")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        bootstrap.private_json(link, {"secret": "no"})
    assert target.read_text() == "preserve"


@pytest.mark.parametrize("url", ["https://evil.example/", "http://localhost:49392/", "http://127.0.0.1:80/", "https://127.0.0.1:49392/"])
def test_bootstrap_redirect_never_leaks_credentials(url: str) -> None:
    with pytest.raises(RuntimeError, match="outside loopback"):
        bootstrap.LocalRedirect().redirect_request(None, None, 302, "", {}, url)


def test_api_errors_do_not_expose_response_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    client = bootstrap.Client()
    monkeypatch.setattr(client, "request", lambda *_args, **_kwargs: (500, "secret-macaroon-and-invitation"))
    with pytest.raises(RuntimeError) as caught:
        client.api("/api/v1/stores")
    assert "secret" not in str(caught.value)


def test_observer_role_is_read_only() -> None:
    assert bootstrap.READ_PERMISSIONS == {
        "btcpay.store.canviewstoresettings", "btcpay.store.canviewlightninginvoice",
    }


def test_observer_canary_removed_even_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    admin = Mock()
    admin.api.side_effect = [{"id": "canary"}, {}, {"apiKey": "temporary"}, None]
    observer = Mock()
    observer.api.return_value = [{"id": "unexpected"}]
    monkeypatch.setattr(bootstrap, "Client", lambda: observer)
    with pytest.raises(RuntimeError, match="unexpected stores"):
        bootstrap.verify_observer(admin, "only-store", "read-only")
    admin.api.assert_called_with("/api/v1/users/canary", method="DELETE")
    assert admin.api.call_args_list[0].kwargs["method"] == "POST"
    assert admin.api.call_args_list[0].args[1]["sendInvitationEmail"] is False


def test_publication_refuses_incomplete_bootstrap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fake_ssh(_operator: Any, command: str, **_kwargs: Any) -> str:
        if command.endswith("access.json") and "cat" in command:
            return '{"complete":false}'
        return ""
    monkeypatch.setattr(deploy, "ssh", fake_ssh)
    operator = Mock()
    with pytest.raises(RuntimeError, match="incomplete"):
        deploy.publish(operator, tmp_path)
    operator.gc.assert_not_called()
    assert not list(tmp_path.iterdir())


def test_bundle_refuses_dirty_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deploy.subprocess, "check_output", lambda *_args, **_kwargs: " M scripts/lightning/btcpay/bootstrap.py")
    with pytest.raises(ValueError, match="Commit"):
        deploy.committed_bundle()


def test_reverse_proxy_does_not_log_invitation_urls() -> None:
    config = (ROOT / "Caddyfile").read_text()
    assert "admin off" in config
    assert "Referrer-Policy no-referrer" in config
    assert not any(line.strip().startswith("log") for line in config.splitlines())


def test_idempotent_bootstrap_keeps_invitation_unconsumed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "access.json"
    bootstrap.private_json(path, {"complete": True, "store_id": "store", "greg_invitation": "unconsumed"})
    constructor = Mock(side_effect=AssertionError("Must not access user invitation"))
    monkeypatch.setattr(bootstrap, "Client", constructor)
    assert bootstrap.bootstrap(path) == {"complete": True, "store_id": "store"}
    assert json.loads(path.read_text())["greg_invitation"] == "unconsumed"
