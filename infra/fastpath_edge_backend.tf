# The edge's load balancer backend (docs/design/fast-admission-enclave-direct.md,
# E1): the fast path nodes on the edge's port, grouped per zone, health-checked
# on the edge's own answer, behind the control load balancer's proxy with the
# same Cloud Armor policy the control backend carries for /internal today. No
# route sends traffic here yet; the path rule for /internal/gateway/* is the
# switch, added to control_lb_url_map.tf on its own, and reverting it is the
# way back.
#
# The health check is the edge's /internal/gateway/edge/health, 200 while the
# node serves and 503 once SIGUSR1 marks it leaving, so the backend follows
# membership: a retiring node stops receiving before its requests drain. The
# nodes keep no external address; the edge reaches Cloud Run's internal
# ingress through Private Google Access on the subnet, which the nodes' Cloud
# NAT egress does not count as (NAT-sourced calls are external to Cloud Run).

locals {
  fastpath_edge_port = 8081
  # Google's health checkers and the global external proxies, the only
  # sources outside the network the edge's port admits (the enclave's calls
  # arrive through them); the network's own default-allow-internal stays.
  google_lb_ranges = ["130.211.0.0/22", "35.191.0.0/16"]
}

import {
  to = google_compute_subnetwork.fastpath_default
  id = "projects/quill-cloud-proxy/regions/us-central1/subnetworks/default"
}

# The default subnet the nodes sit in, adopted for one setting.
resource "google_compute_subnetwork" "fastpath_default" {
  project                  = local.gcp_project_id
  name                     = "default"
  region                   = local.fastpath_region
  network                  = "default"
  ip_cidr_range            = "10.128.0.0/20"
  private_ip_google_access = true

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_compute_firewall" "fastpath_edge" {
  project     = local.gcp_project_id
  name        = "tr-fastpath-edge"
  network     = "default"
  description = "The fast path edge's port, from Google's load balancer and health checker ranges only"
  direction   = "INGRESS"

  allow {
    protocol = "tcp"
    ports    = [tostring(local.fastpath_edge_port)]
  }

  source_ranges           = local.google_lb_ranges
  target_service_accounts = [google_service_account.fastpath_node.email]
}

# A group follows its node's identity, not its name: a node replaced under
# the same name (a startup-script change) keeps its self link, and the
# provider then compares membership by URL and can leave the new node out
# of a group it already lists. The instance id in the name makes the
# replacement a new group, created before the old one leaves the backend.
resource "google_compute_instance_group" "fastpath_edge" {
  for_each = google_compute_instance.fastpath_node

  project   = local.gcp_project_id
  name      = "${each.key}-edge-${substr(each.value.instance_id, 0, 8)}"
  zone      = each.value.zone
  instances = [each.value.self_link]

  named_port {
    name = "edge"
    port = local.fastpath_edge_port
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "google_compute_health_check" "fastpath_edge" {
  project             = local.gcp_project_id
  name                = "tr-fastpath-edge"
  check_interval_sec  = 5
  timeout_sec         = 3
  healthy_threshold   = 2
  unhealthy_threshold = 2

  http_health_check {
    port         = local.fastpath_edge_port
    request_path = "/internal/gateway/edge/health"
  }

  log_config {
    enable = true
  }
}

resource "google_compute_backend_service" "fastpath_edge" {
  project               = local.gcp_project_id
  name                  = "tr-fastpath-edge-backend"
  description           = "The fast path edge for the enclave's /internal/gateway calls (E1)"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  protocol              = "HTTP"
  port_name             = "edge"
  timeout_sec           = 30
  health_checks         = [google_compute_health_check.fastpath_edge.id]
  security_policy       = google_compute_security_policy.legacy_edge.id
  # The unit's own stop timeout; a node marked leaving answers 503 and
  # drains its requests within it.
  connection_draining_timeout_sec = 30

  dynamic "backend" {
    for_each = google_compute_instance_group.fastpath_edge
    content {
      group          = backend.value.self_link
      balancing_mode = "UTILIZATION"
    }
  }

  log_config {
    enable = true
  }
}
