import pytest

from scripts.lightning.deploy_lexe import WALLET, commands, validate_activation, validate_baseline

IMAGE = "us-central1-docker.pkg.dev/quill-cloud-proxy/trusted-router/lightning-router-web@sha256:" + "a" * 64


def test_staged_deploy_preserves_environment_and_never_moves_traffic():
    steps = commands(IMAGE, "lexetest", backend="lnd")
    assert steps[0][:3] == ("run", "jobs", "deploy")
    assert steps[1][:3] == ("run", "jobs", "execute")
    deploy = steps[2]
    assert "--no-traffic" in deploy
    assert not any(arg.startswith("--set-env-vars") for arg in deploy)
    assert not any("LR_PAYMENTS_ENABLED=false" in arg for arg in deploy)
    assert not any("seed" in arg.lower() for arg in deploy)
    assert any("receive-client:1" in arg for arg in deploy)


@pytest.mark.parametrize("image,suffix,backend", [(IMAGE.replace("@sha256:", ":"), "lexetest", "lnd"),
                                                 (IMAGE, "bad", "lnd"), (IMAGE, "lexetest", "unknown")])
def test_deploy_rejects_unsafe_inputs(image, suffix, backend):
    with pytest.raises(ValueError):
        commands(image, suffix, backend=backend)


def test_unhealthy_or_split_baseline_is_rejected():
    service = {"status": {"conditions": [{"type": "Ready", "status": "True"}],
                          "traffic": [{"revisionName": "old", "percent": 100}]}}
    assert validate_baseline(service) == "old"
    service["status"]["traffic"][0]["percent"] = 50
    with pytest.raises(ValueError):
        validate_baseline(service)
    service["status"]["conditions"][0]["status"] = "False"
    with pytest.raises(ValueError):
        validate_baseline(service)


def test_activation_refuses_old_reconciliation_workers():
    revision = {"spec": {"containers": [{"image": IMAGE, "env": [{"name": "LR_PAYMENTS_ENABLED", "value": "true"}]}]}}
    with pytest.raises(ValueError, match="Retire pre-Lexe"):
        validate_activation([revision], IMAGE)
    revision["spec"]["containers"][0]["env"].append({"name": "LR_LEXE_WALLET_ID", "value": WALLET})
    validate_activation([revision], IMAGE)


def test_runtime_permission_expansion_matches_operator_preflight():
    import ast
    from pathlib import Path

    from scripts.lightning.lexe_preflight import PERMISSIONS, SCOPES
    module = ast.parse((Path(__file__).resolve().parents[1] / "experiments/lightning_router/lightning_router/lexe.py").read_text())
    values = {node.targets[0].id: ast.literal_eval(node.value) for node in module.body
              if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and node.targets[0].id in {"SCOPES", "PERMISSIONS"}}
    assert values == {"SCOPES": SCOPES, "PERMISSIONS": PERMISSIONS}
