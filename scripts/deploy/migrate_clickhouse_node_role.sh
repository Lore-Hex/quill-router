#!/usr/bin/env bash
# Give the ClickHouse node programs a table-scoped database role.
#
# The node programs (clickhouse/ingest_outbox.py, ingest_operational_outbox.py,
# refresh_workspace_directory.py, verify_spanner_delivery.py and
# rollup_reservation_overruns.py) need six objects: the two analytics outboxes
# (read and delete), the drain heartbeat (write), tr_generation and
# tr_reservation (read), and the workspace directory. The directory comes from
# tr_entities, which the role must not reach, so a definer-rights view exposes
# only the six workspace fields the refresh projects. The role is used once the
# nodes set TR_SPANNER_DATABASE_ROLE=tr_clickhouse_node and the node service
# account holds the fine-grained-access IAM bindings (infra/clickhouse_node_role.tf).
#
# Additive and idempotent. The view, the role and each single privilege are
# checked in INFORMATION_SCHEMA first and applied only when missing, so a run
# that stopped partway is completed by the next one. A check that cannot read
# the schema fails the run instead of reading as absent. An existing view is
# kept as it is; change its definition with a new migration.
set -euo pipefail

PROJECT="${GCP_PROJECT_ID:-quill-cloud-proxy}"
INSTANCE="${SPANNER_INSTANCE_ID:-trusted-router-nam6}"
DATABASE="${SPANNER_DATABASE_ID:-trusted-router}"

count() {
  gcloud spanner databases execute-sql "$DATABASE" \
    --project="$PROJECT" \
    --instance="$INSTANCE" \
    --sql="$1" \
    --format='value(rows[0])'
}

views=$(count "SELECT COUNT(*) FROM INFORMATION_SCHEMA.VIEWS
               WHERE table_schema='' AND table_name='tr_workspace_directory_source'") || exit $?
if [ "${views:-0}" = "0" ]; then
  gcloud spanner databases ddl update "$DATABASE" \
    --project="$PROJECT" \
    --instance="$INSTANCE" \
    --ddl="CREATE VIEW tr_workspace_directory_source SQL SECURITY DEFINER AS
    SELECT e.id AS id,
      JSON_QUERY(e.body, '\$.id') AS workspace_id,
      JSON_QUERY(e.body, '\$.name') AS name,
      JSON_QUERY(e.body, '\$.deleted') AS deleted,
      JSON_QUERY(e.body, '\$.owner_user_id') AS owner_user_id,
      JSON_QUERY(e.body, '\$.federated_home') AS federated_home,
      JSON_QUERY(e.body, '\$.created_at') AS created_at
    FROM tr_entities AS e
    WHERE e.kind = 'workspace'"
  echo "created tr_workspace_directory_source"
else
  echo "tr_workspace_directory_source exists"
fi

roles=$(count "SELECT COUNT(*) FROM INFORMATION_SCHEMA.ROLES
               WHERE role_name='tr_clickhouse_node'") || exit $?
if [ "${roles:-0}" = "0" ]; then
  gcloud spanner databases ddl update "$DATABASE" \
    --project="$PROJECT" \
    --instance="$INSTANCE" \
    --ddl="CREATE ROLE tr_clickhouse_node"
  echo "created tr_clickhouse_node"
else
  echo "tr_clickhouse_node exists"
fi

# add_privilege STATEMENT OBJECT PRIVILEGE: apply one single-privilege statement
# unless the role already holds that privilege. ROLE_TABLE_GRANTS lists privileges on tables and views.
add_privilege() {
  local granted
  granted=$(count "SELECT COUNT(*) FROM INFORMATION_SCHEMA.ROLE_TABLE_GRANTS
                   WHERE grantee='tr_clickhouse_node' AND table_schema=''
                   AND table_name='$2' AND privilege_type='$3'") || exit $?
  if [ "${granted:-0}" != "0" ]; then
    return 0
  fi
  gcloud spanner databases ddl update "$DATABASE" \
    --project="$PROJECT" \
    --instance="$INSTANCE" \
    --ddl="$1"
  echo "granted $3 on $2"
}

add_privilege "GRANT SELECT ON TABLE tr_analytics_outbox TO ROLE tr_clickhouse_node" tr_analytics_outbox SELECT
add_privilege "GRANT DELETE ON TABLE tr_analytics_outbox TO ROLE tr_clickhouse_node" tr_analytics_outbox DELETE
add_privilege "GRANT SELECT ON TABLE tr_operational_analytics_outbox TO ROLE tr_clickhouse_node" tr_operational_analytics_outbox SELECT
add_privilege "GRANT DELETE ON TABLE tr_operational_analytics_outbox TO ROLE tr_clickhouse_node" tr_operational_analytics_outbox DELETE
add_privilege "GRANT SELECT ON TABLE tr_operational_outbox_heartbeat TO ROLE tr_clickhouse_node" tr_operational_outbox_heartbeat SELECT
add_privilege "GRANT INSERT ON TABLE tr_operational_outbox_heartbeat TO ROLE tr_clickhouse_node" tr_operational_outbox_heartbeat INSERT
add_privilege "GRANT UPDATE ON TABLE tr_operational_outbox_heartbeat TO ROLE tr_clickhouse_node" tr_operational_outbox_heartbeat UPDATE
add_privilege "GRANT SELECT ON TABLE tr_generation TO ROLE tr_clickhouse_node" tr_generation SELECT
add_privilege "GRANT SELECT ON TABLE tr_reservation TO ROLE tr_clickhouse_node" tr_reservation SELECT
add_privilege "GRANT SELECT ON VIEW tr_workspace_directory_source TO ROLE tr_clickhouse_node" tr_workspace_directory_source SELECT
