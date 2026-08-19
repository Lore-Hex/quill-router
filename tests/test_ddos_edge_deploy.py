from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run_bash(driver: Path, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(  # noqa: S603
        ["bash", str(driver)],  # noqa: S607
        capture_output=True,
        text=True,
        env=merged,
    )


def test_cloud_run_security_defaults_are_per_service_and_cost_bounded() -> None:
    shared = (ROOT / "scripts/deploy/_lib.sh").read_text(encoding="utf-8")

    assert (
        'TR_CLOUD_RUN_INGRESS="${TR_CLOUD_RUN_INGRESS:-internal-and-cloud-load-balancing}"'
        in shared
    )
    assert 'TR_CLOUD_RUN_DISABLE_DEFAULT_URL="${TR_CLOUD_RUN_DISABLE_DEFAULT_URL:-0}"' in shared
    assert 'TR_CLOUD_RUN_MAX_INSTANCES="${TR_CLOUD_RUN_MAX_INSTANCES:-20}"' in shared


def test_gcp_edge_reconciler_preserves_headers_and_repairs_every_security_control(
    tmp_path: Path,
) -> None:
    calls = tmp_path / "calls.log"
    driver = tmp_path / "driver.sh"
    driver.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
source {ROOT / 'scripts/deploy/_edge_security.sh'}
CALLS={calls}
gc() {{
  printf '%s\\n' "$*" >> "$CALLS"
  case "$*" in
    "compute security-policies describe tr-public-policy --global") return 1 ;;
    "compute security-policies rules describe "*) return 1 ;;
    "compute backend-services describe tr-public-backend --global --format=json")
      printf '%s\\n' '{{"customRequestHeaders":["X-Existing:keep","x-trustedrouter-client-ip:spoof"]}}'
      ;;
    "compute backend-services describe tr-public-backend --global --format=value(securityPolicy.basename())")
      printf '%s\\n' tr-public-policy
      ;;
    "compute backend-services describe tr-public-backend --global --format=json(customRequestHeaders,securityPolicy)")
      printf '%s\\n' '{{"customRequestHeaders":["X-Existing:keep","X-TrustedRouter-Client-IP:{{client_ip_address}}"],"securityPolicy":"/global/securityPolicies/tr-public-policy"}}'
      ;;
  esac
}}
log() {{ :; }}
reconcile_edge_backend tr-public-backend tr-public-policy
""",
        encoding="utf-8",
    )

    result = _run_bash(driver)
    assert result.returncode == 0, result.stderr
    invoked = calls.read_text(encoding="utf-8")
    assert "security-policies create tr-public-policy" in invoked
    assert "security-policies rules update 2147483647" in invoked
    assert invoked.count("security-policies rules create") == 4
    assert invoked.count("--action=throttle") == 3
    assert invoked.count("--preview") == 4
    assert "--security-policy=tr-public-policy" in invoked
    assert "--enable-logging" in invoked
    assert "--custom-request-header=X-Existing:keep" in invoked
    assert (
        "--custom-request-header=X-TrustedRouter-Client-IP:{client_ip_address}" in invoked
    )
    assert "--custom-request-header=x-trustedrouter-client-ip:spoof" not in invoked


def test_gcp_edge_reconciler_is_parameterized_for_multiple_backends(tmp_path: Path) -> None:
    driver = tmp_path / "driver.sh"
    driver.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
source {ROOT / 'scripts/deploy/_edge_security.sh'}
reconcile_edge_backend() {{ printf '%s=%s\\n' "$1" "$2"; }}
reconcile_edge_backend_mappings 'public=public-policy,control=control-policy,billing=billing-policy'
""",
        encoding="utf-8",
    )

    result = _run_bash(driver)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "public=public-policy",
        "control=control-policy",
        "billing=billing-policy",
    ]


def test_gcp_edge_reconciler_rejects_malformed_logging_fraction(tmp_path: Path) -> None:
    driver = tmp_path / "driver.sh"
    driver.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
source {ROOT / 'scripts/deploy/_edge_security.sh'}
gc() {{ :; }}
log() {{ :; }}
TR_CLOUD_ARMOR_LOG_SAMPLE_RATE=0.not-a-number \
  reconcile_edge_backend tr-public-backend tr-public-policy
""",
        encoding="utf-8",
    )

    result = _run_bash(driver)
    assert result.returncode == 2
    assert "must be between 0 and 1" in result.stderr


def test_private_run_app_reconciler_uses_private_google_access(tmp_path: Path) -> None:
    calls = tmp_path / "calls.log"
    driver = tmp_path / "driver.sh"
    driver.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
source {ROOT / 'scripts/deploy/_private_run_ingress.sh'}
CALLS={calls}
gc() {{
  printf '%s\\n' "$*" >> "$CALLS"
  case "$*" in
    "dns managed-zones describe trusted-router-private-run-app") return 1 ;;
    "dns managed-zones describe trusted-router-private-run-app --format=json")
      printf '%s\\n' '{{"dnsName":"run.app.","visibility":"private","privateVisibilityConfig":{{"networks":[{{"networkUrl":"https://compute.googleapis.com/compute/v1/projects/example/global/networks/default"}}]}}}}'
      ;;
    "dns record-sets describe "*) return 1 ;;
  esac
}}
log() {{ :; }}
ensure_private_run_app_access europe-west4
printf 'ARGS=%s\\n' "${{PRIVATE_RUN_APP_JOB_NETWORK_ARGS[*]}}"
""",
        encoding="utf-8",
    )

    result = _run_bash(driver)
    assert result.returncode == 0, result.stderr
    invoked = calls.read_text(encoding="utf-8")
    assert "services enable dns.googleapis.com" in invoked
    assert (
        "compute networks subnets update default --region=europe-west4 "
        "--enable-private-ip-google-access" in invoked
    )
    assert "dns managed-zones create trusted-router-private-run-app" in invoked
    assert "--rrdatas=199.36.153.8,199.36.153.9,199.36.153.10,199.36.153.11" in invoked
    assert "dns record-sets create *.run.app." in invoked
    assert result.stdout.strip() == (
        "ARGS=--network default --subnet default --vpc-egress private-ranges-only"
    )


def test_aws_waf_enforces_high_ceiling_while_risky_rules_start_in_count(
    tmp_path: Path,
) -> None:
    driver = tmp_path / "driver.sh"
    driver.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
source {ROOT / 'scripts/deploy/_aws_waf.sh'}
_aws_waf_rules_json tr-eu.example.awsapprunner.com
""",
        encoding="utf-8",
    )

    preview = _run_bash(driver, env={"TR_AWS_WAF_PREVIEW": "1"})
    assert preview.returncode == 0, preview.stderr
    preview_rules = {item["Name"]: item for item in json.loads(preview.stdout)}
    assert preview_rules["HighRatePerIpBlock"]["Action"] == {"Block": {}}
    assert preview_rules["HighRatePerIpBlock"]["Statement"]["RateBasedStatement"] == {
        "Limit": 6000,
        "EvaluationWindowSec": 300,
        "AggregateKeyType": "IP",
    }
    assert preview_rules["AllowedHosts"]["Action"] == {"Count": {}}
    allowed_host_json = json.dumps(preview_rules["AllowedHosts"])
    assert "aws.trustedrouter.com" in allowed_host_json
    assert "aws.trustedrouter.com:443" in allowed_host_json
    assert preview_rules["StateChangingRate"]["Action"] == {"Count": {}}
    assert preview_rules["AwsManagedCommon"]["OverrideAction"] == {"Count": {}}

    enforced = _run_bash(driver, env={"TR_AWS_WAF_PREVIEW": "0"})
    assert enforced.returncode == 0, enforced.stderr
    enforced_rules = {item["Name"]: item for item in json.loads(enforced.stdout)}
    assert enforced_rules["AllowedHosts"]["Action"] == {"Block": {}}
    assert enforced_rules["StateChangingRate"]["Action"] == {"Block": {}}
    assert enforced_rules["AwsManagedCommon"]["OverrideAction"] == {"None": {}}


def test_aws_waf_helper_associates_and_verifies_the_resource() -> None:
    waf = (ROOT / "scripts/deploy/_aws_waf.sh").read_text(encoding="utf-8")

    assert "wafv2 create-web-acl" in waf
    assert "wafv2 update-web-acl" in waf
    assert "wafv2 associate-web-acl" in waf
    assert "wafv2 get-web-acl-for-resource" in waf
    assert "verify_app_runner_waf" in waf


def test_aws_waf_reconciliation_updates_associates_and_verifies(tmp_path: Path) -> None:
    calls = tmp_path / "calls.log"
    driver = tmp_path / "driver.sh"
    driver.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
source {ROOT / 'scripts/deploy/_aws_waf.sh'}
CALLS={calls}
expected_arn=arn:aws:wafv2:eu-west-3:123456789012:regional/webacl/tr-edge/acl-id
aws() {{
  printf '%s\\n' "$*" >> "$CALLS"
  case "$*" in
    "wafv2 list-web-acls "*)
      printf 'acl-id\\t%s\\n' "$expected_arn"
      ;;
    "wafv2 get-web-acl-for-resource "*)
      printf '%s\\n' "$expected_arn"
      ;;
    "wafv2 get-web-acl "*)
      printf '%s\\n' '{{"LockToken":"lock","WebACL":{{"Rules":[{{"Name":"HighRatePerIpBlock","Action":{{"Block":{{}}}},"Statement":{{"RateBasedStatement":{{"AggregateKeyType":"IP"}}}}}},{{"Name":"AwsManagedCommon","Statement":{{"ManagedRuleGroupStatement":{{"VendorName":"AWS"}}}}}}]}}}}'
      ;;
  esac
}}
log() {{ :; }}
REGION=eu-west-3 reconcile_app_runner_waf \
  arn:aws:apprunner:eu-west-3:123456789012:service/tr-eu/service-id \
  tr-eu.example.awsapprunner.com
""",
        encoding="utf-8",
    )

    result = _run_bash(driver)
    assert result.returncode == 0, result.stderr
    invoked = calls.read_text(encoding="utf-8")
    assert "wafv2 update-web-acl" in invoked
    assert "wafv2 associate-web-acl" in invoked
    assert "wafv2 get-web-acl-for-resource" in invoked
    assert "--resource-arn arn:aws:apprunner:" in invoked


def test_aws_waf_rejects_non_hostname_service_url(tmp_path: Path) -> None:
    driver = tmp_path / "driver.sh"
    driver.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
source {ROOT / 'scripts/deploy/_aws_waf.sh'}
aws() {{ :; }}
log() {{ :; }}
REGION=eu-west-3 reconcile_app_runner_waf \
  arn:aws:apprunner:eu-west-3:123456789012:service/tr-eu/id \
  'https://not-a-host.example'
""",
        encoding="utf-8",
    )

    result = _run_bash(driver)
    assert result.returncode == 2
    assert "invalid App Runner service hostname" in result.stderr


def test_edge_surface_inventory_is_total_and_never_claims_live_completion() -> None:
    inventory_path = ROOT / "docs/security/edge-surfaces.json"
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    surfaces = inventory["surfaces"]

    required_hosts = {
        f"{prefix}{domain}"
        for domain in ("trustedrouter.com", "allyrouter.com", "uptimerouter.com")
        for prefix in ("", "www.", "status.", "trust.", "api.")
    }
    declared_hosts = {host for surface in surfaces for host in surface.get("hosts", [])}
    assert required_hosts <= declared_hosts
    assert {"aws.trustedrouter.com", "api-aws.trustedrouter.com"} <= declared_hosts
    assert {
        "azure.trustedrouter.com",
        "api-azure.trustedrouter.com",
        "api-azure-sea.trustedrouter.com",
    } <= declared_hosts

    assert inventory["live_state_verified"] is False
    assert all(surface["protection_state"] != "unprotected" for surface in surfaces)
    assert all(surface["owner"] and surface["verification"] for surface in surfaces)
    assert {surface["cloud"] for surface in surfaces} >= {"gcp", "aws", "azure"}
    assert any(surface["protection_state"] == "p0_external" for surface in surfaces)


def test_runbook_has_emergency_promotion_and_independent_cost_caps() -> None:
    runbook = (ROOT / "docs/runbooks/ddos-edge-hardening.md").read_text(encoding="utf-8")

    assert "TR_CLOUD_ARMOR_PREVIEW=0" in runbook
    assert "TR_AWS_WAF_PREVIEW=0" in runbook
    assert "HighRatePerIpBlock" in runbook
    for row in (
        "| Public/static | 4 | 10 |",
        "| Control | 4 | 20 |",
        "| Billing/internal gateway | 8 | 50 |",
        "| Observer/status worker | 4 | 4 |",
    ):
        assert row in runbook
    assert "Do not enable AWS Shield Advanced or Azure Front Door Premium automatically" in runbook
