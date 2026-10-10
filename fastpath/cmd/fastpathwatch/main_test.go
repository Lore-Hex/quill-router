package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"slices"
	"strings"
	"testing"
	"time"

	"cloud.google.com/go/spanner"

	"github.com/Lore-Hex/quill-router/fastpath/internal/service"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store/storetest"
	"github.com/Lore-Hex/quill-router/fastpath/internal/watch"
)

var (
	emulator *storetest.Emulator
	skipped  string
	shared   *spanner.Client
)

func TestMain(m *testing.M) {
	ctx := context.Background()
	var err error
	if emulator, skipped, err = storetest.Start(ctx); err != nil {
		fmt.Fprintln(os.Stderr, "fastpathwatch tests:", err)
		os.Exit(1)
	}
	if emulator != nil {
		if shared, err = emulator.Database(ctx, "spike", nil); err != nil {
			fmt.Fprintln(os.Stderr, "fastpathwatch tests:", err)
			_ = emulator.Close(ctx)
			os.Exit(1)
		}
	}
	code := m.Run()
	if emulator != nil {
		shared.Close()
		if err := emulator.Close(ctx); err != nil {
			fmt.Fprintln(os.Stderr, "fastpathwatch tests: deleting the emulator's instance:", err)
			code = 1
		}
	}
	os.Exit(code)
}

// sources are fixed readings.
type sources struct{ cpu float64 }

func (s sources) SpannerCPU(context.Context) (float64, error)              { return s.cpu, nil }
func (s sources) Subscriptions() []string                                  { return []string{"auditor"} }
func (s sources) Undelivered(context.Context, string) (int64, error)       { return 0, nil }
func (s sources) OldestAge(context.Context, string) (time.Duration, error) { return 0, nil }
func (s sources) Pending(context.Context) ([]string, error)                { return nil, nil }
func (s sources) Booked(context.Context) (int64, error)                    { return 0, nil }

var flags = []string{"-database", "d", "-project", "p", "-instance", "i", "-subscriptions", "auditor",
	"-max-cpu", "0.35", "-every", "10ms", "-stop-wait", "5s"}

// stage is a workspace enabled for the fast path with an open lease, and
// the command's deps on the shared database with src as its readings.
func stage(t *testing.T, src watch.Sources) (string, store.LeaseRef, deps) {
	t.Helper()
	if shared == nil {
		t.Skip(skipped)
	}
	ctx := context.Background()
	s, err := store.New(shared, service.Defaults().Store)
	if err != nil {
		t.Fatal(err)
	}
	ws := storetest.UniqueID("ws")
	if _, err := shared.Apply(ctx, []*spanner.Mutation{storetest.Enabled(ws), spanner.InsertMap("tr_credit_balance",
		map[string]any{"workspace_id": ws, "shard": int64(0), "total_credits": int64(10_000), "trust_tier": int64(3)})}); err != nil {
		t.Fatal(err)
	}
	req := store.GrantRequest{Workspace: ws, LeaseID: store.NewLeaseID(), Region: "us-central1",
		Owner: store.Owner{Node: "node-a", Epoch: 1}, Amount: 100}
	if got, err := s.Grant(ctx, req); err != nil || got.Refused != "" {
		t.Fatalf("the grant: %+v %v", got, err)
	}
	d := production
	d.open = func(context.Context, target) (watch.Sources, *store.Store, func(), error) {
		return src, s, func() {}, nil
	}
	return ws, store.LeaseRef{Workspace: ws, LeaseID: req.LeaseID}, d
}

// load is a process standing for the load generator, which takes a second
// to exit once told to stop, as a run ending does, reaped as it exits.
func load(t *testing.T) (*exec.Cmd, chan struct{}) {
	t.Helper()
	cmd := exec.Command("sh", "-c", `trap 'sleep 1; exit 0' TERM; while :; do sleep 0.1; done`)
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	exited := make(chan struct{})
	go func() {
		_ = cmd.Wait()
		close(exited)
	}()
	t.Cleanup(func() {
		_ = cmd.Process.Kill()
		<-exited
	})
	return cmd, exited
}

// TestAStagePastACeilingIsStopped: a look past a ceiling stops the load
// generator, which exits, and only then turns the workspace off, revoking
// its lease; the command says why and exits 3.
func TestAStagePastACeilingIsStopped(t *testing.T) {
	ws, ref, d := stage(t, sources{cpu: 0.5})
	cmd, exited := load(t)
	term, alive := d.term, d.alive
	d.term = func(pid int) error {
		if !enabled(t, ws) {
			t.Error("the workspace was turned off before the load generator was told to stop")
		}
		return term(pid)
	}
	waited := 0
	d.alive = func(pid int) bool {
		a := alive(pid)
		if a {
			waited++
			if !enabled(t, ws) {
				t.Error("the workspace was turned off while the load generator still ran")
			}
		}
		return a
	}
	var out bytes.Buffer
	code, err := run(context.Background(), append(flags, "-workspace", ws, "-stop-pid", fmt.Sprint(cmd.Process.Pid)),
		&out, d)
	if code != stopped || err != nil {
		t.Fatalf("exit %d, %v", code, err)
	}
	select {
	case <-exited:
	default:
		t.Fatal("the load generator is still running")
	}
	var answer map[string]any
	if err := json.Unmarshal(out.Bytes(), &answer); err != nil {
		t.Fatalf("%q: %v", out.String(), err)
	}
	why, _ := json.Marshal(answer["why"])
	if !strings.Contains(string(why), "CPU") || answer["load"] != "exited" || answer["revoked_leases"] != float64(1) {
		t.Fatalf("the answer: %v", answer)
	}
	if enabled(t, ws) {
		t.Fatal("the workspace is still enabled after the stop")
	}
	if waited == 0 {
		t.Fatal("the load generator was never waited for as it exited")
	}
	s, _ := store.New(shared, service.Defaults().Store)
	if l, _, err := s.ReadLease(context.Background(), ref); err != nil || !l.Revoked {
		t.Fatalf("the lease after the stop: %+v %v", l, err)
	}
}

// enabled says whether the switch enables the workspace.
func enabled(t *testing.T, ws string) bool {
	t.Helper()
	s, _ := store.New(shared, service.Defaults().Store)
	on, _, err := s.EnabledWorkspaces(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	return on[ws]
}

// TestAWatchStoppedFirstStopsNothing: a watch whose own context ends before
// any look is past a ceiling exits 0, the load and the workspace as they were.
func TestAWatchStoppedFirstStopsNothing(t *testing.T) {
	ws, _, d := stage(t, sources{cpu: 0.1})
	cmd, exited := load(t)
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	code, err := run(ctx, append(flags, "-workspace", ws, "-stop-pid", fmt.Sprint(cmd.Process.Pid)), &bytes.Buffer{}, d)
	if code != 0 || err != nil {
		t.Fatalf("exit %d, %v", code, err)
	}
	select {
	case <-exited:
		t.Fatal("the load generator was stopped")
	default:
	}
	if !enabled(t, ws) {
		t.Fatal("the workspace was turned off")
	}
}

// TestALoadThatWillNotStopStillLosesItsWorkspace: a load generator still
// running after it was told to stop, and its wait, is said to be; the
// workspace is turned off all the same.
func TestALoadThatWillNotStopStillLosesItsWorkspace(t *testing.T) {
	ws, _, d := stage(t, sources{cpu: 0.5})
	d.term = func(int) error { return nil }
	d.alive = func(int) bool { return true }
	var out bytes.Buffer
	code, err := run(context.Background(), append(flags, "-workspace", ws, "-stop-pid", "4242", "-stop-wait", "300ms"),
		&out, d)
	if code != stopped || err != nil || !strings.Contains(out.String(), "still running") {
		t.Fatalf("exit %d, %v: %s", code, err, out.String())
	}
	if enabled(t, ws) {
		t.Fatal("the workspace is still enabled")
	}
}

// TestTheCommandRefusesWhatItCannotDo: each flag it needs missing, no
// ceiling, a window under a minute, and arguments past the flags are each
// exit 2, and none opens anything.
func TestTheCommandRefusesWhatItCannotDo(t *testing.T) {
	d := production
	d.open = func(context.Context, target) (watch.Sources, *store.Store, func(), error) {
		t.Fatal("opened")
		return nil, nil, nil, nil
	}
	full := append(slices.Clone(flags), "-workspace", "ws")
	with := func(more ...string) []string { return append(slices.Clone(full), more...) }
	cases := [][]string{with("extra"), with("-window", "30s"), with("-fresh", "11m"), with("-fresh", "0s"),
		with("-max-cpu", "NaN"), with("-max-cpu", "-0.1"), with("-max-cpu", "0", "-max-overdue", "1"), with("-fresh", "1m"),
		with("-every", "0s"), with("-timeout", "0s"), with("-misses", "0"),
		with("-every", "10m", "-window", "5m"), with("-timeout", "10m"), with("-misses", "10", "-window", "5m"),
		with("-every", "10s", "-timeout", "10s", "-fresh", "2m", "-window", "2m30s"),
		{"-database", "d", "-project", "p", "-instance", "i", "-subscriptions", "a", "-workspace", "ws"}}
	for _, name := range []string{"-database", "-project", "-instance", "-subscriptions", "-workspace"} {
		var without []string
		for i := 0; i < len(full); i += 2 {
			if full[i] != name {
				without = append(without, full[i], full[i+1])
			}
		}
		cases = append(cases, without)
	}
	for _, args := range cases {
		if code, err := run(context.Background(), args, &bytes.Buffer{}, d); code != 2 || err == nil {
			t.Errorf("%q: exit %d, %v", args, code, err)
		}
	}
}

// TestTheWorkspaceIsTurnedOffThoughTheWatchIsStopping: the watch's own
// context ending as it stops the load, or the load not told to stop at
// all, still leaves the workspace turned off.
func TestTheWorkspaceIsTurnedOffThoughTheWatchIsStopping(t *testing.T) {
	for _, failTerm := range []bool{false, true} {
		ws, _, d := stage(t, sources{cpu: 0.5})
		ctx, cancel := context.WithCancel(context.Background())
		d.term = func(int) error {
			cancel()
			if failTerm {
				return fmt.Errorf("no such process")
			}
			return nil
		}
		d.alive = func(int) bool { return false }
		var out bytes.Buffer
		code, err := run(ctx, append(slices.Clone(flags), "-workspace", ws, "-stop-pid", "4242"), &out, d)
		if code != stopped || err != nil {
			t.Fatalf("SIGTERM failing %v: exit %d, %v: %s", failTerm, code, err, out.String())
		}
		if failTerm != strings.Contains(out.String(), "not told to stop") {
			t.Fatalf("SIGTERM failing %v: %s", failTerm, out.String())
		}
		if enabled(t, ws) {
			t.Fatalf("SIGTERM failing %v: the workspace is still enabled", failTerm)
		}
	}
}

// cancelling are readings whose first read of the bookings is the moment
// the watch is itself stopped.
type cancelling struct {
	sources
	cancel func()
}

func (c cancelling) Booked(ctx context.Context) (int64, error) {
	c.cancel()
	<-ctx.Done()
	return 0, ctx.Err()
}

// TestAWatchStoppedAsItBeginsExitsZero: stopped while it reads what the
// stage has booked, as it begins, the watch exits 0, having stopped
// nothing, as the runbook says.
func TestAWatchStoppedAsItBeginsExitsZero(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	ws, _, d := stage(t, cancelling{sources{cpu: 0.5}, cancel})
	code, err := run(ctx, append(slices.Clone(flags), "-workspace", ws), &bytes.Buffer{}, d)
	if code != 0 || err != nil {
		t.Fatalf("exit %d, %v", code, err)
	}
	if !enabled(t, ws) {
		t.Fatal("the workspace was turned off")
	}
}

// TestTheWindowCoversTheLooks: looks that answer are at most -misses looks
// apart, a look at most the larger of -every and -timeout after the last,
// and a sample shows up to Monitoring's reporting delay late, or -fresh if
// that is longer; a window of exactly that is taken, a second less refused,
// so no sample falls between the looks.
func TestTheWindowCoversTheLooks(t *testing.T) {
	opened := errors.New("not opened")
	d := production
	d.open = func(context.Context, target) (watch.Sources, *store.Store, func(), error) {
		return nil, nil, nil, opened
	}
	base := []string{"-database", "d", "-project", "p", "-instance", "i", "-subscriptions", "a", "-workspace", "ws",
		"-max-cpu", "0.5", "-every", "1m", "-timeout", "30s", "-misses", "2"}
	for _, c := range []struct{ fresh, exact, short string }{{"4m", "6m", "5m59s"}, {"2m", "5m", "4m59s"}} {
		with := func(window string) []string { return append(slices.Clone(base), "-fresh", c.fresh, "-window", window) }
		if code, err := run(context.Background(), with(c.exact), &bytes.Buffer{}, d); code != 1 || !errors.Is(err, opened) {
			t.Fatalf("-fresh %s: a window of exactly two looks and the delay: exit %d, %v", c.fresh, code, err)
		}
		if code, err := run(context.Background(), with(c.short), &bytes.Buffer{}, d); code != 2 || err == nil ||
			!strings.Contains(err.Error(), "-window") {
			t.Fatalf("-fresh %s: a window a second short: exit %d, %v", c.fresh, code, err)
		}
	}
}

// TestAWatchStoppedAsItOpensExitsZero: stopped while it opens its clients,
// the watch exits 0, having stopped nothing; opening that fails on its own
// exits 1.
func TestAWatchStoppedAsItOpensExitsZero(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	d := production
	d.open = func(ctx context.Context, _ target) (watch.Sources, *store.Store, func(), error) {
		cancel()
		return nil, nil, nil, ctx.Err()
	}
	args := append(slices.Clone(flags), "-workspace", "ws")
	if code, err := run(ctx, args, &bytes.Buffer{}, d); code != 0 || err != nil {
		t.Fatalf("stopped as it opens: exit %d, %v", code, err)
	}
	d.open = func(context.Context, target) (watch.Sources, *store.Store, func(), error) {
		return nil, nil, nil, errors.New("no database")
	}
	if code, err := run(context.Background(), args, &bytes.Buffer{}, d); code != 1 || err == nil {
		t.Fatalf("opening that fails: exit %d, %v", code, err)
	}
}
