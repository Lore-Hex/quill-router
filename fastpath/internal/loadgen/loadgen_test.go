package loadgen

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/frontdoor"
	"github.com/Lore-Hex/quill-router/fastpath/internal/record"
)

var key = bytes.Repeat([]byte("k"), frontdoor.MinKeySize)

// fakeGateway answers as its functions say, and keeps what it was sent.
type fakeGateway struct {
	mu         sync.Mutex
	authorizes []frontdoor.AuthorizeOf
	heartbeats []frontdoor.HeartbeatOf
	settles    []frontdoor.SettleOf
	refunds    []frontdoor.RefundOf
	authorize  func(frontdoor.AuthorizeOf) (frontdoor.Authorized, error)
	heartbeat  func(frontdoor.HeartbeatOf) (frontdoor.HeartbeatAnswer, error)
	terminal   func(attempt int) (frontdoor.TerminalAnswer, error)
}

func (f *fakeGateway) Authorize(_ context.Context, a frontdoor.AuthorizeOf) (frontdoor.Authorized, error) {
	f.mu.Lock()
	f.authorizes = append(f.authorizes, a)
	f.mu.Unlock()
	return f.authorize(a)
}

func (f *fakeGateway) Heartbeat(_ context.Context, hb frontdoor.HeartbeatOf) (frontdoor.HeartbeatAnswer, error) {
	f.mu.Lock()
	f.heartbeats = append(f.heartbeats, hb)
	f.mu.Unlock()
	return f.heartbeat(hb)
}

func (f *fakeGateway) Settle(_ context.Context, s frontdoor.SettleOf) (frontdoor.TerminalAnswer, error) {
	f.mu.Lock()
	f.settles = append(f.settles, s)
	n := len(f.settles)
	f.mu.Unlock()
	return f.terminal(n)
}

func (f *fakeGateway) Refund(_ context.Context, r frontdoor.RefundOf) (frontdoor.TerminalAnswer, error) {
	f.mu.Lock()
	f.refunds = append(f.refunds, r)
	n := len(f.refunds)
	f.mu.Unlock()
	return f.terminal(n)
}

// admitting is a gateway that admits every authorize under lease-1, sealing
// its envelope with key, accepts every heartbeat with a deadline a second
// on, and lets every terminal win.
func admitting(t *testing.T) *fakeGateway {
	t.Helper()
	return &fakeGateway{
		authorize: func(a frontdoor.AuthorizeOf) (frontdoor.Authorized, error) {
			sealed, err := frontdoor.Seal(key, frontdoor.Envelope{Auth: "gwa-" + a.Request, Workspace: a.Workspace,
				Lease: "lease-1", Owner: "node-a", Estimate: a.Estimate, Stream: a.Stream,
				EndOfLife: time.Now().Add(time.Hour)})
			if err != nil {
				t.Error(err)
			}
			return frontdoor.Authorized{Status: frontdoor.Admitted, Envelope: sealed}, nil
		},
		heartbeat: func(hb frontdoor.HeartbeatOf) (frontdoor.HeartbeatAnswer, error) {
			return frontdoor.HeartbeatAnswer{Status: frontdoor.Accepted,
				Deadline: time.Date(2026, 10, 8, 12, 0, int(hb.GatewaySeq), 0, time.UTC)}, nil
		},
		terminal: func(int) (frontdoor.TerminalAnswer, error) {
			return frontdoor.TerminalAnswer{Status: frontdoor.Won, Kind: record.Settle, Charge: 125}, nil
		},
	}
}

// config is a run of one generation, streaming three heartbeats, settling
// 125 against a hold of 100.
func config(gw Gateway) Config {
	return Config{Gateways: []Gateway{gw}, Rate: 1000, Duration: time.Millisecond, MaxInFlight: 10,
		Workspaces: []string{"ws-1"}, HeartbeatEvery: time.Millisecond, Boot: []byte("boot"),
		RetryEvery: time.Millisecond, RetryFor: time.Second, CallWait: time.Second, Key: key, Seed: 7,
		Mix: Mix{StreamShare: 1, Heartbeats: []Weight{{3, 1}}, RefundShare: 0, Estimates: []Weight{{100, 1}},
			BillPermill: []Weight{{1250, 1}}}}
}

// TestAGenerationIsPlayedThrough: a generation authorizes with its boot
// binding, heartbeats its stream, each with its sequence, its snapshot's
// hash, the usage and running charge so far within the hold, and the
// deadline the last granted, the first with the reap's basis; and then
// settles its bill, its full record stating the boot binding.
func TestAGenerationIsPlayedThrough(t *testing.T) {
	gw := admitting(t)
	var log bytes.Buffer
	cfg := config(gw)
	cfg.Log = &log
	rep, err := Run(context.Background(), cfg)
	if err != nil {
		t.Fatal(err)
	}
	if rep.Started != 1 || len(gw.authorizes) != 1 {
		t.Fatalf("started %d, %d authorizes", rep.Started, len(gw.authorizes))
	}
	if a := gw.authorizes[0]; a.Workspace != "ws-1" || a.Estimate != 100 || !a.Stream || string(a.Boot) != "boot" {
		t.Fatalf("the authorize: %+v", a)
	}
	if len(gw.heartbeats) != 3 {
		t.Fatalf("%d heartbeats", len(gw.heartbeats))
	}
	var last frontdoor.HeartbeatOf
	for i, hb := range gw.heartbeats {
		seq := int64(i + 1)
		echoed := time.Time{}
		if i > 0 {
			echoed = time.Date(2026, 10, 8, 12, 0, i, 0, time.UTC)
		}
		if hb.GatewaySeq != seq || len(hb.Hash) != 32 || hb.Running > 100 || hb.Running < last.Running ||
			hb.Usage <= last.Usage || !hb.Echoed.Equal(echoed) || (i == 0) != (len(hb.Basis) > 0) {
			t.Fatalf("heartbeat %d: %+v", i, hb)
		}
		last = hb
	}
	if len(gw.settles) != 1 || gw.settles[0].Charge != 125 {
		t.Fatalf("the settles: %+v", gw.settles)
	}
	var full struct {
		Boot []byte `json:"boot"`
	}
	if err := json.Unmarshal(gw.settles[0].Full, &full); err != nil || string(full.Boot) != "boot" {
		t.Fatalf("the full record %s: %v", gw.settles[0].Full, err)
	}
	var g Generation
	if err := json.Unmarshal(bytes.TrimSpace(log.Bytes()), &g); err != nil {
		t.Fatal(err)
	}
	if g.Auth != "gwa-"+g.Request || g.Lease != "lease-1" || g.Authorized != "admitted" ||
		strings.Join(g.Heartbeats, ",") != "accepted,accepted,accepted" || g.Terminal == nil ||
		g.Terminal.Status != "won" || g.Terminal.Attempts != 1 || g.Terminal.Charge != 125 {
		t.Fatalf("the generation's record: %+v, terminal %+v", g, g.Terminal)
	}
	if rep.Outcomes["heartbeat accepted"] != 3 || rep.Outcomes["settle won"] != 1 ||
		rep.Latencies["authorize"].N != 1 || rep.Latencies["heartbeat"].N != 3 || rep.Latencies["settle"].N != 1 {
		t.Fatalf("the report: %+v", rep)
	}
}

// TestAStreamStopsAtAnAnswerNotAccepted: a heartbeat answered other than
// accepted, or not answered, ends the stream, and its terminal is sent
// still.
func TestAStreamStopsAtAnAnswerNotAccepted(t *testing.T) {
	for name, answer := range map[string]func(frontdoor.HeartbeatOf) (frontdoor.HeartbeatAnswer, error){
		"retry": func(hb frontdoor.HeartbeatOf) (frontdoor.HeartbeatAnswer, error) {
			if hb.GatewaySeq == 2 {
				return frontdoor.HeartbeatAnswer{Status: frontdoor.Retry}, nil
			}
			return frontdoor.HeartbeatAnswer{Status: frontdoor.Accepted, Deadline: time.Now()}, nil
		},
		"an error": func(hb frontdoor.HeartbeatOf) (frontdoor.HeartbeatAnswer, error) {
			if hb.GatewaySeq == 2 {
				return frontdoor.HeartbeatAnswer{}, errors.New("unreachable")
			}
			return frontdoor.HeartbeatAnswer{Status: frontdoor.Accepted, Deadline: time.Now()}, nil
		},
	} {
		gw := admitting(t)
		gw.heartbeat = answer
		if _, err := Run(context.Background(), config(gw)); err != nil {
			t.Fatal(err)
		}
		if len(gw.heartbeats) != 2 || len(gw.settles) != 1 {
			t.Fatalf("%s: %d heartbeats, %d settles", name, len(gw.heartbeats), len(gw.settles))
		}
	}
}

// TestATerminalIsRetriedAsTheEnclaveDoes: a terminal failed or not answered
// is sent again every RetryEvery until answered, or lost once RetryFor has
// passed; a refund is sent with no charge.
func TestATerminalIsRetriedAsTheEnclaveDoes(t *testing.T) {
	gw := admitting(t)
	gw.terminal = func(attempt int) (frontdoor.TerminalAnswer, error) {
		switch attempt {
		case 1:
			return frontdoor.TerminalAnswer{Status: frontdoor.Failed}, nil
		case 2:
			return frontdoor.TerminalAnswer{}, errors.New("unreachable")
		}
		return frontdoor.TerminalAnswer{Status: frontdoor.Recorded, Kind: record.Settle, Charge: 125}, nil
	}
	rep, err := Run(context.Background(), config(gw))
	if err != nil {
		t.Fatal(err)
	}
	if len(gw.settles) != 3 || rep.Outcomes["settle retried"] != 2 || rep.Outcomes["settle recorded"] != 1 {
		t.Fatalf("%d settles, %+v", len(gw.settles), rep.Outcomes)
	}

	lost := admitting(t)
	lost.terminal = func(int) (frontdoor.TerminalAnswer, error) {
		return frontdoor.TerminalAnswer{Status: frontdoor.Failed}, nil
	}
	cfg := config(lost)
	cfg.RetryEvery, cfg.RetryFor = 5*time.Millisecond, 30*time.Millisecond
	cfg.Mix.RefundShare = 1
	var log bytes.Buffer
	cfg.Log = &log
	rep, err = Run(context.Background(), cfg)
	if err != nil {
		t.Fatal(err)
	}
	if n := len(lost.refunds); n < 2 || n > 7 || rep.Outcomes["refund lost"] != 1 || len(lost.settles) != 0 {
		t.Fatalf("%d refunds, %+v", n, rep.Outcomes)
	}
	var g Generation
	if err := json.Unmarshal(bytes.TrimSpace(log.Bytes()), &g); err != nil || g.Terminal == nil ||
		g.Terminal.Kind != record.Refund || g.Terminal.Charge != 0 || !g.Terminal.Lost {
		t.Fatalf("the refund's record: %+v %v", g.Terminal, err)
	}
}

// TestAnAuthorizeNotAdmittedEndsTheGeneration: busy, invalid or an error
// sends nothing more.
func TestAnAuthorizeNotAdmittedEndsTheGeneration(t *testing.T) {
	for name, answer := range map[string]func(frontdoor.AuthorizeOf) (frontdoor.Authorized, error){
		"busy": func(frontdoor.AuthorizeOf) (frontdoor.Authorized, error) {
			return frontdoor.Authorized{Status: frontdoor.Busy}, nil
		},
		"an error": func(frontdoor.AuthorizeOf) (frontdoor.Authorized, error) {
			return frontdoor.Authorized{}, errors.New("down")
		},
	} {
		gw := admitting(t)
		gw.authorize = answer
		rep, err := Run(context.Background(), config(gw))
		if err != nil {
			t.Fatal(err)
		}
		if len(gw.heartbeats) != 0 || len(gw.settles) != 0 || len(gw.refunds) != 0 || rep.Started != 1 {
			t.Fatalf("%s: sent more, %+v", name, rep)
		}
	}
}

// TestTheRateIsKept: generations start at the rate for the duration, each
// on the gateways in turn; one past MaxInFlight does not start and is
// counted.
func TestTheRateIsKept(t *testing.T) {
	a, b := admitting(t), admitting(t)
	cfg := config(a)
	cfg.Gateways = []Gateway{a, b}
	cfg.Rate, cfg.Duration, cfg.Mix.StreamShare = 400, 250*time.Millisecond, 0
	rep, err := Run(context.Background(), cfg)
	if err != nil {
		t.Fatal(err)
	}
	if rep.Started != 100 || rep.NotStarted != 0 || len(a.authorizes) != 50 || len(b.authorizes) != 50 {
		t.Fatalf("started %d, not %d; %d and %d", rep.Started, rep.NotStarted, len(a.authorizes), len(b.authorizes))
	}

	slow := admitting(t)
	hold := make(chan struct{})
	slow.authorize = func(frontdoor.AuthorizeOf) (frontdoor.Authorized, error) {
		<-hold
		return frontdoor.Authorized{Status: frontdoor.Busy}, nil
	}
	cfg = config(slow)
	cfg.Rate, cfg.Duration, cfg.MaxInFlight = 400, 100*time.Millisecond, 1
	go func() {
		time.Sleep(200 * time.Millisecond)
		close(hold)
	}()
	rep, err = Run(context.Background(), cfg)
	if err != nil {
		t.Fatal(err)
	}
	if rep.Started != 1 || rep.NotStarted != 39 {
		t.Fatalf("started %d, not %d", rep.Started, rep.NotStarted)
	}
}

// TestTheLogHasEachGeneration: one JSON object a line, one a generation.
func TestTheLogHasEachGeneration(t *testing.T) {
	gw := admitting(t)
	var log bytes.Buffer
	cfg := config(gw)
	cfg.Rate, cfg.Duration, cfg.Log = 400, 50*time.Millisecond, &log
	rep, err := Run(context.Background(), cfg)
	if err != nil {
		t.Fatal(err)
	}
	seen := map[int64]bool{}
	sc := bufio.NewScanner(&log)
	for sc.Scan() {
		var g Generation
		if err := json.Unmarshal(sc.Bytes(), &g); err != nil || seen[g.N] {
			t.Fatalf("a line %q: %v", sc.Text(), err)
		}
		seen[g.N] = true
	}
	if int64(len(seen)) != rep.Started || rep.Started != 20 {
		t.Fatalf("%d logged, %d started", len(seen), rep.Started)
	}
}

// TestAMixIsRead: a mix reads from JSON; one with a share past 1 or a
// histogram of no weight does not.
func TestAMixIsRead(t *testing.T) {
	good := `{"stream_share":0.6,"heartbeats":[{"value":3,"weight":1}],"refund_share":0.1,` +
		`"estimates":[{"value":100,"weight":2},{"value":1000,"weight":1}],"bill_permill":[{"value":800,"weight":7},{"value":1200,"weight":1}]}`
	if m, err := ReadMix(strings.NewReader(good)); err != nil || m.StreamShare != 0.6 || len(m.Estimates) != 2 {
		t.Fatalf("%+v %v", m, err)
	}
	for name, bad := range map[string]string{
		"a share past 1":   strings.Replace(good, `"stream_share":0.6`, `"stream_share":1.5`, 1),
		"no weight":        strings.Replace(good, `[{"value":3,"weight":1}]`, `[{"value":3,"weight":0}]`, 1),
		"no heartbeat":     strings.Replace(good, `[{"value":3,"weight":1}]`, `[{"value":0,"weight":1}]`, 1),
		"an unknown field": strings.Replace(good, `"stream_share"`, `"extra":1,"stream_share"`, 1),
		"no estimates":     strings.Replace(good, `[{"value":100,"weight":2},{"value":1000,"weight":1}]`, `[]`, 1),
	} {
		if _, err := ReadMix(strings.NewReader(bad)); err == nil {
			t.Fatalf("%s: read", name)
		}
	}
}
