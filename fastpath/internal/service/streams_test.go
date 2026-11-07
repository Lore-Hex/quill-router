package service

import (
	"context"
	"crypto/sha256"
	"net"
	"net/http"
	"strconv"
	"testing"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/frontdoor"
	"github.com/Lore-Hex/quill-router/fastpath/internal/loadgen"
	"github.com/Lore-Hex/quill-router/fastpath/internal/owner"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// TestTheDefaultsCarryTheStreamReapFloor: a node's owner reaps a stream's
// hold no sooner than the control plane's figures for the same stream, the
// reservation's 2 h and a heartbeat's 300 s renewal, plus the store's skew
// allowance (docs/design/fast-admission-streams.md section 4.6 item 4), and
// from increment 2b writes the floor into its records, so the auditor that
// drains a lease honours it too. This release is pinned only once every
// member runs increment 2's, which reads the field
// (docs/runbooks/fastpath-deploy.md, "A record field the auditors must read
// first").
func TestTheDefaultsCarryTheStreamReapFloor(t *testing.T) {
	c := Defaults()
	want := owner.StreamReap{AfterAdmission: 2 * time.Hour, AfterHeartbeat: 300 * time.Second}
	if c.Owner.StreamReap != want {
		t.Fatalf("the defaults' floor %+v, want %+v", c.Owner.StreamReap, want)
	}
	if !c.Owner.RecordReapFloor {
		t.Fatal("increment 2b's defaults do not write the reap floor into the records")
	}
	if c.Store.Skew <= 0 {
		t.Fatalf("the store's skew allowance %v, which the floor carries", c.Store.Skew)
	}
}

// TestTheDefaultsRecordAStreamsCeiling: a node's owner runs a stream
// admitted with a ceiling to that ceiling and states it in the stream's
// records (docs/design/fast-admission-streams.md section 12.4, PR 3). This
// release is pinned only once every member runs PR 2's, which reads the
// field (docs/runbooks/fastpath-deploy.md, "A record field the auditors must
// read first").
func TestTheDefaultsRecordAStreamsCeiling(t *testing.T) {
	if !Defaults().Owner.RecordCeiling {
		t.Fatal("PR 3's defaults do not record a stream's ceiling")
	}
}

// TestAStreamRunsPastItsHoldToItsCeiling (section 12.4, PR 3): one process
// with both roles and the defaults' owner, its gateway's requests over
// HTTP. Two streams are admitted with a hold of 40 and a ceiling of 100;
// each heartbeats a running charge of 70, past its hold, which is accepted,
// and one past its ceiling, which is refused. One settles 90; the other is
// abandoned, and its owner reaps it at 70. The auditor applies the records
// that carry the ceiling and books both, so the lease has consumed 160.
func TestAStreamRunsPastItsHoldToItsCeiling(t *testing.T) {
	if emulator == nil {
		t.Skip(skipped)
	}
	ctx := context.Background()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	ws := workspace(t, 100_000)
	cfg := config(ln)
	// The least grace the store takes with the default publish deadline and
	// skew, short heartbeat deadlines, reap rounds every second, and no
	// stream reap floor, so the abandoned stream is reaped soon.
	cfg.Store.Grace, cfg.Owner.RenewEvery, cfg.Owner.HeartbeatEvery = 10*time.Second, time.Second, 2*time.Second
	cfg.Owner.StreamReap = owner.StreamReap{}
	start(t, cfg, Clients{Spanner: shared, PubSub: pubSub(t)})

	gw := frontdoor.Gateway{Client: &http.Client{Timeout: 10 * time.Second}, Base: "http://" + ln.Addr().String()}
	streamed := func(request string) (frontdoor.Envelope, string) {
		t.Helper()
		var got frontdoor.Authorized
		eventually(t, 20*time.Second, "an admission", func() (bool, error) {
			var err error
			got, err = gw.Authorize(ctx, frontdoor.AuthorizeOf{Workspace: ws, Request: request, Estimate: 40,
				Stream: true, Boot: []byte("boot"), Ceiling: 100})
			return err == nil && got.Status == frontdoor.Admitted, nil
		})
		e, err := frontdoor.Open(key, got.Envelope)
		if err != nil {
			t.Fatal(err)
		}
		for seq, c := range []struct {
			running int64
			want    frontdoor.Status
		}{{70, frontdoor.Accepted}, {101, frontdoor.Rejected}} {
			hash := sha256.Sum256([]byte(request + "/" + strconv.Itoa(seq+1)))
			hb := frontdoor.HeartbeatOf{Envelope: got.Envelope, GatewaySeq: int64(seq + 1), Hash: hash[:],
				Usage: int64(10 * (seq + 1)), Running: c.running, EchoGranted: seq > 0}
			if seq == 0 {
				hb.Basis = []byte(`{"request":"` + request + `","boot":"boot"}`)
			}
			answer, err := gw.Heartbeat(ctx, hb)
			if err != nil || answer.Status != c.want {
				t.Fatalf("%s: a running charge of %d against a hold of 40 and a ceiling of 100: %+v %v, want %s",
					request, c.running, answer, err, c.want)
			}
		}
		return e, got.Envelope
	}
	settled, sealed := streamed("r1")
	reaped, _ := streamed("r2")
	if settled.Lease != reaped.Lease {
		t.Fatalf("the streams are on leases %s and %s", settled.Lease, reaped.Lease)
	}
	got, err := gw.Settle(ctx, frontdoor.SettleOf{Envelope: sealed, Charge: 90,
		Full: []byte(`{"request":"r1","boot":"boot","charge":90}`), Money: []byte(`{"cost":90}`)})
	if err != nil || got.Status != frontdoor.Won || got.Charge != 90 {
		t.Fatalf("the settle: %+v %v", got, err)
	}

	s, err := store.New(shared, cfg.Store)
	if err != nil {
		t.Fatal(err)
	}
	for _, c := range []struct {
		e       frontdoor.Envelope
		outcome string
		cost    int64
	}{{settled, "settled", 90}, {reaped, "reaped_snapshot", 70}} {
		eventually(t, time.Minute, c.outcome+" booked", func() (bool, error) {
			d, err := s.Disposition(ctx, c.e.Auth)
			return err == nil && d.Outcome == c.outcome && d.Cost.Valid && d.Cost.Int64 == c.cost, err
		})
	}
	ref := store.LeaseRef{Workspace: ws, LeaseID: settled.Lease}
	eventually(t, 30*time.Second, "the lease's books", func() (bool, error) {
		lease, _, err := s.ReadLease(ctx, ref)
		return err == nil && lease.Consumed == 160, err
	})

	// The load generator plays streams past their hold against the node:
	// each a hold of 40 and a ceiling of 100, heartbeating 40 and then 80 of
	// its bill of 80, every heartbeat accepted and every settle won.
	rep, err := loadgen.Run(ctx, loadgen.Config{Gateways: []loadgen.Gateway{gw}, Rate: 20,
		Duration: 250 * time.Millisecond, MaxInFlight: 10, Workspaces: []string{ws},
		Mix: loadgen.Mix{StreamShare: 1, Heartbeats: []loadgen.Weight{{Value: 2, Weight: 1}},
			Estimates: []loadgen.Weight{{Value: 40, Weight: 1}}, BillPermill: []loadgen.Weight{{Value: 2000, Weight: 1}}},
		HeartbeatEvery: 100 * time.Millisecond, EchoGranted: true, CeilingPermill: 2500, HeartbeatWait: 5 * time.Second,
		Boot: []byte("boot"), Enclaves: 1, RetryQueue: 16, RetryDelays: []time.Duration{0}, CallWait: 10 * time.Second,
		Seed: 1})
	if err != nil {
		t.Fatal(err)
	}
	if n := rep.Started; n == 0 || rep.Outcomes["authorize admitted"] != n || rep.Outcomes["heartbeat accepted"] != 2*n ||
		rep.Outcomes["stream complete"] != n || rep.Outcomes["settle won"] != n {
		t.Fatalf("the load generator's streams past their hold: %+v", rep)
	}
}

// TestTheOwnerTakesTheStoresSkewAllowance: the owner the service builds
// reads the store's skew allowance, the one its reap floor adds, as it
// reads the grace the floor's cap uses.
func TestTheOwnerTakesTheStoresSkewAllowance(t *testing.T) {
	c := Defaults()
	c.Store.Skew = 7 * time.Second
	c.Store.Grace = 3 * time.Minute
	c = c.agreed()
	if c.Owner.Skew != c.Store.Skew || c.Owner.Grace != c.Store.Grace {
		t.Fatalf("the owner's skew %v and grace %v, the store's %v and %v", c.Owner.Skew, c.Owner.Grace,
			c.Store.Skew, c.Store.Grace)
	}
}
