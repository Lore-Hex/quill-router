from pathlib import Path

import pytest
import yaml
from fastapi import APIRouter

from scripts.lightning import activate_web, deploy_lexe, deploy_web, reliability
from scripts.lightning.btcpay import deploy as deploy_btcpay
from trusted_router.routes.internal import register_gateway_internal_routes

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("module", [activate_web, deploy_lexe, deploy_web, reliability, deploy_btcpay])
def test_retired_lightning_entrypoints_fail_before_any_cloud_access(module) -> None:
    with pytest.raises(SystemExit, match="retired on 2026-10-22"):
        module.main()


def test_retired_lightning_routes_are_not_mounted() -> None:
    router = APIRouter()
    register_gateway_internal_routes(router)
    assert not any("/lightning/" in getattr(route, "path", "") for route in router.routes)
    assert "register_lightning_support_routes" not in (ROOT / "src/trusted_router/main.py").read_text()


def test_retired_lightning_is_not_automatically_recreated() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/lightning-experiment.yml").read_text())
    assert set(workflow.get("on", workflow.get(True))) == {"workflow_dispatch"}
    assert "TR_LIGHTNING_FUNDING_TOKEN" not in (ROOT / "scripts/deploy/rollout.sh").read_text()
    assert 'resource "google_compute_security_policy" "lightning_funding"' not in (ROOT / "infra/cloud_armor.tf").read_text()
