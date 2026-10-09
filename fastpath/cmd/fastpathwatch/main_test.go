package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
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

func (s sources) SpannerCPU(context.Context) (float64, error) { return s.cpu, nil }
func (s sources) Backlogs(context.Context) (map[string]watch.Backlog, error) {
	return map[string]watch.Backlog{"auditor": {}}, nil
}
func (s sources) Pending(context.Context) ([]string, error) { return nil, nil }
func (s sources) Booked(context.Context) (int64, error)     { return 0, nil }

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

// load is a process standing for the load generator, reaped as it exits.
func load(t *testing.T) (*exec.Cmd, chan struct{}) {
	t.Helper()
	cmd := exec.Command("sleep", "60")
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
// generator, which exits, and then turns the workspace off, revoking its
// lease; the command says why and exits 3.
func TestAStagePastACeilingIsStopped(t *testing.T) {
	ws, ref, d := stage(t, sources{cpu: 0.5})
	cmd, exited := load(t)
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
	full := append(flags, "-workspace", "ws")
	cases := [][]string{append(full, "extra"), append(full, "-window", "30s"),
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
