from pathlib import Path

from scripts.lightning import deploy_web


def test_web_deploy_cannot_enable_payment_or_change_main_router_url_map() -> None:
    source = Path(deploy_web.__file__).read_text()
    assert "LR_PAYMENTS_ENABLED=false" in source
    assert "--memory=512Mi" in source
    assert "--ingress=internal-and-cloud-load-balancing" in source
    assert "internal_gateway_token" not in source
    assert "--set-secrets" not in source
    assert "source.extractall(temporary, filter=\"data\")" in source
    assert '"archive"' in source and '"--porcelain"' in source


def test_web_image_runs_unprivileged_and_has_no_tr_backend_or_credentials() -> None:
    root = Path(__file__).resolve().parents[1]
    docker = (root / "experiments/lightning_router/Dockerfile").read_text()
    assert "USER 10001:10001" in docker
    assert "LR_PAYMENTS_ENABLED=false" in docker
    assert 'CMD ["python", "-m", "lightning_router.launch"]' in docker
    launcher = (root / "experiments/lightning_router/lightning_router/launch.py").read_text()
    assert '"--no-access-log"' in launcher
    assert '"--no-proxy-headers"' in launcher
    assert '"127.0.0.1:5393"' in launcher
    assert "COPY src" not in docker
