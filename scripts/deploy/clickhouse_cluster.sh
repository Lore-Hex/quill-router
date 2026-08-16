#!/usr/bin/env bash
# Build and migrate the TrustedRouter three-replica ClickHouse cluster.
#
# Safety order:
#   1. Provision two new private nodes and a three-voter Keeper quorum.
#   2. Backfill a replicated staging table and compare full fingerprints.
#   3. Stop the durable outbox ingester and replay once more.
#   4. Rename replicas 2/3 first; rename node 1 last in one atomic statement.
#   5. Resume ingestion, then expose only the private internal load balancer.
# The original node-1 table remains as provider_benchmark_samples_local_backup.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

NAMES=(tr-clickhouse-1 tr-clickhouse-2 tr-clickhouse-3)
ZONES=(us-central1-a us-central1-b us-central1-c)
KEEPER_IDS=(1 2 3)
CLUSTER_SERVICE_ACCOUNT_NAME="${TR_CLICKHOUSE_CLUSTER_SA:-tr-clickhouse}"
CLUSTER_SERVICE_ACCOUNT="${CLUSTER_SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
SECRET="${TR_CLICKHOUSE_SECRET:-trustedrouter-clickhouse-password}"
SNAPSHOT_POLICY="${TR_CLICKHOUSE_SNAPSHOT_POLICY:-tr-clickhouse-daily-snapshots}"
MIGRATION_SCHEMA="${SCRIPT_DIR}/../../clickhouse/003_provider_benchmark_replicated.sql"
APPLY=0

if [ "${1:-}" = "--apply" ]; then
  APPLY=1
elif [ $# -ne 0 ]; then
  echo "usage: $0 [--apply]" >&2
  exit 2
fi

if [ "$APPLY" -eq 0 ]; then
  log "dry-run: would provision tr-clickhouse-2/3 in zones b/c with 500 GB disks"
  log "dry-run: would form a three-voter Keeper quorum and verify all replicas"
  log "dry-run: would retain the current local table and cut over only after fingerprint parity"
  exit 0
fi

node_ssh() {
  local index="$1"
  shift
  gc compute ssh "${NAMES[$index]}" \
    --zone="${ZONES[$index]}" \
    --tunnel-through-iap \
    --quiet \
    "$@"
}

node_query() {
  local index="$1"
  local query="$2"
  printf '%s\n' "$query" | node_ssh "$index" --command="sudo sh -c '
    set -eu
    set -a
    . /etc/tr-clickhouse-ingest.env
    set +a
    clickhouse-client --user tr --password \"\$CH_PASSWORD\" --database tr --multiquery
  '"
}

node_scalar() {
  node_query "$1" "$2" | tail -1 | tr -d '\r'
}

ensure_service_account() {
  local attempt
  log "ensuring least-privilege ClickHouse replica identity"
  if ! gc iam service-accounts describe "$CLUSTER_SERVICE_ACCOUNT" >/dev/null 2>&1; then
    gc iam service-accounts create "$CLUSTER_SERVICE_ACCOUNT_NAME" \
      --display-name="TrustedRouter ClickHouse replicas"
  fi
  for attempt in {1..24}; do
    if gc iam service-accounts describe "$CLUSTER_SERVICE_ACCOUNT" >/dev/null 2>&1; then
      break
    fi
    if [ "$attempt" -eq 24 ]; then
      echo "service account did not become visible: ${CLUSTER_SERVICE_ACCOUNT}" >&2
      exit 1
    fi
    sleep 5
  done
  for attempt in {1..12}; do
    if gc secrets add-iam-policy-binding "$SECRET" \
        --member="serviceAccount:${CLUSTER_SERVICE_ACCOUNT}" \
        --role=roles/secretmanager.secretAccessor \
        --quiet >/dev/null 2>&1; then
      break
    fi
    if [ "$attempt" -eq 12 ]; then
      echo "could not grant Secret Manager access to ${CLUSTER_SERVICE_ACCOUNT}" >&2
      exit 1
    fi
    sleep 5
  done
  ensure_project_role "serviceAccount:${CLUSTER_SERVICE_ACCOUNT}" roles/monitoring.metricWriter
  ensure_project_role "serviceAccount:${CLUSTER_SERVICE_ACCOUNT}" roles/logging.logWriter
  # The parity and bounded backfill workers compare ClickHouse with the one
  # retained Bigtable instance. Keep this at the instance boundary: the
  # dedicated VM identity must not inherit project-wide Bigtable access.
  for attempt in {1..12}; do
    if gc bigtable instances add-iam-policy-binding "$BIGTABLE_INSTANCE_ID" \
        --member="serviceAccount:${CLUSTER_SERVICE_ACCOUNT}" \
        --role=roles/bigtable.reader \
        --quiet >/dev/null 2>&1; then
      break
    fi
    if [ "$attempt" -eq 12 ]; then
      echo "could not grant Bigtable read access to ${CLUSTER_SERVICE_ACCOUNT}" >&2
      exit 1
    fi
    sleep 5
  done
  # Node 1 drains the durable Spanner analytics outbox. Keep this grant at the
  # database boundary: changing the VM from the broad default Compute identity
  # must not silently stop ingestion with spanner.sessions.create 403s.
  gc spanner databases add-iam-policy-binding "$SPANNER_DATABASE_ID" \
    --instance="$SPANNER_INSTANCE_ID" \
    --member="serviceAccount:${CLUSTER_SERVICE_ACCOUNT}" \
    --role=roles/spanner.databaseUser \
    --quiet >/dev/null
}

ensure_network() {
  log "ensuring private ClickHouse and Keeper network rules"
  gc compute firewall-rules update tr-clickhouse-internal \
    --allow=tcp:8123,tcp:9000,tcp:9181,tcp:9234 \
    --source-ranges=10.128.0.0/9 \
    --quiet
  if gc compute firewall-rules describe tr-clickhouse-health-check >/dev/null 2>&1; then
    gc compute firewall-rules update tr-clickhouse-health-check \
      --allow=tcp:8123 \
      --source-ranges=35.191.0.0/16,130.211.0.0/22 \
      --quiet
  else
    gc compute firewall-rules create tr-clickhouse-health-check \
      --network=default \
      --allow=tcp:8123 \
      --source-ranges=35.191.0.0/16,130.211.0.0/22 \
      --target-tags=tr-clickhouse \
      --description="GCP health checks for private ClickHouse ILB"
  fi
}

provision_nodes() {
  local index
  for index in 1 2; do
    log "ensuring ${NAMES[$index]} in ${ZONES[$index]}"
    PROJECT="$PROJECT_ID" \
      ZONE="${ZONES[$index]}" \
      NAME="${NAMES[$index]}" \
      DISK_GB=500 \
      SERVICE_ACCOUNT="$CLUSTER_SERVICE_ACCOUNT" \
      SNAPSHOT_POLICY="$SNAPSHOT_POLICY" \
      SECRET="$SECRET" \
      "${SCRIPT_DIR}/clickhouse_node.sh"
  done
}

wait_for_nodes() {
  local index attempt
  for index in 0 1 2; do
    for attempt in {1..40}; do
      if node_ssh "$index" --command="sudo systemctl is-active clickhouse-server" \
          >/dev/null 2>&1; then
        break
      fi
      if [ "$attempt" -eq 40 ]; then
        echo "${NAMES[$index]} did not become ready" >&2
        exit 1
      fi
      sleep 15
    done
  done
}

install_password_envs() {
  local password index
  password="$(gc secrets versions access latest --secret="$SECRET")"
  for index in 0 1 2; do
    printf 'CH_PASSWORD=%s\n' "$password" | node_ssh "$index" --command="sudo sh -c '
      umask 077
      cat > /etc/tr-clickhouse-ingest.env
    '"
  done
  unset password
}

load_ips() {
  IPS=()
  local index ip
  for index in 0 1 2; do
    ip="$(gc compute instances describe "${NAMES[$index]}" \
      --zone="${ZONES[$index]}" \
      --format='value(networkInterfaces[0].networkIP)')"
    if [ -z "$ip" ]; then
      echo "missing private IP for ${NAMES[$index]}" >&2
      exit 1
    fi
    IPS+=("$ip")
  done
}

render_cluster_config() {
  local index="$1"
  cat <<XML
<clickhouse>
  <keeper_server>
    <tcp_port>9181</tcp_port>
    <server_id>${KEEPER_IDS[$index]}</server_id>
    <log_storage_path>/var/lib/clickhouse/coordination/log</log_storage_path>
    <snapshot_storage_path>/var/lib/clickhouse/coordination/snapshots</snapshot_storage_path>
    <coordination_settings>
      <operation_timeout_ms>10000</operation_timeout_ms>
      <session_timeout_ms>30000</session_timeout_ms>
      <raft_logs_level>warning</raft_logs_level>
    </coordination_settings>
    <raft_configuration>
      <server><id>1</id><hostname>${IPS[0]}</hostname><port>9234</port></server>
      <server><id>2</id><hostname>${IPS[1]}</hostname><port>9234</port></server>
      <server><id>3</id><hostname>${IPS[2]}</hostname><port>9234</port></server>
    </raft_configuration>
  </keeper_server>
  <zookeeper>
    <node><host>${IPS[0]}</host><port>9181</port></node>
    <node><host>${IPS[1]}</host><port>9181</port></node>
    <node><host>${IPS[2]}</host><port>9181</port></node>
  </zookeeper>
  <remote_servers>
    <trustedrouter>
      <shard>
        <internal_replication>true</internal_replication>
        <replica><host>${IPS[0]}</host><port>9000</port></replica>
        <replica><host>${IPS[1]}</host><port>9000</port></replica>
        <replica><host>${IPS[2]}</host><port>9000</port></replica>
      </shard>
    </trustedrouter>
  </remote_servers>
  <macros>
    <shard>01</shard>
    <replica>${NAMES[$index]}</replica>
  </macros>
</clickhouse>
XML
}

install_cluster_config() {
  local index ready pid_two pid_three
  # Stage every config before restarting anything. The two new Keeper voters
  # restart concurrently so neither blocks forever waiting for the other to
  # form quorum; node 1 joins only after that pair is active.
  for index in 0 1 2; do
    log "configuring Keeper and replica macros on ${NAMES[$index]}"
    render_cluster_config "$index" | node_ssh "$index" --command="sudo sh -c '
      set -eu
      temporary=\$(mktemp)
      cat > \"\$temporary\"
      if ! cmp -s \"\$temporary\" /etc/clickhouse-server/config.d/tr-cluster.xml; then
        install -o clickhouse -g clickhouse -m 0644 \"\$temporary\" \
          /etc/clickhouse-server/config.d/tr-cluster.xml
        touch /run/tr-clickhouse-cluster-restart-needed
      fi
      rm -f \"\$temporary\"
    '"
  done
  node_ssh 1 --command="sudo sh -c 'if [ -e /run/tr-clickhouse-cluster-restart-needed ]; then rm -f /run/tr-clickhouse-cluster-restart-needed; systemctl restart clickhouse-server; fi'" &
  pid_two=$!
  node_ssh 2 --command="sudo sh -c 'if [ -e /run/tr-clickhouse-cluster-restart-needed ]; then rm -f /run/tr-clickhouse-cluster-restart-needed; systemctl restart clickhouse-server; fi'" &
  pid_three=$!
  wait "$pid_two"
  wait "$pid_three"
  node_ssh 0 --command="sudo sh -c 'if [ -e /run/tr-clickhouse-cluster-restart-needed ]; then rm -f /run/tr-clickhouse-cluster-restart-needed; systemctl restart clickhouse-server; fi'"

  for index in 1 2 0; do
    ready=0
    for _ in {1..60}; do
      if node_ssh "$index" --command="sudo systemctl is-active clickhouse-server" \
          >/dev/null 2>&1; then
        ready=1
        break
      fi
      sleep 2
    done
    if [ "$ready" -ne 1 ]; then
      echo "clickhouse-server did not restart on ${NAMES[$index]}" >&2
      exit 1
    fi
  done
  node_scalar 0 "SELECT count() FROM system.zookeeper WHERE path = '/'" >/dev/null
}

install_ops_agents() {
  local index
  for index in 1 2; do
    node_ssh "$index" --command="sudo sh -c '
      set -eu
      if ! dpkg-query -W google-cloud-ops-agent >/dev/null 2>&1; then
        tmp=\$(mktemp)
        curl -fsSL https://dl.google.com/cloudagents/add-google-cloud-ops-agent-repo.sh -o \"\$tmp\"
        bash \"\$tmp\" --also-install
        rm -f \"\$tmp\"
      fi
      systemctl enable --now google-cloud-ops-agent
    '"
  done
}

replicated_table_name() {
  local index="$1"
  local engine
  engine="$(node_scalar "$index" \
    "SELECT engine FROM system.tables WHERE database='tr' AND name='provider_benchmark_samples'")"
  if [ "$engine" = "ReplicatedReplacingMergeTree" ]; then
    printf 'provider_benchmark_samples\n'
  else
    printf 'provider_benchmark_samples_replicated\n'
  fi
}

ensure_replicated_tables() {
  local schema index canonical_engine
  schema="$(cat "$MIGRATION_SCHEMA")"
  canonical_engine="$(node_scalar 0 \
    "SELECT engine FROM system.tables WHERE database='tr' AND name='provider_benchmark_samples'")"
  if [ "$canonical_engine" = "ReplicatedReplacingMergeTree" ]; then
    log "canonical table is already replicated"
    return
  fi
  if [ "$canonical_engine" != "ReplacingMergeTree" ]; then
    echo "refusing migration: unexpected node-1 engine ${canonical_engine}" >&2
    exit 1
  fi
  for index in 0 1 2; do
    node_query "$index" "CREATE DATABASE IF NOT EXISTS tr; ${schema}"
  done
}

fingerprint_sql() {
  local table="$1"
  local cutoff="${2:-}"
  local columns
  local where=""
  columns="id,created_at,provider,model,provider_name,status,usage_type,source,streamed,input_tokens,output_tokens,total_cost_microdollars,speed_tokens_per_second,elapsed_milliseconds,first_token_milliseconds,ttfb_milliseconds,finish_reason,error_type,error_status,error_message,region,app"
  if [ -n "$cutoff" ]; then
    where=" WHERE created_at < toDateTime64('${cutoff}', 3, 'UTC')"
  fi
  printf '%s\n' "SELECT count(), sum(cityHash64(toJSONString(tuple(${columns})))), groupBitXor(cityHash64(toJSONString(tuple(${columns})))) FROM ${table} FINAL${where} FORMAT TSVRaw"
}

sync_replicas() {
  local index table
  for index in 0 1 2; do
    table="$(replicated_table_name "$index")"
    node_query "$index" "SYSTEM SYNC REPLICA ${table}"
  done
}

assert_replica_parity() {
  local cutoff="${1:-}"
  local source expected index table actual
  source="$(node_scalar 0 "$(fingerprint_sql provider_benchmark_samples "$cutoff")")"
  expected=""
  for index in 0 1 2; do
    table="$(replicated_table_name "$index")"
    actual="$(node_scalar "$index" "$(fingerprint_sql "$table" "$cutoff")")"
    if [ -z "$expected" ]; then
      expected="$actual"
    elif [ "$actual" != "$expected" ]; then
      echo "replica fingerprint mismatch on ${NAMES[$index]}" >&2
      exit 1
    fi
  done
  if [ "$source" != "$expected" ]; then
    echo "source and replicated fingerprints differ" >&2
    exit 1
  fi
}

migrate_raw_table() {
  local engine index table cutoff
  engine="$(node_scalar 0 \
    "SELECT engine FROM system.tables WHERE database='tr' AND name='provider_benchmark_samples'")"
  if [ "$engine" = "ReplicatedReplacingMergeTree" ]; then
    sync_replicas
    cutoff="$(python3 -c 'import datetime as d; print((d.datetime.now(d.UTC)-d.timedelta(minutes=5)).isoformat(timespec="milliseconds").replace("+00:00","Z"))')"
    assert_replica_parity "$cutoff"
    return
  fi

  log "performing first replay-safe backfill"
  node_query 0 "INSERT INTO provider_benchmark_samples_replicated SELECT * FROM provider_benchmark_samples FINAL"
  sync_replicas

  log "pausing the outbox ingester for final parity and atomic rename"
  node_ssh 0 --command="sudo systemctl stop tr-clickhouse-ingest.service"
  trap 'node_ssh 0 --command="sudo systemctl start tr-clickhouse-ingest.service" >/dev/null 2>&1 || true' EXIT
  node_query 0 "INSERT INTO provider_benchmark_samples_replicated SELECT * FROM provider_benchmark_samples FINAL"
  sync_replicas
  assert_replica_parity

  for index in 1 2; do
    table="$(replicated_table_name "$index")"
    if [ "$table" = "provider_benchmark_samples_replicated" ]; then
      node_query "$index" \
        "RENAME TABLE provider_benchmark_samples_replicated TO provider_benchmark_samples"
    fi
  done
  if [ "$(node_scalar 0 "SELECT count() FROM system.tables WHERE database='tr' AND name='provider_benchmark_samples_local_backup'")" != "0" ]; then
    echo "refusing node-1 rename: local backup table already exists" >&2
    exit 1
  fi
  node_query 0 \
    "RENAME TABLE provider_benchmark_samples TO provider_benchmark_samples_local_backup, provider_benchmark_samples_replicated TO provider_benchmark_samples"

  for index in 0 1 2; do
    engine="$(node_scalar "$index" \
      "SELECT engine FROM system.tables WHERE database='tr' AND name='provider_benchmark_samples'")"
    if [ "$engine" != "ReplicatedReplacingMergeTree" ]; then
      echo "canonical table is not replicated on ${NAMES[$index]}" >&2
      exit 1
    fi
  done
  node_ssh 0 --command="sudo systemctl start tr-clickhouse-ingest.service"
  trap - EXIT
  assert_replica_parity
}

install_provider_readers() {
  local index
  for index in 0 1 2; do
    PROJECT="$PROJECT_ID" ZONE="${ZONES[$index]}" NAME="${NAMES[$index]}" \
      bash "${SCRIPT_DIR}/clickhouse_provider_reader.sh"
  done
}

ensure_internal_load_balancer() {
  local index group address ip
  log "ensuring private regional ClickHouse load balancer"
  for index in 0 1 2; do
    group="tr-clickhouse-${KEEPER_IDS[$index]}"
    if ! gc compute instance-groups unmanaged describe "$group" \
        --zone="${ZONES[$index]}" >/dev/null 2>&1; then
      gc compute instance-groups unmanaged create "$group" --zone="${ZONES[$index]}"
    fi
    if ! gc compute instance-groups unmanaged list-instances "$group" \
        --zone="${ZONES[$index]}" --format='value(instance.basename())' \
        | grep -Fxq "${NAMES[$index]}"; then
      gc compute instance-groups unmanaged add-instances "$group" \
        --zone="${ZONES[$index]}" \
        --instances="${NAMES[$index]}"
    fi
    gc compute instance-groups unmanaged set-named-ports "$group" \
      --zone="${ZONES[$index]}" \
      --named-ports=http:8123
  done

  if ! gc compute health-checks describe tr-clickhouse-http --region="$REGION" >/dev/null 2>&1; then
    gc compute health-checks create tcp tr-clickhouse-http \
      --region="$REGION" \
      --port=8123 \
      --check-interval=10s \
      --timeout=5s \
      --healthy-threshold=2 \
      --unhealthy-threshold=2
  fi
  if ! gc compute backend-services describe tr-clickhouse-http \
      --region="$REGION" >/dev/null 2>&1; then
    gc compute backend-services create tr-clickhouse-http \
      --region="$REGION" \
      --load-balancing-scheme=INTERNAL \
      --protocol=TCP \
      --health-checks=tr-clickhouse-http \
      --health-checks-region="$REGION"
  fi
  for index in 0 1 2; do
    group="tr-clickhouse-${KEEPER_IDS[$index]}"
    if ! gc compute backend-services describe tr-clickhouse-http \
        --region="$REGION" --format='value(backends[].group.basename())' \
        | grep -Fxq "$group"; then
      gc compute backend-services add-backend tr-clickhouse-http \
        --region="$REGION" \
        --instance-group="$group" \
        --instance-group-zone="${ZONES[$index]}"
    fi
  done
  address=tr-clickhouse-ilb
  if ! gc compute addresses describe "$address" --region="$REGION" >/dev/null 2>&1; then
    gc compute addresses create "$address" \
      --region="$REGION" \
      --subnet=default
  fi
  ip="$(gc compute addresses describe "$address" --region="$REGION" --format='value(address)')"
  if ! gc compute forwarding-rules describe tr-clickhouse-http \
      --region="$REGION" >/dev/null 2>&1; then
    gc compute forwarding-rules create tr-clickhouse-http \
      --region="$REGION" \
      --load-balancing-scheme=INTERNAL \
      --network=default \
      --subnet=default \
      --address="$ip" \
      --ports=8123 \
      --backend-service=tr-clickhouse-http \
      --backend-service-region="$REGION" \
      --allow-global-access
  fi
  if [ "$(node_scalar 0 "SELECT 1")" != "1" ]; then
    echo "node query validation failed before load-balancer smoke" >&2
    exit 1
  fi
  if [ "$(node_ssh 0 --command="curl -fsS --max-time 5 http://${ip}:8123/ping")" != "Ok." ]; then
    echo "private ClickHouse load balancer /ping failed" >&2
    exit 1
  fi
  log "private ClickHouse load balancer is healthy at ${ip}:8123"
}

ensure_service_account
ensure_network
provision_nodes
wait_for_nodes
install_password_envs
load_ips
install_cluster_config
install_ops_agents
ensure_replicated_tables
migrate_raw_table
install_provider_readers
ensure_internal_load_balancer
log "three-replica ClickHouse cluster is healthy; local node-1 backup was retained"
