// Command fastpathwatch watches a stage of the fast path's production
// rollout against its ceilings (docs/design/fast-admission-production-
// rollout.md, W3) and stops the stage past any of them, or once it cannot
// see: it stops the load generator, then turns the stage's workspace off.
//
//	fastpathwatch -database projects/P/instances/I/databases/D -project P \
//	    -instance I -subscriptions auditor,stager,settle-archive,records-archive \
//	    -workspace WS -max-cpu 0.35 -max-undelivered 1000 -max-oldest 2m \
//	    -overdue 5m -max-spend 100000 -stop-pid 1234
//
// It exits 3 once it has stopped the stage, and prints why as JSON; 0 if it
// is itself stopped first, having stopped nothing; 1 if it cannot watch, or
// cannot turn the workspace off; 2 for its usage.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	monitoring "cloud.google.com/go/monitoring/apiv3/v2"
	"cloud.google.com/go/spanner"

	"github.com/Lore-Hex/quill-router/fastpath/internal/service"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
	"github.com/Lore-Hex/quill-router/fastpath/internal/watch"
)

// stopped is the exit code of a watch that stopped its stage.
const stopped = 3

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	code, err := run(ctx, os.Args[1:], os.Stdout, production)
	if err != nil {
		fmt.Fprintln(os.Stderr, "fastpathwatch:", err)
	}
	os.Exit(code)
}

// target is where a watch reads: the database, the project, the Spanner
// instance and the subscriptions it watches, and the stage's workspace.
type target struct {
	database, project, instance, workspace string
	subscriptions                          []string
	window, fresh                          time.Duration
	limit                                  int
}

// deps are what the command reaches: its sources and the store, closed by
// the function returned with them, and the load generator's process.
type deps struct {
	open  func(ctx context.Context, t target) (watch.Sources, *store.Store, func(), error)
	term  func(pid int) error
	alive func(pid int) bool
	clock func() time.Time
}

var production = deps{
	open: func(ctx context.Context, t target) (watch.Sources, *store.Store, func(), error) {
		client, err := spanner.NewClient(ctx, t.database)
		if err != nil {
			return nil, nil, nil, err
		}
		s, err := store.New(client, service.Defaults().Store)
		if err != nil {
			client.Close()
			return nil, nil, nil, err
		}
		metrics, err := monitoring.NewMetricClient(ctx)
		if err != nil {
			client.Close()
			return nil, nil, nil, err
		}
		src := watch.Production{
			Monitoring: &watch.Monitoring{Client: metrics, Project: t.project, Instance: t.instance,
				Subs: t.subscriptions, Window: t.window, Fresh: t.fresh, Clock: time.Now},
			Store: watch.Store{Store: s, Workspace: t.workspace, Limit: t.limit},
		}
		return src, s, func() { _ = metrics.Close(); client.Close() }, nil
	},
	term:  func(pid int) error { return syscall.Kill(pid, syscall.SIGTERM) },
	alive: func(pid int) bool { err := syscall.Kill(pid, 0); return err == nil || errors.Is(err, syscall.EPERM) },
	clock: time.Now,
}

// run is the command, its exit code and what went wrong.
func run(ctx context.Context, args []string, out io.Writer, d deps) (int, error) {
	fs := flag.NewFlagSet("fastpathwatch", flag.ContinueOnError)
	var t target
	fs.StringVar(&t.database, "database", "", "the service's database: projects/P/instances/I/databases/D")
	fs.StringVar(&t.project, "project", "", "the project whose metrics Monitoring has")
	fs.StringVar(&t.instance, "instance", "", "the Spanner instance whose CPU is watched")
	subs := fs.String("subscriptions", "", "the subscriptions whose backlogs are watched, separated by commas")
	fs.StringVar(&t.workspace, "workspace", "", "the stage's workspace, turned off once the stage stops")
	fs.DurationVar(&t.window, "window", 10*time.Minute, "how far back each look reads Monitoring: at least -misses "+
		"times the larger of -every and -timeout, plus the larger of -fresh and Monitoring's reporting delay of three "+
		"minutes, so no sample falls between the looks that answer")
	fs.DurationVar(&t.fresh, "fresh", 5*time.Minute, "how old Monitoring's newest sample may be, its point's time "+
		"plus the minute it stands for: older is a failed read; above a minute, at most the window")
	fs.IntVar(&t.limit, "pending-limit", 10_000, "the most pending packs a look reads; more fails the look")
	var c watch.Ceilings
	fs.Float64Var(&c.SpannerCPU, "max-cpu", 0, "Spanner's high-priority CPU, 0 to 1, past which the stage stops")
	fs.Int64Var(&c.Undelivered, "max-undelivered", 0, "a subscription's undelivered messages past which it stops")
	fs.DurationVar(&c.OldestAge, "max-oldest", 0, "a subscription's oldest unacknowledged message's age past which it stops")
	fs.DurationVar(&c.Overdue, "overdue", 0, "how long a pack's work may be pending before it is overdue")
	fs.IntVar(&c.PendingOverdue, "max-overdue", 0, "how many packs may be overdue at once")
	fs.Int64Var(&c.Spend, "max-spend", 0, "what the stage's workspace may book from the watch's start")
	every := fs.Duration("every", 30*time.Second, "how often the watch looks")
	timeout := fs.Duration("timeout", 30*time.Second, "how long each read of a look may take")
	misses := fs.Int("misses", 3, "how many looks in a row may fail before the watch stops the stage")
	pid := fs.Int("stop-pid", 0, "the load generator's process, stopped first; 0 for none")
	grace := fs.Duration("stop-wait", time.Minute, "how long the load generator has to exit once told to")
	if err := fs.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return 0, nil
		}
		return 2, err
	}
	for _, s := range strings.Split(*subs, ",") {
		if s = strings.TrimSpace(s); s != "" {
			t.subscriptions = append(t.subscriptions, s)
		}
	}
	switch {
	case fs.NArg() != 0:
		return 2, fmt.Errorf("arguments past the flags: %q", fs.Args())
	case t.database == "" || t.project == "" || t.instance == "" || t.workspace == "" || len(t.subscriptions) == 0:
		return 2, errors.New("-database, -project, -instance, -workspace and -subscriptions")
	case t.window < watch.Align || t.fresh <= watch.Align || t.fresh > t.window || t.limit < 1 || *pid < 0 || *grace <= 0:
		return 2, errors.New("-window at least a minute, -fresh above a minute and at most -window, -pending-limit " +
			"at least 1, -stop-pid at least 0 and -stop-wait above 0")
	case *every <= 0 || *timeout <= 0 || *misses < 1:
		return 2, errors.New("-every and -timeout above 0, and -misses at least 1")
	case t.window < time.Duration(*misses)*max(*every, *timeout)+max(t.fresh, watch.ReportingDelay):
		// Looks that answer are at most -misses looks apart, a look at most
		// the larger of -every and -timeout after the last, and a sample
		// shows up to Monitoring's reporting delay late, or -fresh if that
		// is longer: a window shorter than that leaves samples no look
		// reads.
		return 2, fmt.Errorf("-window %v is under -misses times the larger of -every and -timeout, plus the larger "+
			"of -fresh and Monitoring's reporting delay: %v",
			t.window, time.Duration(*misses)*max(*every, *timeout)+max(t.fresh, watch.ReportingDelay))
	}
	if err := c.Valid(); err != nil {
		return 2, err
	}
	src, s, closeAll, err := d.open(ctx, t)
	if err != nil {
		if ctx.Err() != nil {
			// Stopped as it began: it has stopped nothing.
			return 0, nil
		}
		return 1, err
	}
	defer closeAll()
	w, err := watch.New(ctx, watch.Config{Ceilings: c, Sources: src, Every: *every, Timeout: *timeout, Misses: *misses,
		Clock: d.clock})
	if err != nil {
		if ctx.Err() != nil {
			// Stopped as it began: it has stopped nothing.
			return 0, nil
		}
		return 1, err
	}
	why := w.Watch(ctx, func([]string) {})
	if why == nil {
		return 0, nil
	}
	// The stage stops: the load first, so nothing more is sent, then the
	// workspace, whose leases are revoked with it. The watch's own context
	// may be ending, so the workspace has a deadline of its own.
	answer := map[string]any{"why": why, "workspace": t.workspace}
	if *pid > 0 {
		answer["load"] = stopLoad(d, *pid, *grace)
	}
	octx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	revoked, at, err := s.SetWorkspace(octx, t.workspace, false)
	if err != nil {
		answer["turn_off_failed"] = err.Error()
		_ = json.NewEncoder(out).Encode(answer)
		return 1, fmt.Errorf("turning %s off: %w", t.workspace, err)
	}
	answer["revoked_leases"], answer["turned_off_at"] = revoked, at
	if err := json.NewEncoder(out).Encode(answer); err != nil {
		return 1, err
	}
	return stopped, nil
}

// stopLoad tells the load generator to stop and waits for it to exit, for
// at most grace, and says how it went.
func stopLoad(d deps, pid int, grace time.Duration) string {
	if err := d.term(pid); err != nil {
		return fmt.Sprintf("not told to stop: %v", err)
	}
	for deadline := d.clock().Add(grace); d.clock().Before(deadline); time.Sleep(100 * time.Millisecond) {
		if !d.alive(pid) {
			return "exited"
		}
	}
	return fmt.Sprintf("still running %v after it was told to stop", grace)
}
