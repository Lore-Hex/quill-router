package service

import (
	"context"
	"errors"
	"net"
	"strings"
	"testing"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// stallingRing is the ring's store, whose join or read of the members
// stalls when told to: until its context ends, or, deaf to it, until
// released. Its heartbeats are written at once.
type stallingRing struct {
	stallJoin, stallMembers bool
	// deaf, when set, is what a stalled call waits for instead of its
	// context.
	deaf <-chan struct{}
}

func (r *stallingRing) stall(ctx context.Context, stalls bool) error {
	switch {
	case !stalls:
		return nil
	case r.deaf != nil:
		<-r.deaf
		return errors.New("released")
	}
	<-ctx.Done()
	return ctx.Err()
}

func (r *stallingRing) Join(ctx context.Context, address string, roles []string) (int64, time.Time, error) {
	if err := r.stall(ctx, r.stallJoin); err != nil {
		return 0, time.Time{}, err
	}
	return 1, time.Now(), nil
}

func (r *stallingRing) Heartbeat(context.Context, string, int64, string) (bool, time.Time, error) {
	return true, time.Now(), nil
}

func (r *stallingRing) Members(ctx context.Context) ([]store.Member, time.Time, error) {
	if err := r.stall(ctx, r.stallMembers); err != nil {
		return nil, time.Time{}, err
	}
	return []store.Member{{Address: "a", Epoch: 1, Roles: []string{"auditor"}, State: store.Serving, Live: true}},
		time.Now(), nil
}

// startConfig is a process's configuration for the start's tests: its
// ring's store the one given, its Spanner calls' deadline short.
func startConfig(t *testing.T, admission bool, r *stallingRing) Config {
	cfg := Defaults()
	cfg.Admission, cfg.Auditor, cfg.Address = admission, !admission, "127.0.0.1:1"
	cfg.ringStore, cfg.startDeadline = r, 100*time.Millisecond
	if admission {
		ln, err := net.Listen("tcp", "127.0.0.1:0")
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { _ = ln.Close() })
		cfg.Listener = ln
	}
	return cfg
}

// startPart starts the admission node or the auditor member as Run does,
// with no Spanner client or Pub/Sub behind it: a start that gets past its
// ring calls would fail, so these tests stall one of them.
func startPart(p *parts, cfg Config) {
	defer p.begin(cfg)()
	var err error
	if cfg.Admission {
		err = p.admission(cfg, nil, nil, nil)
	} else {
		err = p.auditor(cfg, Clients{}, nil, nil, nil)
	}
	if err != nil {
		p.startFailed(p.caller, err)
	}
}

// TestAStartWhoseRingCallStallsFails: the ring row's join, the auditor
// member's or the admission node's, and the admission node's first read of
// the members, each stalled until its context ends, fail the start within
// StartDeadline, so that the process exits and systemd starts it again,
// though Run's context has no deadline.
func TestAStartWhoseRingCallStallsFails(t *testing.T) {
	for _, tc := range []struct {
		name      string
		admission bool
		ring      stallingRing
		what      string
	}{
		{"an auditor member's join", false, stallingRing{stallJoin: true}, "joining the ring"},
		{"an admission node's join", true, stallingRing{stallJoin: true}, "joining the ring"},
		{"an admission node's read of the members", true, stallingRing{stallMembers: true}, "reading the members"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			cfg := startConfig(t, tc.admission, &tc.ring)
			done := make(chan error, 1)
			go func() { done <- lifecycle(context.Background(), func(p *parts) { startPart(p, cfg) }) }()
			select {
			case err := <-done:
				if !errors.Is(err, context.DeadlineExceeded) || !strings.Contains(err.Error(), tc.what) {
					t.Fatalf("a start whose call stalled: %v", err)
				}
			case <-time.After(10 * time.Second):
				t.Fatal("the start did not end: its stalled call had no deadline")
			}
		})
	}
	if StartDeadline != 15*time.Second || StartStall != 4*StartDeadline {
		t.Fatalf("StartDeadline is %v and StartStall %v", StartDeadline, StartStall)
	}
}

// TestAStartDeafToItsDeadlineExits: a join that does not heed its context
// holds the start past StartStall, which fails the process, so the edge
// answers the balancer that it no longer serves, and the process exits
// with code 1 within FailStop though the join still holds it.
func TestAStartDeafToItsDeadlineExits(t *testing.T) {
	release := make(chan struct{})
	cfg := startConfig(t, false, &stallingRing{stallJoin: true, deaf: release})
	exited := make(chan int, 2)
	cfg.Exit, cfg.failStop, cfg.startStall = func(code int) { exited <- code }, 100*time.Millisecond,
		300*time.Millisecond
	started := make(chan *parts, 1)
	done := make(chan error, 1)
	go func() {
		done <- lifecycle(context.Background(), func(p *parts) {
			started <- p
			startPart(p, cfg)
		})
	}()
	p := <-started
	select {
	case code := <-exited:
		if code != 1 {
			t.Fatalf("exited with %d", code)
		}
	case <-time.After(10 * time.Second):
		close(release)
		t.Fatal("the process did not exit: its start had no limit")
	}
	if p.serving() {
		t.Fatal("the edge answers serving with the start stalled past its limit")
	}
	select {
	case err := <-done:
		t.Fatalf("the start ended though its join is deaf: %v", err)
	default:
	}
	close(release)
	if err := <-done; !errors.Is(err, ErrStartStalled) {
		t.Fatalf("a stalled start: %v", err)
	}
}

// TestAStartWhoseCallsAnswerRuns is the positive control: a ring that
// answers at once and a switch read at once start the parts, which run on
// well past StartStall, serving, and stop cleanly when the process does.
func TestAStartWhoseCallsAnswerRuns(t *testing.T) {
	cfg := startConfig(t, false, &stallingRing{})
	exited := make(chan int, 2)
	cfg.Exit, cfg.failStop, cfg.startStall = func(code int) { exited <- code }, 100*time.Millisecond,
		200*time.Millisecond
	sw := &workspaceSwitch{every: time.Second, clock: time.Now,
		read: func(context.Context) (map[string]bool, time.Time, error) {
			return map[string]bool{"w": true}, time.Now(), nil
		}}
	ctx, cancel := context.WithCancel(context.Background())
	started := make(chan *parts, 1)
	done := make(chan error, 1)
	go func() {
		done <- lifecycle(ctx, func(p *parts) {
			defer p.begin(cfg)()
			node, err := p.join(cfg, nil)
			if err != nil {
				p.startFailed(ctx, err)
				return
			}
			p.stop(node.Stop)
			members, err := p.watchMembers(cfg, nil)
			if err != nil {
				p.startFailed(ctx, err)
				return
			}
			p.stop(members.Stop)
			if err := p.readSwitch(cfg, sw); err != nil {
				p.startFailed(ctx, err)
				return
			}
			if view, _ := members.View(); node.Epoch() != 1 || len(view.Members) != 1 || !sw.Enabled("w") {
				p.startFailed(ctx, errors.New("the start's reads were not kept"))
				return
			}
			p.run("part", func(ctx context.Context) error {
				<-ctx.Done()
				return nil
			})
			started <- p
		})
	}()
	var p *parts
	select {
	case p = <-started:
	case err := <-done:
		t.Fatalf("a start whose calls answered: %v", err)
	}
	time.Sleep(3 * cfg.startStall)
	if !p.serving() {
		t.Fatalf("a process that started is not serving: %v", p.ctx.Err())
	}
	select {
	case code := <-exited:
		t.Fatalf("a process that started exited with %d", code)
	default:
	}
	cancel()
	if err := <-done; err != nil {
		t.Fatalf("a process that started stopped with %v", err)
	}
}

// TestAStalledSwitchReadEndsWithinItsDeadline: the first read of the
// switch, stalled until its context ends, returns within StartDeadline,
// and the admission node goes on to serve, admitting for no workspace
// until a later read succeeds.
func TestAStalledSwitchReadEndsWithinItsDeadline(t *testing.T) {
	cfg := startConfig(t, false, &stallingRing{})
	sw := &workspaceSwitch{every: time.Second, clock: time.Now,
		read: func(ctx context.Context) (map[string]bool, time.Time, error) {
			<-ctx.Done()
			return nil, time.Time{}, ctx.Err()
		}}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() {
		_ = lifecycle(ctx, func(p *parts) {
			done <- p.readSwitch(cfg, sw)
		})
	}()
	select {
	case err := <-done:
		if !errors.Is(err, context.DeadlineExceeded) || sw.Enabled("w") {
			t.Fatalf("a stalled read of the switch: %v", err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("the switch's first read did not end: it had no deadline")
	}
}
