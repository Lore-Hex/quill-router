"""Keep the authorization edge deadline outside the billing work budget."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_authorize_route_has_an_explicit_deadline_above_billing_budget() -> None:
    source = (ROOT / "infra" / "control_lb_url_map.tf").read_text(encoding="utf-8")
    source = re.sub(r"^\s*#.*$", "", source, flags=re.MULTILINE)
    # Terraform fmt gives top-level path rules four-space indentation.
    rules = re.findall(r"^    path_rule \{\n(.*?)^    \}", source, re.MULTILINE | re.DOTALL)
    authorize_rules = [rule for rule in rules if 'paths = ["/internal/gateway/authorize"]' in rule]
    assert len(authorize_rules) == 1
    timeout = re.search(r"route_action\s*\{\s*timeout\s*\{\s*seconds\s*=\s*(\d+)\s*\}", authorize_rules[0])
    assert timeout is not None, "Authorize must not inherit the fast-path backend's 8s timeout"
    from trusted_router.routes.internal.gateway import _BILLING_PATH_SPANNER_BUDGET_SECONDS
    from trusted_router.storage_gcp_io import _ROLLBACK_FLOOR_SECONDS

    # quill-cloud-proxy enclave-go/internal/trustedrouter/http_client_direct.go:
    # ResponseHeaderTimeout. The balancer must answer before the enclave gives up.
    enclave_response_header_seconds = 25
    seconds = int(timeout.group(1))
    assert _BILLING_PATH_SPANNER_BUDGET_SECONDS + _ROLLBACK_FLOOR_SECONDS <= seconds
    assert seconds < enclave_response_header_seconds


def test_authorize_rule_is_byte_for_byte_unchanged() -> None:
    import hashlib

    source = (ROOT / "infra/control_lb_url_map.tf").read_text()
    start = source.index("    # The enclave's authorize")
    # Include the original explanatory comments, path, deadline and weights.
    end = source.index('\n    }', source.index('    path_rule {', start)) + len('\n    }')
    assert hashlib.sha256(source[start:end].encode()).hexdigest() == (
        "9e80c5b2464d684be63eeeb378e17057bbf15de150a37de6e9b8ebb60c7b3fbc"
    )


def test_terminal_backend_matches_recorded_control_settings() -> None:
    import json

    control = json.loads((ROOT / "tests/fixtures/gateway-edge/control-backend.fixture.json").read_text())
    source = (ROOT / "infra/control_terminal_backend.tf").read_text()
    for field, value in {
        "protocol": control["protocol"],
        "load_balancing_scheme": control["loadBalancingScheme"],
        "timeout_sec": control["timeoutSec"],
        "enable": control["logConfig"]["enable"],
        "sample_rate": control["logConfig"]["sampleRate"],
        "optional_mode": control["logConfig"]["optionalMode"],
    }.items():
        assert re.search(rf"\b{field}\s*=\s*{re.escape(json.dumps(value))}(?:\s|$)", source), field
    assert control["securityPolicy"].endswith("/trusted-router-legacy-edge")
    assert re.search(r"security_policy\s*=\s*google_compute_security_policy\.legacy_edge\.id\s", source)
    assert 'name        = "trusted-router-legacy-edge"' in (ROOT / "infra/cloud_armor.tf").read_text()
    groups = re.findall(r'\bgroup\s*=\s*"([^"]+)"', source)
    assert groups == [
        "projects/${local.gcp_project_id}/regions/us-central1/networkEndpointGroups/trusted-router-control-neg"
    ]
    assert control["customRequestHeaders"] == ["X-TrustedRouter-Client-IP:{client_ip_address}"]
    assert '"X-TrustedRouter-Client-IP:{client_ip_address}"' in source
    assert re.search(r"enable_cdn\s*=\s*false\b", source)
    assert control["connectionDraining"] == {"drainingTimeoutSec": 0}
    assert re.search(r"\bconnection_draining_timeout_sec\s*=\s*0\s", source)
    security = (ROOT / "scripts/deploy/_edge_security.sh").read_text()
    assert '${TR_CLOUD_ARMOR_LOG_SAMPLE_RATE:-0.1}' in security
    assert '--custom-request-header=X-TrustedRouter-Client-IP:{client_ip_address}' in security
    assert '--enable-logging' in security and '--security-policy="$policy"' in security
