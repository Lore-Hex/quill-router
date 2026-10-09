package watch

import (
	"context"
	"errors"
	"math"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"
)

var start = time.Date(2026, 10, 9, 12, 0, 0, 0, time.UTC)

// fake is Sources the test sets, failing each read named in fail and
// never answering each named in hang, whatever its context; during, if
// set, runs as each read begins.
type fake struct {
	cpu      float64
	backlogs map[string]Backlog
	pending  []string
	booked   int64
	mu       sync.Mutex // guards fail, hang and during, which a test may change as a watch runs
	fail     map[string]bool
	hang     map[string]bool
	during   func()
}

func (f *fake) err(what string) error {
	f.mu.Lock()
	during, hang, fail := f.during, f.hang[what], f.fail[what]
	f.mu.Unlock()
	if during != nil {
		during()
	}
	if hang {
		select {} // a read that never answers
	}
	if fail {
		return errors.New(what + " failed")
	}
	return nil
}

func (f *fake) SpannerCPU(context.Context) (float64, error) { return f.cpu, f.err("cpu") }
func (f *fake) Subscriptions() []string {
	subs := make([]string, 0, len(f.backlogs))
	for sub := range f.backlogs {
		subs = append(subs, sub)
	}
	slices.Sort(subs)
	return subs
}
func (f *fake) Undelivered(_ context.Context, sub string) (int64, error) {
	return f.backlogs[sub].Undelivered, f.err("undelivered:" + sub)
}
func (f *fake) OldestAge(_ context.Context, sub string) (time.Duration, error) {
	return f.backlogs[sub].OldestAge, f.err("oldest:" + sub)
}
func (f *fake) Pending(context.Context) ([]string, error) { return f.pending, f.err("pending") }
func (f *fake) Booked(context.Context) (int64, error)     { return f.booked, f.err("booked") }

type clock struct{ now time.Time }

func (c *clock) Now() time.Time { return c.now }

var ceilings = Ceilings{SpannerCPU: 0.3, Undelivered: 100, OldestAge: time.Minute, Overdue: 5 * time.Minute,
	PendingOverdue: 0, Spend: 1000}

func watcher(t *testing.T, f *fake, c *clock, misses int) *Watcher {
	t.Helper()
	w, err := New(context.Background(), Config{Ceilings: ceilings, Sources: f, Every: time.Second,
		Timeout: 100 * time.Millisecond, Misses: misses, Clock: c.Now})
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
	if _, err := New(context.Background(), Config{Ceilings: ceilings, Sources: f, Every: time.Second,
		Timeout: time.Second, Misses: 1, Clock: c.Now}); err == nil {
		t.Fatal("a watch that cannot read what was booked as the stage begins began")
	}
	f.fail, f.hang = nil, map[string]bool{"booked": true}
	began := time.Now()
	if _, err := New(context.Background(), Config{Ceilings: ceilings, Sources: f, Every: time.Second,
		Timeout: 100 * time.Millisecond, Misses: 1, Clock: c.Now}); err == nil || time.Since(began) > 5*time.Second {
		t.Fatalf("a watch whose first read never answers: %v after %v", err, time.Since(began))
	}
}

// TestAWatchThatCannotSeeStops: looks that fail stop the stage once Misses
// of them fail in a row, whichever read fails; one that succeeds starts the
// count again.
func TestAWatchThatCannotSeeStops(t *testing.T) {
	for _, what := range []string{"cpu", "undelivered:auditor", "oldest:auditor", "pending", "booked"} {
		f, c := &fake{backlogs: map[string]Backlog{"auditor": {}}}, &clock{now: start}
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

// TestCeilingsAreChecked: a ceiling below 0, a CPU past 1 or not a number,
// a count of overdue packs with no time that makes one overdue, and no
// ceiling at all, are each refused; one ceiling alone is taken.
func TestCeilingsAreChecked(t *testing.T) {
	for _, c := range []Ceilings{{SpannerCPU: -0.1}, {SpannerCPU: 1.1}, {SpannerCPU: math.NaN()},
		{Spend: 1, Undelivered: -1}, {Spend: 1, OldestAge: -1}, {Overdue: -1}, {Spend: 1, PendingOverdue: -1},
		{Spend: -1}, {Spend: 1, PendingOverdue: 1}, {}} {
		if c.Valid() == nil {
			t.Errorf("%+v taken", c)
		}
	}
	for _, c := range []Ceilings{{SpannerCPU: 0.3}, {Undelivered: 1}, {OldestAge: 1}, {Overdue: 1}, {Spend: 1},
		{Overdue: 1, PendingOverdue: 2}} {
		if err := c.Valid(); err != nil {
			t.Errorf("%+v refused: %v", c, err)
		}
	}
}

// TestABreachStopsThoughAnotherReadFails: a look whose CPU is past its
// ceiling stops the stage at once, though the bookings could not be read,
// rather than waiting for the reads to recover.
func TestABreachStopsThoughAnotherReadFails(t *testing.T) {
	f, c := &fake{}, &clock{now: start}
	w := watcher(t, f, c, 3)
	f.cpu, f.fail = 0.8, map[string]bool{"booked": true}
	if _, why := w.Look(context.Background()); len(why) != 1 || !strings.Contains(why[0], "CPU") {
		t.Fatalf("a breach with another read failing: %v", why)
	}
}

// TestAReadThatNeverAnswersIsAMiss: a read that never answers, whatever its
// context, ends its look within the timeout as a miss, and the others are
// still read; Misses such looks stop the stage.
func TestAReadThatNeverAnswersIsAMiss(t *testing.T) {
	f, c := &fake{}, &clock{now: start}
	w := watcher(t, f, c, 2)
	f.hang, f.booked = map[string]bool{"pending": true}, 1001
	began := time.Now()
	if r, why := w.Look(context.Background()); len(why) != 1 || r.Spend != 1001 || time.Since(began) > 5*time.Second {
		t.Fatalf("a look with a read that never answers, the spend past its ceiling: %v %+v after %v", why, r,
			time.Since(began))
	}
	f.booked = 0
	if _, why := w.Look(context.Background()); len(why) != 0 {
		t.Fatalf("the first look missed stops: %v", why)
	}
	if _, why := w.Look(context.Background()); len(why) != 1 || !strings.Contains(why[0], "within") {
		t.Fatalf("the second look missed: %v", why)
	}
}

// TestAWatchStoppedDuringALookStopsNothing: the watch's own context ending
// while a look reads, its reads failing for that, stops nothing, though one
// look missed would.
func TestAWatchStoppedDuringALookStopsNothing(t *testing.T) {
	f, c := &fake{}, &clock{now: start}
	w := watcher(t, f, c, 1)
	ctx, cancel := context.WithCancel(context.Background())
	var once sync.Once
	f.during = func() { once.Do(cancel) }
	f.hang = map[string]bool{"cpu": true}
	if why := w.Watch(ctx, func(why []string) { t.Errorf("stopped: %v", why) }); why != nil {
		t.Fatalf("a watch stopped during a look: %v", why)
	}
}

// TestABacklogBreachStopsThoughAnotherSubscriptionFails: one subscription's
// undelivered messages past their ceiling stop the stage at once, though
// another subscription's reads fail, or this one's age cannot be read.
func TestABacklogBreachStopsThoughAnotherSubscriptionFails(t *testing.T) {
	for _, failing := range []string{"undelivered:archive", "oldest:archive", "oldest:auditor"} {
		f := &fake{backlogs: map[string]Backlog{"auditor": {Undelivered: 1001}, "archive": {}}}
		w := watcher(t, f, &clock{now: start}, 3)
		f.fail = map[string]bool{failing: true}
		if _, why := w.Look(context.Background()); len(why) != 1 || !strings.Contains(why[0], "auditor has 1001") {
			t.Errorf("%s failing: %v", failing, why)
		}
	}
}

// TestAWatchThatCannotSeeStopsWithinItsBound: once every read stops
// answering, each taking its whole timeout, the watch decides to stop within
// misses times the larger of the interval and the timeout, plus a timeout,
// as the runbook states, with the interval shorter than the timeout.
func TestAWatchThatCannotSeeStopsWithinItsBound(t *testing.T) {
	f := &fake{backlogs: map[string]Backlog{"auditor": {}}}
	every, timeout, misses := 10*time.Millisecond, 60*time.Millisecond, 3
	w, err := New(context.Background(), Config{Ceilings: ceilings, Sources: f, Every: every, Timeout: timeout,
		Misses: misses, Clock: time.Now})
	if err != nil {
		t.Fatal(err)
	}
	var stoppedAt time.Time
	var failedAt time.Time
	done := make(chan []string, 1)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { done <- w.Watch(ctx, func([]string) { stoppedAt = time.Now() }) }()
	time.Sleep(5 * every)
	failedAt = time.Now()
	f.mu.Lock()
	f.hang = map[string]bool{"cpu": true, "undelivered:auditor": true, "oldest:auditor": true, "pending": true,
		"booked": true}
	f.mu.Unlock()
	if why := <-done; len(why) != 1 {
		t.Fatalf("the watch returned %v", why)
	}
	bound := time.Duration(misses)*max(every, timeout) + timeout
	if took := stoppedAt.Sub(failedAt); took > bound+300*time.Millisecond {
		t.Fatalf("decided to stop %v after the reads stopped answering, past its bound of %v", took, bound)
	}
}
