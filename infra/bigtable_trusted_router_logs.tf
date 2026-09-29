# Bigtable retirement, step 4c. The instance was created by scripts/deploy/infra.sh
# (2026-05-02) and never managed here. It is imported first so that Terraform,
# not a CLI one-off, performs the destroy: this file plans to "1 to import,
# 0 to change"; the follow-up change deletes the resource block and plans to
# "1 to destroy". Both tables it still holds belong to the switched-off ledger
# pilots (regional quota, spend lease); the analytics table's final export is
# gs://quill-cloud-proxy-tr-clickhouse-archive/bigtable-final-export/
# trustedrouter-generations/2026-09-29/ (manifest.json verified 2026-09-29).
import {
  to = google_bigtable_instance.trusted_router_logs
  id = "projects/quill-cloud-proxy/instances/trusted-router-logs"
}

resource "google_bigtable_instance" "trusted_router_logs" {
  project             = "quill-cloud-proxy"
  name                = "trusted-router-logs"
  display_name        = "TrustedRouter logs"
  # Provider-side guard only (no API field): the follow-up change that removes
  # this block is the destroy, and it must plan as exactly "1 to destroy".
  deletion_protection = false

  cluster {
    cluster_id   = "trusted-router-logs-c1"
    zone         = "us-central1-a"
    num_nodes    = 1
    storage_type = "SSD"
  }

  cluster {
    cluster_id   = "trusted-router-logs-eu"
    zone         = "europe-west4-a"
    num_nodes    = 1
    storage_type = "SSD"
  }
}
