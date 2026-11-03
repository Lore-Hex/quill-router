# Direct Cloud Run terminal billing beside the Spanner leader. Unlike the
# script-managed multi-region control backend, this fixed topology belongs
# beside the Terraform-owned URL map. rollout.sh owns the NEG and
# Cloud Run service; this resource only references their existing us-central1
# NEG and never changes its membership. See docs/runbooks/control-terminals-us-central1.md.
locals {
  terminal_c1_route_enabled = false
}

resource "google_compute_backend_service" "control_terminal_c1" {
  project                = local.gcp_project_id
  name                   = "trusted-router-control-terminal-c1-backend"
  description            = "Direct us-central1 Cloud Run for gateway terminal billing"
  protocol               = "HTTP"
  load_balancing_scheme  = "EXTERNAL_MANAGED"
  compression_mode       = "AUTOMATIC"
  security_policy        = google_compute_security_policy.legacy_edge.id
  custom_request_headers = [
    "X-TrustedRouter-Client-IP:{client_ip_address}",
  ]
  # timeout_sec does not apply to serverless NEGs; leave the API default.
  # port_name is optional/computed and is deliberately not configured.
  # Authenticated billing reads must never be cached (exception to control).
  enable_cdn = false
  # The live control backend reports drainingTimeoutSec 0; Terraform's
  # default is 300, which a serverless NEG backend does not use.
  connection_draining_timeout_sec = 0

  backend {
    group = "projects/${local.gcp_project_id}/regions/us-central1/networkEndpointGroups/trusted-router-control-neg"
  }

  # Match the control backend's captured settings in
  # tests/fixtures/gateway-edge/control-backend.fixture.json and _edge_security.sh.
  log_config {
    enable        = true
    sample_rate   = 0.1
    optional_mode = "EXCLUDE_ALL_OPTIONAL"
  }
}
