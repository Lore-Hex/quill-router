package main

import (
	"bytes"
	"context"
	"strings"
	"testing"

	"cloud.google.com/go/spanner"

	"github.com/Lore-Hex/quill-router/fastpath/internal/service"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store/storetest"
	"github.com/Lore-Hex/quill-router/fastpath/internal/watch"
	"github.com/Lore-Hex/quill-router/fastpath/schema"
)

// TestATripOnADatabaseWithoutHoldsFailsAndNamesTheFallbacks: on a database
// the migration has not given the hold table, the stop writes nothing (the
// switch, the revocation and the hold are one transaction), so the watch
// exits 1 with the workspace still on, and its answer and error name the
// ways to turn it off that still work.
func TestATripOnADatabaseWithoutHoldsFailsAndNamesTheFallbacks(t *testing.T) {
	if emulator == nil {
		t.Skip(skipped)
	}
	ctx := context.Background()
	all, err := schema.Statements()
	if err != nil {
		t.Fatal(err)
	}
	var older []string
	for _, s := range all {
		if !strings.Contains(s, "tr_fastpath_workspace_hold") && !strings.Contains(s, "tr_fastpath_kill") {
			older = append(older, s)
		}
	}
	db, err := emulator.Database(ctx, storetest.UniqueID("noholds"), older)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(db.Close)
	s, err := store.New(db, service.Defaults().Store)
	if err != nil {
		t.Fatal(err)
	}
	ws := storetest.UniqueID("ws")
	if _, err := db.Apply(ctx, []*spanner.Mutation{storetest.Enabled(ws)}); err != nil {
		t.Fatal(err)
	}
	d := production
	d.open = func(context.Context, target) (watch.Sources, *store.Store, func(), error) {
		return sources{cpu: 0.5}, s, func() {}, nil
	}
	var out bytes.Buffer
	code, err := run(ctx, append(flags, "-workspace", ws), &out, d)
	if code != 1 || err == nil || !strings.Contains(err.Error(), "TR_FASTPATH_PILOT_ENABLED=false") {
		t.Fatalf("exit %d, %v", code, err)
	}
	for _, want := range []string{"turn_off_failed", "a release without holds", "TR_FASTPATH_PILOT_ENABLED=false",
		"decide off"} {
		if !strings.Contains(out.String(), want) {
			t.Fatalf("the answer %q does not name %q", out.String(), want)
		}
	}
	on, _, err := s.EnabledWorkspaces(ctx)
	if err != nil || !on[ws] {
		t.Fatalf("the workspace after a failed stop: %v %v", on, err)
	}
}

// TestATripHoldsItsWorkspaceThroughTheEnablersNextRun: a watch's stop holds
// its workspace, reason watch, with the ceiling that tripped
// (docs/design/fast-admission-widening.md, section 5.6); once the watch has
// exited, the enabler's next run, armed and nothing else holding it, leaves
// the workspace off, while it turns on an unheld workspace beside it. On a
// database of its own, since the enabler's arm reaches every workspace.
func TestATripHoldsItsWorkspaceThroughTheEnablersNextRun(t *testing.T) {
	if emulator == nil {
		t.Skip(skipped)
	}
	ctx := context.Background()
	db, err := emulator.Database(ctx, storetest.UniqueID("trip"), nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(db.Close)
	s, err := store.New(db, service.Defaults().Store)
	if err != nil {
		t.Fatal(err)
	}
	ws, beside := storetest.UniqueID("ws"), storetest.UniqueID("ws")
	if _, err := db.Apply(ctx, []*spanner.Mutation{storetest.Enabled(ws), spanner.InsertMap("tr_fastpath_workspace_hold",
		map[string]any{"workspace_id": store.ArmedRow, "reason": "armed", "held_at": spanner.CommitTimestamp})}); err != nil {
		t.Fatal(err)
	}
	d := production
	d.open = func(context.Context, target) (watch.Sources, *store.Store, func(), error) {
		return sources{cpu: 0.5}, s, func() {}, nil
	}
	var out bytes.Buffer
	code, err := run(ctx, append(flags, "-workspace", ws), &out, d)
	if code != stopped || err != nil {
		t.Fatalf("exit %d, %v", code, err)
	}
	row, err := db.Single().ReadRow(ctx, "tr_fastpath_workspace_hold", spanner.Key{ws}, []string{"reason", "detail"})
	if err != nil {
		t.Fatalf("the trip's hold: %v", err)
	}
	var reason string
	var detail spanner.NullString
	if err := row.Columns(&reason, &detail); err != nil {
		t.Fatal(err)
	}
	if reason != store.HoldWatch || !strings.Contains(detail.StringVal, "CPU") {
		t.Fatalf("the trip's hold: %q, %q", reason, detail.StringVal)
	}
	// The watch has exited; the enabler runs.
	next, err := storetest.RunEnabler(ctx, db, ws, beside)
	if err != nil {
		t.Fatal(err)
	}
	if len(next.Enabled) != 1 || next.Enabled[0] != beside || next.Stopped != "" {
		t.Fatalf("the enabler's next run: %+v", next)
	}
	on, _, err := s.EnabledWorkspaces(ctx)
	if err != nil || on[ws] || !on[beside] {
		t.Fatalf("after the enabler's run: %v %v", on, err)
	}
}
