# Flow logs for traffic between internal hosts.
#
# From 2026-11-08 outsiders used a server inside the production network to
# make requests to internal addresses, and through it reached the internal
# ClickHouse load balancer
# (docs/incidents/2026-11-11-spanner-stolen-token-table-scans.md). Nothing
# recorded those connections: flow logs were off on every subnet. These
# records name the source and destination of each internal connection, so a
# host talking to one it never talks to can be seen after the fact.
#
# Only the four regional subnets of the default network that hold hosts are
# covered (the fast-path nodes, ClickHouse and the enclave groups; counted
# 2026-11-16: us-central1 9, us-east4 2, europe-west4 2, us-west1 2). The
# other 39 auto-created subnets hold none.
#
# The filter keeps a record only when both ends are internal addresses. The
# gateways' public traffic, which is nearly all of their traffic, the load
# balancer's health checks and calls to Google APIs all have one end outside
# 10.0.0.0/8 and are left out, so the cost follows the number of internal
# connections and not the number of customer requests. Every kept record is
# written (flow_sampling 1): a rare connection is the one worth seeing, and
# sampling would drop it. The 15-minute interval folds a long-lived
# connection into one record per interval. Measure the log volume a day after
# the apply; if it is material, lower flow_sampling here.
#
# These subnets were created by the network's auto mode, so they are
# imported. The addresses are the ranges auto mode assigned; a change to a
# range, a name or a region would replace the subnet under running hosts,
# which prevent_destroy refuses at plan time.

locals {
  flow_log_subnets = {
    "us-central1"  = "10.128.0.0/20"
    "us-east4"     = "10.150.0.0/20"
    "europe-west4" = "10.164.0.0/20"
    "us-west1"     = "10.138.0.0/20"
  }
}

import {
  for_each = local.flow_log_subnets
  to       = google_compute_subnetwork.default[each.key]
  id       = "projects/${local.gcp_project_id}/regions/${each.key}/subnetworks/default"
}

resource "google_compute_subnetwork" "default" {
  for_each = local.flow_log_subnets

  project       = local.gcp_project_id
  name          = "default"
  region        = each.key
  network       = "default"
  ip_cidr_range = each.value

  log_config {
    aggregation_interval = "INTERVAL_15_MIN"
    flow_sampling        = 1
    metadata             = "INCLUDE_ALL_METADATA"
    filter_expr          = "inIpRange(connection.src_ip, '10.0.0.0/8') && inIpRange(connection.dest_ip, '10.0.0.0/8')"
  }

  lifecycle {
    prevent_destroy = true
  }
}
