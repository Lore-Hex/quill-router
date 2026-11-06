package store

import (
	"context"
	"strings"
	"testing"
	"time"

	"cloud.google.com/go/spanner"

	"github.com/Lore-Hex/quill-router/fastpath/internal/store/storetest"
)

// decideRows is the workspace's rows, read strongly, each as
// "cloud/region=decide".
func decideRows(t *testing.T, s *Store, ws string) []string {
	t.Helper()
	rows, read, err := s.ReadDecide(context.Background())
	if err != nil || read.IsZero() {
		t.Fatalf("reading the rows: %v at %v", err, read)
	}
	out := []string{}
	for _, r := range rows {
		if r.Workspace != ws {
			continue
		}
		if r.ChangedAt.IsZero() {
			t.Fatalf("a row of %s: %+v", ws, r)
		}
		state := "off"
		if r.Decide {
			state = "on"
		}
		out = append(out, r.Cloud+"/"+r.Region+"="+state)
	}
	return out
}

// TestAWorkspaceDecidesOnlyWhereItsRowSays: a workspace with no row has
// none, so decides nowhere; each row is its cloud's and region's alone,
// turned on and off in place, its changed_at the commit's time, and a
// workspace's rows are not another's.
func TestAWorkspaceDecidesOnlyWhereItsRowSays(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	ws, other := storetest.UniqueID("ws"), storetest.UniqueID("ws")
	if got := decideRows(t, s, ws); len(got) != 0 {
		t.Fatalf("a workspace never set has rows %v", got)
	}
	set := func(w, cloud, region string, decide bool) {
		t.Helper()
		at, err := s.SetDecide(ctx, w, cloud, region, decide)
		if err != nil || at.IsZero() {
			t.Fatalf("setting %s %s/%s %v: %v at %v", w, cloud, region, decide, err, at)
		}
		rows, _, err := s.ReadDecide(ctx)
		if err != nil {
			t.Fatal(err)
		}
		for _, r := range rows {
			if r.Workspace == w && r.Cloud == cloud && r.Region == region && (r.Decide != decide || !r.ChangedAt.Equal(at)) {
				t.Fatalf("the row set at %v: %+v", at, r)
			}
		}
	}
	set(ws, "gcp", "us-central1", true)
	set(ws, "gcp", "europe-west4", false)
	set(other, "gcp", "us-central1", true)
	if got, want := strings.Join(decideRows(t, s, ws), " "), "gcp/europe-west4=off gcp/us-central1=on"; got != want {
		t.Fatalf("the workspace's rows are %s, want %s", got, want)
	}
	set(ws, "gcp", "us-central1", false)
	set(ws, "gcp", "europe-west4", true)
	if got, want := strings.Join(decideRows(t, s, ws), " "), "gcp/europe-west4=on gcp/us-central1=off"; got != want {
		t.Fatalf("the workspace's rows are %s, want %s", got, want)
	}
	if got, want := strings.Join(decideRows(t, s, other), " "), "gcp/us-central1=on"; got != want {
		t.Fatalf("another workspace's rows are %s, want %s", got, want)
	}
}

// TestSetDecideRefusesAKeyNoEdgeLooksUp: a workspace missing or too long,
// a cloud or region missing, not lower case or too long, are refused, and
// nothing is written.
func TestSetDecideRefusesAKeyNoEdgeLooksUp(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	ws := storetest.UniqueID("ws")
	for _, key := range [][3]string{
		{"", "gcp", "us-central1"}, {strings.Repeat("w", 65), "gcp", "us-central1"},
		{ws, "", "us-central1"}, {ws, "GCP", "us-central1"}, {ws, "gcp ", "us-central1"},
		{ws, strings.Repeat("c", 17), "us-central1"}, {ws, "gcp", ""}, {ws, "gcp", "US-CENTRAL1"},
		{ws, "gcp", "1-region"}, {ws, "gcp", "us-" + strings.Repeat("r", 30)},
	} {
		if _, err := s.SetDecide(ctx, key[0], key[1], key[2], true); err == nil {
			t.Errorf("%q was taken", key)
		}
	}
	if got := decideRows(t, s, ws); len(got) != 0 {
		t.Fatalf("refused keys wrote %v", got)
	}
}

// TestTheSwitchLeavesDecideRowsAlone: turning a workspace off, or every
// workspace, leaves its rows as they are, and turning it on sets none: the
// switch and who decides are apart.
func TestTheSwitchLeavesDecideRowsAlone(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	ws := unswitched(t, 100)
	if _, _, err := s.SetWorkspace(ctx, ws, true); err != nil {
		t.Fatal(err)
	}
	if got := decideRows(t, s, ws); len(got) != 0 {
		t.Fatalf("turning the workspace on set rows %v", got)
	}
	if _, err := s.SetDecide(ctx, ws, "gcp", "us-central1", true); err != nil {
		t.Fatal(err)
	}
	if _, _, err := s.SetWorkspace(ctx, ws, false); err != nil {
		t.Fatal(err)
	}
	if got, want := strings.Join(decideRows(t, s, ws), " "), "gcp/us-central1=on"; got != want {
		t.Fatalf("after the workspace was turned off its rows are %s, want %s", got, want)
	}
}

// TestDecideOffAllStopsEveryWorkspaceDeciding, on a database of its own,
// since it reaches every workspace: every row on is turned off, in every
// cloud and region, and counted, rows off are not, and the switch and the
// leases are left as they were; run again, it turns off nothing.
func TestDecideOffAllStopsEveryWorkspaceDeciding(t *testing.T) {
	if emulator == nil {
		t.Skip(skipped)
	}
	ctx := context.Background()
	db, err := emulator.Database(ctx, storetest.UniqueID("decide"), nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(db.Close)
	s, err := New(db, testConfig())
	if err != nil {
		t.Fatal(err)
	}
	a, b := storetest.UniqueID("ws"), storetest.UniqueID("ws")
	if _, err := db.Apply(ctx, []*spanner.Mutation{storetest.Enabled(a), storetest.Member("node-a", 1),
		spanner.InsertMap("tr_credit_balance",
			map[string]any{"workspace_id": a, "shard": int64(0), "total_credits": int64(100), "trust_tier": int64(3)}),
		storetest.Decide(a, "gcp", "us-central1", true), storetest.Decide(a, "gcp", "europe-west4", true),
		storetest.Decide(b, "gcp", "us-central1", true), storetest.Decide(b, "gcp", "us-east4", false)}); err != nil {
		t.Fatal(err)
	}
	req := grantOf(a, 10)
	if got, err := s.Grant(ctx, req); err != nil || got.Refused != "" {
		t.Fatalf("the grant: %+v %v", got, err)
	}
	before, _, err := s.ReadDecide(ctx)
	if err != nil || len(before) != 4 {
		t.Fatalf("the table before: %+v %v", before, err)
	}
	var wasOff DecideRow
	for _, r := range before {
		if !r.Decide {
			wasOff = r
		}
	}
	off, at, err := s.DecideOffAll(ctx)
	if err != nil || off != 3 || at.IsZero() {
		t.Fatalf("turning every row off: %d rows at %v, %v; want 3", off, at, err)
	}
	rows, _, err := s.ReadDecide(ctx)
	if err != nil || len(rows) != 4 {
		t.Fatalf("the table after: %+v %v; every row kept", rows, err)
	}
	for _, r := range rows {
		if r.Decide {
			t.Errorf("a row still on: %+v", r)
		}
		// The row that was already off is left as it was.
		isWasOff := r.Workspace == wasOff.Workspace && r.Cloud == wasOff.Cloud && r.Region == wasOff.Region
		if isWasOff && !r.ChangedAt.Equal(wasOff.ChangedAt) {
			t.Errorf("the row already off changed: %+v, before %+v", r, wasOff)
		}
		if !isWasOff && !r.ChangedAt.Equal(at) {
			t.Errorf("a row turned off at %v: %+v", at, r)
		}
	}
	enabled, _, err := s.EnabledWorkspaces(ctx)
	if err != nil || !enabled[a] {
		t.Fatalf("the switch after: %v %v", enabled, err)
	}
	if l := readLease(t, s, LeaseRef{a, req.LeaseID}); l.Revoked || l.State != "open" {
		t.Fatalf("the lease after: %+v", l)
	}
	if off, _, err := s.DecideOffAll(ctx); err != nil || off != 0 {
		t.Fatalf("again: %d rows, %v", off, err)
	}
}

// TestDeleteDecideRemovesOneRow: deleting removes the workspace's row for
// that cloud and region alone, says it found one, and leaves the
// workspace's other rows and another workspace's as they were; deleting it
// again finds none; a row under a malformed workspace, such as a flag a
// command took for one, can be removed; and a key the table cannot hold is
// refused.
func TestDeleteDecideRemovesOneRow(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	ws, other := storetest.UniqueID("ws"), storetest.UniqueID("ws")
	junk := "-" + storetest.UniqueID("h")
	for _, k := range [][3]string{{ws, "gcp", "us-central1"}, {ws, "gcp", "europe-west4"}, {other, "gcp", "us-central1"},
		{junk, "gcp", "us-central1"}} {
		if _, err := s.SetDecide(ctx, k[0], k[1], k[2], true); err != nil {
			t.Fatalf("setting %q: %v", k, err)
		}
	}
	got, err := s.DeleteDecide(ctx, ws, "gcp", "us-central1", false)
	if err != nil || !got.Found || !got.Deleted || got.Wildcard || got.Refused || got.At.IsZero() {
		t.Fatalf("deleting a row: %+v, %v", got, err)
	}
	if got, want := strings.Join(decideRows(t, s, ws), " "), "gcp/europe-west4=on"; got != want {
		t.Fatalf("the workspace's rows are %s, want %s", got, want)
	}
	if got, want := strings.Join(decideRows(t, s, other), " "), "gcp/us-central1=on"; got != want {
		t.Fatalf("another workspace's rows are %s, want %s", got, want)
	}
	if got, err := s.DeleteDecide(ctx, ws, "gcp", "us-central1", false); err != nil || got.Found || got.Deleted ||
		got.At.IsZero() {
		t.Fatalf("deleting it again: %+v, %v", got, err)
	}
	if got, err := s.DeleteDecide(ctx, junk, "gcp", "us-central1", false); err != nil || !got.Found || !got.Deleted {
		t.Fatalf("deleting the malformed workspace's row: %+v, %v", got, err)
	}
	if got := decideRows(t, s, junk); len(got) != 0 {
		t.Fatalf("the malformed workspace's rows after: %v", got)
	}
	for _, key := range [][3]string{{"", "gcp", "us-central1"}, {ws, "GCP", "europe-west4"}, {ws, "gcp", ""}} {
		if _, err := s.DeleteDecide(ctx, key[0], key[1], key[2], false); err == nil {
			t.Errorf("%q was taken", key)
		}
	}
	if got, want := strings.Join(decideRows(t, s, ws), " "), "gcp/europe-west4=on"; got != want {
		t.Fatalf("after the refused keys the workspace's rows are %s, want %s", got, want)
	}
}

// ownDecideStore is a store on a database of its own: the wildcard row and
// DecideOffAll reach every workspace.
func ownDecideStore(t *testing.T) *Store {
	t.Helper()
	if emulator == nil {
		t.Skip(skipped)
	}
	db, err := emulator.Database(context.Background(), storetest.UniqueID("decide"), nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(db.Close)
	s, err := New(db, testConfig())
	if err != nil {
		t.Fatal(err)
	}
	return s
}

// TestEffectiveDecideIsTheWideningsRule (docs/design/fast-admission-widening.md
// section 6.1): a workspace's own row, on or off, wins over the wildcard;
// with no row of its own the wildcard decides; with neither, off.
func TestEffectiveDecideIsTheWideningsRule(t *testing.T) {
	for _, v := range []struct {
		own, ownFound, wildcard, wildcardFound bool
		decide                                 bool
		via                                    string
	}{
		{true, true, false, false, true, DecideViaOwn},
		{true, true, false, true, true, DecideViaOwn},
		{false, true, true, true, false, DecideViaOwn},
		{false, true, false, false, false, DecideViaOwn},
		{false, false, true, true, true, DecideViaWildcard},
		{false, false, false, true, false, DecideViaWildcard},
		{false, false, false, false, false, DecideViaNone},
	} {
		if decide, via := EffectiveDecide(v.own, v.ownFound, v.wildcard, v.wildcardFound); decide != v.decide || via != v.via {
			t.Errorf("%+v: %v via %s", v, decide, via)
		}
	}
}

// TestDeleteDecideOnlyEverStopsOneDecidingUnderTheWildcard (section 6.2):
// under an on wildcard, deleting a workspace's off row would make it decide,
// so it is refused, nothing written, unless followWildcard, which removes
// it and says the workspace now follows the wildcard; an own row that is on
// is removed always; an off row under a wildcard that is off, or none, is
// removed; and the wildcard row itself is removed, said to be the wildcard.
func TestDeleteDecideOnlyEverStopsOneDecidingUnderTheWildcard(t *testing.T) {
	s := ownDecideStore(t)
	ctx := context.Background()
	on, off, otherRegion := storetest.UniqueID("ws"), storetest.UniqueID("ws"), storetest.UniqueID("ws")
	for _, k := range []struct {
		ws, region string
		decide     bool
	}{{DecideWildcard, "us-central1", true}, {on, "us-central1", true}, {off, "us-central1", false},
		{otherRegion, "us-east4", false}} {
		if _, err := s.SetDecide(ctx, k.ws, "gcp", k.region, k.decide); err != nil {
			t.Fatal(err)
		}
	}
	got, err := s.DeleteDecide(ctx, off, "gcp", "us-central1", false)
	if err != nil || !got.Found || got.Deleted || !got.Refused || got.FollowsWildcard {
		t.Fatalf("an off row under an on wildcard: %+v, %v", got, err)
	}
	if rows := strings.Join(decideRows(t, s, off), " "); rows != "gcp/us-central1=off" {
		t.Fatalf("the refused row: %q, want it kept", rows)
	}
	// Another region has no wildcard: its off row goes.
	if got, err := s.DeleteDecide(ctx, otherRegion, "gcp", "us-east4", false); err != nil || !got.Deleted ||
		got.Refused || got.FollowsWildcard {
		t.Fatalf("an off row with no wildcard in its region: %+v, %v", got, err)
	}
	// An on row goes always, and the workspace keeps deciding through the
	// wildcard, which the answer says.
	if got, err := s.DeleteDecide(ctx, on, "gcp", "us-central1", false); err != nil || !got.Deleted || got.Refused ||
		!got.FollowsWildcard {
		t.Fatalf("an on row under an on wildcard: %+v, %v", got, err)
	}
	got, err = s.DeleteDecide(ctx, off, "gcp", "us-central1", true)
	if err != nil || !got.Deleted || got.Refused || !got.FollowsWildcard {
		t.Fatalf("an off row under an on wildcard, followed: %+v, %v", got, err)
	}
	if rows := decideRows(t, s, off); len(rows) != 0 {
		t.Fatalf("the followed row: %v, want none", rows)
	}
	// With the wildcard off, an off row goes without the flag.
	if _, err := s.SetDecide(ctx, off, "gcp", "us-central1", false); err != nil {
		t.Fatal(err)
	}
	if _, err := s.SetDecide(ctx, DecideWildcard, "gcp", "us-central1", false); err != nil {
		t.Fatal(err)
	}
	if got, err := s.DeleteDecide(ctx, off, "gcp", "us-central1", false); err != nil || !got.Deleted || got.Refused ||
		got.FollowsWildcard {
		t.Fatalf("an off row under an off wildcard: %+v, %v", got, err)
	}
	got, err = s.DeleteDecide(ctx, DecideWildcard, "gcp", "us-central1", false)
	if err != nil || !got.Found || !got.Deleted || !got.Wildcard || got.Refused || got.FollowsWildcard {
		t.Fatalf("the wildcard row: %+v, %v", got, err)
	}
	if rows := decideRows(t, s, DecideWildcard); len(rows) != 0 {
		t.Fatalf("the wildcard's rows after: %v", rows)
	}
}

// TestDecideRestoreTurnsBackOnExactlyTheKillsRows (section 6.2): every row
// DecideOffAll turned off, the wildcard's among them, keeps its commit
// timestamp, and DecideRestore at it turns exactly those back on: an off
// override set before the kill, and a row turned off after it, keep their
// own timestamps and stay off; restored rows take the restore's commit
// timestamp, so a second restore at the kill's turns on nothing.
func TestDecideRestoreTurnsBackOnExactlyTheKillsRows(t *testing.T) {
	s := ownDecideStore(t)
	ctx := context.Background()
	a, b, override, later := storetest.UniqueID("ws"), storetest.UniqueID("ws"), storetest.UniqueID("ws"),
		storetest.UniqueID("ws")
	for _, k := range []struct {
		ws, region string
		decide     bool
	}{{DecideWildcard, "us-central1", true}, {a, "us-central1", true}, {b, "europe-west4", true},
		{override, "us-central1", false}, {later, "us-central1", true}} {
		if _, err := s.SetDecide(ctx, k.ws, "gcp", k.region, k.decide); err != nil {
			t.Fatal(err)
		}
	}
	off, killedAt, err := s.DecideOffAll(ctx)
	if err != nil || off != 4 {
		t.Fatalf("the kill: %d rows, %v; want 4, the wildcard's among them", off, err)
	}
	if rows := strings.Join(decideRows(t, s, DecideWildcard), " "); rows != "gcp/us-central1=off" {
		t.Fatalf("the wildcard after the kill: %q", rows)
	}
	// Turned off again after the kill: its own changed_at, not the kill's.
	if _, err := s.SetDecide(ctx, later, "gcp", "us-central1", false); err != nil {
		t.Fatal(err)
	}
	on, restoredAt, err := s.DecideRestore(ctx, killedAt)
	if err != nil || on != 3 || !restoredAt.After(killedAt) {
		t.Fatalf("the restore: %d rows at %v, %v; want 3 after %v", on, restoredAt, err, killedAt)
	}
	rows, _, err := s.ReadDecide(ctx)
	if err != nil {
		t.Fatal(err)
	}
	want := map[string]bool{DecideWildcard: true, a: true, b: true, override: false, later: false}
	for _, r := range rows {
		if r.Decide != want[r.Workspace] {
			t.Errorf("after the restore: %+v", r)
		}
		if r.Decide && !r.ChangedAt.Equal(restoredAt) {
			t.Errorf("a restored row keeps %v, not the restore's %v: %+v", r.ChangedAt, restoredAt, r)
		}
	}
	if on, _, err := s.DecideRestore(ctx, killedAt); err != nil || on != 0 {
		t.Fatalf("a second restore: %d rows, %v", on, err)
	}
	if _, _, err := s.DecideRestore(ctx, time.Time{}); err == nil {
		t.Fatal("a restore with no timestamp was taken")
	}
}
