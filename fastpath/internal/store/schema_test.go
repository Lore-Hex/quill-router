package store

import (
	"context"
	"sort"
	"strings"
	"testing"
	"time"

	"cloud.google.com/go/spanner"

	"github.com/Lore-Hex/quill-router/fastpath/internal/store/storetest"
)

// TestSchemaIsAsWritten: the emulator holds spike.sql's tables, with their
// parents, cascades and deletion policies, and its indexes.
func TestSchemaIsAsWritten(t *testing.T) {
	spikeStore(t, Config{LiveFor: time.Second})
	ctx := context.Background()
	tables := map[string]string{}
	iter := shared.Single().Query(ctx, spanner.Statement{SQL: `
		SELECT TABLE_NAME, COALESCE(PARENT_TABLE_NAME, ''), COALESCE(ON_DELETE_ACTION, ''),
		       COALESCE(ROW_DELETION_POLICY_EXPRESSION, '')
		  FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = ''`})
	err := iter.Do(func(row *spanner.Row) error {
		var name, parent, onDelete, policy string
		if err := row.Columns(&name, &parent, &onDelete, &policy); err != nil {
			return err
		}
		tables[name] = strings.Join([]string{parent, onDelete, policy}, " | ")
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	want := map[string]string{
		"tr_credit_balance":  " |  | ",
		"tr_lease":           " |  | OLDER_THAN(retire_at, INTERVAL 7 DAY)",
		"tr_lease_donor":     "tr_lease | CASCADE | ",
		"tr_lease_hold":      "tr_lease | CASCADE | ",
		"tr_lease_winners":   "tr_lease | CASCADE | OLDER_THAN(deletable_at, INTERVAL 7 DAY)",
		"tr_lease_drain":     "tr_lease | CASCADE | ",
		"tr_lease_record":    " |  | ",
		"tr_spike_staged":    " |  | ",
		"tr_fastpath_member": " |  | ",
	}
	if len(tables) != len(want) {
		t.Errorf("the database has tables %v", tables)
	}
	for name, w := range want {
		if got := tables[name]; got != w {
			t.Errorf("%s: parent | on delete | deletion policy is %q, want %q", name, got, w)
		}
	}
	var indexes []string
	iter = shared.Single().Query(ctx, spanner.Statement{SQL: `
		SELECT CONCAT(INDEX_NAME, ' ON ', TABLE_NAME, ' IN ', COALESCE(PARENT_TABLE_NAME, ''))
		  FROM INFORMATION_SCHEMA.INDEXES WHERE TABLE_SCHEMA = '' AND INDEX_TYPE = 'INDEX'`})
	err = iter.Do(func(row *spanner.Row) error {
		var index string
		if err := row.Columns(&index); err != nil {
			return err
		}
		indexes = append(indexes, index)
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	sort.Strings(indexes)
	wantIndexes := "tr_lease_by_id ON tr_lease IN , tr_lease_by_state ON tr_lease IN , " +
		"tr_lease_drain_by_commit ON tr_lease_drain IN tr_lease, " +
		"tr_lease_winners_by_work ON tr_lease_winners IN , tr_spike_staged_by_lease ON tr_spike_staged IN "
	if got := strings.Join(indexes, ", "); got != wantIndexes {
		t.Errorf("the indexes are %s", got)
	}
}

// leaseRow is an open lease that every check accepts.
func leaseRow(workspace, lease string, expiry time.Time) map[string]any {
	return map[string]any{
		"workspace_id": workspace, "lease_id": lease, "region": "us-central1", "workspace_shard": int64(0),
		"owner_node": "node-a", "owner_epoch": int64(1), "state": "open", "granted": int64(100),
		"allocation": int64(100), "expiry": expiry, "key_status_version": int64(0),
	}
}

// TestChecksRefuseRowsTheDesignForbids: each check refuses the row it is
// there for, naming itself, where the row without the defect is taken.
func TestChecksRefuseRowsTheDesignForbids(t *testing.T) {
	spikeStore(t, Config{LiveFor: time.Second})
	ctx := context.Background()
	expiry := time.Date(2026, 10, 7, 12, 0, 0, 0, time.UTC)
	insert := func(table string, row map[string]any) error {
		_, err := shared.Apply(ctx, []*spanner.Mutation{spanner.InsertMap(table, row)})
		return err
	}
	with := func(row map[string]any, changes map[string]any) map[string]any {
		out := map[string]any{}
		for k, v := range row {
			out[k] = v
		}
		for k, v := range changes {
			out[k] = v
		}
		return out
	}
	newLease := func() (string, string) {
		workspace, lease := storetest.UniqueID("ws"), storetest.UniqueID("l")
		if err := insert("tr_lease", leaseRow(workspace, lease, expiry)); err != nil {
			t.Fatalf("a lease every check accepts is refused: %v", err)
		}
		return workspace, lease
	}
	draining := map[string]any{"state": "draining", "fence_time": expiry.Add(time.Minute), "drained_by": "owner"}
	leases := map[string]struct {
		changes map[string]any
		check   string
	}{
		"an unknown state":            {map[string]any{"state": "expired"}, "tr_lease_state"},
		"an unknown closer":           {with(draining, map[string]any{"state": "closed", "closed_at": expiry, "close_kind": "member"}), "tr_lease_kinds"},
		"a draining lease without F":  {map[string]any{"state": "draining", "drained_by": "owner"}, "tr_lease_fence"},
		"F before the expiry":         {with(draining, map[string]any{"fence_time": expiry.Add(-time.Second)}), "tr_lease_fence_after"},
		"S without T":                 {with(draining, map[string]any{"boundary_seq": int64(3)}), "tr_lease_boundary"},
		"S on an open lease":          {map[string]any{"boundary_seq": int64(3), "boundary_publish_time": expiry}, "tr_lease_boundary"},
		"closed without a time":       {with(draining, map[string]any{"state": "closed", "close_kind": "auditor"}), "tr_lease_closed"},
		"consumed beyond allocation":  {map[string]any{"consumed": int64(101)}, "tr_lease_room"},
		"an allocation not accounted": {map[string]any{"allocation": int64(101)}, "tr_lease_accounted"},
		"a negative return":           {map[string]any{"returned": int64(-5), "allocation": int64(105)}, "tr_lease_accounted"},
	}
	for name, c := range leases {
		err := insert("tr_lease", with(leaseRow(storetest.UniqueID("ws"), storetest.UniqueID("l"), expiry), c.changes))
		if err == nil || !strings.Contains(err.Error(), c.check) {
			t.Errorf("%s: want a refusal by %s, got %v", name, c.check, err)
		}
	}
	workspace, lease := newLease()
	// A lease ID is the lease's alone, across workspaces.
	if err := insert("tr_lease", leaseRow(storetest.UniqueID("ws"), lease, expiry)); err == nil ||
		!strings.Contains(err.Error(), "tr_lease_by_id") {
		t.Errorf("a second workspace's lease with the ID %s: want a refusal by tr_lease_by_id, got %v", lease, err)
	}
	key := map[string]any{"workspace_id": workspace, "lease_id": lease}
	children := []struct {
		name, table string
		row         map[string]any
		check       string
	}{
		{"a donor consumed beyond its allocation", "tr_lease_donor",
			with(key, map[string]any{"credit_shard": int64(0), "allocation": int64(10), "consumed": int64(11)}), "tr_lease_donor_room"},
		{"a snapshot without its charge", "tr_lease_hold",
			with(key, map[string]any{"authorization_id": "a1", "estimate": int64(5), "deadline": expiry, "snapshot_seq": int64(2)}), "tr_lease_hold_snapshot"},
		{"a pack deletable before its work is done", "tr_lease_winners",
			with(key, map[string]any{"commit_version": int64(1), "pack": []byte("{}"), "winner_count": int64(0), "deletable_at": expiry}), "tr_lease_winners_done"},
		{"a drain row of no kind", "tr_lease_drain",
			with(key, map[string]any{"authorization_id": "a1", "record_id": "r1", "kind": "adopt", "charge": int64(1),
				"estimate": int64(5), "money": []byte("{}"), "cause": "test", "commit_ts": spanner.CommitTimestamp}), "tr_lease_drain_kind"},
	}
	for _, c := range children {
		if err := insert(c.table, c.row); err == nil || !strings.Contains(err.Error(), c.check) {
			t.Errorf("%s: want a refusal by %s, got %v", c.name, c.check, err)
		}
	}
	others := []struct {
		name, table string
		row         map[string]any
		check       string
	}{
		{"a record of no kind", "tr_lease_record", map[string]any{"authorization_id": storetest.UniqueID("a"), "kind": "usage",
			"workspace_id": workspace, "lease_id": lease, "outcome": "settled", "body": []byte("{}")}, "tr_lease_record_kind"},
		{"a record of no outcome", "tr_lease_record", map[string]any{"authorization_id": storetest.UniqueID("a"), "kind": "generation",
			"workspace_id": workspace, "lease_id": lease, "outcome": "lost", "body": []byte("{}")}, "tr_lease_record_outcome"},
		{"a member of no state", "tr_fastpath_member", map[string]any{"address": storetest.UniqueID("node"), "epoch": int64(1),
			"roles": []string{"owner"}, "state": "gone", "started_at": expiry, "heartbeat_at": expiry}, "tr_fastpath_member_state"},
	}
	for _, c := range others {
		if err := insert(c.table, c.row); err == nil || !strings.Contains(err.Error(), c.check) {
			t.Errorf("%s: want a refusal by %s, got %v", c.name, c.check, err)
		}
	}
}

// TestTheEmulatorRunsTheStatementsTheStoreUses runs, once, each form of
// statement the store's later operations rest on, so that a form the
// emulator lacks shows here.
func TestTheEmulatorRunsTheStatementsTheStoreUses(t *testing.T) {
	spikeStore(t, Config{LiveFor: time.Second})
	ctx := context.Background()
	workspace, lease := storetest.UniqueID("ws"), storetest.UniqueID("l")
	expiry := time.Date(2026, 10, 7, 12, 0, 0, 0, time.UTC)
	_, err := shared.Apply(ctx, []*spanner.Mutation{
		spanner.InsertMap("tr_lease", leaseRow(workspace, lease, expiry)),
		spanner.InsertMap("tr_credit_balance", map[string]any{"workspace_id": workspace, "shard": int64(0), "total_credits": int64(1000)}),
	})
	if err != nil {
		t.Fatal(err)
	}
	var headroom int64
	var counts []int64
	var renewed time.Time
	resp, err := shared.ReadWriteTransactionWithOptions(ctx, func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		// A conditional update that returns what it wrote.
		iter := txn.Query(ctx, spanner.Statement{
			SQL: `UPDATE tr_credit_balance SET reserved = reserved + @x
			       WHERE workspace_id = @w AND shard = 0 AND total_credits - total_usage - reserved >= @x
			      THEN RETURN total_credits - total_usage - reserved`,
			Params: map[string]any{"w": workspace, "x": int64(300)},
		})
		if err := iter.Do(func(row *spanner.Row) error { return row.Column(0, &headroom) }); err != nil {
			return err
		}
		// A batch whose second statement changes no row, and an expiry
		// extended from Spanner's time, never backwards.
		var err error
		counts, err = txn.BatchUpdate(ctx, []spanner.Statement{
			{SQL: `UPDATE tr_lease SET expiry = GREATEST(expiry, TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL @ms MILLISECOND))
			        WHERE workspace_id = @w AND lease_id = @l AND state = 'open'`,
				Params: map[string]any{"w": workspace, "l": lease, "ms": int64(30000)}},
			{SQL: `UPDATE tr_lease SET revoked = TRUE WHERE workspace_id = @w AND lease_id = 'no-such-lease'`,
				Params: map[string]any{"w": workspace}},
		})
		if err != nil {
			return err
		}
		row, err := txn.ReadRow(ctx, "tr_lease", spanner.Key{workspace, lease}, []string{"expiry"})
		if err != nil {
			return err
		}
		if err := row.Column(0, &renewed); err != nil {
			return err
		}
		// Two drain rows with one commit timestamp, the second inserted
		// last, as the store writes them.
		_, err = txn.Update(ctx, spanner.Statement{
			SQL: `INSERT INTO tr_lease_drain (workspace_id, lease_id, authorization_id, record_id, kind, charge, estimate,
			        money, cause, commit_ts)
			      VALUES (@w, @l, 'a2', 'r-b', 'settle', 1, 5, b'{}', 'probe', PENDING_COMMIT_TIMESTAMP()),
			             (@w, @l, 'a1', 'r-a', 'refund', 0, 5, b'{}', 'probe', PENDING_COMMIT_TIMESTAMP())`,
			Params: map[string]any{"w": workspace, "l": lease},
		})
		return err
	}, spanner.TransactionOptions{TransactionTag: tag("probe")})
	if err != nil {
		t.Fatal(err)
	}
	if headroom != 700 {
		t.Errorf("THEN RETURN gave headroom %d, want 700", headroom)
	}
	if len(counts) != 2 || counts[0] != 1 || counts[1] != 0 {
		t.Errorf("the batch's counts are %v, want [1 0]", counts)
	}
	if !renewed.After(resp.CommitTs.Add(25 * time.Second)) {
		t.Errorf("the expiry read back is %v, not 30 s past the commit at %v", renewed, resp.CommitTs)
	}
	// The drain rows in commit order, then record ID, through the index.
	var order []string
	iter := shared.Single().Query(ctx, spanner.Statement{
		SQL: `SELECT record_id, commit_ts FROM tr_lease_drain@{FORCE_INDEX=tr_lease_drain_by_commit}
		       WHERE workspace_id = @w AND lease_id = @l ORDER BY commit_ts, record_id`,
		Params: map[string]any{"w": workspace, "l": lease},
	})
	err = iter.Do(func(row *spanner.Row) error {
		var id string
		var at time.Time
		if err := row.Columns(&id, &at); err != nil {
			return err
		}
		if !at.Equal(resp.CommitTs) {
			t.Errorf("row %s has commit_ts %v, not its commit's %v", id, at, resp.CommitTs)
		}
		order = append(order, id)
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if strings.Join(order, " ") != "r-a r-b" {
		t.Errorf("the drain rows read %v", order)
	}
}
