// Package watch is the production rollout's ceilings and their stop
// (docs/design/fast-admission-production-rollout.md, W3): it reads, every
// interval, what a stage must not pass, Spanner's CPU, each subscription's
// backlog, the pending work overdue and the stage's spend, and once a
// reading is past a ceiling, or the readings keep failing, it stops the
// stage: the load generator first, then the workspace turned off.
package watch

import (
	"context"
	"errors"
	"fmt"
	"math"
	"sort"
	"strings"
	"sync"
	"time"
)

// Backlog is a subscription's: its messages not yet delivered, and the age
// of its oldest message not acknowledged.
type Backlog struct {
	Undelivered int64
	OldestAge   time.Duration
}

// Reading is one look at what the ceilings bound.
type Reading struct {
	// SpannerCPU is the instance's high-priority CPU, 0 to 1, at its
	// highest over the last window Monitoring reports.
	SpannerCPU float64
	// Backlogs are each subscription's, by its name.
	Backlogs map[string]Backlog
	// PendingOverdue are the packs whose work has been pending longer than
	// Ceilings.Overdue.
	PendingOverdue int
	// Spend is what the stage's workspace has booked since the stage began.
	Spend int64
}

// Ceilings are what a stage stops at: a reading past any of them stops it.
// A zero ceiling bounds nothing; Spend is bounded only when set.
type Ceilings struct {
	SpannerCPU     float64
	Undelivered    int64
	OldestAge      time.Duration
	Overdue        time.Duration // how long a pack's work may be pending
	PendingOverdue int           // how many packs may be overdue at once
	Spend          int64
}

// Valid says whether the ceilings can be watched: each at least 0, Spanner's
// CPU a number at most 1, a count of overdue packs only with how long makes
// one overdue, and at least one ceiling that bounds something, since a
// watch that bounds nothing stops nothing.
func (c Ceilings) Valid() error {
	switch {
	case math.IsNaN(c.SpannerCPU) || c.SpannerCPU < 0 || c.SpannerCPU > 1 || c.Undelivered < 0 ||
		c.OldestAge < 0 || c.Overdue < 0 || c.PendingOverdue < 0 || c.Spend < 0:
		return errors.New("watch: ceilings are at least 0, and Spanner's CPU a number at most 1")
	case c.PendingOverdue > 0 && c.Overdue == 0:
		return errors.New("watch: a count of overdue packs needs how long a pack's work may be pending")
	case c.SpannerCPU == 0 && c.Undelivered == 0 && c.OldestAge == 0 && c.Overdue == 0 && c.Spend == 0:
		return errors.New("watch: no ceiling: a watch that bounds nothing stops nothing")
	}
	return nil
}

// Past says which ceilings a reading is past, each with its value, in a
// fixed order; none if it is past none.
func (c Ceilings) Past(r Reading) []string {
	var out []string
	if c.SpannerCPU > 0 && r.SpannerCPU > c.SpannerCPU {
		out = append(out, fmt.Sprintf("Spanner's CPU is %.0f%%, past %.0f%%", 100*r.SpannerCPU, 100*c.SpannerCPU))
	}
	names := make([]string, 0, len(r.Backlogs))
	for name := range r.Backlogs {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		b := r.Backlogs[name]
		if c.Undelivered > 0 && b.Undelivered > c.Undelivered {
			out = append(out, fmt.Sprintf("%s has %d messages undelivered, past %d", name, b.Undelivered,
				c.Undelivered))
		}
		if c.OldestAge > 0 && b.OldestAge > c.OldestAge {
			out = append(out, fmt.Sprintf("%s's oldest message is %v old, past %v", name, b.OldestAge, c.OldestAge))
		}
	}
	if c.Overdue > 0 && r.PendingOverdue > c.PendingOverdue {
		out = append(out, fmt.Sprintf("%d packs' work is pending longer than %v, past %d", r.PendingOverdue,
			c.Overdue, c.PendingOverdue))
	}
	if c.Spend > 0 && r.Spend > c.Spend {
		out = append(out, fmt.Sprintf("the stage has spent %d, past %d", r.Spend, c.Spend))
	}
	return out
}

// Sources read what the ceilings bound. Each is read once a look; one that
// fails fails the look.
type Sources interface {
	// SpannerCPU is the instance's high-priority CPU over the last window.
	SpannerCPU(ctx context.Context) (float64, error)
	// Backlogs are the watched subscriptions', every one of them: one
	// missing is a failed read, not an empty backlog.
	Backlogs(ctx context.Context) (map[string]Backlog, error)
	// Pending are the packs whose work is not done, each by a name of its
	// own, as of the read.
	Pending(ctx context.Context) ([]string, error)
	// Booked is what the workspace has booked, all told.
	Booked(ctx context.Context) (int64, error)
}

// Config is a watch: its ceilings, its sources, how often it looks, how
// long each read of a look may take, Timeout, and how many looks in a row
// may have a read fail before it stops the stage, since a watch that cannot
// see must not let the stage run on. Clock is the time a look is taken at.
type Config struct {
	Ceilings Ceilings
	Sources  Sources
	Every    time.Duration
	Timeout  time.Duration
	Misses   int
	Clock    func() time.Time
}

// Watcher keeps what one look needs of the last: when each pending pack
// was first seen, and what was booked when the stage began.
type Watcher struct {
	cfg      Config
	start    int64
	firstSaw map[string]time.Time
	misses   int
}

// New is a watch, reading what is booked as the stage begins, within
// Timeout.
func New(ctx context.Context, cfg Config) (*Watcher, error) {
	if err := cfg.Ceilings.Valid(); err != nil {
		return nil, err
	}
	if cfg.Sources == nil || cfg.Every <= 0 || cfg.Timeout <= 0 || cfg.Misses < 1 || cfg.Clock == nil {
		return nil, errors.New("watch: sources, an interval, a read's timeout, at least one look that may fail, " +
			"and a clock")
	}
	rctx, cancel := context.WithTimeout(ctx, cfg.Timeout)
	defer cancel()
	type booked struct {
		v   int64
		err error
	}
	done := make(chan booked, 1)
	go func() {
		v, err := cfg.Sources.Booked(rctx)
		done <- booked{v, err}
	}()
	var start int64
	var err error
	select {
	case b := <-done:
		start, err = b.v, b.err
	case <-rctx.Done():
		select {
		case b := <-done:
			start, err = b.v, b.err
		default:
			err = fmt.Errorf("no answer within %v", cfg.Timeout)
		}
	}
	if err != nil {
		return nil, fmt.Errorf("watch: what the stage's workspace has booked as it begins: %w", err)
	}
	return &Watcher{cfg: cfg, start: start, firstSaw: map[string]time.Time{}}, nil
}

// Look reads once, its four reads at once, each within Timeout, so a look
// takes at most Timeout. It reports why the stage must stop, if it must: the
// ceilings past by any read that succeeded, whatever the others did; else,
// once Misses looks in a row have had a read fail, that it cannot see. A
// read that fails leaves its part of the reading at zero, which is past no
// ceiling.
func (w *Watcher) Look(ctx context.Context) (Reading, []string) {
	// Each read's result is its own, sent once it ends; one that ends after
	// its timeout is never taken, so nothing it does reaches the reading.
	type result struct {
		cpu      float64
		backlogs map[string]Backlog
		pending  []string
		booked   int64
		err      error
	}
	reads := []func(context.Context) result{
		func(ctx context.Context) result {
			v, err := w.cfg.Sources.SpannerCPU(ctx)
			return result{cpu: v, err: err}
		},
		func(ctx context.Context) result {
			v, err := w.cfg.Sources.Backlogs(ctx)
			return result{backlogs: v, err: err}
		},
		func(ctx context.Context) result {
			v, err := w.cfg.Sources.Pending(ctx)
			return result{pending: v, err: err}
		},
		func(ctx context.Context) result {
			v, err := w.cfg.Sources.Booked(ctx)
			return result{booked: v, err: err}
		},
	}
	results := make([]result, len(reads))
	var wg sync.WaitGroup
	for i, read := range reads {
		wg.Add(1)
		go func() {
			defer wg.Done()
			rctx, cancel := context.WithTimeout(ctx, w.cfg.Timeout)
			defer cancel()
			done := make(chan result, 1)
			go func() { done <- read(rctx) }()
			select {
			case results[i] = <-done:
			case <-rctx.Done():
				// An answer that came as the time ran out is taken.
				select {
				case results[i] = <-done:
				default:
					results[i] = result{err: fmt.Errorf("no answer within %v", w.cfg.Timeout)}
				}
			}
		}()
	}
	wg.Wait()
	var r Reading
	var failed []string
	for i, what := range []string{"Spanner's CPU", "the backlogs", "the pending work", "what is booked"} {
		if results[i].err != nil {
			failed = append(failed, fmt.Sprintf("%s: %v", what, results[i].err))
		}
	}
	if results[0].err == nil {
		r.SpannerCPU = results[0].cpu
	}
	if results[1].err == nil {
		r.Backlogs = results[1].backlogs
	}
	if results[2].err == nil {
		pending := results[2].pending
		now := w.cfg.Clock()
		seen := make(map[string]time.Time, len(pending))
		for _, p := range pending {
			first, ok := w.firstSaw[p]
			if !ok {
				first = now
			}
			seen[p] = first
			if now.Sub(first) > w.cfg.Ceilings.Overdue {
				r.PendingOverdue++
			}
		}
		w.firstSaw = seen
	}
	if results[3].err == nil {
		r.Spend = results[3].booked - w.start
	}
	if past := w.cfg.Ceilings.Past(r); len(past) > 0 {
		return r, past
	}
	if len(failed) > 0 {
		w.misses++
		if w.misses >= w.cfg.Misses {
			return r, []string{fmt.Sprintf("%d looks in a row could not read: %s", w.misses, strings.Join(failed, "; "))}
		}
		return r, nil
	}
	w.misses = 0
	return r, nil
}

// Watch looks every interval until ctx ends, and once a look says the stage
// must stop, stops it: stop is given why, and Watch returns them. A look
// that ctx ended during stops nothing: the watch itself was stopped first,
// and its reads failed for that.
func (w *Watcher) Watch(ctx context.Context, stop func(why []string)) []string {
	t := time.NewTicker(w.cfg.Every)
	defer t.Stop()
	for {
		if ctx.Err() != nil {
			return nil
		}
		_, why := w.Look(ctx)
		if ctx.Err() != nil {
			return nil
		}
		if len(why) > 0 {
			stop(why)
			return why
		}
		select {
		case <-ctx.Done():
			return nil
		case <-t.C:
		}
	}
}
