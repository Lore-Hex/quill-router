import pytest

from scripts.lightning.activate_web import CONNECTION, Operator, deployment_commands
from scripts.lightning.connect_funding_node import NODE_SCRIPT


def test_activation_never_uses_operator_or_wallet_credentials() -> None:
    image = "us-central1-docker.pkg.dev/quill-cloud-proxy/trusted-router/lightning-router-web@sha256:" + "a" * 64
    commands = deployment_commands(image)
    assert commands[0][:4] == ("run", "jobs", "deploy", "lightning-router-migrate")
    assert commands[1][:3] == ("run", "jobs", "execute")
    web = commands[2]
    assert "--no-cpu-throttling" in web
    assert "--min-instances=1" in web
    assert "--max-instances=2" in web
    assert "--ingress=internal-and-cloud-load-balancing" in web
    assert "--add-cloudsql-instances=" + CONNECTION in web
    joined = " ".join(web)
    assert "db-admin" not in joined
    assert "operator-token" not in joined
    assert "internal-gateway-token" not in joined
    assert "invoice-macaroon" in joined


@pytest.mark.parametrize("image", ["latest", "repo:tag", "us-central1-docker.pkg.dev/other/image@sha256:" + "a" * 64])
def test_activation_requires_our_digest_pinned_image(image: str) -> None:
    with pytest.raises(ValueError, match="immutable"):
        deployment_commands(image)


def test_ops_identity_cannot_become_a_deployer() -> None:
    with pytest.raises(ValueError, match="separate deployment"):
        Operator("tr-ops-local@quill-cloud-proxy.iam.gserviceaccount.com")


def test_node_setup_has_exact_invoice_rpcs_and_no_spending_authority() -> None:
    import ast

    parsed = ast.parse(NODE_SCRIPT)
    permissions = {node.value for node in ast.walk(parsed) if isinstance(node, ast.Constant)
                   and isinstance(node.value, str) and node.value.startswith("uri:")}
    assert permissions == {
        "uri:/lnrpc.Lightning/GetInfo", "uri:/lnrpc.Lightning/ListChannels",
        "uri:/lnrpc.Lightning/AddInvoice", "uri:/lnrpc.Lightning/LookupInvoice",
        "uri:/invoicesrpc.Invoices/CancelInvoice",
    }
    assert "pending_htlcs" in NODE_SCRIPT
    assert "restlisten=10.92.0.2:8080" in NODE_SCRIPT
    assert 'check("/v1/balance/blockchain") not in (401, 403)' in NODE_SCRIPT
    assert "SendPayment" not in NODE_SCRIPT


@pytest.mark.parametrize("status,body,expected", [
    (500, {"code": 2, "message": "permission denied"}, 403),
    (500, {"code": 2, "message": "database unavailable"}, 500),
    (500, {"code": 13, "message": "permission denied"}, 500),
])
def test_node_negative_control_accepts_only_exact_lnd_denial(status, body, expected) -> None:
    import ast
    import io
    import json
    import urllib.error
    from unittest.mock import Mock

    module = ast.parse(NODE_SCRIPT)
    function = next(node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == "check")
    opener = Mock()
    opener.error.HTTPError = urllib.error.HTTPError
    opener.request.urlopen.side_effect = urllib.error.HTTPError("https://node.test", status, "failure", {}, io.BytesIO(json.dumps(body).encode()))
    namespace = {"urllib": opener, "context": None, "macaroon": Mock(), "json": json}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "node-check", "exec"), namespace)  # noqa: S102 - fixed repository function under test
    assert namespace["check"]("/v1/balance/blockchain") == expected
