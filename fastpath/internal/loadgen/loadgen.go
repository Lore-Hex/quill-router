// Package loadgen plays the spike's gateways (spike plan §2, §5): each
// generation authorizes, heartbeats if it streams, echoing the deadline it
// was granted, and then settles or refunds as the enclave does (§4.5). A
// stream whose first heartbeat is not accepted sends no terminal; one that
// stops at a later heartbeat settles the usage it delivered; and a terminal
// not answered goes to a retry queue. Generations start at a set rate,
// whatever the ones before are doing, and each one's outcome is recorded, so
// a run says how many were admitted, turned away, charged, refunded, or
// lost, and how long each step took.
package loadgen

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"math/big"
	"math/bits"
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

// MaxHeartbeats bounds a stream's heartbeats, so that its usage, ten a
// heartbeat, is a whole number however long it runs.
const MaxHeartbeats = 1 << 20

// ReadMix reads a mix as JSON, such as fastpath/testdata/load-mix.json: one
// object, and nothing after it.
func ReadMix(r io.Reader) (Mix, error) {
	d := json.NewDecoder(r)
	d.DisallowUnknownFields()
	var m Mix
	if err := d.Decode(&m); err != nil {
		return Mix{}, fmt.Errorf("loadgen: the mix: %w", err)
	}
	if _, err := d.Token(); err != io.EOF {
		return Mix{}, errors.New("loadgen: the mix is one JSON object, with nothing after it")
	}
	return m, m.valid()
}

func (m Mix) valid() error {
	share := func(x float64) bool { return x >= 0 && x <= 1 }
	if !share(m.StreamShare) || !share(m.RefundShare) || !histogram(m.Heartbeats, 1, MaxHeartbeats) ||
		!histogram(m.Estimates, 1, math.MaxInt64) || !histogram(m.BillPermill, 0, math.MaxInt64) {
		return errors.New("loadgen: a mix needs shares between 0 and 1, and histograms of finite weights, not " +
			"negative, of a positive finite total, whose heartbeats are 1 to MaxHeartbeats, estimates at least 1 " +
			"and bills at least 0")
	}
	if _, ok := mulDiv(largest(m.Estimates), largest(m.BillPermill), 1000); !ok {
		return errors.New("loadgen: the mix's largest estimate and bill make a charge past an int64")
	}
	return nil
}

// histogram reports whether ws is one: values from least to most, weights
// not negative, and a total that is positive and finite, so that a draw
// lands on a value of positive weight. A weight that is not a number, or
// is infinite, makes the total so.
func histogram(ws []Weight, least, most int64) bool {
	total := 0.0
	for _, w := range ws {
		if w.Value < least || w.Value > most || w.Weight < 0 {
			return false
		}
		total += w.Weight
	}
	return total > 0 && !math.IsInf(total, 1)
}

func largest(ws []Weight) int64 {
	var most int64
	for _, w := range ws {
		most = max(most, w.Value)
	}
	return most
}

// mulDiv is a × b / c rounded down, for a and b not negative and c
// positive, and whether it fits an int64.
func mulDiv(a, b, c int64) (int64, bool) {
	hi, lo := bits.Mul64(uint64(a), uint64(b))
	if hi >= uint64(c) {
		return 0, false
	}
	q, _ := bits.Div64(hi, lo, uint64(c))
	return int64(q), q <= math.MaxInt64
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
	// x lands past every bin only by rounding: the last bin of any weight
	// takes it.
	for i := len(ws) - 1; ; i-- {
		if ws[i].Weight > 0 {
			return ws[i].Value
		}
	}
}

// Config is a run's.
type Config struct {
	// Gateways are the front doors. Each generation starts at one, in turn,
	// and after a call to it fails moves to the next, as a gateway's load
	// balancer routes around a front door it cannot reach.
	Gateways []Gateway
	// Rate is how many generations start a second, for Duration; at most
	// MaxInFlight run at once, and one that would pass it does not start,
	// and is counted, since the generator then sets the pace, not the
	// load. Rate is at most MaxRate.
	Rate        float64
	Duration    time.Duration
	MaxInFlight int
	// Workspaces are the workspaces generations spread over, uniformly.
	Workspaces []string
	Mix        Mix
	// HeartbeatEvery is how often a stream heartbeats, on a schedule its
	// answers' latency does not move: well within the owners' heartbeat
	// deadline.
	HeartbeatEvery time.Duration
	// HeartbeatWait bounds a heartbeat's attempts together, as the enclave's
	// 5 seconds do: three at most, the later two after a Retry or a call
	// that failed (§4.5).
	HeartbeatWait time.Duration
	// Boot is the boot binding every authorize and full record carries.
	Boot []byte
	// RetryDelays are the settle retry queue's, as the enclave's are (§4.5:
	// 0, 0.5, 1, 2, 4 and 8 seconds): a terminal failed or not answered is
	// sent again after each delay in turn, and is lost once the last such
	// attempt is.
	RetryDelays []time.Duration
	// CallWait bounds each call but a heartbeat's.
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

// MaxRate bounds Config.Rate.
const MaxRate = 1e6

// heartbeatTries is how many attempts the enclave gives a heartbeat.
const heartbeatTries = 3

func (c Config) valid() error {
	if len(c.Gateways) == 0 || !(c.Rate > 0 && c.Rate <= MaxRate) || c.Duration <= 0 || c.MaxInFlight < 1 ||
		len(c.Workspaces) == 0 || c.HeartbeatEvery <= 0 || c.HeartbeatWait <= 0 || len(c.Boot) == 0 ||
		c.CallWait <= 0 || slices.ContainsFunc(c.RetryDelays, func(d time.Duration) bool { return d < 0 }) {
		return errors.New("loadgen: a run needs gateways, a rate above 0 and at most MaxRate, a duration, room " +
			"in flight, workspaces, a heartbeat interval and wait, a boot binding, retry delays not negative and " +
			"a call wait")
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
	Authorized string `json:"authorized"`
	Stream     bool   `json:"stream"`
	Heartbeats []Beat `json:"heartbeats,omitempty"`
	// Streamed is how a stream ended: "complete"; "stopped" at a heartbeat
	// after the first that was not accepted, which settles what was
	// delivered; "refused" at a first heartbeat not accepted, which sends
	// no terminal (§4.5); or "cancelled" with the run.
	Streamed string `json:"streamed,omitempty"`
	// Terminal is the settle or refund sent, if one was.
	Terminal *TerminalDone `json:"terminal,omitempty"`
	Started  time.Time     `json:"started"`
}

// Beat is one heartbeat: each attempt's answer, or "error" for a call that
// failed.
type Beat struct {
	Seq   int64    `json:"seq"`
	Tries []string `json:"tries"`
}

// TerminalDone is a generation's terminal: what it sent, each attempt's
// answer ("error" for a call that failed), and the answer that ended it,
// with the winner or disposition it named; "lost" once the retry queue gave
// it up, "cancelled" with the run.
type TerminalDone struct {
	Kind      record.Kind `json:"kind"`
	Charge    int64       `json:"charge"`
	Tries     []string    `json:"tries"`
	Status    string      `json:"status"`
	Won       record.Kind `json:"won,omitempty"`
	WonFor    int64       `json:"won_for,omitempty"`
	Outcome   string      `json:"outcome,omitempty"`
	Cost      int64       `json:"cost,omitempty"`
	CostKnown bool        `json:"cost_known,omitempty"`
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
	rate := new(big.Rat).SetFloat64(cfg.Rate)
	t := time.NewTicker(time.Millisecond)
	defer t.Stop()
	began := time.Now()
	var all sync.WaitGroup
	inFlight := make(chan struct{}, cfg.MaxInFlight)
	var n int64
	for ctx.Err() == nil {
		elapsed := min(time.Since(began), cfg.Duration)
		for due := dueBy(rate, elapsed); n < due; {
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

// dueBy is how many generations are due elapsed into the run: the whole
// part of rate × elapsed, worked exactly, so that none due is rounded away.
func dueBy(rate *big.Rat, elapsed time.Duration) int64 {
	x := new(big.Rat).Mul(rate, big.NewRat(int64(elapsed), int64(time.Second)))
	return new(big.Int).Quo(x.Num(), x.Denom()).Int64()
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

// played is one generation's calls: the gateway it calls now, which a call
// that fails moves on.
type played struct {
	r    *run
	door int
}

func (p *played) gateway() Gateway { return p.r.cfg.Gateways[p.door] }

// failed moves the generation to the next gateway.
func (p *played) failed() { p.door = (p.door + 1) % len(p.r.cfg.Gateways) }

// generation plays one gateway request through.
func (r *run) generation(ctx context.Context, n int64) {
	rng := rand.New(rand.NewPCG(r.cfg.Seed, uint64(n)))
	cfg := r.cfg
	p := &played{r: r, door: int(n % int64(len(cfg.Gateways)))}
	g := Generation{N: n, Workspace: cfg.Workspaces[rng.IntN(len(cfg.Workspaces))],
		Stream: rng.Float64() < cfg.Mix.StreamShare, Started: time.Now().UTC()}
	sum := sha256.Sum256([]byte(fmt.Sprintf("%d/%d", cfg.Seed, n)))
	g.Request = hex.EncodeToString(sum[:8])
	estimate := pick(rng, cfg.Mix.Estimates)
	r.count("generation")
	defer r.record(&g)

	actx, cancel := context.WithTimeout(ctx, cfg.CallWait)
	began := time.Now()
	got, err := p.gateway().Authorize(actx, frontdoor.AuthorizeOf{Workspace: g.Workspace, Request: g.Request,
		Estimate: estimate, Stream: g.Stream, Boot: cfg.Boot})
	cancel()
	if err != nil {
		g.Authorized = "error"
		r.count("authorize error")
		return
	}
	r.latency("authorize", time.Since(began))
	g.Authorized = string(got.Status)
	r.count("authorize " + g.Authorized)
	if got.Status != frontdoor.Admitted {
		return
	}
	if len(cfg.Key) > 0 {
		if e, err := frontdoor.Open(cfg.Key, got.Envelope); err == nil {
			g.Auth, g.Lease = e.Auth, e.Lease
		}
	}
	// The bill: a share of the hold, at times more than it. The mix keeps
	// it within an int64.
	bill, _ := mulDiv(estimate, pick(rng, cfg.Mix.BillPermill), 1000)
	kind, charge := record.Settle, bill
	if rng.Float64() < cfg.Mix.RefundShare {
		kind, charge = record.Refund, 0
	}
	if g.Stream {
		var delivered int64
		g.Streamed, delivered = p.stream(ctx, &g, got.Envelope, estimate, bill, pick(rng, cfg.Mix.Heartbeats))
		r.count("stream " + g.Streamed)
		switch g.Streamed {
		case "refused", "cancelled":
			return
		case "stopped":
			// A stream cut short settles the usage it delivered, not a
			// refund.
			kind, charge = record.Settle, delivered
		}
	}
	if ctx.Err() != nil {
		r.count("generation cancelled")
		return
	}
	g.Terminal = p.terminal(ctx, got.Envelope, kind, charge, g.Request)
}

// stream heartbeats beats times, the seq-th HeartbeatEvery × seq after the
// stream began, each with its sequence, a hash of its snapshot, the usage
// and running charge so far within the hold, and the deadline the last one
// granted; the first carries the reap's basis. It reports how the stream
// ended, and the charge it delivered: at a heartbeat not accepted, what that
// heartbeat reports, past the hold if the bill is.
func (p *played) stream(ctx context.Context, g *Generation, envelope string, estimate, bill, beats int64) (string,
	int64) {
	cfg := p.r.cfg
	began := time.Now()
	var echoed time.Time
	for seq := int64(1); seq <= beats; seq++ {
		wait := time.NewTimer(time.Until(began.Add(time.Duration(seq) * cfg.HeartbeatEvery)))
		select {
		case <-ctx.Done():
			wait.Stop()
			return "cancelled", 0
		case <-wait.C:
		}
		delivered, _ := mulDiv(bill, seq, beats)
		snapshot := sha256.Sum256([]byte(g.Request + "/" + strconv.FormatInt(seq, 10)))
		hb := frontdoor.HeartbeatOf{Envelope: envelope, GatewaySeq: seq, Hash: snapshot[:], Usage: 10 * seq,
			Running: min(delivered, estimate), Echoed: echoed}
		if seq == 1 {
			hb.Basis = []byte(`{"model":"spike","prices":"fixed"}`)
		}
		deadline, accepted := p.heartbeat(ctx, g, hb)
		switch {
		case accepted:
			echoed = deadline
		case ctx.Err() != nil:
			return "cancelled", 0
		case seq == 1:
			return "refused", 0
		default:
			return "stopped", delivered
		}
	}
	return "complete", 0
}

// heartbeat sends one heartbeat as the enclave does: up to three attempts
// within HeartbeatWait, the later two after a Retry or a call that failed.
// It reports the deadline granted, and whether the heartbeat was accepted.
func (p *played) heartbeat(ctx context.Context, g *Generation, hb frontdoor.HeartbeatOf) (time.Time, bool) {
	r := p.r
	beat := Beat{Seq: hb.GatewaySeq}
	defer func() { g.Heartbeats = append(g.Heartbeats, beat) }()
	hctx, cancel := context.WithTimeout(ctx, r.cfg.HeartbeatWait)
	defer cancel()
	for range heartbeatTries {
		began := time.Now()
		got, err := p.gateway().Heartbeat(hctx, hb)
		status := "error"
		if err == nil {
			status = string(got.Status)
			r.latency("heartbeat", time.Since(began))
		} else {
			p.failed()
		}
		beat.Tries = append(beat.Tries, status)
		r.count("heartbeat " + status)
		switch {
		case err == nil && got.Status == frontdoor.Accepted:
			return got.Deadline, true
		case err == nil && got.Status != frontdoor.Retry, hctx.Err() != nil:
			return time.Time{}, false
		}
	}
	return time.Time{}, false
}

// terminal sends the generation's settle or refund until it is answered, as
// the enclave's retry queue does: again after each of RetryDelays, through
// the next gateway after a call that failed. A Failed answer or a call that
// fails is tried again; any other answer ends it.
func (p *played) terminal(ctx context.Context, envelope string, kind record.Kind, charge int64,
	request string) *TerminalDone {
	r := p.r
	done := &TerminalDone{Kind: kind, Charge: charge}
	full, _ := json.Marshal(map[string]any{"request": request, "boot": r.cfg.Boot, "charge": charge})
	money := []byte(`{}`)
	for attempt := 0; ; attempt++ {
		if attempt > 0 {
			if attempt > len(r.cfg.RetryDelays) {
				done.Status = "lost"
				r.count(string(kind) + " lost")
				return done
			}
			wait := time.NewTimer(r.cfg.RetryDelays[attempt-1])
			select {
			case <-ctx.Done():
				wait.Stop()
			case <-wait.C:
			}
			if ctx.Err() != nil {
				done.Status = "cancelled"
				r.count(string(kind) + " cancelled")
				return done
			}
		}
		tctx, cancel := context.WithTimeout(ctx, r.cfg.CallWait)
		began := time.Now()
		var got frontdoor.TerminalAnswer
		var err error
		if kind == record.Settle {
			got, err = p.gateway().Settle(tctx, frontdoor.SettleOf{Envelope: envelope, Charge: charge, Full: full,
				Money: money})
		} else {
			got, err = p.gateway().Refund(tctx, frontdoor.RefundOf{Envelope: envelope, Money: money})
		}
		cancel()
		status := "error"
		if err == nil {
			status = string(got.Status)
			r.latency(string(kind), time.Since(began))
		} else {
			p.failed()
		}
		done.Tries = append(done.Tries, status)
		r.count(string(kind) + " attempt " + status)
		if err == nil && got.Status != frontdoor.Failed {
			done.Status, done.Won, done.WonFor = status, got.Kind, got.Charge
			done.Outcome, done.Cost, done.CostKnown = got.Outcome, got.Cost, got.CostKnown
			r.count(string(kind) + " " + status)
			return done
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
