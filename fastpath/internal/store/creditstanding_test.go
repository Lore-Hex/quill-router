package store

import (
	"context"
	"errors"
	"math"
	"slices"
	"strings"
	"testing"

	"cloud.google.com/go/spanner"

	"github.com/Lore-Hex/quill-router/fastpath/internal/store/storetest"
)

// TestReadCreditStandingReadsEveryRowStrongly: each row's headroom in shard
// order (credit less usage less reservations, negative included), and the
// mark when any row carries it; a write committed just before the read is in
// it, as it is in Python's own. It is held so twice: by what the emulator
// answers, and in the source, where the read takes Single() with no
// timestamp bound, at low priority.
func TestReadCreditStandingReadsEveryRowStrongly(t *testing.T) {
	source := funcSource(t, "creditstanding.go", "func (s *Store) ReadCreditStanding(")
	if !strings.Contains(source, "ro := s.client.Single()\n") || strings.Contains(source, "TimestampBound") ||
		strings.Contains(source, "Staleness") || !strings.Contains(source, `lowPriority("read-credit-standing")`) {
		t.Fatal("ReadCreditStanding must be a strong single-use read at low priority")
	}
	s := spikeStore(t)
	ctx := context.Background()
	ws := seedWorkspace(t, 100, 0, 250)
	setRow(t, ws, 0, map[string]any{"total_usage": int64(30), "reserved": int64(20)})
	setRow(t, ws, 2, map[string]any{"total_usage": int64(300)})
	got, err := s.ReadCreditStanding(ctx, ws)
	if err != nil || !slices.Equal(got.Headroom, []int64{50, 0, -50}) || got.Marked {
		t.Fatalf("unmarked rows: %+v, %v; want [50 0 -50] unmarked", got, err)
	}
	// One more hold, then the mark on one row only: each is in the next read.
	setRow(t, ws, 0, map[string]any{"reserved": int64(45)})
	if got, err = s.ReadCreditStanding(ctx, ws); err != nil || !slices.Equal(got.Headroom, []int64{25, 0, -50}) || got.Marked {
		t.Fatalf("after a hold: %+v, %v; want [25 0 -50] unmarked", got, err)
	}
	setRow(t, ws, 1, map[string]any{"in_debt": true})
	if got, err = s.ReadCreditStanding(ctx, ws); err != nil || !slices.Equal(got.Headroom, []int64{25, 0, -50}) || !got.Marked {
		t.Fatalf("one row marked: %+v, %v; want [25 0 -50] marked", got, err)
	}
}

// TestReadCreditStandingRefusesRowsItCannotJudge: no workspace, a workspace
// with no rows, rows that are not shards 0 to n-1 and a row whose headroom
// is past int64 each read as an error, so the shadow settles nothing from them.
func TestReadCreditStandingRefusesRowsItCannotJudge(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	if _, err := s.ReadCreditStanding(ctx, ""); err == nil {
		t.Error("a read with no workspace")
	}
	if _, err := s.ReadCreditStanding(ctx, storetest.UniqueID("ws")); !errors.Is(err, ErrCreditRowsIncomplete) {
		t.Errorf("a workspace with no rows: %v", err)
	}
	gap := seedWorkspace(t, 100)
	if _, err := shared.Apply(ctx, []*spanner.Mutation{spanner.InsertMap("tr_credit_balance", map[string]any{
		"workspace_id": gap, "shard": int64(2), "total_credits": int64(5)})}); err != nil {
		t.Fatal(err)
	}
	if _, err := s.ReadCreditStanding(ctx, gap); !errors.Is(err, ErrCreditRowsIncomplete) {
		t.Errorf("shards 0 and 2: %v", err)
	}
	// Usage at the top of int64 beside a reservation: the row's headroom is
	// past int64, which the read's own subtraction refuses. The row is
	// deleted when the test ends: the emulator evaluates another test's
	// condition on every row it scans (bounds_test.go).
	past := seedWorkspace(t, 0)
	dropWorkspace(t, past)
	setRow(t, past, 0, map[string]any{"total_usage": int64(math.MaxInt64), "reserved": int64(2)})
	if got, err := s.ReadCreditStanding(ctx, past); err == nil {
		t.Errorf("a headroom past int64 read as %+v", got)
	}
}
