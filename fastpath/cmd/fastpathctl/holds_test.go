package main

import (
	"bytes"
	"context"
	"slices"
	"strings"
	"testing"
	"time"

	"cloud.google.com/go/spanner"

	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store/storetest"
)

// The hold commands' tests (docs/design/fast-admission-widening.md, section
// 5.6 and PR 4's tests in section 11), each on a database of its own, since
// the hold table's own rows reach every workspace. The enabler is PR 6's;
// storetest.RunEnabler stands for its next run.

type opener = func(context.Context, string, store.Config) (*store.Store, func(), error)

// ownDB is a database of the test's own, its client and the command's
// store on it.
func ownDB(t *testing.T) (*spanner.Client, opener) {
	t.Helper()
	if emulator == nil {
		t.Skip(skipped)
	}
	db, err := emulator.Database(context.Background(), storetest.UniqueID("holds"), nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(db.Close)
	return db, func(_ context.Context, _ string, cfg store.Config) (*store.Store, func(), error) {
		s, err := store.New(db, cfg)
		return s, func() {}, err
	}
}

// ctl runs a command that must exit with want, and returns its answer.
func ctl(t *testing.T, open opener, want int, args ...string) map[string]any {
	t.Helper()
	code, got, err := doOn(t, open, args...)
	if code != want {
		t.Fatalf("%q: exit %d, %v, %v; want exit %d", args, code, got, err, want)
	}
	return got
}

// holdOf is a workspace's hold's reason, "" for none.
func holdOf(t *testing.T, db *spanner.Client, w string) string {
	t.Helper()
	row, err := db.Single().ReadRow(context.Background(), "tr_fastpath_workspace_hold", spanner.Key{w}, []string{"reason"})
	if spanner.ErrCode(err) == 5 {
		return ""
	}
	if err != nil {
		t.Fatal(err)
	}
	var reason string
	if err := row.Column(0, &reason); err != nil {
		t.Fatal(err)
	}
	return reason
}

// isOn says whether the switch enables the workspace.
func isOn(t *testing.T, db *spanner.Client, w string) bool {
	t.Helper()
	row, err := db.Single().ReadRow(context.Background(), "tr_fastpath_workspace", spanner.Key{w}, []string{"enabled"})
	if spanner.ErrCode(err) == 5 {
		return false
	}
	if err != nil {
		t.Fatal(err)
	}
	var on bool
	if err := row.Column(0, &on); err != nil {
		t.Fatal(err)
	}
	return on
}

// armedWith is WS2's state: the workspaces enabled with this release's
// enable, every row held, the enabler armed.
func armedWith(t *testing.T, open opener, ws ...string) {
	t.Helper()
	for _, w := range ws {
		ctl(t, open, 0, "enable", w)
	}
	ctl(t, open, 0, "hold-backfill")
	if got := ctl(t, open, 0, "enabler", "arm"); got["armed"] != true {
		t.Fatalf("arm: %v", got)
	}
}

// nextRun is the enabler's next run over the workspaces.
func nextRun(t *testing.T, db *spanner.Client, ws ...string) storetest.EnablerRun {
	t.Helper()
	run, err := storetest.RunEnabler(context.Background(), db, ws...)
	if err != nil {
		t.Fatal(err)
	}
	return run
}

func ids(t *testing.T, n int) []string {
	out := make([]string, n)
	for i := range out {
		out[i] = storetest.UniqueID("ws")
	}
	return out
}

// TestTheHoldCommandsRefuseWhatTheyCannotDo: release and enable take a
// workspace's id alone, never '*', '#pause' or '#armed'; enabler takes
// pause, resume or arm; release-stamped a timestamp, and -yes after it
// alone; each exit 2, and none reaches the store.
func TestTheHoldCommandsRefuseWhatTheyCannotDo(t *testing.T) {
	never := func(context.Context, string, store.Config) (*store.Store, func(), error) {
		t.Fatal("the store was opened")
		return nil, nil, nil
	}
	for _, args := range [][]string{
		{"release"}, {"release", ""}, {"release", "*"}, {"release", "#pause"}, {"release", "#armed"},
		{"release", "ws", "more"}, {"enable", "*"}, {"enable", "#pause"}, {"enable", "#armed"},
		{"disable", "*"}, {"disable", "#pause"}, {"disable", "#armed"},
		{"enabler"}, {"enabler", "go"}, {"enabler", "pause", "more"}, {"enabler", "-h"},
		{"release-stamped"}, {"release-stamped", "yesterday"}, {"release-stamped", "2026-11-06T00:00:00Z", "more"},
		{"release-stamped", "2026-11-06T00:00:00Z", "-nope"}, {"-yes", "status", "ws"}, {"-yes", "release-all"},
		{"release-all", "x"}, {"hold-backfill", "x"}, {"holds", "x"}, {"enable", "ws", "-yes"},
	} {
		if code, err := run(context.Background(), append([]string{"-database", "d"}, args...), &bytes.Buffer{},
			never); code != 2 || err == nil {
			t.Errorf("%q: exit %d, %v", args, code, err)
		}
	}
}

// TestDisableHoldsAndEnableClearsTheHold: disable holds the workspace,
// reason operator, enable removes the hold in its transaction; holds exits
// 3 while a row is off with no hold of its own and 0 once none is.
func TestDisableHoldsAndEnableClearsTheHold(t *testing.T) {
	db, open := ownDB(t)
	w := storetest.UniqueID("ws")
	ctl(t, open, 0, "enable", w)
	ctl(t, open, 0, "disable", w)
	if got := holdOf(t, db, w); got != store.HoldOperator {
		t.Fatalf("after disable the hold is %q", got)
	}
	if got := ctl(t, open, 0, "holds"); got["holds"].(map[string]any)[store.HoldOperator] != float64(1) {
		t.Fatalf("holds: %v", got)
	}
	ctl(t, open, 0, "enable", w)
	if got := holdOf(t, db, w); got != "" {
		t.Fatalf("after enable the hold is %q", got)
	}
	if _, err := storetest.UnheldDisable(context.Background(), db, w); err != nil {
		t.Fatal(err)
	}
	got := ctl(t, open, 3, "holds")
	if u := got["unheld_disabled"].(map[string]any); u["other"] != float64(1) || u["under_kill"] != float64(0) {
		t.Fatalf("holds with an unheld row: %v", got)
	}
}

// TestHoldsCountsAKillsRowsAsUnheld: rows a kill stopped have no hold of
// their own, only the fleet hold, so holds exits 3 with them all under_kill
// and none other; hold-backfill holds them and holds exits 0.
func TestHoldsCountsAKillsRowsAsUnheld(t *testing.T) {
	_, open := ownDB(t)
	for _, w := range ids(t, 2) {
		ctl(t, open, 0, "enable", w)
	}
	ctl(t, open, 0, "disable-all")
	got := ctl(t, open, 3, "holds")
	if u := got["unheld_disabled"].(map[string]any); u["under_kill"] != float64(2) || u["other"] != float64(0) {
		t.Fatalf("holds after a kill: %v", got)
	}
	ctl(t, open, 0, "hold-backfill")
	ctl(t, open, 0, "holds")
}

// TestReleaseUnderThePauseIsRefused: release and release-all while the
// enabler is paused exit 2, saying resume first, and write nothing; resumed,
// release removes the hold without enabling, and a second release finds
// none, exit 3.
func TestReleaseUnderThePauseIsRefused(t *testing.T) {
	db, open := ownDB(t)
	w := storetest.UniqueID("ws")
	armedWith(t, open, w)
	ctl(t, open, 0, "disable", w)
	ctl(t, open, 0, "enabler", "pause")
	for _, args := range [][]string{{"release", w}, {"release-all"}} {
		code, got, err := doOn(t, open, args...)
		if code != 2 || err == nil || !strings.Contains(err.Error(), "resume first") || got["refused"] != true {
			t.Fatalf("%q under the pause: exit %d, %v, %v", args, code, got, err)
		}
	}
	if holdOf(t, db, w) != store.HoldOperator || holdOf(t, db, store.PauseRow) == "" {
		t.Fatal("a refused release wrote")
	}
	ctl(t, open, 0, "enabler", "resume")
	if got := ctl(t, open, 0, "release", w); got["released"] != true {
		t.Fatalf("release: %v", got)
	}
	if holdOf(t, db, w) != "" || isOn(t, db, w) {
		t.Fatal("release left the hold, or enabled")
	}
	ctl(t, open, 3, "release", w)
	if run := nextRun(t, db, w); !slices.Equal(run.Enabled, []string{w}) {
		t.Fatalf("the enabler's next run after release: %+v", run)
	}
}

// TestAStopUnderThePauseStaysThroughTheKillsUndoing: the sequence the
// review described. Paused, an older release's disable of W at T1, then
// disable-all; resume holds W, reason paused, and leaves the kill's rows to
// the kill; release-all; the enabler's next run turns the kill's rows on
// again, and W stays off and held.
func TestAStopUnderThePauseStaysThroughTheKillsUndoing(t *testing.T) {
	db, open := ownDB(t)
	ctx := context.Background()
	ws := ids(t, 3)
	a, b, w := ws[0], ws[1], ws[2]
	armedWith(t, open, a, b, w)
	ctl(t, open, 0, "enabler", "pause")
	if _, err := storetest.UnheldDisable(ctx, db, w); err != nil {
		t.Fatal(err)
	}
	if got := ctl(t, open, 0, "disable-all"); got["workspaces_turned_off"] != float64(2) || got["kill_at"] != got["at"] {
		t.Fatalf("disable-all: %v", got)
	}
	got := ctl(t, open, 0, "enabler", "resume")
	if got["held"] != float64(1) || got["under_kill"] != float64(2) || got["already_held"] != float64(0) ||
		got["held_workspaces"].([]any)[0] != w {
		t.Fatalf("resume: %v", got)
	}
	ctl(t, open, 0, "release-all")
	run := nextRun(t, db, a, b, w)
	slices.Sort(run.Enabled)
	want := []string{a, b}
	slices.Sort(want)
	if !slices.Equal(run.Enabled, want) || len(run.Disabled) != 0 || run.Stopped != "" {
		t.Fatalf("the enabler's next run: %+v, want %v enabled", run, want)
	}
	if isOn(t, db, w) || holdOf(t, db, w) != store.HoldPaused {
		t.Fatalf("W after the kill's undoing: on %v, held %q", isOn(t, db, w), holdOf(t, db, w))
	}
}

// TestTwoKillsUnderOnePauseAreBothUndone: the same with two disable-alls in
// the pause: both kills' rows come back after release-all, and W stays held.
func TestTwoKillsUnderOnePauseAreBothUndone(t *testing.T) {
	db, open := ownDB(t)
	ctx := context.Background()
	ws := ids(t, 4)
	a, b, c, w := ws[0], ws[1], ws[2], ws[3]
	armedWith(t, open, a, b, w)
	ctl(t, open, 0, "enabler", "pause")
	if _, err := storetest.UnheldDisable(ctx, db, w); err != nil {
		t.Fatal(err)
	}
	first := ctl(t, open, 0, "disable-all")
	ctl(t, open, 0, "enable", c)
	second := ctl(t, open, 0, "disable-all")
	if first["at"] == second["at"] {
		t.Fatal("two kills share a timestamp")
	}
	if got := ctl(t, open, 3, "holds"); len(got["kills"].([]any)) != 2 {
		t.Fatalf("holds: %v", got)
	}
	got := ctl(t, open, 0, "enabler", "resume")
	if got["held"] != float64(1) || got["under_kill"] != float64(3) {
		t.Fatalf("resume: %v", got)
	}
	if got := ctl(t, open, 0, "release-all"); got["kills_removed"] != float64(2) || got["fleet_hold_removed"] != true {
		t.Fatalf("release-all: %v", got)
	}
	run := nextRun(t, db, a, b, c, w)
	slices.Sort(run.Enabled)
	want := []string{a, b, c}
	slices.Sort(want)
	if !slices.Equal(run.Enabled, want) || isOn(t, db, w) || holdOf(t, db, w) != store.HoldPaused {
		t.Fatalf("the next run %+v; W on %v, held %q", run, isOn(t, db, w), holdOf(t, db, w))
	}
}

// TestUnheldDisabledIsScopedToResumesWindow: with the enabler's own
// disables made before the pause, unheld and unstamped, the report counts
// the kill's rows under_kill and W and the enabler's rows other before the
// resume; after it the enabler's rows are still other, and resume's printed
// window holds no unheld row that is not under a kill.
func TestUnheldDisabledIsScopedToResumesWindow(t *testing.T) {
	db, open := ownDB(t)
	ctx := context.Background()
	ws := ids(t, 5)
	e1, e2, a, b, w := ws[0], ws[1], ws[2], ws[3], ws[4]
	armedWith(t, open, e1, e2, a, b, w)
	for _, e := range []string{e1, e2} {
		// The enabler's own disable: no hold.
		if _, err := storetest.UnheldDisable(ctx, db, e); err != nil {
			t.Fatal(err)
		}
	}
	ctl(t, open, 0, "enabler", "pause")
	if _, err := storetest.UnheldDisable(ctx, db, w); err != nil {
		t.Fatal(err)
	}
	ctl(t, open, 0, "disable-all")
	unheld := func(got map[string]any) (map[string]any, map[string]time.Time) {
		u := got["unheld_disabled"].(map[string]any)
		rows := map[string]time.Time{}
		for _, r := range u["rows"].([]any) {
			row := r.(map[string]any)
			at, err := time.Parse(time.RFC3339Nano, row["changed_at"].(string))
			if err != nil {
				t.Fatal(err)
			}
			if row["under_kill"] == true {
				continue
			}
			rows[row["workspace"].(string)] = at
		}
		return u, rows
	}
	u, other := unheld(ctl(t, open, 3, "holds"))
	if u["under_kill"] != float64(2) || u["other"] != float64(3) || len(other) != 3 {
		t.Fatalf("before the resume: %v", u)
	}
	resumed := ctl(t, open, 0, "enabler", "resume")
	window := resumed["window"].(map[string]any)
	from, err := time.Parse(time.RFC3339Nano, window["from"].(string))
	if err != nil {
		t.Fatal(err)
	}
	until, err := time.Parse(time.RFC3339Nano, window["until"].(string))
	if err != nil {
		t.Fatal(err)
	}
	u, other = unheld(ctl(t, open, 3, "holds"))
	if u["under_kill"] != float64(2) || u["other"] != float64(2) || len(other) != 2 {
		t.Fatalf("after the resume: %v", u)
	}
	for _, e := range []string{e1, e2} {
		if _, ok := other[e]; !ok {
			t.Fatalf("the enabler's row %s is not counted other after the resume: %v", e, u)
		}
	}
	for workspace, at := range other {
		if !at.Before(from) && at.Before(until) {
			t.Fatalf("%s, unheld and under no kill, is in resume's window [%v, %v): %v", workspace, from, until, at)
		}
	}
}

// TestResumeRefusesAnEnabledAndHeldRowUntilResolved: an older release's
// enable leaves a row enabled with its hold; resume refuses, exit 2,
// listing it, writing nothing; once this release's enable or disable has
// resolved it, resume goes on.
func TestResumeRefusesAnEnabledAndHeldRowUntilResolved(t *testing.T) {
	for _, resolve := range []string{"enable", "disable"} {
		db, open := ownDB(t)
		ctx := context.Background()
		w := storetest.UniqueID("ws")
		armedWith(t, open, w)
		ctl(t, open, 0, "disable", w)
		ctl(t, open, 0, "enabler", "pause")
		if _, err := storetest.OlderEnable(ctx, db, w); err != nil {
			t.Fatal(err)
		}
		code, got, err := doOn(t, open, "enabler", "resume")
		listed := got["enabled_and_held"].([]any)
		if code != 2 || err == nil || len(listed) != 1 || listed[0].(map[string]any)["workspace"] != w {
			t.Fatalf("resume with %s enabled and held: exit %d, %v, %v", w, code, got, err)
		}
		if holdOf(t, db, store.PauseRow) == "" {
			t.Fatal("a refused resume removed the pause")
		}
		ctl(t, open, 0, resolve, w)
		if got := ctl(t, open, 0, "enabler", "resume"); got["resumed"] != true {
			t.Fatalf("resume once %s resolved it: %v", resolve, got)
		}
		ctl(t, open, 3, "enabler", "resume")
	}
}

// TestTheWayBackFromAKillDuringARollback: section 8.3's sequence. Paused,
// the rollback below holds, this release's disable-all stops A, B and C, an
// older release's enable brings A back; rolled forward, resume lists nothing
// and holds nothing (A has no hold, B and C are under the kill); this
// release's disable C holds it; release-all; and after the enabler's next
// run A and B are on, C off and held, reason operator.
func TestTheWayBackFromAKillDuringARollback(t *testing.T) {
	db, open := ownDB(t)
	ctx := context.Background()
	ws := ids(t, 3)
	a, b, c := ws[0], ws[1], ws[2]
	armedWith(t, open, a, b, c)
	ctl(t, open, 0, "enabler", "pause")
	ctl(t, open, 0, "disable-all")
	if _, err := storetest.OlderEnable(ctx, db, a); err != nil {
		t.Fatal(err)
	}
	got := ctl(t, open, 0, "enabler", "resume")
	if len(got["enabled_and_held"].([]any)) != 0 || got["held"] != float64(0) || got["under_kill"] != float64(2) {
		t.Fatalf("resume after the roll forward: %v", got)
	}
	ctl(t, open, 0, "disable", c)
	ctl(t, open, 0, "release-all")
	nextRun(t, db, a, b, c)
	if !isOn(t, db, a) || !isOn(t, db, b) || isOn(t, db, c) || holdOf(t, db, c) != store.HoldOperator {
		t.Fatalf("after the next run: A %v, B %v, C %v held %q", isOn(t, db, a), isOn(t, db, b), isOn(t, db, c),
			holdOf(t, db, c))
	}
}

// TestAnOlderKillsRowsComeBackOnlyByReleaseStamped: an older release's
// disable-all under a pause leaves no kill row, so resume holds its rows,
// reason paused, and they stay off after release-all; release-stamped TS
// prints what it would release and writes nothing without -yes, and with it
// releases exactly those; the next run turns them on.
func TestAnOlderKillsRowsComeBackOnlyByReleaseStamped(t *testing.T) {
	db, open := ownDB(t)
	ctx := context.Background()
	ws := ids(t, 3)
	a, b, held := ws[0], ws[1], ws[2]
	armedWith(t, open, a, b, held)
	ctl(t, open, 0, "disable", held)
	ctl(t, open, 0, "enabler", "pause")
	stamp, err := storetest.OlderDisableAll(ctx, db)
	if err != nil {
		t.Fatal(err)
	}
	if got := ctl(t, open, 0, "enabler", "resume"); got["held"] != float64(2) || got["under_kill"] != float64(0) {
		t.Fatalf("resume: %v", got)
	}
	ctl(t, open, 3, "release-all")
	if run := nextRun(t, db, a, b, held); len(run.Enabled) != 0 {
		t.Fatalf("the next run before release-stamped: %+v", run)
	}
	ts := stamp.Format(time.RFC3339Nano)
	look := ctl(t, open, 0, "release-stamped", ts)
	if look["holds"] != float64(2) || look["workspaces"] != float64(2) || look["released"] != false {
		t.Fatalf("release-stamped without -yes: %v", look)
	}
	if holdOf(t, db, a) != store.HoldPaused || holdOf(t, db, b) != store.HoldPaused {
		t.Fatal("release-stamped without -yes wrote")
	}
	if got := ctl(t, open, 0, "release-stamped", ts, "-yes"); got["released"] != true || got["holds"] != float64(2) {
		t.Fatalf("release-stamped -yes: %v", got)
	}
	ctl(t, open, 3, "release-stamped", ts, "-yes")
	run := nextRun(t, db, a, b, held)
	slices.Sort(run.Enabled)
	want := []string{a, b}
	slices.Sort(want)
	if !slices.Equal(run.Enabled, want) || holdOf(t, db, held) != store.HoldOperator {
		t.Fatalf("the next run after release-stamped: %+v; held's hold %q", run, holdOf(t, db, held))
	}
}

// TestArmAndHoldBackfillGateEachOther: arm refuses, exit 2, listing the
// rows off with no hold of their own; hold-backfill holds them, a second run
// holds nothing; arm then arms; hold-backfill then refuses, exit 2; and
// until the arm the enabler writes nothing.
func TestArmAndHoldBackfillGateEachOther(t *testing.T) {
	db, open := ownDB(t)
	ctx := context.Background()
	w, other := storetest.UniqueID("ws"), storetest.UniqueID("ws")
	ctl(t, open, 0, "enable", w)
	if _, err := storetest.UnheldDisable(ctx, db, w); err != nil {
		t.Fatal(err)
	}
	if run := nextRun(t, db, w, other); run.Stopped != "unarmed" || len(run.Enabled) != 0 {
		t.Fatalf("the enabler before the arm: %+v", run)
	}
	code, got, err := doOn(t, open, "enabler", "arm")
	if code != 2 || err == nil || len(got["unheld_disabled"].([]any)) != 1 || got["armed"] != false {
		t.Fatalf("arm with an unheld row: exit %d, %v, %v", code, got, err)
	}
	if got := ctl(t, open, 0, "hold-backfill"); got["held"] != float64(1) {
		t.Fatalf("hold-backfill: %v", got)
	}
	if got := ctl(t, open, 0, "hold-backfill"); got["held"] != float64(0) {
		t.Fatalf("a second hold-backfill: %v", got)
	}
	if got := ctl(t, open, 0, "enabler", "arm"); got["armed"] != true || got["already"] != false {
		t.Fatalf("arm: %v", got)
	}
	if got := ctl(t, open, 0, "enabler", "arm"); got["already"] != true {
		t.Fatalf("arm again: %v", got)
	}
	if code, _, err := doOn(t, open, "hold-backfill"); code != 2 || err == nil {
		t.Fatalf("hold-backfill once armed: exit %d, %v", code, err)
	}
	if got := ctl(t, open, 0, "enabler", "pause"); got["already"] != false {
		t.Fatalf("pause: %v", got)
	}
	first := ctl(t, open, 0, "enabler", "pause")
	if first["already"] != true {
		t.Fatalf("a second pause: %v", first)
	}
	if run := nextRun(t, db, other); run.Stopped != "paused" {
		t.Fatalf("the enabler under the pause: %+v", run)
	}
	ctl(t, open, 0, "disable-all")
	ctl(t, open, 0, "enabler", "resume")
	if run := nextRun(t, db, other); run.Stopped != "fleet" {
		t.Fatalf("the enabler under the fleet hold: %+v", run)
	}
}
