package loadgen

import (
	"bufio"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"math"
	"math/big"
	"math/rand/v2"
	"reflect"
	"slices"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/frontdoor"
	"github.com/Lore-Hex/quill-router/fastpath/internal/record"
)

var key = bytes.Repeat([]byte("k"), frontdoor.MinKeySize)

// fakeGateway answers as its functions say, and keeps what it was sent and
// when.
type fakeGateway struct {
	mu         sync.Mutex
	authorizes []frontdoor.AuthorizeOf
	heartbeats []frontdoor.HeartbeatOf
	settles    []frontdoor.SettleOf
	refunds    []frontdoor.RefundOf
	sealed     []string               // the envelopes its authorizes answered
	at         map[string][]time.Time // each call's time, by kind
	authorize  func(frontdoor.AuthorizeOf) (frontdoor.Authorized, error)
	heartbeat  func(frontdoor.HeartbeatOf) (frontdoor.HeartbeatAnswer, error)
	terminal   func(attempt int) (frontdoor.TerminalAnswer, error)
	// refund, when set, answers refunds in terminal's place.
	refund func(frontdoor.RefundOf) (frontdoor.TerminalAnswer, error)
}

func (f *fakeGateway) called(kind string) {
	if f.at == nil {
		f.at = map[string][]time.Time{}
	}
	f.at[kind] = append(f.at[kind], time.Now())
}

func (f *fakeGateway) Authorize(_ context.Context, a frontdoor.AuthorizeOf) (frontdoor.Authorized, error) {
	f.mu.Lock()
	f.authorizes = append(f.authorizes, a)
	f.called("authorize")
	f.mu.Unlock()
	return f.authorize(a)
}

func (f *fakeGateway) Heartbeat(_ context.Context, hb frontdoor.HeartbeatOf) (frontdoor.HeartbeatAnswer, error) {
	f.mu.Lock()
	f.heartbeats = append(f.heartbeats, hb)
	f.called("heartbeat")
	f.mu.Unlock()
	return f.heartbeat(hb)
}

func (f *fakeGateway) Settle(_ context.Context, s frontdoor.SettleOf) (frontdoor.TerminalAnswer, error) {
	f.mu.Lock()
	f.settles = append(f.settles, s)
	f.called("settle")
	n := len(f.settles)
	f.mu.Unlock()
	return f.terminal(n)
}

func (f *fakeGateway) Refund(_ context.Context, r frontdoor.RefundOf) (frontdoor.TerminalAnswer, error) {
	f.mu.Lock()
	f.refunds = append(f.refunds, r)
	f.called("refund")
	n := len(f.refunds)
	f.mu.Unlock()
	if f.refund != nil {
		return f.refund(r)
	}
	return f.terminal(n)
}

// admitting is a gateway that admits every authorize under lease-1, sealing
// its envelope with key, accepts every heartbeat with a deadline a second
// on, and lets every terminal win.
func admitting(t *testing.T) *fakeGateway {
	t.Helper()
	f := &fakeGateway{
		heartbeat: func(hb frontdoor.HeartbeatOf) (frontdoor.HeartbeatAnswer, error) {
			return frontdoor.HeartbeatAnswer{Status: frontdoor.Accepted,
				Deadline: time.Date(2026, 10, 8, 12, 0, int(hb.GatewaySeq), 0, time.UTC)}, nil
		},
		terminal: func(int) (frontdoor.TerminalAnswer, error) {
			return frontdoor.TerminalAnswer{Status: frontdoor.Won, Kind: record.Settle, Charge: 125}, nil
		},
	}
	f.authorize = func(a frontdoor.AuthorizeOf) (frontdoor.Authorized, error) {
		sealed, err := frontdoor.Seal(key, frontdoor.Envelope{Auth: "gwa-" + a.Request, Workspace: a.Workspace,
			Lease: "lease-1", Owner: "node-a", Estimate: a.Estimate, Stream: a.Stream,
			EndOfLife: time.Now().Add(time.Hour)})
		if err != nil {
			t.Error(err)
		}
		f.mu.Lock()
		f.sealed = append(f.sealed, sealed)
		f.mu.Unlock()
		return frontdoor.Authorized{Status: frontdoor.Admitted, Envelope: sealed}, nil
	}
	return f
}

func mustOpen(t *testing.T, sealed string) frontdoor.Envelope {
	t.Helper()
	e, err := frontdoor.Open(key, sealed)
	if err != nil {
		t.Fatal(err)
	}
	return e
}

// ownEnvelopes checks that each heartbeat and settle carries its own
// generation's envelope, which names its request ("gwa-" and the request,
// as admitting seals it): a heartbeat's hash is of its request and
// sequence, and a settle's full record names its request; and that every
// sealed envelope's refunds are one generation's.
func ownEnvelopes(t *testing.T, gws ...*fakeGateway) {
	t.Helper()
	for _, gw := range gws {
		for _, hb := range gw.heartbeats {
			request := strings.TrimPrefix(mustOpen(t, hb.Envelope).Auth, "gwa-")
			want := sha256.Sum256([]byte(request + "/" + strconv.FormatInt(hb.GatewaySeq, 10)))
			if !bytes.Equal(hb.Hash, want[:]) {
				t.Fatalf("heartbeat %d of %s carries another generation's envelope", hb.GatewaySeq, request)
			}
		}
		for _, s := range gw.settles {
			var full struct {
				Request string `json:"request"`
			}
			if err := json.Unmarshal(s.Full, &full); err != nil ||
				"gwa-"+full.Request != mustOpen(t, s.Envelope).Auth {
				t.Fatalf("a settle of %s carries another generation's envelope", full.Request)
			}
		}
	}
}

// echoed checks that every heartbeat, settle and refund the gateways were
// sent carries an envelope one of them sealed, and the one of its
// generation's authorize: with one generation, there is one.
func echoed(t *testing.T, gws ...*fakeGateway) {
	t.Helper()
	sealed := map[string]bool{}
	for _, gw := range gws {
		for _, e := range gw.sealed {
			sealed[e] = true
		}
	}
	for _, gw := range gws {
		for _, hb := range gw.heartbeats {
			if !sealed[hb.Envelope] {
				t.Fatalf("a heartbeat with an envelope %q no authorize sealed", hb.Envelope)
			}
		}
		for _, s := range gw.settles {
			if !sealed[s.Envelope] {
				t.Fatalf("a settle with an envelope %q no authorize sealed", s.Envelope)
			}
		}
		for _, r := range gw.refunds {
			if !sealed[r.Envelope] {
				t.Fatalf("a refund with an envelope %q no authorize sealed", r.Envelope)
			}
		}
	}
}

// config is a run of one generation, streaming three heartbeats, settling
// 125 against a hold of 100.
func config(gw Gateway) Config {
	return Config{Gateways: []Gateway{gw}, Rate: 1000, Duration: time.Millisecond, MaxInFlight: 10,
		Workspaces: []string{"ws-1"}, HeartbeatEvery: time.Millisecond, HeartbeatWait: time.Second,
		Boot: []byte("boot"), Enclaves: 1,
		RetryDelays: []time.Duration{time.Millisecond, time.Millisecond, time.Millisecond},
		CallWait:    time.Second, Key: key, Seed: 7,
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
	// The running charge is the bill so far, 125 over three heartbeats,
	// within the hold of 100.
	running := []int64{41, 83, 100}
	var last frontdoor.HeartbeatOf
	for i, hb := range gw.heartbeats {
		seq := int64(i + 1)
		echoed := time.Time{}
		if i > 0 {
			echoed = time.Date(2026, 10, 8, 12, 0, i, 0, time.UTC)
		}
		if hb.GatewaySeq != seq || len(hb.Hash) != 32 || hb.Running != running[i] || hb.Usage <= last.Usage ||
			!hb.Echoed.Equal(echoed) || (i == 0) != (len(hb.Basis) > 0) {
			t.Fatalf("heartbeat %d: %+v", i, hb)
		}
		last = hb
	}
	// The first heartbeat's basis has what a reap's full record needs: the
	// request's terms and the boot binding.
	var basis struct {
		Request  string `json:"request"`
		Model    string `json:"model"`
		Estimate int64  `json:"estimate"`
		Boot     []byte `json:"boot"`
	}
	if err := json.Unmarshal(gw.heartbeats[0].Basis, &basis); err != nil || basis.Request == "" ||
		basis.Request != strings.TrimPrefix(mustOpen(t, gw.heartbeats[0].Envelope).Auth, "gwa-") ||
		basis.Model == "" || basis.Estimate != 100 || string(basis.Boot) != "boot" {
		t.Fatalf("the reap's basis %s: %v", gw.heartbeats[0].Basis, err)
	}
	if len(gw.settles) != 1 || gw.settles[0].Charge != 125 || len(gw.sealed) != 1 {
		t.Fatalf("the settles: %+v", gw.settles)
	}
	echoed(t, gw)
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
	beats := []Beat{{1, []string{"accepted"}}, {2, []string{"accepted"}}, {3, []string{"accepted"}}}
	if g.Auth != "gwa-"+g.Request || g.Lease != "lease-1" || g.Authorized != "admitted" ||
		!reflect.DeepEqual(g.Heartbeats, beats) || g.Streamed != "complete" || g.Terminal == nil ||
		g.Terminal.Status != "won" || !slices.Equal(g.Terminal.Tries, []string{"won"}) || g.Terminal.Charge != 125 {
		t.Fatalf("the generation's record: %+v, terminal %+v", g, g.Terminal)
	}
	if rep.Outcomes["heartbeat accepted"] != 3 || rep.Outcomes["stream complete"] != 1 ||
		rep.Outcomes["settle attempt won"] != 1 || rep.Outcomes["settle won"] != 1 ||
		rep.Latencies["authorize"].N != 1 || rep.Latencies["heartbeat"].N != 3 || rep.Latencies["settle"].N != 1 {
		t.Fatalf("the report: %+v", rep)
	}
}

// heartbeatsAt answers heartbeat seq with answers in turn, one an attempt,
// the last one again once they run out, and accepts every other.
func heartbeatsAt(seq int64, answers ...func() (frontdoor.HeartbeatAnswer, error)) func(frontdoor.HeartbeatOf) (
	frontdoor.HeartbeatAnswer, error) {
	var mu sync.Mutex
	n := 0
	return func(hb frontdoor.HeartbeatOf) (frontdoor.HeartbeatAnswer, error) {
		if hb.GatewaySeq != seq {
			return frontdoor.HeartbeatAnswer{Status: frontdoor.Accepted, Deadline: time.Now()}, nil
		}
		mu.Lock()
		a := answers[min(n, len(answers)-1)]
		n++
		mu.Unlock()
		return a()
	}
}

func answer(s frontdoor.Status) func() (frontdoor.HeartbeatAnswer, error) {
	return func() (frontdoor.HeartbeatAnswer, error) {
		return frontdoor.HeartbeatAnswer{Status: s, Deadline: time.Now()}, nil
	}
}

func unanswered() (frontdoor.HeartbeatAnswer, error) {
	return frontdoor.HeartbeatAnswer{}, errors.New("unreachable")
}

// TestAStreamEndsAsTheEnclaveEndsIt: a heartbeat gets three attempts, the
// later two after a Retry or a call that failed; any other answer not
// accepted, or the third attempt not accepted, ends the stream. A stream
// whose first heartbeat ends it sends no terminal; one ended later settles
// the usage it delivered by then, not its whole bill, and not a refund
// (§4.5).
func TestAStreamEndsAsTheEnclaveEndsIt(t *testing.T) {
	type answers = func(frontdoor.HeartbeatOf) (frontdoor.HeartbeatAnswer, error)
	for _, c := range []struct {
		name      string
		heartbeat func() answers // a new one each run, since one keeps count
		sent      int            // heartbeats sent
		streamed  string         // how the stream ended
		settled   int64          // the settle's charge, or -1 for none
	}{
		{"the second retried thrice", func() answers { return heartbeatsAt(2, answer(frontdoor.Retry)) }, 4, "stopped", 83},
		{"the second unanswered thrice", func() answers { return heartbeatsAt(2, unanswered) }, 4, "stopped", 83},
		{"the second rejected", func() answers { return heartbeatsAt(2, answer(frontdoor.Rejected)) }, 2, "stopped", 83},
		{"the third decided", func() answers { return heartbeatsAt(3, answer(frontdoor.Decided)) }, 3, "stopped", 125},
		{"the second retried, then accepted", func() answers {
			return heartbeatsAt(2, answer(frontdoor.Retry), unanswered, answer(frontdoor.Accepted))
		}, 5, "complete", 125},
		{"the first rejected", func() answers { return heartbeatsAt(1, answer(frontdoor.Rejected)) }, 1, "refused", -1},
		{"the first unanswered thrice", func() answers { return heartbeatsAt(1, unanswered) }, 3, "refused", -1},
		{"the first retried thrice", func() answers { return heartbeatsAt(1, answer(frontdoor.Retry)) }, 3, "refused", -1},
	} {
		for _, refunds := range []bool{false, true} {
			gw := admitting(t)
			gw.heartbeat = c.heartbeat()
			cfg := config(gw)
			var log bytes.Buffer
			cfg.Log = &log
			if refunds {
				cfg.Mix.RefundShare = 1
			}
			rep, err := Run(context.Background(), cfg)
			if err != nil {
				t.Fatal(err)
			}
			var g Generation
			if err := json.Unmarshal(bytes.TrimSpace(log.Bytes()), &g); err != nil {
				t.Fatal(err)
			}
			if len(gw.heartbeats) != c.sent || g.Streamed != c.streamed || rep.Outcomes["stream "+c.streamed] != 1 {
				t.Fatalf("%s: %d heartbeats, %+v, %+v", c.name, len(gw.heartbeats), g, rep.Outcomes)
			}
			echoed(t, gw)
			// A heartbeat's tries carry one snapshot.
			bySeq := map[int64]frontdoor.HeartbeatOf{}
			for _, hb := range gw.heartbeats {
				if first, ok := bySeq[hb.GatewaySeq]; ok && (first.Running != hb.Running ||
					!bytes.Equal(first.Hash, hb.Hash) || first.Usage != hb.Usage) {
					t.Fatalf("%s: heartbeat %d's tries differ: %+v, %+v", c.name, hb.GatewaySeq, first, hb)
				}
				bySeq[hb.GatewaySeq] = hb
			}
			switch {
			case c.settled < 0:
				if len(gw.settles)+len(gw.refunds) != 0 || g.Terminal != nil {
					t.Fatalf("%s: a terminal after a first heartbeat not accepted: %+v", c.name, g.Terminal)
				}
			case refunds && c.streamed == "complete":
				if len(gw.refunds) != 1 || len(gw.settles) != 0 {
					t.Fatalf("%s: %d refunds, %d settles", c.name, len(gw.refunds), len(gw.settles))
				}
			default:
				if len(gw.settles) != 1 || gw.settles[0].Charge != c.settled || len(gw.refunds) != 0 {
					t.Fatalf("%s, refunds %v: settles %+v, %d refunds", c.name, refunds, gw.settles, len(gw.refunds))
				}
			}
		}
	}
}

// TestACallThatFailsMovesToAnotherFrontDoor: a heartbeat's or a terminal's
// call that fails is tried again through the next front door, as a load
// balancer routes around one it cannot reach.
func TestACallThatFailsMovesToAnotherFrontDoor(t *testing.T) {
	// The first generation starts at the second front door, which admits
	// and then answers nothing.
	a, b := admitting(t), admitting(t)
	b.heartbeat = func(frontdoor.HeartbeatOf) (frontdoor.HeartbeatAnswer, error) { return unanswered() }
	b.terminal = func(int) (frontdoor.TerminalAnswer, error) {
		return frontdoor.TerminalAnswer{}, errors.New("unreachable")
	}
	cfg := config(a)
	cfg.Gateways = []Gateway{a, b}
	var log bytes.Buffer
	cfg.Log = &log
	if _, err := Run(context.Background(), cfg); err != nil {
		t.Fatal(err)
	}
	var g Generation
	if err := json.Unmarshal(bytes.TrimSpace(log.Bytes()), &g); err != nil {
		t.Fatal(err)
	}
	if len(b.authorizes) != 1 || len(b.heartbeats) != 1 || len(a.heartbeats) != 3 || g.Streamed != "complete" ||
		!slices.Equal(g.Heartbeats[0].Tries, []string{"error", "accepted"}) {
		t.Fatalf("heartbeats: %d at b, %d at a; %+v", len(b.heartbeats), len(a.heartbeats), g)
	}
	echoed(t, a, b)
	// The terminal starts where the stream left off, at the first front
	// door; there it fails once, and the second takes it.
	b2, a2 := admitting(t), admitting(t)
	a2.terminal = func(int) (frontdoor.TerminalAnswer, error) {
		return frontdoor.TerminalAnswer{}, errors.New("unreachable")
	}
	cfg = config(a2)
	cfg.Gateways = []Gateway{a2, b2}
	cfg.Mix.StreamShare = 0
	log.Reset()
	cfg.Log = &log
	if _, err := Run(context.Background(), cfg); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(bytes.TrimSpace(log.Bytes()), &g); err != nil {
		t.Fatal(err)
	}
	if len(a2.settles) != 0 || len(b2.settles) != 1 || g.Terminal == nil || g.Terminal.Status != "won" {
		t.Fatalf("settles: %d at a, %d at b; %+v", len(a2.settles), len(b2.settles), g.Terminal)
	}
	// b2 starts generation 1, which fails nowhere: so turn its terminal
	// to the first door, which fails, and back.
	a3, b3 := admitting(t), admitting(t)
	b3.terminal = func(int) (frontdoor.TerminalAnswer, error) {
		return frontdoor.TerminalAnswer{}, errors.New("unreachable")
	}
	cfg = config(a3)
	cfg.Gateways = []Gateway{a3, b3}
	cfg.Mix.StreamShare = 0
	log.Reset()
	cfg.Log = &log
	if _, err := Run(context.Background(), cfg); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(bytes.TrimSpace(log.Bytes()), &g); err != nil {
		t.Fatal(err)
	}
	if len(b3.settles) != 1 || len(a3.settles) != 1 || g.Terminal == nil ||
		!slices.Equal(g.Terminal.Tries, []string{"error", "won"}) {
		t.Fatalf("settles: %d at b, %d at a; %+v", len(b3.settles), len(a3.settles), g.Terminal)
	}
	echoed(t, a3, b3)
}

// TestHeartbeatsKeepTheirSchedule: a stream's seq-th heartbeat is sent
// HeartbeatEvery × seq after it began, however long the answers before it
// took.
func TestHeartbeatsKeepTheirSchedule(t *testing.T) {
	const every, answerTakes = 150 * time.Millisecond, 120 * time.Millisecond
	gw := admitting(t)
	accept := gw.heartbeat
	gw.heartbeat = func(hb frontdoor.HeartbeatOf) (frontdoor.HeartbeatAnswer, error) {
		time.Sleep(answerTakes)
		return accept(hb)
	}
	cfg := config(gw)
	cfg.HeartbeatEvery = every
	if _, err := Run(context.Background(), cfg); err != nil {
		t.Fatal(err)
	}
	gw.mu.Lock()
	defer gw.mu.Unlock()
	if len(gw.at["heartbeat"]) != 3 {
		t.Fatalf("%d heartbeats", len(gw.at["heartbeat"]))
	}
	// After each answer, a wait of HeartbeatEvery would send the third at
	// 3 × every + 2 × answerTakes, 690 ms in.
	third := gw.at["heartbeat"][2].Sub(gw.at["authorize"][0])
	if third < 3*every || third >= 600*time.Millisecond {
		t.Fatalf("the third heartbeat %v after the authorize", third)
	}
}

// TestATerminalIsRetriedAsTheEnclaveDoes: a terminal failed or not answered
// is sent again after each of RetryDelays until it is answered, and once
// the attempt after the last delay goes unanswered it is lost; each
// attempt's answer is counted. A refund is sent with no charge.
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
	if len(gw.settles) != 3 || rep.Outcomes["settle attempt failed"] != 1 || rep.Outcomes["settle attempt error"] != 1 ||
		rep.Outcomes["settle attempt recorded"] != 1 || rep.Outcomes["settle recorded"] != 1 {
		t.Fatalf("%d settles, %+v", len(gw.settles), rep.Outcomes)
	}
	echoed(t, gw)

	lost := admitting(t)
	lost.terminal = func(int) (frontdoor.TerminalAnswer, error) {
		return frontdoor.TerminalAnswer{Status: frontdoor.Failed}, nil
	}
	cfg := config(lost)
	cfg.RetryDelays = []time.Duration{5 * time.Millisecond, 10 * time.Millisecond, 15 * time.Millisecond}
	cfg.Mix.RefundShare = 1
	var log bytes.Buffer
	cfg.Log = &log
	rep, err = Run(context.Background(), cfg)
	if err != nil {
		t.Fatal(err)
	}
	if n := len(lost.refunds); n != 4 || rep.Outcomes["refund attempt failed"] != 4 || rep.Outcomes["refund lost"] != 1 ||
		len(lost.settles) != 0 {
		t.Fatalf("%d refunds, %+v", n, rep.Outcomes)
	}
	if took := lost.at["refund"][3].Sub(lost.at["refund"][0]); took < 30*time.Millisecond {
		t.Fatalf("the last attempt %v after the first, before the delays' 30ms", took)
	}
	echoed(t, lost)
	var g Generation
	if err := json.Unmarshal(bytes.TrimSpace(log.Bytes()), &g); err != nil || g.Terminal == nil ||
		g.Terminal.Kind != record.Refund || g.Terminal.Charge != 0 || g.Terminal.Status != "lost" ||
		len(g.Terminal.Tries) != 4 {
		t.Fatalf("the refund's record: %+v %v", g.Terminal, err)
	}
}

// TestATerminalKeepsItsAnswer: the record keeps what the answer named, a
// disposition's outcome and cost too.
func TestATerminalKeepsItsAnswer(t *testing.T) {
	gw := admitting(t)
	gw.terminal = func(int) (frontdoor.TerminalAnswer, error) {
		return frontdoor.TerminalAnswer{Status: frontdoor.Settled, Outcome: "reaped_snapshot", Cost: 37,
			CostKnown: true}, nil
	}
	cfg := config(gw)
	var log bytes.Buffer
	cfg.Log = &log
	if _, err := Run(context.Background(), cfg); err != nil {
		t.Fatal(err)
	}
	var g Generation
	if err := json.Unmarshal(bytes.TrimSpace(log.Bytes()), &g); err != nil || g.Terminal == nil ||
		g.Terminal.Status != "settled" || g.Terminal.Outcome != "reaped_snapshot" || g.Terminal.Cost != 37 ||
		!g.Terminal.CostKnown {
		t.Fatalf("the terminal's record: %+v %v", g.Terminal, err)
	}
}

// TestAnAuthorizeNotAdmittedEndsTheGeneration: busy, invalid or an error
// sends nothing more; an answer's latency is kept.
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
		// An answer's latency is kept, whatever it says; a call that
		// failed has none.
		if answered := name == "busy"; (rep.Latencies["authorize"].N == 1) != answered {
			t.Fatalf("%s: latencies %+v", name, rep.Latencies)
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

	// 0.29 s × 100 a second is 29, which a product in floating point puts
	// just below.
	cfg = config(admitting(t))
	cfg.Rate, cfg.Duration, cfg.MaxInFlight, cfg.Mix.StreamShare = 100, 290*time.Millisecond, 100, 0
	if rep, err = Run(context.Background(), cfg); err != nil || rep.Started != 29 || rep.NotStarted != 0 {
		t.Fatalf("started %d, not %d: %v", rep.Started, rep.NotStarted, err)
	}
}

// TestARunNeedsARate: a rate not above 0, past MaxRate or not a number,
// or no enclave, is refused before anything starts.
func TestARunNeedsARate(t *testing.T) {
	for _, rate := range []float64{0, -1, math.NaN(), math.Inf(1), 2e9} {
		cfg := config(admitting(t))
		cfg.Rate = rate
		if _, err := Run(context.Background(), cfg); err == nil {
			t.Fatalf("a rate of %v ran", rate)
		}
	}
	cfg := config(admitting(t))
	cfg.Enclaves = 0
	if _, err := Run(context.Background(), cfg); err == nil {
		t.Fatal("a run with no enclave ran")
	}
}

// TestABillIsWorkedWithoutOverflow: an estimate and a bill whose product
// passes an int64 still bill their share, and a mix whose charge itself
// would pass one is refused.
func TestABillIsWorkedWithoutOverflow(t *testing.T) {
	gw := admitting(t)
	cfg := config(gw)
	cfg.Mix.StreamShare, cfg.Mix.Estimates, cfg.Mix.BillPermill = 0, []Weight{{1e17, 1}}, []Weight{{1000, 1}}
	if _, err := Run(context.Background(), cfg); err != nil || len(gw.settles) != 1 || gw.settles[0].Charge != 1e17 {
		t.Fatalf("settles %+v: %v", gw.settles, err)
	}
	gw = admitting(t)
	cfg = config(gw)
	cfg.Mix.Estimates = []Weight{{1e17, 1}}
	cfg.Mix.Heartbeats = []Weight{{MaxHeartbeats / 2, 1}}
	cfg.HeartbeatEvery = time.Nanosecond
	// Stopped at the hundredth heartbeat, the stream settles 100/beats of
	// its bill, 1.25e17, whose product with 100 passes an int64.
	gw.heartbeat = heartbeatsAt(100, answer(frontdoor.Rejected))
	want := new(big.Int).Div(new(big.Int).Mul(big.NewInt(125e15), big.NewInt(100)), big.NewInt(MaxHeartbeats/2))
	if _, err := Run(context.Background(), cfg); err != nil || len(gw.settles) != 1 ||
		gw.settles[0].Charge != want.Int64() {
		t.Fatalf("settles %+v, want %v: %v", gw.settles, want, err)
	}
	cfg.Mix.BillPermill = []Weight{{1e5, 1}}
	if _, err := Run(context.Background(), cfg); err == nil {
		t.Fatal("a charge past an int64 ran")
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

// TestAMixIsRead: a mix reads from JSON, one object with every field given
// and none null, and nothing after it; one with a share past 1, a histogram
// of no weight, or a value out of its range does not. Nor does a mix built
// with a weight that is not a number, or infinite, run.
func TestAMixIsRead(t *testing.T) {
	good := `{"stream_share":0.6,"heartbeats":[{"value":3,"weight":1}],"refund_share":0.1,` +
		`"estimates":[{"value":100,"weight":2},{"value":1000,"weight":1}],"bill_permill":[{"value":800,"weight":7},{"value":1200,"weight":1}]}`
	if m, err := ReadMix(strings.NewReader(good)); err != nil || m.StreamShare != 0.6 || len(m.Estimates) != 2 {
		t.Fatalf("%+v %v", m, err)
	}
	for name, bad := range map[string]string{
		"a share past 1":      strings.Replace(good, `"stream_share":0.6`, `"stream_share":1.5`, 1),
		"no weight":           strings.Replace(good, `[{"value":3,"weight":1}]`, `[{"value":3,"weight":0}]`, 1),
		"no heartbeat":        strings.Replace(good, `[{"value":3,"weight":1}]`, `[{"value":0,"weight":1}]`, 1),
		"an unknown field":    strings.Replace(good, `"stream_share"`, `"extra":1,"stream_share"`, 1),
		"no estimates":        strings.Replace(good, `[{"value":100,"weight":2},{"value":1000,"weight":1}]`, `[]`, 1),
		"too many heartbeats": strings.Replace(good, `[{"value":3,"weight":1}]`, `[{"value":2000000,"weight":1}]`, 1),
		"a second object":     good + ` {}`,
		"garbage after":       good + ` x`,
		"a null share":        strings.Replace(good, `"stream_share":0.6`, `"stream_share":null`, 1),
		"no refund share":     strings.Replace(good, `"refund_share":0.1,`, ``, 1),
		"a field twice": strings.Replace(good, `"estimates":[{"value":100,"weight":2},{"value":1000,"weight":1}]`,
			`"estimates":[{"value":100,"weight":1}],"estimates":[{"weight":2}]`, 1),
		"a bin's field twice": strings.Replace(good, `{"value":100,"weight":2}`,
			`{"value":100,"value":7,"weight":2}`, 1),
		"a field twice in another case": strings.Replace(good,
			`"estimates":[{"value":100,"weight":2},{"value":1000,"weight":1}]`,
			`"estimates":[{"value":100,"weight":2}],"ESTIMATES":[{"weight":3}]`, 1),
		"a field in another case": strings.Replace(good, `"stream_share"`, `"Stream_Share"`, 1),
		// Each beside a bin that would do alone.
		"a null value":    strings.Replace(good, `{"value":100,"weight":2}`, `{"value":null,"weight":2}`, 1),
		"a null weight":   strings.Replace(good, `{"value":100,"weight":2}`, `{"value":100,"weight":null}`, 1),
		"a null bin":      strings.Replace(good, `{"value":100,"weight":2}`, `null`, 1),
		"no weight given": strings.Replace(good, `{"value":100,"weight":2}`, `{"value":100}`, 1),
	} {
		if _, err := ReadMix(strings.NewReader(bad)); err == nil {
			t.Fatalf("%s: read", name)
		}
	}
	if _, err := ReadMix(strings.NewReader(good + "\n \n")); err != nil {
		t.Fatalf("a mix and blank lines: %v", err)
	}
	// Weights whose total would pass a float's range read, and draw in
	// proportion (TestADrawLandsOnAWeight).
	if _, err := ReadMix(strings.NewReader(strings.Replace(good,
		`[{"value":100,"weight":2},{"value":1000,"weight":1}]`,
		`[{"value":100,"weight":1e308},{"value":1000,"weight":1e308},{"value":5,"weight":0}]`, 1))); err != nil {
		t.Fatalf("weights of 1e308: %v", err)
	}
	for _, w := range []float64{math.NaN(), math.Inf(1)} {
		cfg := config(admitting(t))
		cfg.Mix.Estimates = []Weight{{100, w}, {200, 1}}
		if _, err := Run(context.Background(), cfg); err == nil {
			t.Fatalf("a weight of %v ran", w)
		}
	}
}

// TestACancelledRunSendsNoMore: once the run's context ends, a stream sends
// no more heartbeats, a generation no terminal, and the retry queue no more
// attempts; each says so.
func TestACancelledRunSendsNoMore(t *testing.T) {
	for _, c := range []struct {
		name string
		set  func(gw *fakeGateway, cfg *Config, cancel func())
		want string
	}{
		{"in a stream's wait", func(gw *fakeGateway, cfg *Config, cancel func()) {
			cfg.HeartbeatEvery = time.Hour
			time.AfterFunc(20*time.Millisecond, cancel)
		}, "stream cancelled"},
		{"as the last heartbeat is answered", func(gw *fakeGateway, cfg *Config, cancel func()) {
			accept := gw.heartbeat
			gw.heartbeat = func(hb frontdoor.HeartbeatOf) (frontdoor.HeartbeatAnswer, error) {
				if hb.GatewaySeq == 3 {
					cancel()
				}
				return accept(hb)
			}
		}, "generation cancelled"},
		{"in the retry queue", func(gw *fakeGateway, cfg *Config, cancel func()) {
			cfg.Mix.StreamShare, cfg.RetryDelays = 0, []time.Duration{time.Hour}
			gw.terminal = func(int) (frontdoor.TerminalAnswer, error) {
				time.AfterFunc(20*time.Millisecond, cancel)
				return frontdoor.TerminalAnswer{Status: frontdoor.Failed}, nil
			}
		}, "settle cancelled"},
		{"as the queue's last attempt is made", func(gw *fakeGateway, cfg *Config, cancel func()) {
			cfg.Mix.StreamShare, cfg.RetryDelays = 0, []time.Duration{time.Millisecond}
			gw.terminal = func(attempt int) (frontdoor.TerminalAnswer, error) {
				if attempt == 2 {
					cancel()
				}
				return frontdoor.TerminalAnswer{}, context.Canceled
			}
		}, "settle cancelled"},
		{"as the only attempt is made", func(gw *fakeGateway, cfg *Config, cancel func()) {
			cfg.Mix.StreamShare, cfg.RetryDelays = 0, nil
			gw.terminal = func(int) (frontdoor.TerminalAnswer, error) {
				cancel()
				return frontdoor.TerminalAnswer{}, context.Canceled
			}
		}, "settle cancelled"},
	} {
		gw := admitting(t)
		cfg := config(gw)
		ctx, cancel := context.WithCancel(context.Background())
		c.set(gw, &cfg, cancel)
		rep, err := Run(ctx, cfg)
		cancel()
		if err != nil {
			t.Fatal(err)
		}
		attempts := map[string]int{"in the retry queue": 1, "as the queue's last attempt is made": 2,
			"as the only attempt is made": 1}[c.name]
		if rep.Outcomes[c.want] != 1 || len(gw.settles) != attempts || len(gw.refunds) != 0 ||
			rep.Outcomes["settle lost"] != 0 {
			t.Fatalf("%s: %d settles, %+v", c.name, len(gw.settles), rep.Outcomes)
		}
	}
}

// maxSource draws the largest number a source can.
type maxSource struct{}

func (maxSource) Uint64() uint64 { return math.MaxUint64 }

// TestADrawLandsOnAWeight: a draw that rounding carries past every bin
// lands on the last bin of any weight, never on one of none; and weights
// however small draw in proportion.
func TestADrawLandsOnAWeight(t *testing.T) {
	// Scaled by the heaviest, these weights' largest draw passes every bin.
	if got := newHisto([]Weight{{1, 0.3}, {2, 0.4}, {3, 0.1}, {4, 0}}).pick(rand.New(maxSource{})); got != 3 {
		t.Fatalf("drew %d", got)
	}
	for _, w := range []float64{math.SmallestNonzeroFloat64, 1e308} {
		h := newHisto([]Weight{{1, w}, {2, w}, {3, 0}})
		rng := rand.New(rand.NewPCG(1, 2))
		ones := 0
		for range 100_000 {
			if h.pick(rng) == 1 {
				ones++
			}
		}
		if ones < 48_000 || ones > 52_000 {
			t.Fatalf("%d of 100,000 draws on the first of two weights of %v", ones, w)
		}
	}
}

// TestTheRetryQueueServesInTurn: an enclave's one worker serves its queued
// terminals in turn, each through its delays, so one queued behind another
// is tried after it; here the first queued is lost and the second, whose
// tries come after the outage, is not.
func TestTheRetryQueueServesInTurn(t *testing.T) {
	gw := admitting(t)
	var mu sync.Mutex
	calls := 0
	gw.terminal = func(int) (frontdoor.TerminalAnswer, error) {
		mu.Lock()
		defer mu.Unlock()
		// Both generations' first sends, and the first queued one's three
		// tries, fail.
		if calls++; calls <= 5 {
			return frontdoor.TerminalAnswer{}, errors.New("unreachable")
		}
		return frontdoor.TerminalAnswer{Status: frontdoor.Recorded}, nil
	}
	cfg := config(gw)
	cfg.Rate, cfg.Duration, cfg.Mix.StreamShare = 2000, time.Millisecond, 0
	rep, err := Run(context.Background(), cfg)
	if err != nil {
		t.Fatal(err)
	}
	if rep.Started != 2 || rep.Outcomes["settle lost"] != 1 || rep.Outcomes["settle recorded"] != 1 ||
		len(gw.settles) != 6 {
		t.Fatalf("%d settles, %+v", len(gw.settles), rep.Outcomes)
	}
	echoed(t, gw)
}

// TestARateIsTheDecimalWritten: generations due are worked from the rate as
// written, not the float's binary value below it.
func TestARateIsTheDecimalWritten(t *testing.T) {
	for _, c := range []struct {
		rate float64
		over time.Duration
		due  int64
	}{{100.1, 10 * time.Second, 1001}, {0.3, 10 * time.Second, 3}, {100, 290 * time.Millisecond, 29},
		{1e6, time.Second, 1_000_000}} {
		if got := dueBy(exactly(c.rate), c.over); got != c.due {
			t.Fatalf("%v a second over %v: %d due, want %d", c.rate, c.over, got, c.due)
		}
	}
}

// TestAHeartbeatsTriesShareItsWait: a heartbeat's attempts end together at
// HeartbeatWait from its first, however long each takes, and the stream
// ends.
func TestAHeartbeatsTriesShareItsWait(t *testing.T) {
	gw := admitting(t)
	var mu sync.Mutex
	var deadlines []time.Time
	hold := &blockingGateway{fakeGateway: gw, heartbeat: func(ctx context.Context) (frontdoor.HeartbeatAnswer, error) {
		d, _ := ctx.Deadline()
		mu.Lock()
		deadlines = append(deadlines, d)
		n := len(deadlines)
		mu.Unlock()
		if n == 1 {
			// The first takes part of the wait, then is answered Retry.
			time.Sleep(60 * time.Millisecond)
			return frontdoor.HeartbeatAnswer{Status: frontdoor.Retry}, nil
		}
		<-ctx.Done()
		return frontdoor.HeartbeatAnswer{}, ctx.Err()
	}}
	cfg := config(gw)
	cfg.Gateways, cfg.HeartbeatWait = []Gateway{hold}, 150*time.Millisecond
	var log bytes.Buffer
	cfg.Log = &log
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	began := time.Now()
	if _, err := Run(ctx, cfg); err != nil {
		t.Fatal(err)
	}
	var g Generation
	if err := json.Unmarshal(bytes.TrimSpace(log.Bytes()), &g); err != nil {
		t.Fatal(err)
	}
	if took := time.Since(began); took > 2*time.Second || g.Streamed != "refused" || len(deadlines) != 2 ||
		!deadlines[0].Equal(deadlines[1]) {
		t.Fatalf("after %v, %+v, the tries' deadlines %v", took, g, deadlines)
	}
}

// blockingGateway is a fake whose heartbeats see their call's context.
type blockingGateway struct {
	*fakeGateway
	heartbeat func(ctx context.Context) (frontdoor.HeartbeatAnswer, error)
}

func (b *blockingGateway) Heartbeat(ctx context.Context, _ frontdoor.HeartbeatOf) (frontdoor.HeartbeatAnswer, error) {
	return b.heartbeat(ctx)
}

// TestEachGenerationCarriesItsOwnEnvelope: generations run at once, and
// each one's heartbeats, settle and refund, their retries too, carry the
// envelope its own authorize sealed.
func TestEachGenerationCarriesItsOwnEnvelope(t *testing.T) {
	gw := admitting(t)
	var mu sync.Mutex
	tries := map[string]int{}
	cfg := config(gw)
	cfg.Rate, cfg.Duration, cfg.MaxInFlight, cfg.HeartbeatEvery = 2000, 5*time.Millisecond, 20, 2*time.Millisecond
	rep, err := Run(context.Background(), cfg)
	if err != nil {
		t.Fatal(err)
	}
	if rep.Started < 5 || len(gw.settles) != int(rep.Started) {
		t.Fatalf("started %d, %d settles", rep.Started, len(gw.settles))
	}
	ownEnvelopes(t, gw)

	// A refund names no request: each generation's first refund fails, and
	// its retry, through the queue, must carry its own envelope, so each
	// sealed envelope is sent exactly twice.
	refunding := admitting(t)
	failed := map[string]bool{}
	refunding.refund = func(r frontdoor.RefundOf) (frontdoor.TerminalAnswer, error) {
		mu.Lock()
		defer mu.Unlock()
		if !failed[r.Envelope] {
			failed[r.Envelope] = true
			return frontdoor.TerminalAnswer{}, errors.New("unreachable")
		}
		return frontdoor.TerminalAnswer{Status: frontdoor.Won}, nil
	}
	cfg = config(refunding)
	cfg.Rate, cfg.Duration, cfg.MaxInFlight = 2000, 5*time.Millisecond, 20
	cfg.Mix.StreamShare, cfg.Mix.RefundShare = 0, 1
	if _, err := Run(context.Background(), cfg); err != nil {
		t.Fatal(err)
	}
	for _, r := range refunding.refunds {
		tries[r.Envelope]++
	}
	if len(tries) != len(refunding.sealed) || len(refunding.sealed) < 5 {
		t.Fatalf("%d envelopes refunded, %d sealed", len(tries), len(refunding.sealed))
	}
	for _, e := range refunding.sealed {
		if tries[e] != 2 {
			t.Fatalf("a generation's envelope refunded %d times, not its send and its retry: %v", tries[e], tries)
		}
	}
}

// TestTheRetryQueueIsServedInItsOrder: an enclave's one worker serves its
// queued terminals in the order they joined, the second behind the one in
// service, the third behind the second.
func TestTheRetryQueueIsServedInItsOrder(t *testing.T) {
	serving, release := make(chan struct{}), make(chan struct{})
	var mu sync.Mutex
	var served []string
	gw := &orderedGateway{fakeGateway: admitting(t), settle: func(s frontdoor.SettleOf) (frontdoor.TerminalAnswer, error) {
		if s.Envelope == "first" {
			close(serving)
			<-release
		}
		mu.Lock()
		served = append(served, s.Envelope)
		mu.Unlock()
		return frontdoor.TerminalAnswer{Status: frontdoor.Won}, nil
	}}
	cfg := config(gw.fakeGateway)
	cfg.Gateways, cfg.RetryDelays = []Gateway{gw}, []time.Duration{0}
	r := &run{cfg: cfg, outcomes: map[string]int64{}, latencies: map[string][]time.Duration{}}
	workers, stop := r.startEnclaves(context.Background())
	defer func() {
		stop()
		workers.Wait()
	}()
	e := r.enclaves[0]
	job := func(envelope string) *queued {
		return &queued{p: &played{r: r, enclave: e}, envelope: envelope, done: &TerminalDone{Kind: record.Settle},
			served: make(chan struct{})}
	}
	first, second, third := job("first"), job("second"), job("third")
	e.queue(first)
	<-serving
	e.queue(second)
	e.queue(third)
	close(release)
	for _, j := range []*queued{first, second, third} {
		<-j.served
		if j.done.Status != "won" {
			t.Fatalf("%s: %+v", j.envelope, j.done)
		}
	}
	if !slices.Equal(served, []string{"first", "second", "third"}) {
		t.Fatalf("served %v", served)
	}
}

// orderedGateway is a fake whose settles its function answers.
type orderedGateway struct {
	*fakeGateway
	settle func(frontdoor.SettleOf) (frontdoor.TerminalAnswer, error)
}

func (o *orderedGateway) Settle(_ context.Context, s frontdoor.SettleOf) (frontdoor.TerminalAnswer, error) {
	return o.settle(s)
}

// TestACancelledRunSaysSo: a run whose context ends before its duration
// does reports itself cancelled, whatever its generations reached; one that
// runs its course does not.
func TestACancelledRunSaysSo(t *testing.T) {
	cfg := config(admitting(t))
	cfg.Rate, cfg.Duration = 1, time.Minute
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Millisecond)
	defer cancel()
	rep, err := Run(ctx, cfg)
	if err != nil || !rep.Cancelled {
		t.Fatalf("a run cut short: %+v %v", rep, err)
	}
	if rep, err = Run(context.Background(), config(admitting(t))); err != nil || rep.Cancelled {
		t.Fatalf("a run that ran its course: %+v %v", rep, err)
	}
}

// TestNoHeartbeatStartsOnceTheRunEnds: a heartbeat whose time comes as the
// run ends is not sent.
func TestNoHeartbeatStartsOnceTheRunEnds(t *testing.T) {
	gw := admitting(t)
	ctx, cancel := context.WithCancel(context.Background())
	accept := gw.heartbeat
	gw.heartbeat = func(hb frontdoor.HeartbeatOf) (frontdoor.HeartbeatAnswer, error) {
		if hb.GatewaySeq == 1 {
			// The run ends as the first heartbeat is answered, and the
			// second's time comes at once.
			cancel()
			time.Sleep(20 * time.Millisecond)
		}
		return accept(hb)
	}
	cfg := config(gw)
	cfg.HeartbeatEvery = time.Nanosecond
	if _, err := Run(ctx, cfg); err != nil {
		t.Fatal(err)
	}
	if len(gw.heartbeats) != 1 {
		t.Fatalf("%d heartbeats sent, the run ended after the first", len(gw.heartbeats))
	}
}
