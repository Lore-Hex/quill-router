from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "provision_stage_d_probe_workspace.py"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("provision_stage_d_probe_workspace", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _MissingStore:
    def find_user_by_email(self, _email: str) -> Any:
        return None


def test_stage_d_probe_provisioner_is_dry_run_and_documents_isolation(
    capsys: Any,
) -> None:
    module = _module()
    assert module.main([], store=_MissingStore()) == 0
    output = capsys.readouterr().out
    assert '"heartbeat_capable_local_typed_key": true' in output
    assert '"regional_quota_pilot_membership_required": false' in output
    assert '"secret_name": "trustedrouter-stage-d-probe-api-key"' in output
    assert "DRY-RUN: no production state changed" in output


def test_recurring_deploy_uses_expect_stage_d_only_for_dedicated_job() -> None:
    deploy = (ROOT / "scripts" / "deploy" / "synthetic.sh").read_text()
    assert "trustedrouter-stage-d-probe-api-key" in deploy
    assert 'stage_d_probe_job="trusted-router-stage-d-probe-${stage_d_probe_region}"' in deploy
    assert '--args="-m,trusted_router.synthetic.cli,--expect-stage-d"' in deploy
    assert deploy.count("--expect-stage-d") == 1
    assert (
        '"TR_INTERNAL_GATEWAY_TOKEN=trustedrouter-internal-gateway-token:latest"'
        in deploy
    )
    assert '"$JOB_SECRET_FLAG" "$stage_d_job_secrets"' in deploy
