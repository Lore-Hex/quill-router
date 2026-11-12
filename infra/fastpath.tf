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
  # 7f392e79d: main after #2061 (G13), with #2065's hold split: the decide
  # shadow keeps requests carrying a cache affinity key, composes the cold
  # order and measures Python's answer as affinity_cold or affinity_warm,
  # so the soak compares the cohort instead of passing nearly all of it.
  # 892bd7593: main after #2098, with G14's v3 routing snapshot (privacy
  # tiers and parameter tables, so the evaluator answers min_privacy and
  # requested_parameters), G15a's optional async_settle and billing_valid,
  # and the auditor's call deadlines and stall watchdog.
  # 620e0ca00: main after #2118, with G15b's async compose (the edge
  # renders async-v1's additions), option A (the edge decides requests
  # carrying a cache affinity key in the cold order; Python's warm re-rank
  # counts as differ.affinity_warm) and G15c's async admission, dark while
  # edge_decide is false, with the shadow's per-population async counts.
  # fc1ed83bb: the merge of #2130, a deadline on each of the process's
  # startup calls (its ring join and first membership scan among them) and
  # a watchdog on its start and the session's warming that marks the node
  # unhealthy and exits, so systemd restarts it.
  # 0f0b1829a: the merge of #2133, the start before the service runs
  # bounded too: a deadline on each secret read, and a watch that exits the
  # process if it has not reached the service within 15 s per secret read
  # plus a minute.
  # cae6c5479: main after streams increments 5+6 (#2226) and the in-flight
  # 429 shadow fix (#2229): the edge tells the owner whether a decided
  # request streams, composes eligible streams, and decides them only with
  # fastpath_edge_streams on; the shadow counts Python's in-flight 429 to
  # an inflight.key pass as passed. The pilot leases streams. The edge
  # switch, off when this release shipped, is on since gate E (2026-11-02,
  # TR_FASTPATH_EDGE_STREAMS=true in scripts/deploy/rollout.sh, #2252):
  # the edge decides eligible streams for the workspaces whose decide rows
  # are on.
  # 328a3651c: the auditor's pending sweep reads a pack's staged records
  # in one call and works up to eight leases' packs at once, each lease's
  # in order (#2298); at 1,000 a second the packs took about 25 minutes to
  # clear one at a time.
  # 04b39913b: that sweep raised the billing store's CPU from 33% to 47% at
  # 1,000 a second, so the auditor remembers the leases it has found, sweeps
  # retired staged records once a minute, and runs two lease workers (#2303).
  # f1208a3d4: the stager groups the records that arrive while one staging
  # transaction runs into the next one, up to 256 records or 8 MiB, instead
  # of committing once a record (#2318).
  # 09c6a9398: a stream's hold is capped like any catalog request's, matching
  # Python (#2337; streams above the cap leave the cohort by the stream.hold
  # tripwire); every pass-through is timed, logging "fastpath edge pass" and
  # "pass slow" lines (#2331, #2344); a lone slow body read raises no
  # slow-pass alert (#2350); the money flags -required-tier, -allowance and
  # -floor, unset so defaults hold (#2354); the owner keeps an admission
  # ceiling nothing reads yet (#2347); records carry a stream's ceiling, with
  # RecordCeiling off, which needs tr_lease_hold.ceiling, live since
  # 2026-11-06 18:15Z (#2355); the fleet audit, off by default (#2356).
  # 5c5f11fad: a declared-length request body ends at its length, so the
  # transport's last read no longer cuts Python's answer short (#2358); a
  # workspace whose decide row is on but that the node's switch copy leaves
  # off passes at workspace.not_enabled, before the in-flight gate, where it
  # passed at owner.off; Go reads the policy's "all", dark until a policy
  # carries it, and the wildcard decide row, dark until one is written; the
  # decide line gains decided_via= and the shadow line distinct_keys= (#2359);
  # the shadow counts Python's in-flight 429 to any pass but inflight.key as
  # inflight_unchecked.<pass>, never paged (#2367: the 2026-11-06 03:55Z,
  # 18:27Z and 19:27Z edge_would_admit_python_refused pages).
  # 8af87998a: every stop writes the workspace's hold, and disable-all the
  # fleet hold, in the transaction that turns it off, which needs
  # tr_fastpath_workspace_hold and tr_fastpath_kill, live since 2026-11-07 06:35Z;
  # fastpathctl gains release, release-all, release-stamped, hold-backfill,
  # the enabler's pause, resume and arm, and the hold listing (holds);
  # nothing reads a hold to turn a workspace on yet (#2385, widening PR 4,
  # dark); RecordCeiling is on, so a stream admitted with a ceiling runs
  # past its hold to it and its records carry
  # ceil, and no gateway sends a ceiling until fastpath_stream_ceiling moves,
  # so the records match the release before (#2383, stream ceiling PR 3;
  # every node ran 5c5f11fad, which reads ceil, before this pin).
  # bd4398c21: the edge reads the policy's optional fastpath_stream_ceiling
  # member, refusing "false"; with it, a stream whose hold is below its
  # estimate is decided and admitted with that estimate as its ceiling
  # instead of passing at stream.hold (#2406, stream ceiling PR 4). Python
  # publishes the member only with TR_FASTPATH_STREAM_CEILING true, false
  # in rollout.sh, so the policy matches the release before; 8af87998a
  # refuses a policy carrying the member, so this pin comes first.
  # 51e8fabc1: model/provider/workload ranking with stable speed bands and
  # a ZDR preference (#2436). The reader requires ranking.model_workload;
  # upgrade both nodes before relying on the new routing order in the edge.
  # d23591a1f: main with edge tickets (#2506, #2510): the edge asks Python
  # for an async-v1 settlement ticket only for a workspace the policy's
  # async_settle.edge_tickets member names, which Python publishes only with
  # TR_FASTPATH_EDGE_ASYNC_TICKET_WORKSPACES set, unset in rollout.sh; so the
  # nodes answer as the release before until that switch moves. It also
  # carries every provider's async rules (#2451) and all-mode admission by
  # request (#2466), both already live on the Python side.
  fastpath_release        = "d23591a1ff23505fc2700d244d3d52996b6aefd1"
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
  # through nodes added under new names: 9 and 10 beside 7 and 8, which
  # then leave the backend and are removed. On since the I3 soak was green;
  # with every decide row off it still answers nothing.
  fastpath_edge_snapshot = tomap({
    fastpath-edge-snapshot-url        = "${local.fastpath_edge_upstream}/internal/fastpath/routing-snapshot"
    fastpath-edge-snapshot-public-key = "oTQF4Efsi6G7_38Nca4HjiB7QUoNDkNSzaOw26Kt9IM"
    fastpath-edge-snapshot-kid        = "tr-routing-snapshot-2026-10a"
    fastpath-edge-snapshot-plane      = "gcp/${local.fastpath_region}"
    fastpath-edge-shadow              = "true"
    fastpath-edge-policy-url          = "${local.fastpath_edge_upstream}/internal/fastpath/edge-policy"
    fastpath-edge-decide-shadow       = "true"
    fastpath-edge-decide              = "true"
  })
  # The fleet audit (docs/design/fast-admission-widening.md section 7): true
  # gives every node fastpath-fleet-audit "true", so whichever node owns the
  # ring key fleet-audit audits every workspace the switch names, a pass an
  # hour, and turns on the policy on its summary line's absence
  # (infra/fastpath_alerts.tf). Off until its activation: a node from before
  # the startup script that reads the attribute passes nothing, so the
  # script reaches the fleet first through nodes added under new names, as
  # the decide attribute's did, and the pin must take -fleet-audit
  # (docs/runbooks/fastpath-deploy.md, "A flag the unit passes"). On for
  # WS0's gate (widening section 11 item 2, "before WS0's gate is read"):
  # nodes 13 and 14 run the reading script, bd4398c21 takes -fleet-audit, and
  # each node takes the flag by the runbook's unit rewrite, not a reset.
  fastpath_fleet_audit = true
  # The money settings (docs/design/fast-admission-widening.md (b), WS0 of
  # section 8.2): a lease needs effective trust tier 3, a workspace's
  # allowance is $1,000 and a lease leaves at least $20 of the balance, in
  # microdollars. The startup script passes each as -required-tier,
  # -allowance and -floor (8af87998a and later take all three). A reset
  # alone keeps the unit's old arguments: each node takes them one at a
  # time, the other serving, by runbook fastpath-deploy.md's money settings
  # step 5 (leaving, its leases and the balancer drained, the unit stopped,
  # the startup script run again, the process's arguments read back), and
  # WS0's gate counts from the last node's restart. Every pilot workspace
  # passed fastpathctl ready at these values before this change but the
  # customer's, whose switch is off, so it takes today's path and cannot be
  # refused at the tier. Step 4: over the leases kept (seven days), the
  # largest lease under $4 and the lease spend far under a quarter of the
  # allowance; a lease tops up at most $10. Rollback: remove the three
  # and take each node the same way, which restores the release's defaults.
  fastpath_required_tier = "3"
  fastpath_allowance     = "1000000000"
  fastpath_floor         = "20000000"
  # The lower tiers' allowances (docs/design/fast-admission-widening.md
  # (b)), in microdollars: "0" is none, so a tier-2 or tier-1 workspace takes
  # no lease whatever the required tier, and the startup script passes no
  # -allowance-tier2 or -allowance-tier1 for "0", so the pinned release need
  # not take them yet. Dark: with the required tier at 3 neither is read. A
  # later change sets them, each at most the tier above's, re-pins to a
  # release that takes the flags, and only then lowers
  # fastpath_required_tier, and takes each node by the money settings step
  # of docs/runbooks/fastpath-deploy.md.
  fastpath_allowance_tier2 = "0"
  fastpath_allowance_tier1 = "0"
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
  # which the script 5 and 6 ran, from before I1, read neither of. 9 and 10
  # took the places of 7 and 8 the same way, with the decide attribute
  # (stage I2, "false" until the I2 switch), which the script 7 and 8 ran,
  # from before I2, did not read; 7 and 8 were drained, left the edge
  # backend one apply before their removal, then were removed.
  #
  # 11 and 12 took the places of 9 and 10 the same way, on four cores rather
  # than two: at 1,000 generations a second (2026-11-04, P1's runs on
  # tr-fastpath-loadgen) the two-core nodes reached 77% CPU with the
  # authorize p99 at 64 ms; on four cores five minutes at that rate held,
  # the p99 at 27 ms and every audit clean. The same script, release and
  # metadata; 9 and 10 were drained, left the edge backend one apply before
  # their removal, then were removed.
  #
  # 13 and 14 took the places of 11 and 12 the same way, for the money
  # settings (#2354): the script 11 and 12 ran, from before it, passed none
  # of -required-tier, -allowance or -floor from their attributes. The same
  # release and metadata; 11 and 12 were drained, left the edge backend one
  # apply before their removal, then were removed.
  for_each = {
    "tr-fastpath-13" = { zone = "us-central1-a", machine_type = "e2-standard-4" }
    "tr-fastpath-14" = { zone = "us-central1-b", machine_type = "e2-standard-4" }
  }

  project      = local.gcp_project_id
  name         = each.key
  zone         = each.value.zone
  machine_type = each.value.machine_type

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
    fastpath-fleet-audit       = local.fastpath_fleet_audit ? "true" : "false"
    fastpath-required-tier     = local.fastpath_required_tier
    fastpath-allowance         = local.fastpath_allowance
    fastpath-floor             = local.fastpath_floor
    fastpath-allowance-tier2   = local.fastpath_allowance_tier2
    fastpath-allowance-tier1   = local.fastpath_allowance_tier1
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

# --- The load generator ------------------------------------------------------

# One machine of its own for the edge's 1,000 a second run, so the run does
# not measure a generator beside the front door and owner it drives (P1's
# results, "Next"). It runs no node unit and joins no membership: its script
# only installs the pinned release's loadgen, fastpathwatch and load mix, and
# the run is started by hand over OS Login (docs/runbooks/fastpath-p1.md).
# It takes the nodes' service account for the release bucket and the
# readiness read loadgen makes, and the default-allow-internal rule for the
# front doors' port. Off is local.fastpath_loadgen = false, which removes it.
locals {
  fastpath_loadgen = true
}

resource "google_compute_instance" "fastpath_loadgen" {
  count = local.fastpath_loadgen ? 1 : 0

  project      = local.gcp_project_id
  name         = "tr-fastpath-loadgen"
  zone         = "us-central1-c"
  machine_type = "e2-standard-8"

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

  metadata = {
    enable-oslogin   = "TRUE"
    fastpath-release = local.fastpath_release
  }

  metadata_startup_script = <<-EOT
    #!/bin/bash
    set -euo pipefail
    release="$(curl -sf -H 'Metadata-Flavor: Google' 'http://metadata.google.internal/computeMetadata/v1/instance/attributes/fastpath-release')"
    install -d /opt/fastpath
    for tool in loadgen fastpathwatch load-mix.json; do
      gcloud storage cp "gs://${google_storage_bucket.fastpath_releases.name}/fastpath/$release/$tool" "/opt/fastpath/$tool"
      chmod 0755 "/opt/fastpath/$tool"
    done
    echo "$release" > /opt/fastpath/release
  EOT

  labels = {
    service = "fastpath"
    stage   = "loadgen"
  }

  allow_stopping_for_update = true

  depends_on = [
    google_storage_bucket_iam_member.fastpath_releases_reader,
  ]
}
