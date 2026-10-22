# The routing snapshot's signing key (docs/design/routing-snapshot-v1.md,
# "Publishing"): an Ed25519 private key in PEM that the control plane signs
# each snapshot's manifest with and the fast path's edge verifies by its
# public half. The control plane mounts one version, pinned by its number,
# never latest (scripts/deploy/rollout.sh). Versions are added by hand
# (docs/runbooks/routing-snapshot-key.md), so no key material is in this
# file or in Terraform state; until version 1 exists nothing reads it.
resource "google_secret_manager_secret" "routing_snapshot_signing_key" {
  project   = local.gcp_project_id
  secret_id = "trustedrouter-routing-snapshot-signing-key"

  replication {
    auto {}
  }
}

# One reader: the control plane's runtime identity, the service account the
# trusted-router Cloud Run service runs as. Authoritative for this role on
# this secret, so no other member holds it here; the fast path's nodes never
# do, as they need only the public key.
resource "google_secret_manager_secret_iam_binding" "routing_snapshot_signing_key_reader" {
  project   = local.gcp_project_id
  secret_id = google_secret_manager_secret.routing_snapshot_signing_key.secret_id
  role      = "roles/secretmanager.secretAccessor"
  members   = ["serviceAccount:trusted-router-control-run@${local.gcp_project_id}.iam.gserviceaccount.com"]
}
