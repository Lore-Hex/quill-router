# The ClickHouse nodes' service account (tr-clickhouse, created by
# scripts/deploy/clickhouse_cluster.sh) runs the node programs that drain the
# analytics outboxes and read the generation, reservation and workspace
# directory rows. These two bindings let it sign in as the table-scoped role
# tr_clickhouse_node (scripts/deploy/migrate_clickhouse_node_role.sh) and as
# nothing else. They narrow nothing on their own: the account keeps its
# roles/spanner.databaseUser binding until the nodes have moved to the role.
locals {
  clickhouse_node_member = "serviceAccount:tr-clickhouse@${local.gcp_project_id}.iam.gserviceaccount.com"
  clickhouse_node_role   = "tr_clickhouse_node"
}

resource "google_spanner_database_iam_member" "clickhouse_node_fine_grained" {
  project  = local.gcp_project_id
  instance = google_spanner_instance.trusted_router.name
  database = "trusted-router"
  role     = "roles/spanner.fineGrainedAccessUser"
  member   = local.clickhouse_node_member
}

resource "google_spanner_database_iam_member" "clickhouse_node_role_user" {
  project  = local.gcp_project_id
  instance = google_spanner_instance.trusted_router.name
  database = "trusted-router"
  role     = "roles/spanner.databaseRoleUser"
  member   = local.clickhouse_node_member

  condition {
    title       = "tr_clickhouse_node only"
    description = "The ClickHouse node programs' table-scoped role."
    expression  = "resource.type == \"spanner.googleapis.com/DatabaseRole\" && resource.name.endsWith(\"/databaseRoles/${local.clickhouse_node_role}\")"
  }
}
