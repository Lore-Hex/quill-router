"""Security boundaries for the optional, isolated BTCPay dashboard."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
import yaml

from scripts.lightning.btcpay import bootstrap, deploy, explorer

ROOT = Path(__file__).parents[1] / "scripts/lightning/btcpay"


def test_images_are_immutable_and_no_node_is_created() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    assert set(compose["services"]) == {"postgres", "btcpay", "caddy", "nbxplorer"}
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


def test_bitcoin_explorer_is_connected_and_private() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    explorer = compose["services"]["nbxplorer"]
    assert "ports" not in explorer
    assert explorer["environment"]["NBXPLORER_BTCRPCURL"] == "http://10.92.0.2:8332/"
    assert explorer["environment"]["NBXPLORER_BTCNODEENDPOINT"] == "10.92.0.2:8333"
    assert "NBXPLORER_NOAUTH" not in explorer["environment"]
    env = compose["services"]["btcpay"]["environment"]
    assert env["BTCPAY_BTCEXPLORERURL"] == "http://nbxplorer:24444/"
    assert env["BTCPAY_BTCEXPLORERCOOKIEFILE"] == "/run/nbxplorer/Main/.cookie"
    assert "./data/nbxplorer:/run/nbxplorer:ro" in compose["services"]["btcpay"]["volumes"]


def test_explorer_configuration_preserves_existing_node() -> None:
    before = "chain=main\ndisablewallet=1\nlisten=0\nprune=30000\nincludeconf=/etc/bitcoin/lightning.conf\n[main]\nrpcbind=127.0.0.1\nrpcallowip=127.0.0.1\n"
    after = explorer.bitcoin_configuration(before)
    assert "listen=1\n" in after
    assert "prune=30000\n" in after
    assert "rpcbind=127.0.0.1\n" in after
    assert "includeconf=/etc/bitcoin/lightning.conf\n" in after
    assert after.index("includeconf=/etc/bitcoin/btcpay.conf") < after.index("[main]")
    assert explorer.bitcoin_configuration(after) == after


@pytest.mark.parametrize("before", ["[main]\nlisten=0\n", "disablewallet=1\nlisten=0\n", "disablewallet=1\n[main]\n"])
def test_explorer_rejects_unexpected_bitcoin_configuration(before: str) -> None:
    with pytest.raises(ValueError):
        explorer.bitcoin_configuration(before)


def test_explorer_cannot_spend_or_administer_node() -> None:
    assert not {"sendtoaddress", "sendrawtransaction", "stop", "setnetworkactive", "walletpassphrase", "dumpprivkey"} & set(explorer.RPC_METHODS)
    assert "disablewallet=1" in explorer.NODE_PREPARE
    assert "rpcwhitelistdefault=0" in explorer.NODE_PREPARE
    assert "rpcallowip=10.92.2.2/32" in explorer.NODE_PREPARE
    assert "bind=10.92.0.2:8333" in explorer.NODE_PREPARE
    assert "pending_htlcs" in explorer.NODE_ACTIVATE
    assert "tr-lnd-backup.service" in explorer.NODE_ACTIVATE
    assert "before['identity_pubkey']" in explorer.NODE_ACTIVATE
    assert "conf.write_text(p['previous'])" in explorer.NODE_ACTIVATE


def test_explorer_firewall_is_single_source_private_only() -> None:
    rule = {
        "direction": "INGRESS", "sourceRanges": ["10.92.2.2/32"],
        "targetTags": ["tr-lightning"], "network": "projects/test/global/networks/tr-lightning",
        "allowed": [{"IPProtocol": "tcp", "ports": ["8332", "8333"]}],
    }
    explorer.validate_firewall(rule)
    explorer.validate_firewall({**rule, "allowed": [
        {"IPProtocol": "tcp", "ports": ["8332"]}, {"IPProtocol": "tcp", "ports": ["8333"]},
    ]})
    for change in ({"sourceRanges": ["0.0.0.0/0"]}, {"sourceTags": ["broad-access"]},
                   {"allowed": [{"IPProtocol": "tcp"}]}, {"targetTags": []}, {"disabled": True},
                   {"allowed": [{"IPProtocol": "tcp", "ports": ["8332", "8333"]}, {"IPProtocol": "udp"}]}):
        with pytest.raises(ValueError, match="firewall scope"):
            explorer.validate_firewall({**rule, **change})


def test_explorer_backup_covers_both_databases_without_package_changes() -> None:
    installer = (ROOT / "install.sh").read_text()
    assert '"${1:-}" != "--configure-only"' in installer
    assert "for db in btcpay nbxplorer" in installer
    assert "db-$db-" in installer


@pytest.mark.parametrize("synced", [False, True])
def test_explorer_upgrade_requires_real_health(monkeypatch: pytest.MonkeyPatch, synced: bool) -> None:
    operator = Mock()
    operator.gc.side_effect = ["tr-btcpay-bitcoin", json.dumps({
        "direction": "INGRESS", "sourceRanges": ["10.92.2.2/32"],
        "targetTags": ["tr-lightning"], "network": "projects/test/global/networks/tr-lightning",
        "allowed": [{"IPProtocol": "tcp", "ports": ["8332", "8333"]}],
    })]
    commands = Mock(return_value="")
    monkeypatch.setattr(explorer, "ssh", commands)
    monkeypatch.setattr(explorer.time, "sleep", lambda _: None)
    monkeypatch.setattr(explorer, "committed_bundle", lambda: {
        "compose.yaml": "committed-compose", "install.sh": base64.b64encode(b"installer").decode(),
    })

    def remote(_operator: Any, code: str, _payload: Any, **_kwargs: Any) -> str:
        if code == explorer.NODE_PREPARE:
            return json.dumps({"configuration": "disablewallet=1\nlisten=0\n[main]\n", "cookie": "private"})
        if code == explorer.EXPLORER_CHECK:
            return json.dumps({"isFullySynched": synced})
        if code == explorer.BTCPAY_CHECK:
            return '{"synchronized":true}'
        return "{}"

    monkeypatch.setattr(explorer, "remote", remote)
    if synced:
        explorer.apply(operator)
    else:
        with pytest.raises(RuntimeError, match="not synced"):
            explorer.apply(operator)
    executed = [call.args[1] for call in commands.call_args_list]
    assert any("up -d --no-deps btcpay" in command for command in executed) is synced
    assert any("start tr-btcpay-backup.service" in command for command in executed) is synced


def test_publication_requires_synced_explorer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(deploy, "ssh", lambda *_args, **_kwargs: '{"complete":true}')
    monkeypatch.setattr(explorer, "remote", lambda *_args, **_kwargs: '{"isFullySynched":false}')
    operator = Mock()
    with pytest.raises(RuntimeError, match="NBXplorer is not synced"):
        deploy.publish(operator, tmp_path)
    operator.gc.assert_not_called()
    assert not list(tmp_path.iterdir())


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
    assert 'set(decoded["permissions"])' in deploy.NODE_SCRIPT
    assert '{"uri:" + p for p in allowed}' in deploy.NODE_SCRIPT
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
