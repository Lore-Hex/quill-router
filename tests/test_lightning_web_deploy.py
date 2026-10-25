from pathlib import Path

import yaml

from scripts.lightning import deploy_web


def test_lightning_ci_uses_the_production_python_version() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load(
        (root / ".github/workflows/lightning-experiment.yml").read_text()
    )
    job = workflow["jobs"]["test"]
    setup_uv = next(
        step for step in job["steps"] if step.get("uses", "").startswith("astral-sh/setup-uv@")
    )
    python_version = setup_uv.get("with", {}).get("python-version")
    assert python_version == "3.12"
    docker = (root / "experiments/lightning_router/Dockerfile").read_text()
    assert f"FROM python:{python_version}-slim-bookworm" in docker
    assert job["runs-on"] == "ubuntu-24.04"


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
