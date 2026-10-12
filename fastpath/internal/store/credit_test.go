package store

import (
	"context"
	"errors"
	"fmt"
	"slices"
	"strings"
	"testing"
	"time"

	"cloud.google.com/go/spanner"

	"github.com/Lore-Hex/quill-router/fastpath/internal/store/storetest"
)

// seedWorkspace writes a workspace's credit rows, one per amount of credit,
// at tier 3, unmarked, nothing used or reserved, and returns its ID.
func seedWorkspace(t *testing.T, credits ...int64) string {
	t.Helper()
	workspace := storetest.UniqueID("ws")
	// Enabled for the fast path, and the tests' owner a member serving, so
	// its grants are taken (workspace.go, grant.go).
	mutations := []*spanner.Mutation{storetest.Enabled(workspace), storetest.Member("node-a", 1)}
	for shard, c := range credits {
		mutations = append(mutations, spanner.InsertMap("tr_credit_balance", map[string]any{
			"workspace_id": workspace, "shard": int64(shard), "total_credits": c, "trust_tier": int64(3)}))
	}
	if _, err := shared.Apply(context.Background(), mutations); err != nil {
		t.Fatal(err)
	}
	return workspace
}

// setRows sets columns on every credit row of a workspace, and setRow on
// one. Both write with DML: on the emulator, an update mutation resets the
// columns it does not name to their defaults (store.go).
func setRows(t *testing.T, workspace string, values map[string]any) {
	t.Helper()
	setWhere(t, workspace, -1, values)
}

func setRow(t *testing.T, workspace string, shard int64, values map[string]any) {
	t.Helper()
	setWhere(t, workspace, shard, values)
}

func setWhere(t *testing.T, workspace string, shard int64, values map[string]any) {
	t.Helper()
	params := map[string]any{"w": workspace, "shard": shard}
	var set []string
	for column, v := range values {
		set = append(set, column+" = @"+column)
		params[column] = v
	}
	_, err := shared.ReadWriteTransaction(context.Background(), func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		_, err := txn.Update(ctx, spanner.Statement{
			SQL: "UPDATE tr_credit_balance SET " + strings.Join(set, ", ") +
				" WHERE workspace_id = @w AND (@shard < 0 OR shard = @shard)",
			Params: params,
		})
		return err
	})
	if err != nil {
		t.Fatal(err)
	}
}

// row is a credit row as a test reads it.
type row struct {
	credits, usage, reserved int64
	marked                   bool
}

func (r row) headroom() int64 { return r.credits - r.usage - r.reserved }

func readRows(t *testing.T, workspace string) []row {
	t.Helper()
	var rows []row
	err := shared.Single().Query(context.Background(), spanner.Statement{
		SQL: `SELECT total_credits, total_usage, reserved, COALESCE(in_debt, FALSE) FROM tr_credit_balance
		       WHERE workspace_id = @w ORDER BY shard`,
		Params: map[string]any{"w": workspace},
	}).Do(func(r *spanner.Row) error {
		var x row
		if err := r.Columns(&x.credits, &x.usage, &x.reserved, &x.marked); err != nil {
			return err
		}
		rows = append(rows, x)
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	return rows
}

// creditShards says how the workspace's credit rows differ from the shards
// seeded, 0 to n-1, if they do: a row dropped, added or renumbered is seen
// before any value is compared, since readRows returns only what is there.
func creditShards(workspace string, n int) error {
	var got []int64
	err := shared.Single().Query(context.Background(), spanner.Statement{
		SQL:    `SELECT shard FROM tr_credit_balance WHERE workspace_id = @w ORDER BY shard`,
		Params: map[string]any{"w": workspace},
	}).Do(func(r *spanner.Row) error {
		var shard int64
		if err := r.Column(0, &shard); err != nil {
			return err
		}
		got = append(got, shard)
		return nil
	})
	if err != nil {
		return err
	}
	want := make([]int64, n)
	for i := range want {
		want[i] = int64(i)
	}
	if !slices.Equal(got, want) {
		return fmt.Errorf("the credit rows are shards %v, and %v were seeded", got, want)
	}
	return nil
}

// Metadata the store neither reads nor writes, seeded on every credit row
// of a walk's workspace so a write that reset a column to its default, or
// copied one into another, is seen: the tier it reads, a computed time, an
// epoch, and a reconciled-through time an hour on. By DML: an update
// mutation on the emulator resets the row's other defaulted columns.
const seededEpoch = 17

func seedCreditMetadata(t testing.TB, workspace string) {
	t.Helper()
	_, err := shared.ReadWriteTransaction(context.Background(), func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		_, err := txn.Update(ctx, spanner.Statement{
			SQL: `UPDATE tr_credit_balance SET trust_computed_at = @t, pause_epoch = @e, trust_reconciled_through = @u
			       WHERE workspace_id = @w`,
			Params: map[string]any{"t": walkTick, "u": walkTick.Add(time.Hour), "e": int64(seededEpoch), "w": workspace},
		})
		return err
	})
	if err != nil {
		t.Fatalf("seeding the credit rows' metadata: %v", err)
	}
}

// creditMetadata says how a credit row's columns other than its money and
// mark differ from what seedWorkspace and seedCreditMetadata left, if they
// do: the tier, the seeded times and epoch, and nothing else set.
func creditMetadata(workspace string) error {
	type meta struct {
		shard    int64
		tier     spanner.NullInt64
		computed spanner.NullTime
		latched  spanner.NullTime
		override spanner.NullInt64
		causes   []spanner.NullString
		epoch    spanner.NullInt64
		through  spanner.NullTime
		source   spanner.NullTime
	}
	var problem error
	err := shared.Single().Query(context.Background(), spanner.Statement{
		SQL: `SELECT shard, trust_tier, trust_computed_at, trust_latched_at, trust_override_tier, billing_pause_causes,
		             pause_epoch, trust_reconciled_through, source_updated_at
		        FROM tr_credit_balance WHERE workspace_id = @w ORDER BY shard`,
		Params: map[string]any{"w": workspace},
	}).Do(func(r *spanner.Row) error {
		var m meta
		if err := r.Columns(&m.shard, &m.tier, &m.computed, &m.latched, &m.override, &m.causes, &m.epoch, &m.through,
			&m.source); err != nil {
			return err
		}
		if m.tier != (spanner.NullInt64{Int64: 3, Valid: true}) || !m.computed.Valid || !m.computed.Time.Equal(walkTick) ||
			m.latched.Valid || m.override.Valid || len(m.causes) != 0 ||
			m.epoch != (spanner.NullInt64{Int64: seededEpoch, Valid: true}) ||
			!m.through.Valid || !m.through.Time.Equal(walkTick.Add(time.Hour)) || m.source.Valid {
			problem = fmt.Errorf("shard %d's other columns are %+v, and the walk seeded tier 3, epoch %d, computed at %v and reconciled an hour on",
				m.shard, m, seededEpoch, walkTick)
		}
		return nil
	})
	if err != nil {
		return err
	}
	return problem
}

// creditRowsText is every column of the workspace's credit rows, in shard
// order, as text: held equal across a write that must leave them alone.
func creditRowsText(workspace string) (string, error) {
	var b strings.Builder
	err := shared.Single().Query(context.Background(), spanner.Statement{
		SQL:    `SELECT * FROM tr_credit_balance WHERE workspace_id = @w ORDER BY shard`,
		Params: map[string]any{"w": workspace},
	}).Do(func(r *spanner.Row) error {
		for i, name := range r.ColumnNames() {
			var v spanner.GenericColumnValue
			if err := r.Column(i, &v); err != nil {
				return err
			}
			fmt.Fprintf(&b, "%s=%v ", name, v.Value.AsInterface())
		}
		b.WriteString("; ")
		return nil
	})
	return b.String(), err
}

func headrooms(rows []row) []int64 {
	out := make([]int64, len(rows))
	for i, r := range rows {
		out[i] = r.headroom()
	}
	return out
}

func marks(rows []row) []bool {
	out := make([]bool, len(rows))
	for i, r := range rows {
		out[i] = r.marked
	}
	return out
}

// inTransaction runs f in a read-write transaction of the shared database.
func inTransaction(t *testing.T, f func(context.Context, *spanner.ReadWriteTransaction) error) error {
	t.Helper()
	_, err := shared.ReadWriteTransaction(context.Background(), f)
	return err
}

func TestARaiseThatLeavesARowNegativeIsCovered(t *testing.T) {
	spikeStore(t)
	ws := seedWorkspace(t, 10, 50, 50)
	err := inTransaction(t, func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		headroom, err := adjust(ctx, txn, ws, 0, 40, 0, "test")
		if err == nil && headroom != -30 {
			t.Errorf("the raise returned headroom %d, want -30", headroom)
		}
		return err
	})
	if err != nil {
		t.Fatal(err)
	}
	rows := readRows(t, ws)
	if got := headrooms(rows); !slices.Equal(got, []int64{0, 20, 50}) || slices.Contains(marks(rows), true) {
		t.Fatalf("after the raise: headroom %v, marks %v; want [0 20 50] unmarked, covered from shard 1", got, marks(rows))
	}
	if rows[0].reserved != 40 || rows[0].credits != 40 || rows[1].credits != 20 {
		t.Fatalf("the raise moved %+v", rows)
	}
}

func TestARaiseThatLeavesTheSumNegativeMarksEveryRow(t *testing.T) {
	spikeStore(t)
	ws := seedWorkspace(t, 10, 10)
	err := inTransaction(t, func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		_, err := adjust(ctx, txn, ws, 0, 30, 0, "test")
		return err
	})
	if err != nil {
		t.Fatal(err)
	}
	rows := readRows(t, ws)
	if got := headrooms(rows); !slices.Equal(got, []int64{-20, 10}) || !slices.Equal(marks(rows), []bool{true, true}) {
		t.Fatalf("after the raise: headroom %v, marks %v; want [-20 10], every row marked, nothing moved", got, marks(rows))
	}
}

func TestAMatchedBookingKeepsTheHeadroom(t *testing.T) {
	spikeStore(t)
	ws := seedWorkspace(t, 100)
	err := inTransaction(t, func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		if ok, err := reserve(ctx, txn, ws, 0, 40, "test"); err != nil || !ok {
			t.Fatalf("reserve: %v %v", ok, err)
		}
		_, err := adjust(ctx, txn, ws, 0, -25, 25, "test")
		return err
	})
	if err != nil {
		t.Fatal(err)
	}
	if rows := readRows(t, ws); rows[0] != (row{credits: 100, usage: 25, reserved: 15}) {
		t.Fatalf("after booking 25 of 40 reserved: %+v", rows[0])
	}
}

func TestAFaultBookedAsUsageIsCovered(t *testing.T) {
	spikeStore(t)
	ws := seedWorkspace(t, 10, 100)
	err := inTransaction(t, func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		if ok, err := reserve(ctx, txn, ws, 0, 10, "test"); err != nil || !ok {
			t.Fatalf("reserve: %v %v", ok, err)
		}
		// A charge of 15 against an allocation of 10: 10 matched, 5 a fault.
		_, err := adjust(ctx, txn, ws, 0, -10, 15, "test")
		return err
	})
	if err != nil {
		t.Fatal(err)
	}
	rows := readRows(t, ws)
	if got := headrooms(rows); !slices.Equal(got, []int64{0, 95}) || slices.Contains(marks(rows), true) {
		t.Fatalf("after the fault: headroom %v, marks %v; want [0 95] unmarked", got, marks(rows))
	}
}

func TestAReleaseOnAMarkedWorkspaceRepaysDebtFirst(t *testing.T) {
	spikeStore(t)
	// Shard 0 owes 30; shard 1 reserves 20 it does not have: signed -50.
	ws := seedWorkspace(t, 0, 0)
	setRows(t, ws, map[string]any{"in_debt": true})
	setRow(t, ws, 0, map[string]any{"total_usage": int64(30)})
	setRow(t, ws, 1, map[string]any{"reserved": int64(20)})
	release20 := func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		return release(ctx, txn, ws, 1, 20, "test")
	}
	if err := inTransaction(t, release20); err != nil {
		t.Fatal(err)
	}
	rows := readRows(t, ws)
	// The 20 freed on shard 1 repays shard 0 first; the sum is still -30.
	if got := headrooms(rows); !slices.Equal(got, []int64{-10, -20}) || !slices.Equal(marks(rows), []bool{true, true}) {
		t.Fatalf("after the release: headroom %v, marks %v; want [-10 -20], marked", got, marks(rows))
	}

	// Shard 0 owes 20; shard 1 holds 10 and reserves 40: signed -10.
	ws = seedWorkspace(t, 0, 50)
	setRows(t, ws, map[string]any{"in_debt": true})
	setRow(t, ws, 0, map[string]any{"total_usage": int64(20)})
	setRow(t, ws, 1, map[string]any{"reserved": int64(40)})
	err := inTransaction(t, func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		return release(ctx, txn, ws, 1, 40, "test")
	})
	if err != nil {
		t.Fatal(err)
	}
	rows = readRows(t, ws)
	// 20 of the 40 repays shard 0, the mark clears, and 20 stays on shard 1.
	if got := headrooms(rows); !slices.Equal(got, []int64{0, 30}) || slices.Contains(marks(rows), true) {
		t.Fatalf("after the release: headroom %v, marks %v; want [0 30], unmarked", got, marks(rows))
	}
}

func TestCreditRowsMustBeShardsZeroToN(t *testing.T) {
	spikeStore(t)
	ws := storetest.UniqueID("ws")
	if _, err := shared.Apply(context.Background(), []*spanner.Mutation{
		spanner.InsertMap("tr_credit_balance", map[string]any{"workspace_id": ws, "shard": int64(0), "total_credits": int64(10)}),
		spanner.InsertMap("tr_credit_balance", map[string]any{"workspace_id": ws, "shard": int64(2), "total_credits": int64(10)}),
	}); err != nil {
		t.Fatal(err)
	}
	err := inTransaction(t, func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		_, err := squareCreditRows(ctx, txn, ws, "test")
		return err
	})
	if !errors.Is(err, ErrCreditRowsIncomplete) {
		t.Fatalf("rows 0 and 2: %v", err)
	}
}

func TestAWriteToARowThatChangedStopsTheTransaction(t *testing.T) {
	spikeStore(t)
	ws := seedWorkspace(t, 10, 10)
	err := inTransaction(t, func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		stale := creditRows{headroom: []int64{11, 10}, marks: []bool{false, false}}
		return writeCreditRows(ctx, txn, ws, stale, []int64{0, 21}, false, "test")
	})
	if !errors.Is(err, ErrCreditRowsChanged) {
		t.Fatalf("a write from a stale read: %v", err)
	}
	if got := headrooms(readRows(t, ws)); !slices.Equal(got, []int64{10, 10}) {
		t.Fatalf("the stopped transaction wrote %v", got)
	}
}
