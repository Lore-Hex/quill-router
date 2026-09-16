"""No-wallet/no-network tests for the Lexe receive-only setup check."""

import copy
import json

import httpx
import pytest

from scripts.lightning import lexe_preflight as preflight

USER = "a" * 64


def responses():
    return {
        "/v2/health": {"status": "ok"},
        "/v2/node/client_info": {"kind": "client_credentials", "scopes": sorted(preflight.SCOPES),
                                 "permissions": [], "effective_permissions": sorted(preflight.PERMISSIONS),
                                 "expires_at": None, "label": "secret-label"},
        "/v2/node/node_info": {"user_pk": USER, "version": "0.10.4", "measurement": "b" * 64,
                               "balance": "private-balance", "node_pk": "private-node-id"},
        "/v2/node/list_channels": {"channels": [
            {"is_usable": True, "inbound_capacity": "1000.123", "channel_id": "secret-channel"},
            {"is_usable": False, "inbound_capacity": "5000"},
            {"is_usable": True, "inbound_capacity": "2"},
        ]},
    }


def inspect(data):
    paths = []

    def handle(request):
        assert request.method == "GET"
        assert "authorization" not in request.headers
        paths.append(request.url.path)
        return httpx.Response(200, json=data[request.url.path])

    with httpx.Client(base_url="http://127.0.0.1:5393", transport=httpx.MockTransport(handle)) as client:
        return preflight.inspect_sidecar(client, USER), paths


def test_read_only_no_payment_or_secret_output():
    result, paths = inspect(responses())
    assert paths == list(responses())
    assert result["usable_channels"] == 2
    assert result["existing_inbound_msat"] == "1002123"
    assert result["production_cutover_allowed"] is False
    assert result["payment_verified"] is False
    serialized = json.dumps(result)
    for forbidden in [USER, "secret-label", "private-balance", "private-node-id", "secret-channel"]:
        assert forbidden not in serialized


def test_jit_wallet_can_have_no_channels_but_is_not_payment_verified():
    data = responses()
    data["/v2/node/list_channels"] = {"channels": []}
    result, _ = inspect(data)
    assert result["existing_inbound_msat"] == "0"
    assert result["payment_verified"] is False


def test_published_sidecar_omits_empty_extra_permissions():
    data = responses()
    del data["/v2/node/client_info"]["permissions"]
    result, _ = inspect(data)
    assert result["status"] == "configuration_checked"
    data["/v2/node/client_info"]["effective_permissions"].append("pay_invoice")
    with pytest.raises(preflight.PreflightError, match="effective_permissions"):
        inspect(data)


@pytest.mark.parametrize("patch", [
    {"kind": "root_seed"}, {"scopes": ["full"]}, {"scopes": ["read", "receive"]},
    {"scopes": ["read_info", "read_payments", "receive", "spend"]},
    {"scopes": ["read_info", "read_payments", "receive", "manage_channels"]},
    {"scopes": []}, {"scopes": "read"}, {"scopes": [None]},
    {"permissions": ["pay_invoice"]}, {"permissions": ["create_invoice"]},
    {"effective_permissions": ["create_invoice"]},
    {"effective_permissions": sorted(preflight.PERMISSIONS | {"pay_invoice"})},
    {"effective_permissions": sorted(preflight.PERMISSIONS | {"future_unknown"})},
    {"effective_permissions": None}, {"expires_at": 123}, {"expires_at": "9999999999999"},
    {"expires_at": True},
])
def test_reject_unsafe_missing_or_unknown_permissions(patch):
    data = responses()
    data["/v2/node/client_info"].update(patch)
    with pytest.raises(preflight.PreflightError):
        inspect(data)


def test_expiry_guard():
    credentials = responses()["/v2/node/client_info"]
    credentials["expires_at"] = 3_600_101
    preflight.check_credentials(credentials, 100)
    with pytest.raises(preflight.PreflightError, match="expiring"):
        preflight.check_credentials(credentials, 101)
    del credentials["expires_at"]
    with pytest.raises(preflight.PreflightError, match="expiry"):
        preflight.check_credentials(credentials, 100)


@pytest.mark.parametrize("url", [
    "https://lexe.app", "http://10.0.0.1:5393", "http://localhost:5393", "http://0.0.0.0:5393",
    "http://127.0.0.1", "http://127.0.0.1:5393/v2", "http://secret@127.0.0.1:5393",
    "http://127.0.0.1:5393?token=secret", "http://127.0.0.1:5393#secret", "file:///tmp/foo",
])
def test_only_loopback_sidecar(url):
    with pytest.raises(preflight.PreflightError):
        preflight.sidecar_url(url)


@pytest.mark.parametrize("url", ["http://127.0.0.1:5393", "http://[::1]:5393/"])
def test_valid_sidecar_urls(url):
    assert preflight.sidecar_url(url) == url.rstrip("/")


@pytest.mark.parametrize("path,patch", [
    ("/v2/health", {"status": "not ok"}),
    ("/v2/node/node_info", {"user_pk": "c" * 64}),
    ("/v2/node/node_info", {"version": "secret upstream message"}),
    ("/v2/node/node_info", {"measurement": "invalid"}),
    ("/v2/node/list_channels", {"channels": None}),
    ("/v2/node/list_channels", {"channels": [None]}),
    ("/v2/node/list_channels", {"channels": [{"is_usable": "true", "inbound_capacity": "1"}]}),
])
def test_identity_health_and_shape_fail_closed(path, patch):
    data = responses()
    data[path].update(patch)
    with pytest.raises(preflight.PreflightError):
        inspect(data)


@pytest.mark.parametrize("amount", [True, 1.5, "-1", "NaN", "1e2", "1.0001", "9" * 100, None])
def test_capacity_is_exact_nonnegative_msats(amount):
    data = responses()
    data["/v2/node/list_channels"]["channels"][0]["inbound_capacity"] = amount
    with pytest.raises(preflight.PreflightError, match="capacity"):
        inspect(data)


@pytest.mark.parametrize("response", [
    httpx.Response(302, headers={"location": "http://remote.invalid/secret"}),
    httpx.Response(500, text="upstream-secret"),
    httpx.Response(200, json=[]),
    httpx.Response(200, content=b"x" * 1_048_577),
])
def test_bad_responses_fail_closed(response):
    with httpx.Client(base_url="http://127.0.0.1:5393", transport=httpx.MockTransport(lambda _: response)) as client:
        with pytest.raises(preflight.PreflightError) as error:
            preflight.inspect_sidecar(client, USER)
    assert "secret" not in str(error.value)


def test_wallet_identity_required_before_any_io():
    def unexpected(_):
        pytest.fail("Must validate expected wallet before connecting")

    with httpx.Client(transport=httpx.MockTransport(unexpected)) as client:
        with pytest.raises(preflight.PreflightError, match="public_key"):
            preflight.inspect_sidecar(client, "not-a-wallet")


def test_cli_scrubs_failures(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["preflight", "--expected-user-pk", USER])

    def fail(*args):
        raise httpx.ConnectError("secret credential and response body")

    monkeypatch.setattr(preflight, "inspect_sidecar", fail)
    assert preflight.main() == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "blocked", "reason": "sidecar_unavailable_or_invalid",
    }


def test_cli_success_and_static_error(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["preflight", "--expected-user-pk", USER])
    result, _ = inspect(copy.deepcopy(responses()))
    monkeypatch.setattr(preflight, "inspect_sidecar", lambda *_: result)
    assert preflight.main() == 0
    assert json.loads(capsys.readouterr().out) == result
    monkeypatch.setattr("sys.argv", ["preflight", "--expected-user-pk", USER, "--sidecar-url", "https://remote.invalid"])
    assert preflight.main() == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "loopback_sidecar_required"
