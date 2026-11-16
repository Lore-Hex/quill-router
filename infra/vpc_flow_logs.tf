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
# Covered: the default network's subnet in every region that has an address
# in use. Four hold hosts (the fast-path nodes, ClickHouse and the enclave
# groups; counted 2026-11-16: us-central1 9, us-east4 2, europe-west4 2,
# us-west1 2). The control plane's Cloud Run services send their internal
# traffic from the same subnets (Direct VPC egress), and in southamerica-east1
# they are the subnet's only users. The other auto-created subnets have no
# address in use.
#
# The filter keeps a record only when both ends are internal addresses. The
# gateways' public traffic, which is nearly all of their traffic, the load
# balancer's health checks and calls to Google APIs all have one end outside
# 10.0.0.0/8 and are left out. What is left still grows with requests where
# the control plane opens a connection per call to ClickHouse (its calls to
# the fast-path nodes reuse connections). Estimated from packet counts on
# 2026-11-16: 0.1 to 1 GB of records a day, against about 4 to 5 GB a day of
# ingestion in all and the 8 GB a day ingestion alert
# (scripts/deploy/gcp_security_baseline_check.sh). Measure a day after the
# apply; if it is material, lower flow_sampling here.
#
# A missing record does not prove there was no connection. flow_sampling 1
# writes every record Google produces, but Google samples packets before
# that at a rate that cannot be set, and a connection of a few packets can
# leave no record. Packets a firewall rule denies on the way in are not
# sampled into these records. Google documents separate records for dropped
# traffic (disposition DROPPED); whether a subnet's log_config produces them
# is not established, so look for them after the apply before relying on
# either answer.
#
# These subnets were created by the network's auto mode, so they are
# imported. The addresses are the ranges auto mode assigned; a change to a
# range, a name or a region would replace the subnet under running hosts,
# which prevent_destroy refuses at plan time. Declare nothing else here: an
# argument the subnet does not already have can force the same replacement,
# and scripts/deploy/_private_run_ingress.sh owns private_ip_google_access.
# To stop managing all of them, use a `removed` block (destroy = false). A
# `removed` block cannot name one region of the map: to drop one, an operator
# runs `terraform state rm` for that instance and then deletes its map entry.
# Deleting an entry alone plans a destroy, which prevent_destroy fails.
# deletion_policy ABANDON keeps a mistaken removal from deleting the subnet
# itself, once an apply has written it to state: if the first apply fails,
# fix forward and do not revert this file.

locals {
  flow_log_subnets = {
    "us-central1"        = "10.128.0.0/20"
    "us-east4"           = "10.150.0.0/20"
    "europe-west4"       = "10.164.0.0/20"
    "us-west1"           = "10.138.0.0/20"
    "southamerica-east1" = "10.158.0.0/20"
  }
}

import {
  for_each = local.flow_log_subnets
  to       = google_compute_subnetwork.default[each.key]
  id       = "projects/${local.gcp_project_id}/regions/${each.key}/subnetworks/default"
}

resource "google_compute_subnetwork" "default" {
  for_each = local.flow_log_subnets

  project         = local.gcp_project_id
  name            = "default"
  region          = each.key
  network         = "default"
  ip_cidr_range   = each.value
  deletion_policy = "ABANDON"

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
