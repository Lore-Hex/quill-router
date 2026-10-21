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
# edge's upstream is Cloud Run's internal ingress, which counts a request
# from a VM in this project's VPC as internal, Cloud NAT egress included
# (Cloud Run, "Restricting network ingress"): the nodes need nothing more.

# local.fastpath_edge_port is infra/fastpath.tf's; the nodes' unit reads it too.
locals {
  # Nodes on their way out: drained, out of the backend, removed next.
  fastpath_edge_retiring = []
  # The share, in percent, of the enclave's authorize calls the control load
  # balancer sends through the edge (infra/control_lb_url_map.tf). 1 once
  # Python counted every comparison of the edge's verdict (#1904) on the
  # serving revision, so the share has parity evidence: agree, differ and
  # skipped lines, and no lines at all from an edge whose token is stale.
  # 0 is the way back.
  fastpath_edge_route_weight = 1
  # Google's health checkers and the global external proxies, the only
  # sources outside the network the edge's port admits (the enclave's calls
  # arrive through them); the network's own default-allow-internal stays.
  google_lb_ranges = ["130.211.0.0/22", "35.191.0.0/16"]
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

# A group is named for its node, and its membership is read back on every
# plan. A node replaced under the same name (a startup-script change) keeps
# its self link, which the provider compares membership by, so the apply
# that replaces the node does not re-add it: the next plan sees the group
# empty and adds it back. A replacement therefore ends with a second apply
# (docs/runbooks/fastpath-deploy.md), and the health check shows the gap
# meanwhile. The lifecycle tricks that avoid the second apply (a group named
# for the instance id, brought up before the old one goes) propagate that
# ordering to the fixed-name nodes, whose replacement they then break.
resource "google_compute_instance_group" "fastpath_edge" {
  for_each = google_compute_instance.fastpath_node

  project   = local.gcp_project_id
  name      = "${each.key}-edge"
  zone      = each.value.zone
  instances = [each.value.self_link]

  named_port {
    name = "edge"
    port = local.fastpath_edge_port
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
  # Under the enclave's authorize budget (a 25 s response-header timeout,
  # 28 s in all, three attempts): a stalled upstream answers 504 at 8 s,
  # which the enclave retries, rather than outliving the budget as a
  # transport error it does not. Authorize through the balancer over a day
  # of 2026-10-20: p99 2.85 s, max 5.8 s of 3,000.
  timeout_sec     = 8
  health_checks   = [google_compute_health_check.fastpath_edge.id]
  security_policy = google_compute_security_policy.legacy_edge.id
  # The unit's own stop timeout. Draining runs only when a node is removed
  # from its group, not when its health check fails or a whole group is
  # detached: a node marked leaving answers 503 and finishes its own
  # requests within the unit's stop timeout, which this does not extend.
  connection_draining_timeout_sec = 30

  # Groups leave the backend one apply before their nodes are removed:
  # GCP refuses to delete a group a backend service still names, and
  # Terraform would delete the group before updating the backend.
  dynamic "backend" {
    for_each = {
      for name, group in google_compute_instance_group.fastpath_edge : name => group
      if !contains(local.fastpath_edge_retiring, name)
    }
    content {
      group          = backend.value.self_link
      balancing_mode = "UTILIZATION"
    }
  }

  log_config {
    enable = true
  }
}
