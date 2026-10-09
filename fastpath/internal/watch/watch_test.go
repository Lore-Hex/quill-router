package watch

import (
	"context"
	"errors"
	"slices"
	"strings"
	"testing"
	"time"
)

var start = time.Date(2026, 10, 9, 12, 0, 0, 0, time.UTC)

// fake is Sources the test sets, failing each read named in fail.
type fake struct {
	cpu      float64
	backlogs map[string]Backlog
	pending  []string
	booked   int64
	fail     map[string]bool
}

func (f *fake) err(what string) error {
	if f.fail[what] {
		return errors.New(what + " failed")
	}
	return nil
}

func (f *fake) SpannerCPU(context.Context) (float64, error) { return f.cpu, f.err("cpu") }
func (f *fake) Backlogs(context.Context) (map[string]Backlog, error) {
	return f.backlogs, f.err("backlogs")
}
func (f *fake) Pending(context.Context) ([]string, error) { return f.pending, f.err("pending") }
func (f *fake) Booked(context.Context) (int64, error)     { return f.booked, f.err("booked") }

type clock struct{ now time.Time }

func (c *clock) Now() time.Time { return c.now }

var ceilings = Ceilings{SpannerCPU: 0.3, Undelivered: 100, OldestAge: time.Minute, Overdue: 5 * time.Minute,
	PendingOverdue: 0, Spend: 1000}

func watcher(t *testing.T, f *fake, c *clock, misses int) *Watcher {
	t.Helper()
	w, err := New(context.Background(), Config{Ceilings: ceilings, Sources: f, Every: time.Second, Misses: misses,
		Clock: c.Now})
	if err != nil {
		t.Fatal(err)
	}
	return w
}

// TestEachCeilingStopsAlone: a reading within every ceiling stops nothing;
// one past any one ceiling stops the stage, naming it alone; a zero ceiling
// bounds nothing.
func TestEachCeilingStopsAlone(t *testing.T) {
	within := Reading{SpannerCPU: 0.3, Backlogs: map[string]Backlog{"auditor": {100, time.Minute}}, Spend: 1000}
	if past := ceilings.Past(within); len(past) != 0 {
		t.Fatalf("a reading at every ceiling stops: %v", past)
	}
	for name, r := range map[string]Reading{
		"Spanner's CPU": {SpannerCPU: 0.31},
		"undelivered":   {Backlogs: map[string]Backlog{"auditor": {Undelivered: 101}}},
		"oldest":        {Backlogs: map[string]Backlog{"stager": {OldestAge: time.Minute + time.Second}}},
		"pending":       {PendingOverdue: 1},
		"spent":         {Spend: 1001},
	} {
		past := ceilings.Past(r)
		if len(past) != 1 || !strings.Contains(past[0], name) {
			t.Errorf("%s past its ceiling: %v", name, past)
		}
		if past := (Ceilings{}).Past(r); len(past) != 0 {
			t.Errorf("%s with no ceilings: %v", name, past)
		}
	}
}

// TestPendingWorkIsOverdueOnceSeenLongEnough: a pack's work is overdue once
// it has been seen pending for longer than the ceiling allows; a pack done
// meanwhile is forgotten, and one seen again later starts its time afresh.
func TestPendingWorkIsOverdueOnceSeenLongEnough(t *testing.T) {
	f, c := &fake{pending: []string{"a", "b"}}, &clock{now: start}
	w := watcher(t, f, c, 1)
	if r, why := w.Look(context.Background()); r.PendingOverdue != 0 || len(why) != 0 {
		t.Fatalf("packs just seen: %+v %v", r, why)
	}
	c.now = start.Add(5 * time.Minute)
	if r, _ := w.Look(context.Background()); r.PendingOverdue != 0 {
		t.Fatalf("packs pending as long as allowed: %+v", r)
	}
	f.pending = []string{"b"}
	c.now = start.Add(6 * time.Minute)
	r, why := w.Look(context.Background())
	if r.PendingOverdue != 1 || len(why) != 1 {
		t.Fatalf("a pack pending six minutes, the other done: %+v %v", r, why)
	}
	f.pending = []string{"a"}
	c.now = start.Add(7 * time.Minute)
	if r, _ := w.Look(context.Background()); r.PendingOverdue != 0 {
		t.Fatalf("a pack seen again, its time afresh: %+v", r)
	}
}

// TestSpendIsFromTheStagesStart: what the stage spends is what the
// workspace has booked since the watch began, not all it ever booked.
func TestSpendIsFromTheStagesStart(t *testing.T) {
	f, c := &fake{booked: 5000}, &clock{now: start}
	w := watcher(t, f, c, 1)
	f.booked = 6000
	if r, why := w.Look(context.Background()); r.Spend != 1000 || len(why) != 0 {
		t.Fatalf("spent as much as the ceiling: %+v %v", r, why)
	}
	f.booked = 6001
	if r, why := w.Look(context.Background()); r.Spend != 1001 || len(why) != 1 {
		t.Fatalf("spent past the ceiling: %+v %v", r, why)
	}
	f.fail = map[string]bool{"booked": true}
	if _, err := New(context.Background(), Config{Ceilings: ceilings, Sources: f, Every: time.Second, Misses: 1,
		Clock: c.Now}); err == nil {
		t.Fatal("a watch that cannot read what was booked as the stage begins began")
	}
}

// TestAWatchThatCannotSeeStops: looks that fail stop the stage once Misses
// of them fail in a row, whichever read fails; one that succeeds starts the
// count again.
func TestAWatchThatCannotSeeStops(t *testing.T) {
	for _, what := range []string{"cpu", "backlogs", "pending", "booked"} {
		f, c := &fake{}, &clock{now: start}
		w := watcher(t, f, c, 3)
		f.fail = map[string]bool{what: true}
		for i := 1; i <= 2; i++ {
			if _, why := w.Look(context.Background()); len(why) != 0 {
				t.Fatalf("%s: look %d, failed, stops: %v", what, i, why)
			}
		}
		f.fail = nil
		if _, why := w.Look(context.Background()); len(why) != 0 {
			t.Fatalf("%s: a look that succeeds stops: %v", what, why)
		}
		f.fail = map[string]bool{what: true}
		for i := 1; i <= 3; i++ {
			_, why := w.Look(context.Background())
			if (i == 3) != (len(why) == 1) {
				t.Fatalf("%s: look %d after a success: %v", what, i, why)
			}
		}
	}
}

// TestWatchStopsOnce: the watch looks until a look says stop, calls stop
// once with why, and returns it; ended first, it stops nothing.
func TestWatchStopsOnce(t *testing.T) {
	f, c := &fake{cpu: 0.5}, &clock{now: start}
	w := watcher(t, f, c, 1)
	var stopped [][]string
	why := w.Watch(context.Background(), func(why []string) { stopped = append(stopped, why) })
	if len(stopped) != 1 || !slices.Equal(stopped[0], why) || len(why) != 1 {
		t.Fatalf("stopped %v, returned %v", stopped, why)
	}
	f.cpu = 0
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if why := watcher(t, f, c, 1).Watch(ctx, func([]string) { t.Fatal("stopped") }); why != nil {
		t.Fatalf("an ended watch: %v", why)
	}
}

// TestCeilingsAreChecked: a ceiling below 0, or a CPU past 1, is refused.
func TestCeilingsAreChecked(t *testing.T) {
	for _, c := range []Ceilings{{SpannerCPU: -0.1}, {SpannerCPU: 1.1}, {Undelivered: -1}, {OldestAge: -1},
		{Overdue: -1}, {PendingOverdue: -1}, {Spend: -1}} {
		if _, err := New(context.Background(), Config{Ceilings: c, Sources: &fake{}, Every: time.Second, Misses: 1,
			Clock: time.Now}); err == nil {
			t.Errorf("%+v taken", c)
		}
	}
}
