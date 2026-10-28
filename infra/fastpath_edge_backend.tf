# The edge's load balancer backend (docs/design/fast-admission-enclave-direct.md,
# E1): the fast path nodes on the edge's port, grouped per zone, health-checked
# on the edge's own answer, behind the control load balancer's proxy with the
# same Cloud Armor policy the control backend carries for /internal today. The
# control load balancer sends a weighted share of the exact path
# /internal/gateway/authorize here (control_lb_url_map.tf,
# local.fastpath_edge_route_weight below); a weight of 0 is the way back.
# Traffic flows, so wait for a node to turn UNHEALTHY before stopping it
# (docs/runbooks/fastpath-deploy.md).
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
  # Each node of infra/fastpath.tf has a group here and is a backend; none
  # is retiring (7 and 8 left once 9 and 10 served and reported HEALTHY,
  # one apply before their removal, stage I2).
  fastpath_edge_retiring = []
  # The share, in percent, of the enclave's authorize calls the control load
  # balancer sends through the edge (infra/control_lb_url_map.tf). 1 once
  # Python counted every comparison of the edge's verdict (#1904) on the
  # serving revision, so the share has parity evidence: agree, differ and
  # skipped lines, and no lines at all from an edge whose token is stale.
  # 0 is the way back. Held at 0 from 2026-10-22 05:33Z until the authorize
  # alerts (docs/incidents/2026-10-22-authorize-edge-deadline.md) were
  # explained: the route's own timeout, and the enclave's settle retry,
  # which no longer overlaps the settle it retries. 50 from 2026-10-26
  # 17:25Z, back to 5 at 19:33Z (#2086) when the auditor's commit loop
  # blocked on a call with no deadline and its subscription lagged; 50 again
  # once every call of the auditor's running loop has a deadline and a
  # watchdog fails a wedged loop (in 892bd7593 and since, on both nodes).
  # 100 from 2026-10-28, after 50's gates held, once both nodes run
  # 0f0b1829a: each call of a start has a deadline, the secret reads
  # (#2133) and the ring join and first membership scan (#2130) among them,
  # and watchdogs on the start exit a stalled node for systemd to restart.
  # The control backend stays in the route at 0, so the way back is this
  # line.
  fastpath_edge_route_weight = 100
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
  # Applies only where no route timeout is set. The one route to this
  # backend, /internal/gateway/authorize, sets 24 s (control_lb_url_map.tf),
  # which governs: billing's 20 s budget plus cleanup must finish before the
  # balancer cuts the call off (docs/incidents/2026-10-22-authorize-edge-
  # deadline.md).
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
