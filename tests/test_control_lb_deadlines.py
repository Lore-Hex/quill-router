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
