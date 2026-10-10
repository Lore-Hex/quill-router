#!/usr/bin/env bash
# Apply the fast admission service's own tables and indexes
# (docs/design/fast-admission-production-rollout.md, W4), statement for
# statement as fastpath/schema/fastpath.sql has them; a Go test,
# fastpath/schema/migration_test.go, holds the two equal. The one table the
# service shares, tr_credit_balance, is production's own
# (migrate_typed_counters.sh), and this leaves it as it is.
#
# Idempotent: every table and index is guarded by an INFORMATION_SCHEMA
# existence check, and every index is checked read-write on every run. The
# tables are new and empty: nothing reads or writes them until a fast path
# node runs, and none admits for a workspace the switch, tr_fastpath_workspace,
# does not enable, which none is.
#
# A check that cannot read the schema fails the run, rather than being taken
# for an object that is not there.
#
# Each statement is its own schema update, as in the other migrations: the
# conformance extractor (tests/conformance/spanner_schema_source.py) reads
# one statement per dispatch. So on the first run each index is added to a
# table made by an earlier update: fifteen updates, each waited on, and five
# of them index builds, which add schema versions Spanner keeps and throttles
# on, and whose version changes can abort a transaction in flight, which its
# client retries. The tables are empty and nothing reads or writes them yet.
# Reruns apply nothing.
#
# Usage:
#   SPANNER_INSTANCE_ID=... SPANNER_DATABASE_ID=... [GCP_PROJECT_ID=...] \
#     scripts/deploy/migrate_fastpath.sh
set -euo pipefail

INSTANCE="${SPANNER_INSTANCE_ID:?set SPANNER_INSTANCE_ID}"
DATABASE="${SPANNER_DATABASE_ID:?set SPANNER_DATABASE_ID}"
PROJECT_ARG=()
[ -n "${GCP_PROJECT_ID:-}" ] && PROJECT_ARG=(--project "${GCP_PROJECT_ID}")

log() { printf '%s %s\n' "[migrate_fastpath]" "$*"; }

table_exists() {
  local name="$1" n
  n=$(gcloud spanner databases execute-sql "$DATABASE" \
        --instance="$INSTANCE" "${PROJECT_ARG[@]}" \
        --sql="SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE table_schema='' AND table_name='${name}'" \
        --format='value(rows[0])') || exit $?
  [ "${n:-0}" != "0" ]
}

index_exists() {
  local name="$1" n
  n=$(gcloud spanner databases execute-sql "$DATABASE" \
        --instance="$INSTANCE" "${PROJECT_ARG[@]}" \
        --sql="SELECT COUNT(*) FROM INFORMATION_SCHEMA.INDEXES WHERE table_schema='' AND index_name='${name}'" \
        --format='value(rows[0])') || exit $?
  [ "${n:-0}" != "0" ]
}

apply_ddl() {
  log "applying: $1"
  gcloud spanner databases ddl update "$DATABASE" \
    --instance="$INSTANCE" "${PROJECT_ARG[@]}" --ddl="$1"
}

wait_index_read_write() {
  local name="$1" state=""
  for _ in $(seq 1 360); do
    state=$(gcloud spanner databases execute-sql "$DATABASE" \
      --instance="$INSTANCE" "${PROJECT_ARG[@]}" \
      --sql="SELECT INDEX_STATE FROM INFORMATION_SCHEMA.INDEXES
             WHERE table_schema='' AND index_name='${name}'" \
      --format='value(rows[0])') || exit $?
    if [ "$state" = "READ_WRITE" ]; then
      log "${name} is read-write"
      return 0
    fi
    log "waiting for ${name} backfill (state=${state:-unknown})"
    sleep 5
  done
  log "timed out waiting for ${name} to become read-write"
  return 1
}

if table_exists tr_lease; then
  log "tr_lease: already present"
else
  apply_ddl "CREATE TABLE tr_lease (
    workspace_id STRING(64) NOT NULL,
    lease_id STRING(32) NOT NULL,
    region STRING(32) NOT NULL,
    workspace_shard INT64 NOT NULL,
    owner_node STRING(128) NOT NULL,
    owner_epoch INT64 NOT NULL,
    state STRING(16) NOT NULL,
    granted INT64 NOT NULL,
    allocation INT64 NOT NULL,
    consumed INT64 NOT NULL DEFAULT (0),
    shortfall_total INT64 NOT NULL DEFAULT (0),
    door_raised INT64 NOT NULL DEFAULT (0),
    returned INT64 NOT NULL DEFAULT (0),
    fault_usage INT64 NOT NULL DEFAULT (0),
    expiry TIMESTAMP NOT NULL,
    revoked BOOL NOT NULL DEFAULT (FALSE),
    revoked_at TIMESTAMP,
    key_status_version INT64 NOT NULL,
    commit_version INT64 NOT NULL DEFAULT (0),
    applied_seq INT64 NOT NULL DEFAULT (0),
    last_tick INT64 NOT NULL DEFAULT (0),
    audit_osum INT64 NOT NULL DEFAULT (0),
    holds_listed_seq INT64,
    audit_fault_seq INT64,
    gap_seq INT64,
    fence_time TIMESTAMP,
    boundary_seq INT64,
    boundary_publish_time TIMESTAMP,
    drained_by STRING(16),
    closed_at TIMESTAMP,
    close_kind STRING(16),
    retire_at TIMESTAMP,
    CONSTRAINT tr_lease_state CHECK (state IN ('open', 'draining', 'closed')),
    CONSTRAINT tr_lease_kinds CHECK ((close_kind IS NULL OR close_kind IN ('auditor', 'operator'))
    AND (drained_by IS NULL OR drained_by IN ('owner', 'auditor'))),
    CONSTRAINT tr_lease_fence CHECK (state = 'open' OR (fence_time IS NOT NULL AND drained_by IS NOT NULL)),
    CONSTRAINT tr_lease_fence_after CHECK (fence_time IS NULL OR fence_time >= expiry),
    CONSTRAINT tr_lease_boundary CHECK ((boundary_seq IS NULL) = (boundary_publish_time IS NULL)
    AND (boundary_seq IS NULL OR state != 'open')),
    CONSTRAINT tr_lease_closed CHECK ((state = 'closed') = (closed_at IS NOT NULL)
    AND (closed_at IS NULL) = (close_kind IS NULL)),
    CONSTRAINT tr_lease_room CHECK (consumed >= 0 AND consumed <= allocation),
    CONSTRAINT tr_lease_accounted CHECK (allocation = granted + shortfall_total + door_raised - returned
    AND shortfall_total >= 0 AND door_raised >= 0 AND returned >= 0 AND fault_usage >= 0),
    ) PRIMARY KEY (workspace_id, lease_id),
    ROW DELETION POLICY (OLDER_THAN(retire_at, INTERVAL 7 DAY))"
  log "tr_lease: created"
fi

if index_exists tr_lease_by_state; then
  log "tr_lease_by_state: already present"
else
  apply_ddl "CREATE INDEX tr_lease_by_state ON tr_lease (state)"
  log "tr_lease_by_state: created"
fi
wait_index_read_write tr_lease_by_state

if index_exists tr_lease_by_owner; then
  log "tr_lease_by_owner: already present"
else
  apply_ddl "CREATE INDEX tr_lease_by_owner ON tr_lease (owner_node, state)"
  log "tr_lease_by_owner: created"
fi
wait_index_read_write tr_lease_by_owner

if index_exists tr_lease_by_id; then
  log "tr_lease_by_id: already present"
else
  apply_ddl "CREATE UNIQUE INDEX tr_lease_by_id ON tr_lease (lease_id)"
  log "tr_lease_by_id: created"
fi
wait_index_read_write tr_lease_by_id

if table_exists tr_lease_donor; then
  log "tr_lease_donor: already present"
else
  apply_ddl "CREATE TABLE tr_lease_donor (
    workspace_id STRING(64) NOT NULL,
    lease_id STRING(32) NOT NULL,
    credit_shard INT64 NOT NULL,
    allocation INT64 NOT NULL,
    consumed INT64 NOT NULL DEFAULT (0),
    CONSTRAINT tr_lease_donor_room CHECK (consumed >= 0 AND consumed <= allocation),
    ) PRIMARY KEY (workspace_id, lease_id, credit_shard),
    INTERLEAVE IN PARENT tr_lease ON DELETE CASCADE"
  log "tr_lease_donor: created"
fi

if table_exists tr_lease_hold; then
  log "tr_lease_hold: already present"
else
  apply_ddl "CREATE TABLE tr_lease_hold (
    workspace_id STRING(64) NOT NULL,
    lease_id STRING(32) NOT NULL,
    authorization_id STRING(64) NOT NULL,
    estimate INT64 NOT NULL,
    deadline TIMESTAMP NOT NULL,
    listed BOOL NOT NULL DEFAULT (FALSE),
    snapshot_seq INT64,
    snapshot_hash BYTES(32),
    snapshot_usage BYTES(MAX),
    running_charge INT64,
    snapshot_owner_seq INT64,
    reap_basis BYTES(MAX),
    boot_binding BYTES(MAX),
    CONSTRAINT tr_lease_hold_snapshot CHECK ((snapshot_seq IS NULL) = (running_charge IS NULL)),
    ) PRIMARY KEY (workspace_id, lease_id, authorization_id),
    INTERLEAVE IN PARENT tr_lease ON DELETE CASCADE"
  log "tr_lease_hold: created"
fi

if table_exists tr_lease_handoff; then
  log "tr_lease_handoff: already present"
else
  apply_ddl "CREATE TABLE tr_lease_handoff (
    workspace_id STRING(64) NOT NULL,
    lease_id STRING(32) NOT NULL,
    chunk_seq INT64 NOT NULL,
    holds BYTES(MAX) NOT NULL,
    ) PRIMARY KEY (workspace_id, lease_id, chunk_seq),
    INTERLEAVE IN PARENT tr_lease ON DELETE CASCADE"
  log "tr_lease_handoff: created"
fi

if table_exists tr_lease_winners; then
  log "tr_lease_winners: already present"
else
  apply_ddl "CREATE TABLE tr_lease_winners (
    workspace_id STRING(64) NOT NULL,
    lease_id STRING(32) NOT NULL,
    commit_version INT64 NOT NULL,
    pack BYTES(MAX) NOT NULL,
    winner_count INT64 NOT NULL,
    work_done_at TIMESTAMP,
    ) PRIMARY KEY (workspace_id, lease_id, commit_version),
    INTERLEAVE IN PARENT tr_lease ON DELETE CASCADE"
  log "tr_lease_winners: created"
fi

if index_exists tr_lease_winners_by_work; then
  log "tr_lease_winners_by_work: already present"
else
  apply_ddl "CREATE INDEX tr_lease_winners_by_work ON tr_lease_winners (work_done_at)"
  log "tr_lease_winners_by_work: created"
fi
wait_index_read_write tr_lease_winners_by_work

if table_exists tr_lease_drain; then
  log "tr_lease_drain: already present"
else
  apply_ddl "CREATE TABLE tr_lease_drain (
    workspace_id STRING(64) NOT NULL,
    lease_id STRING(32) NOT NULL,
    authorization_id STRING(64) NOT NULL,
    record_id STRING(64) NOT NULL,
    kind STRING(16) NOT NULL,
    charge INT64 NOT NULL,
    estimate INT64 NOT NULL,
    door_raise INT64 NOT NULL DEFAULT (0),
    record_digest BYTES(32),
    money BYTES(MAX) NOT NULL,
    snapshot_owner_seq INT64,
    cause STRING(160) NOT NULL,
    commit_ts TIMESTAMP NOT NULL OPTIONS (allow_commit_timestamp=true),
    CONSTRAINT tr_lease_drain_kind CHECK (kind IN ('settle', 'refund', 'reap')),
    ) PRIMARY KEY (workspace_id, lease_id, authorization_id, record_id),
    INTERLEAVE IN PARENT tr_lease ON DELETE CASCADE"
  log "tr_lease_drain: created"
fi

if index_exists tr_lease_drain_by_commit; then
  log "tr_lease_drain_by_commit: already present"
else
  apply_ddl "CREATE INDEX tr_lease_drain_by_commit ON tr_lease_drain (workspace_id, lease_id, commit_ts, record_id)
    STORING (kind, charge, estimate, door_raise, record_digest, money, snapshot_owner_seq, cause),
    INTERLEAVE IN tr_lease"
  log "tr_lease_drain_by_commit: created"
fi
wait_index_read_write tr_lease_drain_by_commit

if table_exists tr_lease_record; then
  log "tr_lease_record: already present"
else
  apply_ddl "CREATE TABLE tr_lease_record (
    authorization_id STRING(64) NOT NULL,
    kind STRING(16) NOT NULL,
    workspace_id STRING(64) NOT NULL,
    lease_id STRING(32) NOT NULL,
    outcome STRING(16) NOT NULL,
    cost INT64,
    winner_digest BYTES(32),
    boot_binding BYTES(MAX),
    body BYTES(MAX) NOT NULL,
    CONSTRAINT tr_lease_record_kind CHECK (kind IN ('generation', 'activity', 'disposition')),
    CONSTRAINT tr_lease_record_outcome CHECK (outcome IN ('settled', 'refunded', 'reaped_snapshot', 'released')),
    ) PRIMARY KEY (authorization_id, kind)"
  log "tr_lease_record: created"
fi

if table_exists tr_lease_staged; then
  log "tr_lease_staged: already present"
else
  apply_ddl "CREATE TABLE tr_lease_staged (
    authorization_id STRING(64) NOT NULL,
    record_digest BYTES(32) NOT NULL,
    workspace_id STRING(64) NOT NULL,
    lease_id STRING(32) NOT NULL,
    body BYTES(MAX) NOT NULL,
    message_id STRING(128) NOT NULL,
    publish_time TIMESTAMP NOT NULL,
    ) PRIMARY KEY (authorization_id, record_digest)"
  log "tr_lease_staged: created"
fi

if index_exists tr_lease_staged_by_lease; then
  log "tr_lease_staged_by_lease: already present"
else
  apply_ddl "CREATE INDEX tr_lease_staged_by_lease ON tr_lease_staged (workspace_id, lease_id)"
  log "tr_lease_staged_by_lease: created"
fi
wait_index_read_write tr_lease_staged_by_lease

if table_exists tr_fastpath_workspace; then
  log "tr_fastpath_workspace: already present"
else
  apply_ddl "CREATE TABLE tr_fastpath_workspace (
    workspace_id STRING(64) NOT NULL,
    enabled BOOL NOT NULL,
    changed_at TIMESTAMP NOT NULL OPTIONS (allow_commit_timestamp=true),
    ) PRIMARY KEY (workspace_id)"
  log "tr_fastpath_workspace: created"
fi

if table_exists tr_fastpath_member; then
  log "tr_fastpath_member: already present"
else
  apply_ddl "CREATE TABLE tr_fastpath_member (
    address STRING(256) NOT NULL,
    epoch INT64 NOT NULL,
    roles ARRAY<STRING(16)> NOT NULL,
    state STRING(16) NOT NULL,
    started_at TIMESTAMP NOT NULL OPTIONS (allow_commit_timestamp=true),
    heartbeat_at TIMESTAMP NOT NULL OPTIONS (allow_commit_timestamp=true),
    CONSTRAINT tr_fastpath_member_state CHECK (state IN ('serving', 'leaving', 'withdrawn')),
    ) PRIMARY KEY (address)"
  log "tr_fastpath_member: created"
fi
log "done"
