# The ClickHouse node service account's database-wide binding, from before the
# nodes signed in as tr_clickhouse_node (infra/clickhouse_node_role.tf). It was
# granted by scripts/deploy/clickhouse_cluster.sh, outside Terraform, and is
# adopted here unchanged so that removing it (step 4 in
# docs/clickhouse-reliability.md) is a Terraform change. The next change deletes
# this file and the script's grant. Every node program that opens Spanner signs
# in as tr_clickhouse_node since the step 3 node deploy (2026-11-12).
import {
  to = google_spanner_database_iam_member.clickhouse_node_database_user
  id = "quill-cloud-proxy/trusted-router-nam6/trusted-router roles/spanner.databaseUser serviceAccount:tr-clickhouse@quill-cloud-proxy.iam.gserviceaccount.com"
}

resource "google_spanner_database_iam_member" "clickhouse_node_database_user" {
  project  = local.gcp_project_id
  instance = google_spanner_instance.trusted_router.name
  database = "trusted-router"
  role     = "roles/spanner.databaseUser"
  member   = local.clickhouse_node_member
}
