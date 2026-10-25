package store

import (
	"context"
	"strings"
	"testing"

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
