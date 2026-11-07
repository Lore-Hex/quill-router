package store

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"cloud.google.com/go/spanner"

	"github.com/Lore-Hex/quill-router/fastpath/internal/store/storetest"
	"github.com/Lore-Hex/quill-router/fastpath/internal/switchsql"
)

// The holds' tests (docs/design/fast-admission-widening.md, section 5.6, and
// PR 4's tests in section 11). The hold table's own rows, '*', '#pause' and
// '#armed', reach every workspace, so each test has a database of its own.

// holdStore is a store of a database of the test's own, and its client.
func holdStore(t *testing.T) (*Store, *spanner.Client) {
	t.Helper()
	if emulator == nil {
		t.Skip(skipped)
	}
	db, err := emulator.Database(context.Background(), storetest.UniqueID("holds"), nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(db.Close)
	s, err := New(db, testConfig())
	if err != nil {
		t.Fatal(err)
	}
	return s, db
}

// heldRow is a row of the hold table as read.
type heldRow struct {
	reason string
	detail spanner.NullString
	heldAt time.Time
}

// holdsOf reads the hold table whole.
func holdsOf(t *testing.T, db *spanner.Client) map[string]heldRow {
	t.Helper()
	out := map[string]heldRow{}
	err := db.Single().Query(context.Background(), spanner.Statement{
		SQL: `SELECT workspace_id, reason, detail, held_at FROM tr_fastpath_workspace_hold`,
	}).Do(func(r *spanner.Row) error {
		var w string
		var h heldRow
		if err := r.Columns(&w, &h.reason, &h.detail, &h.heldAt); err != nil {
			return err
		}
		h.heldAt = h.heldAt.UTC()
		out[w] = h
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	return out
}

// switchOf reads a workspace's switch row.
func switchOf(t *testing.T, db *spanner.Client, w string) (bool, time.Time) {
	t.Helper()
	row, err := db.Single().ReadRow(context.Background(), "tr_fastpath_workspace", spanner.Key{w},
		[]string{"enabled", "changed_at"})
	if err != nil {
		t.Fatalf("%s's switch: %v", w, err)
	}
	var on bool
	var at time.Time
	if err := row.Columns(&on, &at); err != nil {
		t.Fatal(err)
	}
	return on, at.UTC()
}

// killsOf reads tr_fastpath_kill whole, in order.
func killsOf(t *testing.T, db *spanner.Client) []time.Time {
	t.Helper()
	var out []time.Time
	err := db.Single().Query(context.Background(), spanner.Statement{
		SQL: `SELECT kill_at FROM tr_fastpath_kill ORDER BY kill_at`,
	}).Do(func(r *spanner.Row) error {
		var at time.Time
		if err := r.Column(0, &at); err != nil {
			return err
		}
		out = append(out, at.UTC())
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	return out
}

// switchesOf reads the switch whole, by workspace.
func switchesOf(t *testing.T, db *spanner.Client) map[string]time.Time {
	t.Helper()
	out := map[string]time.Time{}
	err := db.Single().Query(context.Background(), spanner.Statement{
		SQL: `SELECT workspace_id, enabled, changed_at FROM tr_fastpath_workspace`,
	}).Do(func(r *spanner.Row) error {
		var w string
		var on bool
		var at time.Time
		if err := r.Columns(&w, &on, &at); err != nil {
			return err
		}
		if on {
			w += "+on"
		}
		out[w] = at.UTC()
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	return out
}

func on(t *testing.T, s *Store, ws ...string) {
	t.Helper()
	for _, w := range ws {
		if _, _, err := s.SetWorkspace(context.Background(), w, true, Reason{}); err != nil {
			t.Fatal(err)
		}
	}
}

func off(t *testing.T, s *Store, w string) time.Time {
	t.Helper()
	_, at, err := s.SetWorkspace(context.Background(), w, false, Reason{Kind: HoldOperator})
	if err != nil {
		t.Fatal(err)
	}
	return at
}

func unheldOff(t *testing.T, db *spanner.Client, w string) time.Time {
	t.Helper()
	at, err := storetest.UnheldDisable(context.Background(), db, w)
	if err != nil {
		t.Fatal(err)
	}
	return at
}

// TestADisableHoldsAndAnEnableReleases: turning a workspace off holds it in
// the switch's transaction, with its caller's reason and detail, held at the
// switch's changed_at; turning it on removes the hold in its transaction; a
// stop without a reason of a stop's, or an enable with one, is refused and
// writes nothing.
func TestADisableHoldsAndAnEnableReleases(t *testing.T) {
	s, db := holdStore(t)
	ctx := context.Background()
	w, watched := storetest.UniqueID("ws"), storetest.UniqueID("ws")
	on(t, s, w, watched)
	at := off(t, s, w)
	h, ok := holdsOf(t, db)[w]
	if _, changed := switchOf(t, db, w); !ok || h.reason != HoldOperator || h.detail.Valid || !h.heldAt.Equal(at) ||
		!changed.Equal(at) {
		t.Fatalf("after disable: hold %+v %v, changed_at %v, commit %v", h, ok, changed, at)
	}
	_, at, err := s.SetWorkspace(ctx, watched, false, Reason{Kind: HoldWatch, Detail: "Spanner CPU 0.5 past 0.35"})
	if err != nil {
		t.Fatal(err)
	}
	if h := holdsOf(t, db)[watched]; h.reason != HoldWatch || h.detail.StringVal != "Spanner CPU 0.5 past 0.35" ||
		!h.heldAt.Equal(at) {
		t.Fatalf("a watch's hold: %+v", h)
	}
	_, at, err = s.SetWorkspace(ctx, w, true, Reason{})
	if err != nil {
		t.Fatal(err)
	}
	if _, held := holdsOf(t, db)[w]; held {
		t.Fatal("enable left the workspace's hold")
	}
	if enabled, changed := switchOf(t, db, w); !enabled || !changed.Equal(at) {
		t.Fatalf("after enable: %v at %v, commit %v", enabled, changed, at)
	}
	before := holdsOf(t, db)
	for _, reason := range []Reason{{}, {Kind: HoldBackfill}, {Kind: HoldPaused}, {Kind: "kill"}, {Kind: "nope"},
		{Detail: "no kind"}} {
		if _, _, err := s.SetWorkspace(ctx, w, false, reason); err == nil {
			t.Errorf("a stop with the reason %+v was taken", reason)
		}
	}
	if _, _, err := s.SetWorkspace(ctx, watched, true, Reason{Kind: HoldOperator}); err == nil {
		t.Error("an enable with a reason was taken")
	}
	if enabled, _ := switchOf(t, db, w); !enabled || len(holdsOf(t, db)) != len(before) {
		t.Fatal("a refused switch write wrote")
	}
}

// TestTheHoldTablesOwnRowsAreNoWorkspace: enable, disable and release
// refuse '*', '#pause' and '#armed', and any id beginning as they do, and
// write nothing: turning '#pause' on would remove the pause.
func TestTheHoldTablesOwnRowsAreNoWorkspace(t *testing.T) {
	s, db := holdStore(t)
	ctx := context.Background()
	if _, err := s.Pause(ctx); err != nil {
		t.Fatal(err)
	}
	if _, _, _, err := s.DisableAll(ctx); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Apply(ctx, []*spanner.Mutation{holdRow(ArmedRow, holdArmed, "")}); err != nil {
		t.Fatal(err)
	}
	before := holdsOf(t, db)
	for _, id := range []string{FleetHold, PauseRow, ArmedRow, "*x", "#other"} {
		for _, enabled := range []bool{true, false} {
			reason := Reason{}
			if !enabled {
				reason.Kind = HoldOperator
			}
			if _, _, err := s.SetWorkspace(ctx, id, enabled, reason); err == nil {
				t.Errorf("SetWorkspace(%q, %v) was taken", id, enabled)
			}
		}
		if _, err := s.Release(ctx, id); err == nil {
			t.Errorf("Release(%q) was taken", id)
		}
	}
	if after := holdsOf(t, db); len(after) != len(before) || len(switchesOf(t, db)) != 0 {
		t.Fatalf("the refusals wrote: holds %v, then %v; switch %v", before, after, switchesOf(t, db))
	}
}

// TestDisableAllWritesTheFleetHoldTheSwitchAndTheRevocationsInOneCommit:
// one disable-all turns off every enabled row, revokes every open lease,
// writes the fleet hold and a kill row, all at one commit timestamp, which
// every row it turned off keeps as its changed_at; a row stopped before it
// keeps its own. A second disable-all has a kill row of its own, equal to
// the changed_at of the rows it turned off, and leaves the fleet hold's
// first time.
func TestDisableAllWritesTheFleetHoldTheSwitchAndTheRevocationsInOneCommit(t *testing.T) {
	s, db := holdStore(t)
	ctx := context.Background()
	a, b, before, later := storetest.UniqueID("ws"), storetest.UniqueID("ws"), storetest.UniqueID("ws"),
		storetest.UniqueID("ws")
	var leases []LeaseRef
	for _, w := range []string{a, b} {
		if _, err := db.Apply(ctx, []*spanner.Mutation{storetest.Enabled(w), storetest.Member("node-a", 1),
			spanner.InsertMap("tr_credit_balance", map[string]any{"workspace_id": w, "shard": int64(0),
				"total_credits": int64(100), "trust_tier": int64(3)})}); err != nil {
			t.Fatal(err)
		}
		req := grantOf(w, 10)
		if got, err := s.Grant(ctx, req); err != nil || got.Refused != "" {
			t.Fatalf("the grant: %+v %v", got, err)
		}
		leases = append(leases, LeaseRef{w, req.LeaseID})
	}
	on(t, s, before)
	stoppedAt := off(t, s, before)
	turnedOff, revoked, at, err := s.DisableAll(ctx)
	if err != nil || turnedOff != 2 || revoked != 2 {
		t.Fatalf("disable-all: %d, %d, %v", turnedOff, revoked, err)
	}
	holds := holdsOf(t, db)
	if h := holds[FleetHold]; h.reason != "kill" || !h.heldAt.Equal(at) {
		t.Fatalf("the fleet hold: %+v, the commit %v", h, at)
	}
	if kills := killsOf(t, db); len(kills) != 1 || !kills[0].Equal(at) {
		t.Fatalf("the kill rows: %v, the commit %v", kills, at)
	}
	for _, w := range []string{a, b} {
		if enabled, changed := switchOf(t, db, w); enabled || !changed.Equal(at) {
			t.Fatalf("%s after the kill: %v at %v, the commit %v", w, enabled, changed, at)
		}
		if _, held := holds[w]; held {
			t.Fatalf("the kill held %s one by one", w)
		}
	}
	if _, changed := switchOf(t, db, before); !changed.Equal(stoppedAt) {
		t.Fatalf("a row stopped before the kill: changed_at %v, stopped at %v", changed, stoppedAt)
	}
	for _, ref := range leases {
		if l := readLease(t, s, ref); !l.Revoked {
			t.Fatalf("lease %s not revoked", ref.LeaseID)
		}
	}
	on(t, s, later)
	_, _, second, err := s.DisableAll(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if kills := killsOf(t, db); len(kills) != 2 || !kills[0].Equal(at) || !kills[1].Equal(second) || second.Equal(at) {
		t.Fatalf("the kill rows after two: %v; the commits %v and %v", kills, at, second)
	}
	if _, changed := switchOf(t, db, later); !changed.Equal(second) {
		t.Fatalf("the second kill's row: changed_at %v, the commit %v", changed, second)
	}
	if _, changed := switchOf(t, db, a); !changed.Equal(at) {
		t.Fatal("the second kill moved the first kill's rows")
	}
	if h := holdsOf(t, db)[FleetHold]; !h.heldAt.Equal(at) {
		t.Fatalf("the fleet hold after a second kill: %+v, want the first's time %v", h, at)
	}
}

// TestReleaseAllRemovesTheFleetHoldAndEveryKillInOneCommit: release-all
// deletes '*' and every kill row in one transaction and leaves every other
// hold and the switch as they were; run again it finds nothing.
func TestReleaseAllRemovesTheFleetHoldAndEveryKillInOneCommit(t *testing.T) {
	s, db := holdStore(t)
	ctx := context.Background()
	w, held := storetest.UniqueID("ws"), storetest.UniqueID("ws")
	on(t, s, w, held)
	off(t, s, held)
	for range 2 {
		if _, _, _, err := s.DisableAll(ctx); err != nil {
			t.Fatal(err)
		}
	}
	switches := switchesOf(t, db)
	got, err := s.ReleaseAll(ctx)
	if err != nil || !got.Fleet || got.Kills != 2 || got.Paused {
		t.Fatalf("release-all: %+v %v", got, err)
	}
	holds := holdsOf(t, db)
	if _, ok := holds[FleetHold]; ok || len(killsOf(t, db)) != 0 {
		t.Fatalf("after release-all: holds %v, kills %v", holds, killsOf(t, db))
	}
	if h := holds[held]; h.reason != HoldOperator || len(holds) != 1 {
		t.Fatalf("release-all touched a workspace's hold: %v", holds)
	}
	if after := switchesOf(t, db); !mapsEqual(after, switches) {
		t.Fatalf("release-all wrote the switch: %v, then %v", switches, after)
	}
	if again, err := s.ReleaseAll(ctx); err != nil || again.Fleet || again.Kills != 0 {
		t.Fatalf("release-all again: %+v %v", again, err)
	}
}

func mapsEqual(a, b map[string]time.Time) bool {
	if len(a) != len(b) {
		return false
	}
	for k, v := range a {
		if w, ok := b[k]; !ok || !w.Equal(v) {
			return false
		}
	}
	return true
}

// TestReleasesUnderAPauseAreRefused: release-all while '#pause' stands
// writes nothing, '*', the kill rows and every switch row as they were; and
// so does release of a workspace's hold, the hold left as it was.
func TestReleasesUnderAPauseAreRefused(t *testing.T) {
	s, db := holdStore(t)
	ctx := context.Background()
	w, held := storetest.UniqueID("ws"), storetest.UniqueID("ws")
	on(t, s, w, held)
	off(t, s, held)
	if _, _, _, err := s.DisableAll(ctx); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Pause(ctx); err != nil {
		t.Fatal(err)
	}
	holds, kills, switches := holdsOf(t, db), killsOf(t, db), switchesOf(t, db)
	got, err := s.ReleaseAll(ctx)
	if err != nil || !got.Paused || got.Fleet || got.Kills != 0 {
		t.Fatalf("release-all under a pause: %+v %v", got, err)
	}
	released, err := s.Release(ctx, held)
	if err != nil || !released.Paused || released.Released {
		t.Fatalf("release under a pause: %+v %v", released, err)
	}
	if after := holdsOf(t, db); len(after) != len(holds) || after[FleetHold] != holds[FleetHold] ||
		after[held] != holds[held] || after[PauseRow] != holds[PauseRow] {
		t.Fatalf("a refused release wrote the holds: %v, then %v", holds, after)
	}
	if after := killsOf(t, db); !slices.EqualFunc(after, kills, time.Time.Equal) {
		t.Fatalf("a refused release-all wrote the kills: %v, then %v", kills, after)
	}
	if after := switchesOf(t, db); !mapsEqual(after, switches) {
		t.Fatalf("a refused release wrote the switch: %v, then %v", switches, after)
	}
}

// TestAReleaseRemovesAHoldWithoutEnabling: the hold goes, the switch stays
// off; a workspace with no hold is released of nothing.
func TestAReleaseRemovesAHoldWithoutEnabling(t *testing.T) {
	s, db := holdStore(t)
	ctx := context.Background()
	w := storetest.UniqueID("ws")
	on(t, s, w)
	stopped := off(t, s, w)
	got, err := s.Release(ctx, w)
	if err != nil || !got.Released || got.Paused {
		t.Fatalf("release: %+v %v", got, err)
	}
	if _, held := holdsOf(t, db)[w]; held {
		t.Fatal("the hold is still there")
	}
	if enabled, changed := switchOf(t, db, w); enabled || !changed.Equal(stopped) {
		t.Fatalf("release wrote the switch: %v at %v", enabled, changed)
	}
	if again, err := s.Release(ctx, w); err != nil || again.Released {
		t.Fatalf("release again: %+v %v", again, err)
	}
}

// TestAPauseIsHeldAtItsCommitTimestamp: the pause's held_at is its
// commit timestamp, on the switch's clock: after a changed_at committed just
// before it and before one committed just after it. A second pause writes
// nothing and keeps the first's time.
func TestAPauseIsHeldAtItsCommitTimestamp(t *testing.T) {
	s, db := holdStore(t)
	ctx := context.Background()
	before, after := storetest.UniqueID("ws"), storetest.UniqueID("ws")
	on(t, s, before)
	_, justBefore := switchOf(t, db, before)
	got, err := s.Pause(ctx)
	if err != nil || !got.Paused {
		t.Fatalf("pause: %+v %v", got, err)
	}
	on(t, s, after)
	_, justAfter := switchOf(t, db, after)
	h := holdsOf(t, db)[PauseRow]
	if h.reason != "pause" || !h.heldAt.Equal(got.HeldAt) || !justBefore.Before(h.heldAt) || !h.heldAt.Before(justAfter) {
		t.Fatalf("the pause %+v, its commit %v; changed_at %v before and %v after", h, got.HeldAt, justBefore, justAfter)
	}
	again, err := s.Pause(ctx)
	if err != nil || again.Paused || !again.HeldAt.Equal(h.heldAt) {
		t.Fatalf("a second pause: %+v %v, want the first's %v", again, err, h.heldAt)
	}
	if h2 := holdsOf(t, db)[PauseRow]; h2 != h {
		t.Fatalf("a second pause wrote the pause: %+v, then %+v", h, h2)
	}
}

// TestHoldBackfillHoldsEveryUnheldDisabledRowOnce: it holds, reason
// backfill, every row turned off with no hold of its own, leaves enabled rows
// and rows already held as they were, and a second run writes nothing. Once
// '#armed' stands it refuses, writing nothing.
func TestHoldBackfillHoldsEveryUnheldDisabledRowOnce(t *testing.T) {
	s, db := holdStore(t)
	ctx := context.Background()
	enabled, u1, u2, held := storetest.UniqueID("ws"), storetest.UniqueID("ws"), storetest.UniqueID("ws"),
		storetest.UniqueID("ws")
	on(t, s, enabled, u1, u2, held)
	unheldOff(t, db, u1)
	unheldOff(t, db, u2)
	off(t, s, held)
	operator := holdsOf(t, db)[held]
	got, err := s.HoldBackfill(ctx)
	slices.Sort(got.Held)
	want := []string{u1, u2}
	slices.Sort(want)
	if err != nil || got.Armed || !slices.Equal(got.Held, want) {
		t.Fatalf("hold-backfill: %+v %v, want %v held", got, err, want)
	}
	holds := holdsOf(t, db)
	for _, w := range want {
		if h := holds[w]; h.reason != HoldBackfill || !h.heldAt.Equal(got.At) {
			t.Fatalf("%s's hold: %+v, the commit %v", w, h, got.At)
		}
	}
	if _, ok := holds[enabled]; ok || holds[held] != operator || len(holds) != 3 {
		t.Fatalf("hold-backfill held an enabled row or rewrote a held one: %v", holds)
	}
	again, err := s.HoldBackfill(ctx)
	if err != nil || len(again.Held) != 0 {
		t.Fatalf("a second backfill: %+v %v", again, err)
	}
	if after := holdsOf(t, db); len(after) != len(holds) || after[u1] != holds[u1] || after[u2] != holds[u2] {
		t.Fatalf("a second backfill wrote: %v, then %v", holds, after)
	}
	if armed, err := s.Arm(ctx); err != nil || !armed.Armed {
		t.Fatalf("arm: %+v %v", armed, err)
	}
	unheldOff(t, db, enabled)
	refused, err := s.HoldBackfill(ctx)
	if err != nil || !refused.Armed || len(refused.Held) != 0 {
		t.Fatalf("hold-backfill once armed: %+v %v", refused, err)
	}
	if _, ok := holdsOf(t, db)[enabled]; ok {
		t.Fatal("hold-backfill held a row once armed")
	}
}

// TestArmRefusesWhileAnyDisabledRowLacksAHold: arm refuses, writing
// nothing, while a row is off with no hold of its own, the fleet hold not
// counted, and lists it; once held, it arms, and arming again keeps the arm.
func TestArmRefusesWhileAnyDisabledRowLacksAHold(t *testing.T) {
	s, db := holdStore(t)
	ctx := context.Background()
	w := storetest.UniqueID("ws")
	on(t, s, w)
	if _, _, _, err := s.DisableAll(ctx); err != nil {
		t.Fatal(err)
	}
	got, err := s.Arm(ctx)
	if err != nil || got.Armed || len(got.Unheld) != 1 || got.Unheld[0].Workspace != w || !got.Unheld[0].UnderKill {
		t.Fatalf("arm with %s off and only the fleet hold: %+v %v", w, got, err)
	}
	if _, ok := holdsOf(t, db)[ArmedRow]; ok {
		t.Fatal("a refused arm armed")
	}
	if _, err := s.HoldBackfill(ctx); err != nil {
		t.Fatal(err)
	}
	if got, err := s.Arm(ctx); err != nil || !got.Armed || got.Already {
		t.Fatalf("arm with every row held: %+v %v", got, err)
	}
	armed := holdsOf(t, db)[ArmedRow]
	if got, err := s.Arm(ctx); err != nil || !got.Armed || !got.Already || holdsOf(t, db)[ArmedRow] != armed {
		t.Fatalf("arm again: %+v %v", got, err)
	}
}

// TestAStopCommittedDuringArmIsSeenOrHeld: a stop committed while arm's
// transaction runs, between its check and its commit, is either seen by the
// check, which refuses, or ordered after the arm. A stop of this release
// holds either way; one of a release older than holds, which holds nothing,
// is seen, or committed after the arm's time.
func TestAStopCommittedDuringArmIsSeenOrHeld(t *testing.T) {
	for _, older := range []bool{false, true} {
		s, db := holdStore(t)
		ctx := context.Background()
		w := storetest.UniqueID("ws")
		on(t, s, w)
		var once sync.Once
		stopped := make(chan time.Time, 1)
		s.armChecked = func() {
			once.Do(func() {
				go func() {
					var at time.Time
					var err error
					if older {
						at, err = storetest.UnheldDisable(ctx, db, w)
					} else {
						_, at, err = s.SetWorkspace(ctx, w, false, Reason{Kind: HoldOperator})
					}
					if err != nil {
						t.Error(err)
					}
					stopped <- at
				}()
				// Long enough for the stop to reach the database while
				// arm's transaction is open.
				time.Sleep(200 * time.Millisecond)
			})
		}
		got, err := s.Arm(ctx)
		if err != nil {
			t.Fatal(err)
		}
		stopAt := <-stopped
		s.armChecked = nil
		holds := holdsOf(t, db)
		_, held := holds[w]
		switch {
		case !older && !held:
			t.Fatalf("a stop during arm holds nothing: %v", holds)
		case got.Armed && !held && !stopAt.After(holds[ArmedRow].heldAt):
			t.Fatalf("older %v: armed at %v with %s off unheld at %v, not after the arm", older,
				holds[ArmedRow].heldAt, w, stopAt)
		case !got.Armed && (len(got.Unheld) != 1 || got.Unheld[0].Workspace != w):
			t.Fatalf("older %v: a refused arm lists %+v", older, got.Unheld)
		}
		t.Logf("older %v: armed %v, the stop at %v", older, got.Armed, stopAt)
	}
}

// TestNothingButResumeRemovesThePause: under a pause, release '#pause',
// enable '#pause', release-all, hold-backfill, arm, release-stamped and a
// kill each leave the pause as it was.
func TestNothingButResumeRemovesThePause(t *testing.T) {
	s, db := holdStore(t)
	ctx := context.Background()
	w := storetest.UniqueID("ws")
	on(t, s, w)
	if _, err := s.Pause(ctx); err != nil {
		t.Fatal(err)
	}
	pause := holdsOf(t, db)[PauseRow]
	if _, err := s.Release(ctx, PauseRow); err == nil {
		t.Fatal("release took '#pause'")
	}
	if _, _, err := s.SetWorkspace(ctx, PauseRow, true, Reason{}); err == nil {
		t.Fatal("enable took '#pause'")
	}
	if _, _, _, err := s.DisableAll(ctx); err != nil {
		t.Fatal(err)
	}
	if got, err := s.ReleaseAll(ctx); err != nil || !got.Paused {
		t.Fatalf("release-all under the pause: %+v %v", got, err)
	}
	if _, err := s.HoldBackfill(ctx); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Arm(ctx); err != nil {
		t.Fatal(err)
	}
	if got, err := s.ReleaseStamped(ctx, pause.heldAt, true); err != nil || !got.Paused {
		t.Fatalf("release-stamped under the pause: %+v %v", got, err)
	}
	if h, ok := holdsOf(t, db)[PauseRow]; !ok || h != pause {
		t.Fatalf("the pause after every other command: %+v %v, was %+v", h, ok, pause)
	}
	if got, err := s.Resume(ctx); err != nil || !got.Paused {
		t.Fatalf("resume: %+v %v", got, err)
	}
	if _, ok := holdsOf(t, db)[PauseRow]; ok {
		t.Fatal("resume left the pause")
	}
}

// TestResumeHoldsTheRowsStoppedSinceThePauseAndRemovesItInTheSameCommit:
// resume holds, reason paused, every row turned off at or after the pause
// with no hold of its own, an older release's disable-all's rows among them,
// none turned off before it, keeps the reason of a disable's hold made
// inside the pause, and removes '#pause' at the same commit as its holds.
func TestResumeHoldsTheRowsStoppedSinceThePauseAndRemovesItInTheSameCommit(t *testing.T) {
	s, db := holdStore(t)
	ctx := context.Background()
	early, late, operator, olderKill := storetest.UniqueID("ws"), storetest.UniqueID("ws"), storetest.UniqueID("ws"),
		storetest.UniqueID("ws")
	on(t, s, early, late, operator, olderKill)
	unheldOff(t, db, early)
	if _, err := s.Pause(ctx); err != nil {
		t.Fatal(err)
	}
	pause := holdsOf(t, db)[PauseRow]
	unheldOff(t, db, late)
	off(t, s, operator)
	operatorHold := holdsOf(t, db)[operator]
	if _, err := storetest.OlderDisableAll(ctx, db); err != nil {
		t.Fatal(err)
	}
	got, err := s.Resume(ctx)
	if err != nil || !got.Paused || len(got.Refused) != 0 {
		t.Fatalf("resume: %+v %v", got, err)
	}
	slices.Sort(got.Held)
	want := []string{late, olderKill}
	slices.Sort(want)
	if !slices.Equal(got.Held, want) || got.AlreadyHeld != 1 || got.UnderKill != 0 || !got.From.Equal(pause.heldAt) {
		t.Fatalf("resume held %v, %d already held, %d under a kill, from %v; want %v, 1, 0, from %v", got.Held,
			got.AlreadyHeld, got.UnderKill, got.From, want, pause.heldAt)
	}
	holds := holdsOf(t, db)
	if _, ok := holds[PauseRow]; ok {
		t.Fatal("resume left the pause")
	}
	for _, w := range want {
		if h := holds[w]; h.reason != HoldPaused || !h.heldAt.Equal(got.At) {
			t.Fatalf("%s after resume: %+v, the resume's commit %v", w, h, got.At)
		}
	}
	if _, ok := holds[early]; ok {
		t.Fatal("resume held a row turned off before the pause")
	}
	if holds[operator] != operatorHold {
		t.Fatalf("resume rewrote a disable's hold made inside the pause: %+v, then %+v", operatorHold, holds[operator])
	}
	if again, err := s.Resume(ctx); err != nil || again.Paused {
		t.Fatalf("resume with no pause: %+v %v", again, err)
	}
}

// TestResumeLeavesExactlyTheKillsRowsToTheKill: under a pause, resume's
// backfill skips exactly the rows a kill row stamped and holds every other
// row turned off since the pause.
func TestResumeLeavesExactlyTheKillsRowsToTheKill(t *testing.T) {
	s, db := holdStore(t)
	ctx := context.Background()
	a, b, w := storetest.UniqueID("ws"), storetest.UniqueID("ws"), storetest.UniqueID("ws")
	on(t, s, a, b, w)
	if _, err := s.Pause(ctx); err != nil {
		t.Fatal(err)
	}
	unheldOff(t, db, w)
	if _, _, _, err := s.DisableAll(ctx); err != nil {
		t.Fatal(err)
	}
	got, err := s.Resume(ctx)
	if err != nil || !slices.Equal(got.Held, []string{w}) || got.UnderKill != 2 || got.AlreadyHeld != 0 {
		t.Fatalf("resume: %+v %v", got, err)
	}
	holds := holdsOf(t, db)
	if _, ok := holds[a]; ok {
		t.Fatal("resume held a row of the kill's")
	}
	if _, ok := holds[b]; ok {
		t.Fatal("resume held a row of the kill's")
	}
}

// TestResumeRefusesEnabledAndHeldRows: a row an older release's enable left
// enabled with its hold makes resume refuse, writing nothing, listing it,
// the fleet hold not counted; once this release's enable or disable has
// resolved it, resume goes on.
func TestResumeRefusesEnabledAndHeldRows(t *testing.T) {
	for _, resolve := range []string{"enable", "disable"} {
		s, db := holdStore(t)
		ctx := context.Background()
		w := storetest.UniqueID("ws")
		on(t, s, w)
		off(t, s, w)
		if _, err := s.Pause(ctx); err != nil {
			t.Fatal(err)
		}
		if _, _, _, err := s.DisableAll(ctx); err != nil {
			t.Fatal(err)
		}
		if _, err := storetest.OlderEnable(ctx, db, w); err != nil {
			t.Fatal(err)
		}
		holds := holdsOf(t, db)
		got, err := s.Resume(ctx)
		if err != nil || !got.Paused || len(got.Refused) != 1 || got.Refused[0].Workspace != w ||
			got.Refused[0].Reason != HoldOperator {
			t.Fatalf("resume with %s enabled and held: %+v %v", w, got, err)
		}
		if after := holdsOf(t, db); len(after) != len(holds) || after[PauseRow] != holds[PauseRow] {
			t.Fatalf("a refused resume wrote: %v, then %v", holds, after)
		}
		if resolve == "enable" {
			on(t, s, w)
		} else {
			off(t, s, w)
		}
		got, err = s.Resume(ctx)
		if err != nil || !got.Paused || len(got.Refused) != 0 {
			t.Fatalf("resume once %s resolved it: %+v %v", resolve, got, err)
		}
		if _, ok := holdsOf(t, db)[PauseRow]; ok {
			t.Fatal("the pause is still there")
		}
	}
}

// TestReleaseStampedReleasesOnlyPausedHoldsOfThatStamp: it finds the holds
// of reason paused on rows whose changed_at is exactly the stamp, writes
// nothing without apply, and with it releases those alone: a backfill's or
// an operator's hold on a row of the same stamp, and a paused hold on a row
// of another, stay.
func TestReleaseStampedReleasesOnlyPausedHoldsOfThatStamp(t *testing.T) {
	s, db := holdStore(t)
	ctx := context.Background()
	p1, p2, backfill, operator, other := storetest.UniqueID("ws"), storetest.UniqueID("ws"), storetest.UniqueID("ws"),
		storetest.UniqueID("ws"), storetest.UniqueID("ws")
	on(t, s, other)
	unheldOff(t, db, other)
	on(t, s, p1, p2, backfill, operator)
	stamp, err := storetest.OlderDisableAll(ctx, db)
	if err != nil {
		t.Fatal(err)
	}
	seed := func(w, reason string) *spanner.Mutation {
		return spanner.InsertOrUpdate("tr_fastpath_workspace_hold", []string{"workspace_id", "reason", "held_at"},
			[]any{w, reason, spanner.CommitTimestamp})
	}
	if _, err := db.Apply(ctx, []*spanner.Mutation{seed(p1, HoldPaused), seed(p2, HoldPaused),
		seed(backfill, HoldBackfill), seed(operator, HoldOperator), seed(other, HoldPaused)}); err != nil {
		t.Fatal(err)
	}
	holds := holdsOf(t, db)
	want := []string{p1, p2}
	slices.Sort(want)
	look, err := s.ReleaseStamped(ctx, stamp, false)
	if err != nil || look.Applied || !slices.Equal(look.Workspaces, want) {
		t.Fatalf("release-stamped without apply: %+v %v, want %v", look, err, want)
	}
	if after := holdsOf(t, db); len(after) != len(holds) {
		t.Fatalf("release-stamped without apply wrote: %v, then %v", holds, after)
	}
	got, err := s.ReleaseStamped(ctx, stamp, true)
	if err != nil || !got.Applied || !slices.Equal(got.Workspaces, want) {
		t.Fatalf("release-stamped: %+v %v", got, err)
	}
	after := holdsOf(t, db)
	for _, w := range want {
		if _, ok := after[w]; ok {
			t.Fatalf("%s is still held", w)
		}
	}
	for _, w := range []string{backfill, operator, other} {
		if after[w] != holds[w] {
			t.Fatalf("release-stamped touched %s's hold: %+v, then %+v", w, holds[w], after[w])
		}
	}
	if _, err := s.ReleaseStamped(ctx, stamp.Add(time.Microsecond), true); err != nil {
		t.Fatal(err)
	}
	if again := holdsOf(t, db); len(again) != len(after) {
		t.Fatal("a stamp a microsecond off released something")
	}
}

// TestUnheldDisabledCountsEveryUnheldRow: the report counts every row off
// with no hold of its own, under_kill when its changed_at is a kill's and
// other when not, the fleet hold not a hold of any row's; and enabled rows
// with a hold of their own apart.
func TestUnheldDisabledCountsEveryUnheldRow(t *testing.T) {
	s, db := holdStore(t)
	ctx := context.Background()
	enablers, killed, held, enabledHeld := storetest.UniqueID("ws"), storetest.UniqueID("ws"), storetest.UniqueID("ws"),
		storetest.UniqueID("ws")
	on(t, s, enablers, killed, held, enabledHeld)
	unheldOff(t, db, enablers)
	off(t, s, held)
	off(t, s, enabledHeld)
	if _, _, _, err := s.DisableAll(ctx); err != nil {
		t.Fatal(err)
	}
	if _, err := storetest.OlderEnable(ctx, db, enabledHeld); err != nil {
		t.Fatal(err)
	}
	got, err := s.Holds(ctx)
	if err != nil {
		t.Fatal(err)
	}
	byName := map[string]UnheldRow{}
	for _, u := range got.UnheldDisabled {
		byName[u.Workspace] = u
	}
	if got.UnderKill != 1 || got.Other != 1 || !byName[killed].UnderKill || byName[enablers].UnderKill ||
		len(byName) != 2 || got.Fleet == nil || len(got.Kills) != 1 || got.Holds[HoldOperator] != 2 {
		t.Fatalf("the report: %+v", got)
	}
	if len(got.EnabledHeld) != 1 || got.EnabledHeld[0].Workspace != enabledHeld {
		t.Fatalf("enabled and held: %+v", got.EnabledHeld)
	}
}

// TestSetWorkspaceExecutesTheFixture: SetWorkspace writes what the fixture
// says, not statements of its own: with the fixture's disable writing
// another reason and its enable another statement, the switch writes those.
func TestSetWorkspaceExecutesTheFixture(t *testing.T) {
	s, db := holdStore(t)
	f, err := switchsql.Load()
	if err != nil {
		t.Fatal(err)
	}
	saved := switchOps
	t.Cleanup(func() { switchOps = saved })
	changed := switchsql.Fixture{Version: f.Version, Operations: map[string]switchsql.Operation{}}
	for name, op := range f.Operations {
		changed.Operations[name] = op
	}
	disable := f.Operations[switchsql.Disable]
	disable.Mutations = slices.Clone(disable.Mutations)
	for i, m := range disable.Mutations {
		if m.Table == "tr_fastpath_workspace_hold" {
			m.Values = slices.Clone(m.Values)
			m.Values[slices.Index(m.Columns, "reason")] = json.RawMessage(`"backfill"`)
			disable.Mutations[i] = m
		}
	}
	changed.Operations[switchsql.Disable] = disable
	enable := f.Operations[switchsql.Enable]
	enable.Statements = append(slices.Clone(enable.Statements), switchsql.Statement{
		SQL: `INSERT INTO tr_fastpath_kill (kill_at) VALUES (TIMESTAMP '2001-01-01T00:00:00Z')`, Counts: "marked"})
	changed.Operations[switchsql.Enable] = enable
	switchOps = changed
	w := storetest.UniqueID("ws")
	on(t, s, w)
	if kills := killsOf(t, db); len(kills) != 1 || !kills[0].Equal(time.Date(2001, 1, 1, 0, 0, 0, 0, time.UTC)) {
		t.Fatalf("SetWorkspace on did not execute the fixture's enable: kills %v", kills)
	}
	off(t, s, w)
	if h := holdsOf(t, db)[w]; h.reason != HoldBackfill {
		t.Fatalf("SetWorkspace off did not execute the fixture's disable: the hold %+v", h)
	}
	// And the fixture as it is.
	switchOps = saved
	on(t, s, w)
	off(t, s, w)
	if h := holdsOf(t, db)[w]; h.reason != HoldOperator || len(killsOf(t, db)) != 1 {
		t.Fatalf("the fixture's disable: %+v", h)
	}
}

// TestTheFixturesUnheldDisableIsTheDisableLessItsHold: disable_unheld is
// disable's switch and revocation exactly, without the hold, and disable's
// hold is one mutation of the hold table with the caller's reason; enable
// removes the hold.
func TestTheFixturesUnheldDisableIsTheDisableLessItsHold(t *testing.T) {
	f, err := switchsql.Load()
	if err != nil {
		t.Fatal(err)
	}
	disable, unheld, enable := f.Operations[switchsql.Disable], f.Operations[switchsql.DisableUnheld],
		f.Operations[switchsql.Enable]
	var holds []switchsql.Mutation
	var rest []switchsql.Mutation
	for _, m := range disable.Mutations {
		if m.Table == "tr_fastpath_workspace_hold" {
			holds = append(holds, m)
		} else {
			rest = append(rest, m)
		}
	}
	same := func(a, b []switchsql.Mutation) bool {
		x, _ := json.Marshal(a)
		y, _ := json.Marshal(b)
		return string(x) == string(y)
	}
	if len(holds) != 1 || !slices.Contains(holds[0].Columns, "reason") || !same(rest, unheld.Mutations) {
		t.Fatalf("disable's mutations %+v, disable_unheld's %+v", disable.Mutations, unheld.Mutations)
	}
	x, _ := json.Marshal(disable.Statements)
	y, _ := json.Marshal(unheld.Statements)
	if string(x) != string(y) {
		t.Fatalf("disable's statements %s, disable_unheld's %s", x, y)
	}
	for _, m := range unheld.Mutations {
		if m.Table == "tr_fastpath_workspace_hold" {
			t.Fatal("disable_unheld writes a hold")
		}
	}
	for _, st := range unheld.Statements {
		if strings.Contains(st.SQL, "tr_fastpath_workspace_hold") {
			t.Fatal("disable_unheld writes a hold")
		}
	}
	if len(enable.Statements) != 1 || !strings.HasPrefix(enable.Statements[0].SQL, "DELETE FROM tr_fastpath_workspace_hold") {
		t.Fatalf("enable's statements: %+v", enable.Statements)
	}
}

// TestGoHasNoUnheldDisable: no Go code but the fixture's reader and the
// tests' stand-ins names the fixture's unheld disable, so a new Go caller
// can only turn a workspace off through SetWorkspace, which holds.
func TestGoHasNoUnheldDisable(t *testing.T) {
	root := filepath.Join("..", "..")
	err := filepath.WalkDir(root, func(path string, d os.DirEntry, err error) error {
		if err != nil || d.IsDir() || !strings.HasSuffix(path, ".go") || strings.HasSuffix(path, "_test.go") {
			return err
		}
		rel, _ := filepath.Rel(root, path)
		if rel == filepath.Join("internal", "switchsql", "switchsql.go") ||
			rel == filepath.Join("internal", "store", "storetest", "switch.go") {
			return nil
		}
		b, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		if strings.Contains(string(b), "DisableUnheld") || strings.Contains(string(b), "disable_unheld") {
			t.Errorf("%s names the unheld disable", rel)
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
}
