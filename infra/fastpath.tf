# The fast admission path's production install, stage P0 of
# docs/design/fast-admission-production-rollout.md: the settle log and the
# record topic, the auditor's subscriptions, an archive of each topic, the
# nodes' identity and the envelope key's secret, and two nodes in
# us-central1 with no external address, the workspace allow-list empty, so
# nothing admits until W1's switch names a workspace. The schema is the
# deploy workflow's (scripts/deploy/migrate_fastpath.sh), not Terraform's.
#
# The nodes run the release named below: a commit on main whose CI passed,
# built and uploaded by .github/workflows/fastpath-release.yml. Changing it
# changes the instances' metadata only; a node picks it up when replaced or
# restarted, per docs/runbooks/fastpath-deploy.md.
locals {
  fastpath_region = "us-central1"
  # A main commit whose CI passed and whose binaries fastpath-release.yml
  # uploaded; a node picks a new one up when reset or replaced
  # (docs/runbooks/fastpath-p0.md). bfa94e22e: the merge of #1728, P1's run,
  # the first release carrying loadgen, fastpathwatch and load-mix.json.
  # 6709d268e: main after the edge's health answer (#1875); the first
  # release a node with the edge attributes may run. 04c5ad769: the merge
  # of #1970, the routing snapshot compiled as of its window's start, on
  # #1969's edge flags (-edge-snapshot-url, -public-key, -kid, -plane and
  # -edge-shadow); the first release a node with the snapshot attributes
  # below may run. Its fastpath/ tree is main's as of f12f8967d.
  # 2d90fa113: main after #2040, with G10's decide pipeline and shadow
  # (#2027/#2028), G11's decide path (#2030, off by flag), the reservation
  # commit timestamp (#2038) and G12's per-period counts (#2039); it takes
  # -edge-policy-url and -edge-decide-shadow, which the nodes' policy
  # attributes below pass (stage I1).
  fastpath_release        = "2d90fa113678233b9edcdf9d0ea833ace37b6c94"
  fastpath_release_bucket = "tr-fastpath-releases-${local.gcp_project_id}"
  fastpath_database       = "projects/${local.gcp_project_id}/instances/trusted-router-nam6/databases/trusted-router"
  fastpath_pubsub_agent   = "serviceAccount:service-${local.gcp_project_number}@gcp-sa-pubsub.iam.gserviceaccount.com"
  # The edge (E1): its port, the control plane's internal surface in the
  # nodes' region (the Cloud Run service behind /internal; its internal
  # ingress admits a VM of this project's VPC, Cloud NAT egress included)
  # and the internal token Python holds, a version pinned by its number
  # (the node reads secrets that way only; Python reads the latest). The
  # token is the enclaves' credential, held in their running clients, so
  # its rotation is the control plane's procedure, not this file's: this
  # pin moves to a new version only once the callers carry it, and each
  # node is then drained and restarted in turn with its running argument
  # checked (docs/runbooks/fastpath-deploy.md). Set on the nodes'
  # metadata, read when a node's startup script runs.
  fastpath_edge_port         = 8081
  fastpath_edge_upstream     = "https://trusted-router-vsjf6qu4la-uc.a.run.app"
  fastpath_edge_token_secret = "projects/${local.gcp_project_id}/secrets/trustedrouter-internal-gateway-token/versions/1"
  # The edge's routing snapshot and its shadow comparator (E2, docs/design/
  # routing-snapshot-v1.md): every node polls Python's published snapshot
  # on the edge's own upstream (the startup script refuses any other
  # origin), verifies its manifest with version 1 of
  # trustedrouter-routing-snapshot-signing-key's public half, under its kid
  # and plane, and, shadow on, compares Go's verdict with Python's on each
  # authorize the edge passes on, logging counts alone: Python still
  # decides. A node reads these when its startup script runs, and only a
  # script from #1969 on reads them; every node runs one, 5 and 6 having
  # been added under new names for it (docs/runbooks/fastpath-deploy.md,
  # "Turning the edge on"). Until publishing is on
  # (docs/runbooks/routing-snapshot-key.md) the poll answers 404 and the
  # edge keeps polling, admission unaffected.
  # The edge policy (stage I1, docs/design/fast-admission-e2-decide.md
  # section 4): Python's signed live facts, polled on the same upstream and
  # verified with the same key, kid and plane. Only a script from I1 on
  # reads the policy's two attributes, which nodes 7 and 8 run.
  # The decide shadow (G10) is on, once the policy had been held through a
  # measured soak (the I1 runbook); each node in turn took it drained, its
  # unit stopped and its startup script run again (a reset alone keeps the
  # unit's old arguments). Python still decides.
  # The decide path (G11, stage I2): fastpath-edge-decide "true" has the
  # edge answer the authorizes of the workspaces the decide table names
  # (docs/runbooks/fastpath-edge-decide.md); "false", the default, answers
  # nothing. It is a flag the unit takes, so a node takes a change to it as
  # it took the shadow's: drained, its unit stopped and its startup script
  # run again, one node at a time. Only a script that reads the attribute
  # passes the flag: a node from before that script carries the attribute
  # and passes nothing, so the script reaches the fleet first, as I1's did,
  # through nodes added under new names.
  fastpath_edge_snapshot = tomap({
    fastpath-edge-snapshot-url        = "${local.fastpath_edge_upstream}/internal/fastpath/routing-snapshot"
    fastpath-edge-snapshot-public-key = "oTQF4Efsi6G7_38Nca4HjiB7QUoNDkNSzaOw26Kt9IM"
    fastpath-edge-snapshot-kid        = "tr-routing-snapshot-2026-10a"
    fastpath-edge-snapshot-plane      = "gcp/${local.fastpath_region}"
    fastpath-edge-shadow              = "true"
    fastpath-edge-policy-url          = "${local.fastpath_edge_upstream}/internal/fastpath/edge-policy"
    fastpath-edge-decide-shadow       = "true"
    fastpath-edge-decide              = "false"
  })
}

# --- The topics and the auditor's subscriptions ---------------------------

# The settle log: an owner's records for a lease, in order on the lease's
# ordering key, published through the region's locational endpoint, so the
# topic stores its messages in that region.
resource "google_pubsub_topic" "fastpath_settle_log" {
  project                    = local.gcp_project_id
  name                       = "settle-log"
  message_retention_duration = "604800s"

  message_storage_policy {
    allowed_persistence_regions = [local.fastpath_region]
  }
}

# The record topic: each request's full record and each winner's outcome,
# unordered.
resource "google_pubsub_topic" "fastpath_records" {
  project                    = local.gcp_project_id
  name                       = "records"
  message_retention_duration = "604800s"

  message_storage_policy {
    allowed_persistence_regions = [local.fastpath_region]
  }
}

# The auditor's ordered subscription to the settle log. The ack deadline is
# the one the service's tests give it; the client extends it while a
# message is in hand.
resource "google_pubsub_subscription" "fastpath_auditor" {
  project                    = local.gcp_project_id
  name                       = "auditor"
  topic                      = google_pubsub_topic.fastpath_settle_log.id
  enable_message_ordering    = true
  ack_deadline_seconds       = 10
  message_retention_duration = "604800s"

  expiration_policy {
    ttl = "" # never expires for want of subscribers
  }
}

# The record stager's subscription to the record topic.
resource "google_pubsub_subscription" "fastpath_stager" {
  project                    = local.gcp_project_id
  name                       = "stager"
  topic                      = google_pubsub_topic.fastpath_records.id
  ack_deadline_seconds       = 10
  message_retention_duration = "604800s"

  expiration_policy {
    ttl = ""
  }
}

# --- The archives (W7): every message of each topic, with its attributes
# --- and metadata, kept; the only way back for a lease stopped at a gap.

resource "google_storage_bucket" "fastpath_archive" {
  for_each = toset(["settle-log", "records"])

  project                     = local.gcp_project_id
  name                        = "tr-fastpath-archive-${each.key}-${local.gcp_project_id}"
  location                    = upper(local.fastpath_region)
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  lifecycle {
    prevent_destroy = true
  }
}

# Pub/Sub's service agent writes the archives.
resource "google_storage_bucket_iam_member" "fastpath_archive_writer" {
  for_each = google_storage_bucket.fastpath_archive

  bucket = each.value.name
  role   = "roles/storage.admin"
  member = local.fastpath_pubsub_agent
}

resource "google_pubsub_subscription" "fastpath_archive" {
  for_each = {
    "settle-log" = google_pubsub_topic.fastpath_settle_log.id
    "records"    = google_pubsub_topic.fastpath_records.id
  }

  project = local.gcp_project_id
  name    = "${each.key}-archive"
  topic   = each.value
  # A Cloud Storage subscription's max_duration (300 s) must not exceed its
  # deadline, and the live subscriptions report 300: so says this, and a
  # plan is clean.
  ack_deadline_seconds       = 300
  message_retention_duration = "604800s"

  expiration_policy {
    ttl = ""
  }

  cloud_storage_config {
    bucket          = google_storage_bucket.fastpath_archive[each.key].name
    filename_prefix = "${each.key}/"
    max_duration    = "300s"
    max_bytes       = 100000000

    avro_config {
      write_metadata = true
    }
  }

  depends_on = [google_storage_bucket_iam_member.fastpath_archive_writer]
}

# --- The nodes' identity, the key's secret, and the releases ---------------

resource "google_service_account" "fastpath_node" {
  project      = local.gcp_project_id
  account_id   = "tr-fastpath-node"
  display_name = "TrustedRouter fast admission node"
}

# The envelope key: a version is added by hand, pinned by number in the
# nodes' -key-secret (docs/runbooks/fastpath-key-rotation.md).
resource "google_secret_manager_secret" "fastpath_envelope_key" {
  project   = local.gcp_project_id
  secret_id = "fastpath-envelope-key"

  replication {
    auto {}
  }
}

# The internal token the edge checks, the same secret the control plane
# holds (scripts/deploy/secrets.sh creates it; it is not managed here).
resource "google_secret_manager_secret_iam_member" "fastpath_node_gateway_token" {
  project   = local.gcp_project_id
  secret_id = "trustedrouter-internal-gateway-token"
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.fastpath_node.member
}

resource "google_secret_manager_secret_iam_member" "fastpath_node_key" {
  project   = local.gcp_project_id
  secret_id = google_secret_manager_secret.fastpath_envelope_key.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.fastpath_node.member
}

resource "google_pubsub_topic_iam_member" "fastpath_node_publisher" {
  for_each = {
    "settle-log" = google_pubsub_topic.fastpath_settle_log.name
    "records"    = google_pubsub_topic.fastpath_records.name
  }

  project = local.gcp_project_id
  topic   = each.value
  role    = "roles/pubsub.publisher"
  member  = google_service_account.fastpath_node.member
}

resource "google_pubsub_subscription_iam_member" "fastpath_node_subscriber" {
  for_each = {
    "auditor" = google_pubsub_subscription.fastpath_auditor.name
    "stager"  = google_pubsub_subscription.fastpath_stager.name
  }

  project      = local.gcp_project_id
  subscription = each.value
  role         = "roles/pubsub.subscriber"
  member       = google_service_account.fastpath_node.member
}

resource "google_spanner_database_iam_member" "fastpath_node_database" {
  project  = local.gcp_project_id
  instance = google_spanner_instance.trusted_router.name
  database = "trusted-router"
  role     = "roles/spanner.databaseUser"
  member   = google_service_account.fastpath_node.member
}

# The nodes' project-level roles, roles/logging.logWriter and
# roles/monitoring.metricWriter, are granted by hand (docs/runbooks/
# fastpath-p0.md): the deploy identity that applies this does not edit the
# project's IAM policy, by design.

# The releases: fastpath-release.yml uploads each CI-passed main commit's
# binaries under fastpath/<commit>/; the nodes read the one named above.
resource "google_storage_bucket" "fastpath_releases" {
  project                     = local.gcp_project_id
  name                        = local.fastpath_release_bucket
  location                    = upper(local.fastpath_region)
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
}

resource "google_storage_bucket_iam_member" "fastpath_releases_writer" {
  bucket = google_storage_bucket.fastpath_releases.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:tr-deploy@${local.gcp_project_id}.iam.gserviceaccount.com"
}

resource "google_storage_bucket_iam_member" "fastpath_releases_reader" {
  bucket = google_storage_bucket.fastpath_releases.name
  role   = "roles/storage.objectViewer"
  member = google_service_account.fastpath_node.member
}

# --- The nodes ---------------------------------------------------------------

# A node in each of two zones, on the default network's us-central1 subnet
# behind the project's NAT (tr-nat), no external address; the network's
# default-allow-internal rule lets the fleet reach port 8080. A node is
# replaced, never restarted in place, once it holds leases
# (docs/runbooks/fastpath-deploy.md); at P0 it holds none. Nothing counts
# the nodes: front doors find owners and peers in the membership table,
# auditor members share the subscriptions, and the edge's firewall rule
# names the nodes' service account.
resource "google_compute_instance" "fastpath_node" {
  # 3 and 4 took the places of 1 and 2 on 2026-10-20, with the edge on
  # (docs/runbooks/fastpath-deploy.md, "Turning the edge on"); 5 and 6 took
  # the places of 3 and 4 the same way, with the routing snapshot and the
  # shadow on (local.fastpath_edge_snapshot), which the script 3 and 4 ran,
  # from before #1969, read neither of. Each replaced pair was marked
  # leaving and drained, left the edge backend (local.fastpath_edge_retiring)
  # one apply before its removal, then was removed. 7 and 8 took the places
  # of 5 and 6 the same way, with the edge policy's attributes (stage I1),
  # which the script 5 and 6 ran, from before I1, read neither of.
  for_each = {
    "tr-fastpath-7" = "us-central1-a"
    "tr-fastpath-8" = "us-central1-b"
  }

  project      = local.gcp_project_id
  name         = each.key
  zone         = each.value
  machine_type = "e2-standard-2"

  boot_disk {
    initialize_params {
      image = "debian-cloud/debian-12"
      size  = 20
    }
  }

  network_interface {
    network    = "default"
    subnetwork = "default"
    # No access_config: no external address.
  }

  service_account {
    email  = google_service_account.fastpath_node.email
    scopes = ["cloud-platform"]
  }

  metadata = merge({
    enable-oslogin             = "TRUE"
    fastpath-release           = local.fastpath_release
    fastpath-edge-upstream     = local.fastpath_edge_upstream
    fastpath-edge-token-secret = local.fastpath_edge_token_secret
  }, local.fastpath_edge_snapshot)

  metadata_startup_script = templatefile("${path.module}/fastpath_node_startup.sh.tftpl", {
    release_bucket = google_storage_bucket.fastpath_releases.name
    region         = local.fastpath_region
    database       = local.fastpath_database
    project        = local.gcp_project_id
    key_secret     = "projects/${local.gcp_project_id}/secrets/${google_secret_manager_secret.fastpath_envelope_key.secret_id}/versions/1"
    edge_port      = local.fastpath_edge_port
  })

  labels = {
    service = "fastpath"
    stage   = "p0"
  }

  allow_stopping_for_update = true

  lifecycle {
    # A node is replaced, never restarted in place, and the new one starts
    # first (docs/runbooks/fastpath-deploy.md): a startup-script change
    # reaches the fleet through nodes added under new names and the old
    # ones drained and removed, not through a same-name replacement that
    # takes both down at once. A node keeps the script it was created
    # with; the pin, the edge's attributes and the snapshot's are metadata,
    # applied in place, read when the script next runs.
    # The cost: a script change is not drift here, so a node created
    # before it runs the old script while the plan is clean and its
    # binary matches the pin; which script a node runs is read from the
    # instance (docs/runbooks/fastpath-deploy.md, "Turning the edge on").
    ignore_changes = [metadata_startup_script]
  }

  depends_on = [
    google_spanner_database_iam_member.fastpath_node_database,
    google_pubsub_subscription_iam_member.fastpath_node_subscriber,
    google_storage_bucket_iam_member.fastpath_releases_reader,
  ]
}
