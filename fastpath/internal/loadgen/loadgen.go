// Package loadgen plays the spike's gateways (spike plan §2, §5): each
// generation authorizes, heartbeats if it streams, echoing the deadline it
// was granted, and then settles or refunds, keeping a settle that is not
// answered in a retry queue as the enclave does (§4.5). Generations start
// at a set rate, whatever the ones before are doing, and each one's outcome
// is recorded, so a run says how many were admitted, turned away, charged,
// refunded, or lost, and how long each step took.
package loadgen

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math/rand/v2"
	"slices"
	"strconv"
	"sync"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/frontdoor"
	"github.com/Lore-Hex/quill-router/fastpath/internal/record"
)

// Gateway is a front door as a gateway calls it; frontdoor.Gateway is one.
type Gateway interface {
	Authorize(ctx context.Context, a frontdoor.AuthorizeOf) (frontdoor.Authorized, error)
	Heartbeat(ctx context.Context, hb frontdoor.HeartbeatOf) (frontdoor.HeartbeatAnswer, error)
	Settle(ctx context.Context, s frontdoor.SettleOf) (frontdoor.TerminalAnswer, error)
	Refund(ctx context.Context, r frontdoor.RefundOf) (frontdoor.TerminalAnswer, error)
}

// Mix is the load's mix (spike plan §5): the share of generations that
// stream, how many heartbeats a stream sends, the share of terminals that
// refund, a hold's estimate, and the bill as a share of the hold, in
// thousandths, which is above a thousand for a settle that charges more
// than its hold.
type Mix struct {
	StreamShare float64  `json:"stream_share"`
	Heartbeats  []Weight `json:"heartbeats"`
	RefundShare float64  `json:"refund_share"`
	Estimates   []Weight `json:"estimates"`
	BillPermill []Weight `json:"bill_permill"`
}

// Weight is one value of a histogram and its weight.
type Weight struct {
	Value  int64   `json:"value"`
	Weight float64 `json:"weight"`
}

// ReadMix reads a mix as JSON, such as fastpath/testdata/load-mix.json.
func ReadMix(r io.Reader) (Mix, error) {
	d := json.NewDecoder(r)
	d.DisallowUnknownFields()
	var m Mix
	if err := d.Decode(&m); err != nil {
		return Mix{}, fmt.Errorf("loadgen: the mix: %w", err)
	}
	return m, m.valid()
}

func (m Mix) valid() error {
	share := func(x float64) bool { return x >= 0 && x <= 1 }
	if !share(m.StreamShare) || !share(m.RefundShare) || !histogram(m.Heartbeats, 1) || !histogram(m.Estimates, 1) ||
		!histogram(m.BillPermill, 0) {
		return errors.New("loadgen: a mix needs shares between 0 and 1, and histograms of positive weights whose " +
			"heartbeats and estimates are at least 1 and bills at least 0")
	}
	return nil
}

func histogram(ws []Weight, least int64) bool {
	total := 0.0
	for _, w := range ws {
		if w.Value < least || w.Weight < 0 {
			return false
		}
		total += w.Weight
	}
	return total > 0
}

// pick draws a value of a histogram.
func pick(rng *rand.Rand, ws []Weight) int64 {
	total := 0.0
	for _, w := range ws {
		total += w.Weight
	}
	x := rng.Float64() * total
	for _, w := range ws {
		if x < w.Weight {
			return w.Value
		}
		x -= w.Weight
	}
	return ws[len(ws)-1].Value
}

// Config is a run's.
type Config struct {
	// Gateways are the front doors; each generation calls one, in turn.
	Gateways []Gateway
	// Rate is how many generations start a second, for Duration; at most
	// MaxInFlight run at once, and one that would pass it does not start,
	// and is counted, since the generator then sets the pace, not the
	// load.
	Rate        float64
	Duration    time.Duration
	MaxInFlight int
	// Workspaces are the workspaces generations spread over, uniformly.
	Workspaces []string
	Mix        Mix
	// HeartbeatEvery is how often a stream heartbeats: well within the
	// owners' heartbeat deadline.
	HeartbeatEvery time.Duration
	// Boot is the boot binding every authorize and full record carries.
	Boot []byte
	// RetryEvery and RetryFor are the settle retry queue's: a terminal not
	// answered is sent again every RetryEvery until RetryFor has passed,
	// and is then lost.
	RetryEvery time.Duration
	RetryFor   time.Duration
	// CallWait bounds each call.
	CallWait time.Duration
	// Key, when set, opens envelopes, so each generation's record names its
	// authorization and lease.
	Key []byte
	// Seed makes the run's draws repeatable.
	Seed uint64
	// Log, when set, receives each generation's record, one JSON object a
	// line.
	Log io.Writer
}

func (c Config) valid() error {
	if len(c.Gateways) == 0 || c.Rate <= 0 || c.Duration <= 0 || c.MaxInFlight < 1 || len(c.Workspaces) == 0 ||
		c.HeartbeatEvery <= 0 || len(c.Boot) == 0 || c.RetryEvery <= 0 || c.RetryFor < 0 || c.CallWait <= 0 {
		return errors.New("loadgen: a run needs gateways, a rate, a duration, room in flight, workspaces, a " +
			"heartbeat interval, a boot binding, a retry interval and a call wait")
	}
	return c.Mix.valid()
}

// Generation is one generation's record.
type Generation struct {
	N         int64  `json:"n"`
	Request   string `json:"request"`
	Workspace string `json:"workspace"`
	// Auth and Lease are the envelope's, when Config.Key opens it.
	Auth  string `json:"auth,omitempty"`
	Lease string `json:"lease,omitempty"`
	// Authorized is the authorize's status, or "error" for a call that
	// failed.
	Authorized string        `json:"authorized"`
	Stream     bool          `json:"stream"`
	Heartbeats []string      `json:"heartbeats,omitempty"`
	Terminal   *TerminalDone `json:"terminal,omitempty"`
	Started    time.Time     `json:"started"`
}

// TerminalDone is a generation's terminal: what it sent, the answer it
// took, after how many tries, or that the retry queue gave it up.
type TerminalDone struct {
	Kind     record.Kind `json:"kind"`
	Charge   int64       `json:"charge"`
	Status   string      `json:"status"`
	Won      record.Kind `json:"won,omitempty"`
	WonFor   int64       `json:"won_for,omitempty"`
	Attempts int         `json:"attempts"`
	Lost     bool        `json:"lost,omitempty"`
}

// Report is a run's: generations started and not, each step's outcomes,
// and each call's latencies by kind.
type Report struct {
	Started    int64             `json:"started"`
	NotStarted int64             `json:"not_started"`
	Outcomes   map[string]int64  `json:"outcomes"`
	Latencies  map[string]Spread `json:"latencies"`
}

// Spread is a set of latencies' count and percentiles.
type Spread struct {
	N   int           `json:"n"`
	P50 time.Duration `json:"p50"`
	P90 time.Duration `json:"p90"`
	P99 time.Duration `json:"p99"`
	Max time.Duration `json:"max"`
}

// Run runs the load until Duration has passed and every generation started
// has ended, or ctx ends, and reports it.
func Run(ctx context.Context, cfg Config) (Report, error) {
	if err := cfg.valid(); err != nil {
		return Report{}, err
	}
	r := &run{cfg: cfg, outcomes: map[string]int64{}, latencies: map[string][]time.Duration{}}
	// Each wake starts every generation due by then, so the rate holds
	// however late the wakes come.
	t := time.NewTicker(min(time.Millisecond, time.Duration(float64(time.Second)/cfg.Rate)))
	defer t.Stop()
	began := time.Now()
	var all sync.WaitGroup
	inFlight := make(chan struct{}, cfg.MaxInFlight)
	var n int64
	for ctx.Err() == nil {
		elapsed := min(time.Since(began), cfg.Duration)
		for due := int64(elapsed.Seconds() * cfg.Rate); n < due; {
			n++
			select {
			case inFlight <- struct{}{}:
				all.Add(1)
				go func(n int64) {
					defer all.Done()
					defer func() { <-inFlight }()
					r.generation(ctx, n)
				}(n)
			default:
				r.mu.Lock()
				r.notStarted++
				r.mu.Unlock()
			}
		}
		if elapsed == cfg.Duration {
			break
		}
		select {
		case <-ctx.Done():
		case <-t.C:
		}
	}
	all.Wait()
	return r.report(), r.logErr
}

type run struct {
	cfg Config

	mu         sync.Mutex
	started    int64
	notStarted int64
	outcomes   map[string]int64
	latencies  map[string][]time.Duration
	logErr     error
}

// generation plays one gateway request through.
func (r *run) generation(ctx context.Context, n int64) {
	rng := rand.New(rand.NewPCG(r.cfg.Seed, uint64(n)))
	cfg := r.cfg
	gw := cfg.Gateways[int(n)%len(cfg.Gateways)]
	g := Generation{N: n, Workspace: cfg.Workspaces[rng.IntN(len(cfg.Workspaces))],
		Stream: rng.Float64() < cfg.Mix.StreamShare, Started: time.Now().UTC()}
	sum := sha256.Sum256([]byte(fmt.Sprintf("%d/%d", cfg.Seed, n)))
	g.Request = hex.EncodeToString(sum[:8])
	estimate := pick(rng, cfg.Mix.Estimates)
	r.count("generation")
	defer r.record(&g)

	actx, cancel := context.WithTimeout(ctx, cfg.CallWait)
	began := time.Now()
	got, err := gw.Authorize(actx, frontdoor.AuthorizeOf{Workspace: g.Workspace, Request: g.Request, Estimate: estimate,
		Stream: g.Stream, Boot: cfg.Boot})
	cancel()
	if err != nil {
		g.Authorized = "error"
		r.count("authorize error")
		return
	}
	g.Authorized = string(got.Status)
	r.count("authorize " + g.Authorized)
	if got.Status != frontdoor.Admitted {
		return
	}
	r.latency("authorize", time.Since(began))
	if len(cfg.Key) > 0 {
		if e, err := frontdoor.Open(cfg.Key, got.Envelope); err == nil {
			g.Auth, g.Lease = e.Auth, e.Lease
		}
	}
	// The bill: a share of the hold, at times more than it.
	charge := estimate * pick(rng, cfg.Mix.BillPermill) / 1000
	if g.Stream {
		r.stream(ctx, gw, &g, got.Envelope, estimate, charge, pick(rng, cfg.Mix.Heartbeats))
	}
	kind := record.Settle
	if rng.Float64() < cfg.Mix.RefundShare {
		kind, charge = record.Refund, 0
	}
	g.Terminal = r.terminal(ctx, gw, got.Envelope, kind, charge, g.Request)
}

// stream heartbeats every HeartbeatEvery, beats times or until an answer is
// not Accepted, which stops it, each with its sequence, a hash of its
// snapshot, the usage and running charge so far, and the deadline the last
// one granted; the first carries the reap's basis.
func (r *run) stream(ctx context.Context, gw Gateway, g *Generation, envelope string, estimate, charge, beats int64) {
	var echoed time.Time
	for seq := int64(1); seq <= beats; seq++ {
		select {
		case <-ctx.Done():
			return
		case <-time.After(r.cfg.HeartbeatEvery):
		}
		running := min(charge*seq/beats, estimate)
		snapshot := sha256.Sum256([]byte(g.Request + "/" + strconv.FormatInt(seq, 10)))
		hb := frontdoor.HeartbeatOf{Envelope: envelope, GatewaySeq: seq, Hash: snapshot[:], Usage: 10 * seq,
			Running: running, Echoed: echoed}
		if seq == 1 {
			hb.Basis = []byte(`{"model":"spike","prices":"fixed"}`)
		}
		hctx, cancel := context.WithTimeout(ctx, r.cfg.CallWait)
		began := time.Now()
		got, err := gw.Heartbeat(hctx, hb)
		cancel()
		status := "error"
		if err == nil {
			status = string(got.Status)
			r.latency("heartbeat", time.Since(began))
		}
		g.Heartbeats = append(g.Heartbeats, status)
		r.count("heartbeat " + status)
		if err != nil || got.Status != frontdoor.Accepted {
			return
		}
		echoed = got.Deadline
	}
}

// terminal sends the generation's settle or refund until it is answered,
// as the enclave's retry queue does: every RetryEvery while RetryFor lasts.
// A Failed answer or a call that fails is tried again; any other answer
// ends it.
func (r *run) terminal(ctx context.Context, gw Gateway, envelope string, kind record.Kind, charge int64,
	request string) *TerminalDone {
	done := &TerminalDone{Kind: kind, Charge: charge}
	full, _ := json.Marshal(map[string]any{"request": request, "boot": r.cfg.Boot, "charge": charge})
	money := []byte(`{}`)
	giveUp := time.Now().Add(r.cfg.RetryFor)
	for {
		done.Attempts++
		tctx, cancel := context.WithTimeout(ctx, r.cfg.CallWait)
		began := time.Now()
		var got frontdoor.TerminalAnswer
		var err error
		if kind == record.Settle {
			got, err = gw.Settle(tctx, frontdoor.SettleOf{Envelope: envelope, Charge: charge, Full: full, Money: money})
		} else {
			got, err = gw.Refund(tctx, frontdoor.RefundOf{Envelope: envelope, Money: money})
		}
		cancel()
		if err == nil && got.Status != frontdoor.Failed {
			done.Status, done.Won, done.WonFor = string(got.Status), got.Kind, got.Charge
			r.latency(string(kind), time.Since(began))
			r.count(string(kind) + " " + done.Status)
			return done
		}
		if !time.Now().Add(r.cfg.RetryEvery).Before(giveUp) || ctx.Err() != nil {
			done.Status, done.Lost = "lost", true
			r.count(string(kind) + " lost")
			return done
		}
		r.count(string(kind) + " retried")
		select {
		case <-ctx.Done():
		case <-time.After(r.cfg.RetryEvery):
		}
	}
}

func (r *run) count(what string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.outcomes[what]++
}

func (r *run) latency(what string, d time.Duration) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.latencies[what] = append(r.latencies[what], d)
}

// record ends a generation: counted, and logged if the run logs.
func (r *run) record(g *Generation) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.started++
	if r.cfg.Log == nil || r.logErr != nil {
		return
	}
	line, err := json.Marshal(g)
	if err == nil {
		_, err = r.cfg.Log.Write(append(line, '\n'))
	}
	r.logErr = err
}

func (r *run) report() Report {
	r.mu.Lock()
	defer r.mu.Unlock()
	rep := Report{Started: r.started, NotStarted: r.notStarted, Outcomes: map[string]int64{},
		Latencies: map[string]Spread{}}
	for k, v := range r.outcomes {
		rep.Outcomes[k] = v
	}
	for k, ds := range r.latencies {
		rep.Latencies[k] = spread(ds)
	}
	return rep
}

// spread is the latencies' count and percentiles, nearest rank.
func spread(ds []time.Duration) Spread {
	if len(ds) == 0 {
		return Spread{}
	}
	s := slices.Clone(ds)
	slices.Sort(s)
	at := func(p float64) time.Duration {
		i := int(p*float64(len(s))+0.999999) - 1
		return s[min(max(i, 0), len(s)-1)]
	}
	return Spread{N: len(s), P50: at(0.50), P90: at(0.90), P99: at(0.99), Max: s[len(s)-1]}
}
