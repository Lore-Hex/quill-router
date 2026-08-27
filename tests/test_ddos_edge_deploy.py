from __future__ import annotations

import base64
import json
import os
import subprocess
from pathlib import Path

import pytest

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
    assert invoked.count("--preview") == 2
    host_rule = next(
        line
        for line in invoked.splitlines()
        if "security-policies rules create 900" in line
    )
    assert "--preview" not in host_rule
    assert "--action=deny-403" in host_rule
    global_rule = next(
        line
        for line in invoked.splitlines()
        if "security-policies rules create 1200" in line
    )
    assert "--preview" not in global_rule
    assert "--action=throttle" in global_rule
    assert "--exceed-action=deny-429" in global_rule
    assert "--enforce-on-key=IP" in global_rule
    assert "--security-policy=tr-public-policy" in invoked
    assert "--enable-logging" in invoked
    assert "--custom-request-header=X-Existing:keep" in invoked
    assert (
        "--custom-request-header=X-TrustedRouter-Client-IP:{client_ip_address}" in invoked
    )
    assert "--custom-request-header=x-trustedrouter-client-ip:spoof" not in invoked


def test_gcp_existing_host_gate_and_all_path_ceiling_are_forced_out_of_preview(
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
}}
log() {{ :; }}
TR_CLOUD_ARMOR_PREVIEW=1 _reconcile_cloud_armor_policy tr-existing-policy
""",
        encoding="utf-8",
    )

    result = _run_bash(driver)

    assert result.returncode == 0, result.stderr
    updates = calls.read_text(encoding="utf-8").splitlines()
    host_updates = [
        line
        for line in updates
        if "security-policies rules update 900" in line
    ]
    assert len(host_updates) == 1
    assert "--no-preview" in host_updates[0]
    assert "--preview" not in host_updates[0]
    assert "--action=deny-403" in host_updates[0]

    global_updates = [
        line
        for line in updates
        if "security-policies rules update 1200" in line
    ]
    assert len(global_updates) == 1
    global_update = global_updates[0]
    assert "--no-preview" in global_update
    assert "--preview" not in global_update
    assert "--enforce-on-key=IP" in global_update
    assert "--action=throttle" in global_update
    assert "--exceed-action=deny-429" in global_update


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
    search_strings: list[str] = []

    def collect_search_strings(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "SearchString":
                    assert isinstance(child, str)
                    search_strings.append(child)
                else:
                    collect_search_strings(child)
        elif isinstance(value, list):
            for child in value:
                collect_search_strings(child)

    collect_search_strings(preview_rules["AllowedHosts"])
    decoded_search_strings = {
        base64.b64decode(value, validate=True).decode("utf-8") for value in search_strings
    }
    assert "aws.trustedrouter.com" in decoded_search_strings
    assert "aws.trustedrouter.com:443" in decoded_search_strings
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


def test_aws_observer_deploy_is_wafed_bounded_and_uses_tcp_health() -> None:
    deploy = (ROOT / "scripts/deploy/aws_eu_control_plane.sh").read_text(
        encoding="utf-8"
    )
    capacity = (ROOT / "scripts/deploy/_aws_app_runner_security.sh").read_text(
        encoding="utf-8"
    )

    assert 'source "${SCRIPT_DIR}/_aws_waf.sh"' in deploy
    assert 'source "${SCRIPT_DIR}/_aws_app_runner_security.sh"' in deploy
    assert '"TR_RATE_LIMIT_CLIENT_IP_MODE": "untrusted"' in deploy
    assert '"TR_MAX_REQUEST_BODY_BYTES": "4194304"' in deploy
    assert '"TR_MAX_IN_FLIGHT_REQUEST_BODY_BYTES": "8388608"' in deploy
    assert '"TR_MAX_CONCURRENT_REQUEST_BODIES": "2"' in deploy
    assert '"TR_REQUEST_BODY_READ_TIMEOUT_SECONDS": "10"' in deploy
    assert '"TR_OBSERVER_INTERNAL_TOKEN": "${OBSERVER_TOKEN_SECRET_ARN}"' in deploy
    assert '"TR_INTERNAL_GATEWAY_TOKEN=' not in deploy
    assert "TR_INTERNAL_GATEWAY_TOKEN=trustedrouter" not in deploy
    assert "quill/trustedrouter-observer-internal-token" in deploy
    assert deploy.count("quill/trustedrouter-internal-gateway-token") == 1
    assert 'AWS_OBSERVER_MAX_CONCURRENCY="${TR_AWS_OBSERVER_MAX_CONCURRENCY:-10}"' in deploy
    assert 'AWS_OBSERVER_MAX_INSTANCES="${TR_AWS_OBSERVER_MAX_INSTANCES:-4}"' in deploy
    assert "create-auto-scaling-configuration" in capacity
    assert deploy.count('--auto-scaling-configuration-arn "$AUTO_SCALING_ARN"') == 2
    assert deploy.count('--health-check-configuration "$HEALTH_CHECK_CONFIG"') == 2
    assert 'HEALTH_CHECK_CONFIG="Protocol=TCP' in deploy
    assert "Protocol=HTTP,Path=/health" not in deploy
    assert 'verify_app_runner_capacity_and_health "$ARN" "$AUTO_SCALING_ARN"' in deploy
    assert '[ "$S" = "RUNNING" ] ||' in deploy
    assert "*.awsapprunner.com" in deploy
    assert 'reconcile_app_runner_waf "$ARN" "$URL"' in deploy
    assert deploy.index('reconcile_app_runner_waf "$ARN" "$URL"') < deploy.index(
        'if [ "${SKIP_EVENTBRIDGE:-0}" != "1" ]'
    )


def test_app_runner_capacity_reuses_exact_revision_and_verifies_live_state(
    tmp_path: Path,
) -> None:
    calls = tmp_path / "calls.log"
    expected = (
        "arn:aws:apprunner:eu-west-3:330422590279:"
        "autoscalingconfiguration/tr-eu-observer-bounded/1/config-id"
    )
    driver = tmp_path / "driver.sh"
    driver.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
source {ROOT / 'scripts/deploy/_aws_app_runner_security.sh'}
CALLS={calls}
EXPECTED={expected}
log() {{ :; }}
aws() {{
  printf '%s\\n' "$*" >> "$CALLS"
  case "$*" in
    "apprunner describe-auto-scaling-configuration "*) printf '10\\t1\\t4\\t%s\\n' "$EXPECTED" ;;
    *"AutoScalingConfigurationSummary"*) printf '%s\\n' "$EXPECTED" ;;
    *"HealthCheckConfiguration.Protocol"*) printf '%s\\n' TCP ;;
    *) return 90 ;;
  esac
}}
REGION=eu-west-3
actual="$(reconcile_app_runner_capacity 330422590279 tr-eu-observer-bounded 10 1 4)"
[ "$actual" = "$EXPECTED" ]
verify_app_runner_capacity_and_health \
  arn:aws:apprunner:eu-west-3:330422590279:service/tr-eu/id "$EXPECTED"
""",
        encoding="utf-8",
    )

    result = _run_bash(driver)

    assert result.returncode == 0, result.stderr
    invoked = calls.read_text(encoding="utf-8")
    assert "create-auto-scaling-configuration" not in invoked
    assert invoked.count("apprunner describe-service") == 2


def test_app_runner_capacity_creates_only_after_confirmed_missing_or_drift(
    tmp_path: Path,
) -> None:
    calls = tmp_path / "calls.log"
    created = (
        "arn:aws:apprunner:eu-west-3:330422590279:"
        "autoscalingconfiguration/tr-eu-observer-bounded/2/new-id"
    )
    driver = tmp_path / "driver.sh"
    driver.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
source {ROOT / 'scripts/deploy/_aws_app_runner_security.sh'}
CALLS={calls}
CREATED={created}
log() {{ :; }}
aws() {{
  printf '%s\\n' "$*" >> "$CALLS"
  case "$*" in
    "apprunner describe-auto-scaling-configuration "*)
      printf '%s\\n' 'An error occurred (ResourceNotFoundException)' >&2
      return 254
      ;;
    "apprunner create-auto-scaling-configuration "*) printf '%s\\n' "$CREATED" ;;
    *) return 90 ;;
  esac
}}
REGION=eu-west-3
actual="$(reconcile_app_runner_capacity 330422590279 tr-eu-observer-bounded 10 1 4)"
[ "$actual" = "$CREATED" ]
""",
        encoding="utf-8",
    )

    result = _run_bash(driver)

    assert result.returncode == 0, result.stderr
    invoked = calls.read_text(encoding="utf-8")
    assert invoked.count("create-auto-scaling-configuration") == 1
    assert "--max-concurrency 10 --min-size 1 --max-size 4" in invoked


def test_app_runner_capacity_fails_closed_on_inspection_error_and_bad_name(
    tmp_path: Path,
) -> None:
    calls = tmp_path / "calls.log"
    driver = tmp_path / "driver.sh"
    driver.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
source {ROOT / 'scripts/deploy/_aws_app_runner_security.sh'}
CALLS={calls}
log() {{ :; }}
aws() {{
  printf '%s\\n' "$*" >> "$CALLS"
  printf '%s\\n' 'An error occurred (AccessDeniedException)' >&2
  return 254
}}
REGION=eu-west-3
if reconcile_app_runner_capacity 330422590279 tr-eu-observer-bounded 10 1 4; then
  exit 81
fi
if reconcile_app_runner_capacity 330422590279 'good$name' 10 1 4; then
  exit 82
fi
for bad_name in '-leading' 'trailing-' '_leading' 'trailing_' 'abc'; do
  if reconcile_app_runner_capacity 330422590279 "$bad_name" 10 1 4; then
    exit 83
  fi
done
""",
        encoding="utf-8",
    )

    result = _run_bash(driver)

    assert result.returncode == 0, result.stderr
    invoked = calls.read_text(encoding="utf-8")
    assert invoked.count("describe-auto-scaling-configuration") == 1
    assert "create-auto-scaling-configuration" not in invoked


@pytest.mark.parametrize(
    "reported_configuration",
    [
        "",
        "None",
        "garbage",
        "10\t1\t4\tarn:aws:apprunner:eu-west-3:330422590279:"
        "autoscalingconfiguration/a-different-name/1/config-id",
    ],
    ids=("empty", "none", "malformed", "wrong-configuration-arn"),
)
def test_app_runner_capacity_fails_closed_on_malformed_successful_inspection(
    tmp_path: Path,
    reported_configuration: str,
) -> None:
    calls = tmp_path / "calls.log"
    driver = tmp_path / "driver.sh"
    driver.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
source {ROOT / 'scripts/deploy/_aws_app_runner_security.sh'}
CALLS={calls}
log() {{ :; }}
aws() {{
  printf '%s\\n' "$*" >> "$CALLS"
  case "$*" in
    "apprunner describe-auto-scaling-configuration "*)
      printf '%s\\n' "$REPORTED_CONFIGURATION"
      ;;
    *) return 90 ;;
  esac
}}
REGION=eu-west-3
if reconcile_app_runner_capacity \
    330422590279 tr-eu-observer-bounded 10 1 4; then
  exit 81
fi
""",
        encoding="utf-8",
    )

    result = _run_bash(
        driver,
        env={"REPORTED_CONFIGURATION": reported_configuration},
    )

    assert result.returncode == 0, result.stderr
    invoked = calls.read_text(encoding="utf-8")
    assert invoked.count("describe-auto-scaling-configuration") == 1
    assert "create-auto-scaling-configuration" not in invoked


@pytest.mark.parametrize(
    ("actual_scaling_arn", "health_protocol", "expected_error"),
    [
        ("arn:wrong", "TCP", "App Runner uses arn:wrong"),
        ("arn:expected", "HTTP", "health protocol is HTTP"),
    ],
)
def test_app_runner_live_postcondition_rejects_scaling_and_health_drift_separately(
    tmp_path: Path,
    actual_scaling_arn: str,
    health_protocol: str,
    expected_error: str,
) -> None:
    driver = tmp_path / "driver.sh"
    driver.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
source {ROOT / 'scripts/deploy/_aws_app_runner_security.sh'}
log() {{ :; }}
aws() {{
  case "$*" in
    *"AutoScalingConfigurationSummary"*) printf '%s\\n' {actual_scaling_arn} ;;
    *"HealthCheckConfiguration.Protocol"*) printf '%s\\n' {health_protocol} ;;
    *) return 90 ;;
  esac
}}
REGION=eu-west-3
if verify_app_runner_capacity_and_health \
    arn:aws:apprunner:eu-west-3:330422590279:service/tr-eu/id arn:expected; then
  exit 81
fi
""",
        encoding="utf-8",
    )

    result = _run_bash(driver)

    assert result.returncode == 0
    assert expected_error in result.stderr


def test_azure_surfaces_use_untrusted_identity_and_bounded_http_scaling() -> None:
    for relative, expected_max in (
        ("scripts/deploy/azure_control_plane.sh", "$OBSERVER_MAX_REPLICAS_EFFECTIVE"),
        ("scripts/deploy/azure_canary_app.sh", "${OBSERVER_MAX_REPLICAS:-2}"),
    ):
        deploy = (ROOT / relative).read_text(encoding="utf-8")
        for env_var in (
            "TR_RATE_LIMIT_CLIENT_IP_MODE=untrusted",
            "TR_MAX_REQUEST_BODY_BYTES=4194304",
            "TR_MAX_IN_FLIGHT_REQUEST_BODY_BYTES=8388608",
            "TR_MAX_CONCURRENT_REQUEST_BODIES=2",
            "TR_REQUEST_BODY_READ_TIMEOUT_SECONDS=10",
        ):
            assert env_var in deploy
        assert f'--max-replicas "{expected_max}"' in deploy
        assert deploy.count('--scale-rule-name observer-http') == 2
        assert deploy.count('--scale-rule-type http') == 2
        assert (
            deploy.count(
                '--scale-rule-http-concurrency "${OBSERVER_HTTP_CONCURRENCY:-10}"'
            )
            == 2
        )
        assert deploy.count("az containerapp revision set-mode") == 1
        assert "--mode single" in deploy
        assert "properties.configuration.activeRevisionsMode" in deploy
        assert "properties.template.scale.maxReplicas" in deploy
        assert "concurrentRequests | [0]" in deploy
        assert "TR_INTERNAL_GATEWAY_TOKEN=secretref" not in deploy
        if relative.endswith("azure_control_plane.sh"):
            configured_env = deploy.split("ENV_VARS=(", 1)[1].split("\n)", 1)[0]
            assert 'OBSERVER_MAX_REPLICAS_EFFECTIVE=1' in deploy
            assert (
                '"TR_SYNTHETIC_SCHEDULER_INTERVAL_SECONDS='
                '${SYNTHETIC_INTERVAL_SECONDS}"'
                in configured_env
            )
            assert (
                '"TR_REMEDIATOR_IN_PROCESS_ENABLED='
                '${OBSERVER_REMEDIATOR_IN_PROCESS_ENABLED}"'
                in configured_env
            )
            assert 'OBSERVER_REMEDIATOR_IN_PROCESS_ENABLED="true"' in deploy
            assert "TR_INTERNAL_GATEWAY_TOKEN" not in configured_env
            assert "TR_FEDERATION_" not in configured_env
            assert "TR_OBSERVER_INTERNAL_TOKEN=secretref:observer-token" in deploy
            assert "trustedrouter-observer-internal-token" in deploy
            retired_env = deploy.split("RETIRED_OBSERVER_ENV_VARS=(", 1)[1].split(
                "\n)", 1
            )[0]
            for retired_name in (
                "TR_INTERNAL_GATEWAY_TOKEN",
                "TR_FEDERATION_HOME_TOKEN",
                "TR_FEDERATION_SETTLEMENT_HOME_TOKEN",
                "TR_FEDERATION_DEFERRED_SETTLEMENT_ENABLED",
                "TR_FEDERATION_HOME_BASE_URL",
            ):
                assert retired_name in retired_env
                assert f'"{retired_name}=secretref' not in deploy
            assert '--remove-env-vars "${RETIRED_OBSERVER_ENV_VARS[@]}"' in deploy
            assert 'for retired_env_name in "${RETIRED_OBSERVER_ENV_VARS[@]}"' in deploy
        else:
            assert "TR_INTERNAL_GATEWAY_TOKEN" not in deploy
            assert "TR_FEDERATION_" not in deploy


def test_azure_canary_is_public_with_only_a_dedicated_attribution_signer() -> None:
    deploy = (ROOT / "scripts/deploy/azure_canary_app.sh").read_text(
        encoding="utf-8"
    )

    assert '"TR_SERVICE_SURFACE=public"' in deploy
    assert '"TR_SERVICE_SURFACE=observer"' not in deploy
    assert (
        '"TR_ATTRIBUTION_COOKIE_SECRET=secretref:${ATTRIBUTION_SECRET_NAME}"'
        in deploy
    )
    assert 'ATTRIBUTION_SECRET_NAME="attribution-cookie-secret"' in deploy
    assert "openssl rand -hex 32" in deploy
    assert "TR_INTERNAL_GATEWAY_TOKEN" not in deploy
    assert "TR_SYNTHETIC_MONITOR_API_KEY" not in deploy
    assert "TR_FEDERATION_" not in deploy
    assert '"TR_GOOGLE_OAUTH_LOGIN_AVAILABLE=false"' in deploy
    assert '"TR_GITHUB_OAUTH_LOGIN_AVAILABLE=false"' in deploy
    retired_oauth_env = deploy.split(
        "RETIRED_PUBLIC_OAUTH_ENV_VARS=(", 1
    )[1].split("\n)", 1)[0]
    for retired_name in (
        "TR_GOOGLE_CLIENT_ID",
        "TR_GOOGLE_CLIENT_SECRET",
        "TR_GOOGLE_OAUTH_REDIRECT_URL",
        "TR_GOOGLE_ALIAS_CREDENTIALS_JSON",
        "TR_GITHUB_CLIENT_ID",
        "TR_GITHUB_CLIENT_SECRET",
        "TR_GITHUB_OAUTH_REDIRECT_URL",
        "TR_GITHUB_ALIAS_CREDENTIALS_JSON",
    ):
        assert retired_name in retired_oauth_env
    assert '--remove-env-vars "${RETIRED_PUBLIC_OAUTH_ENV_VARS[@]}"' in deploy
    assert (
        'for retired_env_name in "${RETIRED_PUBLIC_OAUTH_ENV_VARS[@]}"'
        in deploy
    )
    assert "public canary retains forbidden OAuth env" in deploy
    assert "public canary OAuth capability verification failed" in deploy


def test_every_synthetic_job_uses_private_run_app_ingress() -> None:
    deploy = (ROOT / "scripts/deploy/synthetic.sh").read_text(encoding="utf-8")

    assert 'source "${SCRIPT_DIR}/_private_run_ingress.sh"' in deploy
    assert '"TR_SERVICE_SURFACE=observer"' in deploy
    assert (
        '"TR_OBSERVER_INTERNAL_TOKEN=trustedrouter-observer-internal-token:latest"'
        in deploy
    )
    assert (
        '"TR_INTERNAL_GATEWAY_TOKEN=trustedrouter-internal-gateway-token:latest"'
        in deploy
    )
    assert (
        'SYNTHETIC_INGEST_SERVICE="$TR_BILLING_SERVICE"'
    ) in deploy
    assert 'SYNTHETIC_INGEST_SERVICE="$SERVICE"' in deploy
    assert "TR_SYNTHETIC_INGEST_SERVICE" not in deploy
    assert deploy.count("${SYNTHETIC_INGEST_SERVICE}-${PROJECT_NUMBER}") == 4
    assert deploy.count("gc run jobs deploy") == 4
    assert deploy.count('"$JOB_SECRET_FLAG" "$JOB_SECRETS"') == 4
    assert 'JOB_SECRET_FLAG="--set-secrets"' in deploy
    assert 'JOB_SECRET_FLAG="--update-secrets"' in deploy
    assert deploy.count("ensure_private_run_app_access") == 1
    assert deploy.count("verify_synthetic_ingest_service_contract") == 2
    guarded_network_args = (
        '"${PRIVATE_RUN_APP_JOB_NETWORK_ARGS[@]+'
        '"${PRIVATE_RUN_APP_JOB_NETWORK_ARGS[@]}"}"'
    )
    assert deploy.count(guarded_network_args) == 4


def test_secret_bootstrap_provisions_a_distinct_observer_credential() -> None:
    deploy = (ROOT / "scripts/deploy/secrets.sh").read_text(encoding="utf-8")

    assert "generated secret trustedrouter-observer-internal-token" in deploy
    assert "secrets.token_urlsafe(48)" in deploy
    assert "--secret=trustedrouter-observer-internal-token" in deploy
    assert "--secret=trustedrouter-internal-gateway-token" in deploy
    assert 'if [ "$_observer_token_check" = "$_gateway_token_check" ]' in deploy
    assert "unset _observer_token_check _gateway_token_check" in deploy


def test_edge_surface_inventory_is_total_and_never_claims_live_completion() -> None:
    inventory_path = ROOT / "docs/security/edge-surfaces.json"
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    surfaces = inventory["surfaces"]

    required_hosts = {
        f"{prefix}{domain}"
        for domain in ("trustedrouter.com", "allyrouter.com", "uptimerouter.com")
        for prefix in ("", "www.", "status.", "trust.", "api.")
    }
    required_hosts.update(
        {"eu.trustedrouter.com", "status-us.trustedrouter.com", "status-eu.trustedrouter.com"}
    )
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
    assert "actions-backend=actions-edge" in runbook
    for row in (
        "| Public/static | 4 | 10 |",
        "| Anonymous actions | 4 | 2 |",
        "| Control | 4 | 20 |",
        "| Billing/internal gateway | 8 | 50 |",
        "| Observer/status worker | 4 | 4 |",
    ):
        assert row in runbook
    assert "Do not enable AWS Shield Advanced or Azure Front Door Premium automatically" in runbook
    assert "quill/trustedrouter-observer-internal-token" in runbook
    assert "TR_BILLING_SERVICE" in runbook
    assert "different from `SERVICE`" in runbook
