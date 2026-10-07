-- The fast-admission spike's schema (docs/design/fast-admission-spike.md, §4):
-- a first draft of what production will need, applied only to the spike's
-- databases, the emulator's in CI and the spike's own instance. Production's
-- schema comes after the spike, from what it learned.
--
-- GoogleSQL, statements separated by semicolons. Section numbers are the
-- design's (docs/design/fast-admission-and-batched-settlement.md). The
-- design fixes what is stored; where a column's form is the spike's own
-- choice, its comment says so.

-- Credit rows, as production's scripts/deploy/migrate_typed_counters.sh
-- creates them, word for word, so the spike's statements meet production's
-- columns, nullability and defaults; a test holds the two equal. Grants,
-- raises, bookings and returns move `reserved` and `total_usage`, covering
-- moves `total_credits`, and `in_debt` is the debt mark (§4.7), NULL on rows
-- older than the column, so read COALESCE(in_debt, FALSE).
CREATE TABLE tr_credit_balance (
  workspace_id STRING(64) NOT NULL,
  shard INT64 NOT NULL DEFAULT (0),
  total_credits INT64 NOT NULL DEFAULT (0),
  total_usage INT64 NOT NULL DEFAULT (0),
  reserved INT64 NOT NULL DEFAULT (0),
  trust_tier INT64 DEFAULT (0),
  trust_computed_at TIMESTAMP,
  trust_latched_at TIMESTAMP,
  trust_override_tier INT64,
  billing_pause_causes ARRAY<STRING(32)>,
  pause_epoch INT64 DEFAULT (0),
  trust_reconciled_through TIMESTAMP,
  in_debt BOOL DEFAULT (FALSE),
  source_updated_at TIMESTAMP OPTIONS (allow_commit_timestamp=true),
  updated_at TIMESTAMP OPTIONS (allow_commit_timestamp=true),
) PRIMARY KEY (workspace_id, shard);

-- Leases (§4.2), keyed by workspace first, since a grant reads the
-- workspace's leases under its lock.
CREATE TABLE tr_lease (
  workspace_id STRING(64) NOT NULL,
  -- Minted by the caller, so a retried grant finds its lease: 16 random
  -- bytes, base64url. It names the lease in every authorization (§4.9).
  lease_id STRING(32) NOT NULL,
  region STRING(32) NOT NULL,
  -- The workspace shard the lease serves (§4.3), not a credit shard.
  workspace_shard INT64 NOT NULL,
  owner_node STRING(128) NOT NULL,
  owner_epoch INT64 NOT NULL,
  state STRING(16) NOT NULL,
  -- The money (§4.2, §4.7): L as granted, the allocation reserved across the
  -- donors, and what is booked against it. The allocation grows by the
  -- owner's shortfall writes (shortfall_total) and the front doors' raises
  -- (door_raised), and shrinks by returns (returned); consumption beyond it
  -- is booked as usage (fault_usage). The last three columns are the
  -- spike's, so that the accounting is a check on the row.
  granted INT64 NOT NULL,
  allocation INT64 NOT NULL,
  consumed INT64 NOT NULL DEFAULT (0),
  shortfall_total INT64 NOT NULL DEFAULT (0),
  door_raised INT64 NOT NULL DEFAULT (0),
  returned INT64 NOT NULL DEFAULT (0),
  fault_usage INT64 NOT NULL DEFAULT (0),
  -- Spanner's time: renewals extend it, and it never moves back.
  expiry TIMESTAMP NOT NULL,
  revoked BOOL NOT NULL DEFAULT (FALSE),
  revoked_at TIMESTAMP,
  key_status_version INT64 NOT NULL,
  -- The auditor's progress (§4.8): the version every commit is conditional
  -- on, the highest owner sequence number applied, the last tick, the sum
  -- the checkpoints are audited against, the sequence number of the applied
  -- final checkpoint or manifest that listed the open holds (NULL until a
  -- whole list is applied), the alert, and the gap that stops the lease.
  commit_version INT64 NOT NULL DEFAULT (0),
  applied_seq INT64 NOT NULL DEFAULT (0),
  last_tick INT64 NOT NULL DEFAULT (0),
  audit_osum INT64 NOT NULL DEFAULT (0),
  holds_listed_seq INT64,
  audit_fault_seq INT64,
  gap_seq INT64,
  -- Draining (§4.8): the fence F, the boundary S and its publish time T,
  -- and who marked it.
  fence_time TIMESTAMP,
  boundary_seq INT64,
  boundary_publish_time TIMESTAMP,
  drained_by STRING(16),
  -- Closing (§4.5, §4.8): when, by whom, and from when the row may go: set
  -- once the lease is closed and no pack has pending work, and kept seven
  -- days after, as the answers to refused appends need.
  closed_at TIMESTAMP,
  close_kind STRING(16),
  retire_at TIMESTAMP,
  CONSTRAINT tr_lease_state CHECK (state IN ('open', 'draining', 'closed')),
  CONSTRAINT tr_lease_kinds CHECK ((close_kind IS NULL OR close_kind IN ('auditor', 'operator'))
    AND (drained_by IS NULL OR drained_by IN ('owner', 'auditor'))),
  -- No draining lease lacks F, and F is after the expiry it was set from.
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
  ROW DELETION POLICY (OLDER_THAN(retire_at, INTERVAL 7 DAY));

-- The auditor's scan for expired open leases. A lease changes state twice,
-- so the index is not written by renewals.
CREATE INDEX tr_lease_by_state ON tr_lease (state);

-- A lease by its ID alone. An authorization names its lease but not the
-- workspace, and the answer for an authorization (§4.9) is looked up from
-- its ID: this finds the lease's workspace, and with it the lease's packs.
-- Lease IDs are random, and unique across workspaces.
CREATE UNIQUE INDEX tr_lease_by_id ON tr_lease (lease_id);

-- The allocation per donor shard (§4.2). The spike's choice of form: a row
-- per donor, so each of the three writers' arithmetic is one conditional
-- statement. Donors are in ascending shard order, the first the lowest:
-- bookings take from the first donor first, returns from the last.
CREATE TABLE tr_lease_donor (
  workspace_id STRING(64) NOT NULL,
  lease_id STRING(32) NOT NULL,
  credit_shard INT64 NOT NULL,
  allocation INT64 NOT NULL,
  consumed INT64 NOT NULL DEFAULT (0),
  CONSTRAINT tr_lease_donor_room CHECK (consumed >= 0 AND consumed <= allocation),
) PRIMARY KEY (workspace_id, lease_id, credit_shard),
  INTERLEAVE IN PARENT tr_lease ON DELETE CASCADE;

-- The stored open holds (§4.8), a row for each hold the log has shown: its
-- estimate, its latest valid snapshot and its deadline. The auditor's commit
-- inserts and updates them, and deletes a hold's row in the commit that
-- stores its winner. reap_basis keeps what a member that takes over needs to
-- build a reap's full record (§4.9): the first heartbeat record's fields,
-- which Pub/Sub does not deliver again once acknowledged.
CREATE TABLE tr_lease_hold (
  workspace_id STRING(64) NOT NULL,
  lease_id STRING(32) NOT NULL,
  authorization_id STRING(64) NOT NULL,
  estimate INT64 NOT NULL,
  deadline TIMESTAMP NOT NULL,
  -- The row came from a hand-off's list, not a heartbeat.
  listed BOOL NOT NULL DEFAULT (FALSE),
  snapshot_seq INT64,
  snapshot_hash BYTES(32),
  snapshot_usage BYTES(MAX),
  running_charge INT64,
  snapshot_owner_seq INT64,
  reap_basis BYTES(MAX),
  CONSTRAINT tr_lease_hold_snapshot CHECK ((snapshot_seq IS NULL) = (running_charge IS NULL)),
) PRIMARY KEY (workspace_id, lease_id, authorization_id),
  INTERLEAVE IN PARENT tr_lease ON DELETE CASCADE;

-- The winners (§4.8), one pack per lease per commit, each winner with its
-- pending work, refunds and zero charges included. A pack may be deleted
-- seven days after its lease has closed and its work is done.
CREATE TABLE tr_lease_winners (
  workspace_id STRING(64) NOT NULL,
  lease_id STRING(32) NOT NULL,
  commit_version INT64 NOT NULL,
  pack BYTES(MAX) NOT NULL,
  winner_count INT64 NOT NULL,
  work_done_at TIMESTAMP,
  deletable_at TIMESTAMP,
  CONSTRAINT tr_lease_winners_done CHECK (deletable_at IS NULL OR work_done_at IS NOT NULL),
) PRIMARY KEY (workspace_id, lease_id, commit_version),
  INTERLEAVE IN PARENT tr_lease ON DELETE CASCADE,
  ROW DELETION POLICY (OLDER_THAN(deletable_at, INTERVAL 7 DAY));

-- Packs with work pending, for the sweep over closed leases.
CREATE INDEX tr_lease_winners_by_work ON tr_lease_winners (work_done_at);

-- The drain log (§4.5): terminals the front doors append once a lease drains
-- or its owner is gone, and the auditor's reaps. A record's ID is minted by
-- whoever appends it and reused on that append's retries. Rows are read in
-- the order of their commit, then their ID, since two appends can share a
-- commit timestamp. door_raise is the raise the append made (§4.2).
CREATE TABLE tr_lease_drain (
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
  -- A reap names the heartbeat record its charge came from.
  snapshot_owner_seq INT64,
  -- The event that caused the append, for the spike's traces (spike plan §6).
  cause STRING(160) NOT NULL,
  commit_ts TIMESTAMP NOT NULL OPTIONS (allow_commit_timestamp=true),
  CONSTRAINT tr_lease_drain_kind CHECK (kind IN ('settle', 'refund', 'reap')),
) PRIMARY KEY (workspace_id, lease_id, authorization_id, record_id),
  INTERLEAVE IN PARENT tr_lease ON DELETE CASCADE;

CREATE INDEX tr_lease_drain_by_commit ON tr_lease_drain (workspace_id, lease_id, commit_ts, record_id)
  STORING (kind, charge, estimate, door_raise, record_digest, money, snapshot_owner_seq, cause),
  INTERLEAVE IN tr_lease;

-- The records the pending work writes (§4.9), standing for the generation
-- and activity records and the disposition records. Keyed by authorization,
-- so writing one twice leaves one row; they outlive the lease.
CREATE TABLE tr_lease_record (
  authorization_id STRING(64) NOT NULL,
  kind STRING(16) NOT NULL,
  workspace_id STRING(64) NOT NULL,
  lease_id STRING(32) NOT NULL,
  outcome STRING(16) NOT NULL,
  -- NULL when the cost is not known; never zero in its place.
  cost INT64,
  winner_digest BYTES(32),
  boot_binding BYTES(MAX),
  body BYTES(MAX) NOT NULL,
  CONSTRAINT tr_lease_record_kind CHECK (kind IN ('generation', 'activity', 'disposition')),
  CONSTRAINT tr_lease_record_outcome CHECK (outcome IN ('settled', 'refunded', 'reaped_snapshot', 'released')),
) PRIMARY KEY (authorization_id, kind);

-- The stand-in for staging full records (spike plan §2; design §4.9), keyed
-- by authorization and digest: the pending work joins on both, never on the
-- authorization alone. Its writes are tagged and counted apart.
CREATE TABLE tr_spike_staged (
  authorization_id STRING(64) NOT NULL,
  record_digest BYTES(32) NOT NULL,
  workspace_id STRING(64) NOT NULL,
  lease_id STRING(32) NOT NULL,
  body BYTES(MAX) NOT NULL,
  message_id STRING(128) NOT NULL,
  publish_time TIMESTAMP NOT NULL,
) PRIMARY KEY (authorization_id, record_digest);

CREATE INDEX tr_spike_staged_by_lease ON tr_spike_staged (workspace_id, lease_id);

-- Membership (spike plan §4): each node writes its row every second. A node
-- is live while its heartbeat is younger than three seconds, both times
-- Spanner's. Each start of the node takes the next epoch, which its leases
-- carry as owner_epoch.
CREATE TABLE tr_fastpath_member (
  address STRING(256) NOT NULL,
  epoch INT64 NOT NULL,
  roles ARRAY<STRING(16)> NOT NULL,
  state STRING(16) NOT NULL,
  started_at TIMESTAMP NOT NULL OPTIONS (allow_commit_timestamp=true),
  heartbeat_at TIMESTAMP NOT NULL OPTIONS (allow_commit_timestamp=true),
  CONSTRAINT tr_fastpath_member_state CHECK (state IN ('serving', 'leaving', 'withdrawn')),
) PRIMARY KEY (address);
