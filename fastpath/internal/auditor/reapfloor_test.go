package auditor

import (
	"testing"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/record"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// TestAHoldsRowWaitsForAStreamsReapFloor
// (docs/design/fast-admission-streams.md section 4.6 item 4): a heartbeat
// record or a hand-off's hold carrying a stream's reap deadline stores it as
// the row's deadline, the one the drain's reap and store.Reap wait for plus
// the grace; one without, from an owner of an earlier release or one not
// yet stating the field, stores the heartbeat's deadline, as before.
func TestAHoldsRowWaitsForAStreamsReapFloor(t *testing.T) {
	floor := deadline.Add(4 * time.Minute)
	put := func(t *testing.T, l *Lease) map[string]store.HoldRow {
		t.Helper()
		out := map[string]store.HoldRow{}
		for _, h := range l.Request().PutHolds {
			out[h.AuthorizationID] = h
		}
		return out
	}

	l := load(t, "open")
	floored := hb(2, "f", 1, 5)
	floored.Snapshot.ReapDeadline = floor
	apply(t, l, Applied, hb(1, "p", 1, 5), floored)
	rows := put(t, l)
	if got := rows["p"].Deadline; !got.Equal(deadline) {
		t.Fatalf("a row with no floor waits for %v, want the heartbeat's %v", got, deadline)
	}
	if got := rows["f"].Deadline; !got.Equal(floor) {
		t.Fatalf("a row with a floor waits for %v, want %v", got, floor)
	}
	// A later heartbeat moves the floor with the snapshot; one without
	// a floor (an owner from before the field) puts the deadline back.
	later := hb(3, "f", 2, 6)
	later.Snapshot.ReapDeadline = floor.Add(time.Minute)
	apply(t, l, Applied, later)
	if got := put(t, l)["f"].Deadline; !got.Equal(floor.Add(time.Minute)) {
		t.Fatalf("a later floor: %v", got)
	}
	apply(t, l, Applied, hb(4, "f", 3, 7))
	if got := put(t, l)["f"].Deadline; !got.Equal(deadline) {
		t.Fatalf("a later heartbeat without a floor: %v", got)
	}

	// A hand-off's hold carries its floor to the row the drain reaps from.
	snap := &record.Snapshot{GatewaySeq: 2, Hash: sum("h"), Usage: []byte(`{"tokens":2}`), Running: 9,
		Deadline: deadline, ReapDeadline: floor}
	held := []record.HeldHold{{Auth: "h", Estimate: 100, Deadline: deadline, Boot: boot, Snapshot: snap,
		SnapshotSeq: 1, Basis: []byte("terms")}, {Auth: "n", Estimate: 50, Deadline: deadline, Boot: boot}}
	m := load(t, "open")
	if err := m.LoadWinners(loaded("open").Lease, nil); err != nil {
		t.Fatal(err)
	}
	apply(t, m, Applied, hb(1, "h", 1, 1), chunkRec(2, held...), manifestRec(3, digestOf(t, held...), 2))
	rows = put(t, m)
	if got := rows["h"].Deadline; !got.Equal(floor) {
		t.Fatalf("a handed-off stream's row waits for %v, want its floor %v", got, floor)
	}
	if got := rows["n"].Deadline; !got.Equal(deadline) {
		t.Fatalf("a handed-off hold with no heartbeat waits for %v, want %v", got, deadline)
	}
}
