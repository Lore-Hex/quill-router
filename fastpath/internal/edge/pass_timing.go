package edge

import (
	"context"
	"crypto/tls"
	"maps"
	"net/http"
	"net/http/httptrace"
	"strconv"
	"sync"
	"time"
)

// The pass's timing: the decide path tells a pass's time at the edge apart
// up to its forwarding (edgeMarks, slowPhase), and this tells apart the
// rest, the proxy's way to Python and back, read from net/http/httptrace.
// Every authorize passed to Python with the internal token is timed, in
// five phases that follow one another from its arrival to its answer
// written back whole:
//
//   - edge: the arrival to the forwarding (the body's read, the boot check
//     and the decide path, which count their own slow passes);
//   - conn: the forwarding to a connection to Python, an idle one handed
//     over or a new one's DNS lookup, dial and TLS handshake (logged apart
//     too, as dns_ms, connect_ms and tls_ms);
//   - write: the connection to the request written;
//   - python: the request written to Python's first byte;
//   - response: Python's first byte to the answer written back whole.
//
// A phase whose end never came (Python not reached, or not answering) takes
// the rest of the time, so the phases add up to the total. A pass slower
// than SlowPass in all gets one line of its phases, at most SlowPassLines a
// period; each period's counts are logged in one line when the first pass
// after the period ends, so a quiet node logs none (Run keeps no goroutine
// for them). No line carries a body, an id, a key, a path or an address.

const (
	// SlowPass is the time in all, from the arrival, past which a pass's
	// phases are logged: Python answers an authorize in tens of
	// milliseconds, so a pass over it spent its time somewhere to look at.
	SlowPass = 500 * time.Millisecond
	// SlowPassLines bounds a period's slow-pass lines; past it a slow pass
	// is counted (unlogged) in the period's counts.
	SlowPassLines = 30
)

// The phases of a pass, in order; a slow pass is counted under the one it
// spent the most time in (of phases that took the same time, the earlier).
const (
	PassPhaseEdge     = "edge"
	PassPhaseConn     = "conn"
	PassPhaseWrite    = "write"
	PassPhasePython   = "python"
	PassPhaseResponse = "response"
)

// passKey carries a pass's trace in the proxied request's context, for the
// transport to attach its hooks to.
type passKey struct{}

// passTrace is one pass's instants. The hooks of a dial can run after the
// pass has ended (a dial the transport keeps for its pool), so every field
// is read and written under mu.
type passTrace struct {
	mu                        sync.Mutex
	arrived, forwarded        time.Time
	getConn, gotConn          time.Time
	dnsStart, dnsDone         time.Time
	connectStart, connectDone time.Time
	tlsStart, tlsDone         time.Time
	wrote, firstByte          time.Time
	// reused and idle are the connection's (GotConnInfo); attempts counts
	// the transport's asks for one, more than one when it retried the
	// request on another connection.
	reused     bool
	idle       time.Duration
	attempts   int
	dialFailed bool
	tlsFailed  bool
	// status is Python's; 0 when no answer came.
	status int
}

func (p *passTrace) at(set func(now time.Time)) {
	now := time.Now()
	p.mu.Lock()
	set(now)
	p.mu.Unlock()
}

// hooks are the pass's httptrace hooks. Each records the first instant of
// its kind and the last end, so a lookup or dial raced over several
// addresses spans them all.
func (p *passTrace) hooks() *httptrace.ClientTrace {
	return &httptrace.ClientTrace{
		GetConn: func(string) {
			p.at(func(now time.Time) {
				p.attempts++
				if p.attempts > 1 {
					// A retry on another connection: the request is written,
					// and answered, on that one.
					p.wrote, p.firstByte = time.Time{}, time.Time{}
				}
				if p.getConn.IsZero() {
					p.getConn = now
				}
			})
		},
		GotConn: func(info httptrace.GotConnInfo) {
			p.at(func(now time.Time) { p.gotConn, p.reused, p.idle = now, info.Reused, info.IdleTime })
		},
		DNSStart: func(httptrace.DNSStartInfo) {
			p.at(func(now time.Time) {
				if p.dnsStart.IsZero() {
					p.dnsStart = now
				}
			})
		},
		DNSDone: func(httptrace.DNSDoneInfo) { p.at(func(now time.Time) { p.dnsDone = now }) },
		ConnectStart: func(string, string) {
			p.at(func(now time.Time) {
				if p.connectStart.IsZero() {
					p.connectStart = now
				}
			})
		},
		ConnectDone: func(_, _ string, err error) {
			p.at(func(now time.Time) {
				p.connectDone = now
				p.dialFailed = p.dialFailed || err != nil
			})
		},
		TLSHandshakeStart: func() {
			p.at(func(now time.Time) {
				if p.tlsStart.IsZero() {
					p.tlsStart = now
				}
			})
		},
		TLSHandshakeDone: func(_ tls.ConnectionState, err error) {
			p.at(func(now time.Time) {
				p.tlsDone = now
				p.tlsFailed = p.tlsFailed || err != nil
			})
		},
		WroteRequest: func(httptrace.WroteRequestInfo) { p.at(func(now time.Time) { p.wrote = now }) },
		GotFirstResponseByte: func() {
			p.at(func(now time.Time) {
				if p.firstByte.IsZero() {
					p.firstByte = now
				}
			})
		},
	}
}

// passPhases is a pass's time, by phase and in all, with what its
// connection was.
type passPhases struct {
	total                               time.Duration
	edge, conn, write, python, response time.Duration
	dns, connect, tls, idle             time.Duration
	connection                          string // reused, new, or none (no connection was had)
	attempts, status                    int
	dialFailed, tlsFailed               bool
	slowest                             string
}

// phases reads a pass's phases as of done, its answer written back.
func (p *passTrace) phases(done time.Time) passPhases {
	p.mu.Lock()
	defer p.mu.Unlock()
	s := passPhases{total: span(p.arrived, done), dns: span(p.dnsStart, p.dnsDone),
		connect: span(p.connectStart, p.connectDone), tls: span(p.tlsStart, p.tlsDone),
		attempts: p.attempts, status: p.status, dialFailed: p.dialFailed, tlsFailed: p.tlsFailed}
	switch {
	case p.gotConn.IsZero():
		s.connection = "none"
	case p.reused:
		s.connection, s.idle = "reused", p.idle
	default:
		s.connection = "new"
	}
	from := p.arrived
	for _, phase := range []struct {
		to   time.Time
		took *time.Duration
	}{{p.forwarded, &s.edge}, {p.gotConn, &s.conn}, {p.wrote, &s.write}, {p.firstByte, &s.python}, {done, &s.response}} {
		if phase.to.IsZero() || phase.to.Before(from) {
			// Its end never came: the rest is this phase's.
			*phase.took = span(from, done)
			break
		}
		*phase.took, from = phase.to.Sub(from), phase.to
	}
	s.slowest = PassPhaseEdge
	longest := s.edge
	for _, phase := range []struct {
		name string
		took time.Duration
	}{{PassPhaseConn, s.conn}, {PassPhaseWrite, s.write}, {PassPhasePython, s.python}, {PassPhaseResponse, s.response}} {
		if phase.took > longest {
			s.slowest, longest = phase.name, phase.took
		}
	}
	return s
}

// span is to minus from, or 0 when either is unknown or to came first.
func span(from, to time.Time) time.Duration {
	if from.IsZero() || to.IsZero() || to.Before(from) {
		return 0
	}
	return to.Sub(from)
}

// setupBucket is a new connection's conn phase as the counts name it: a
// lost SYN is retried after a second, and again two seconds later.
func setupBucket(took time.Duration) string {
	switch {
	case took <= 10*time.Millisecond:
		return "le_10ms"
	case took <= 100*time.Millisecond:
		return "le_100ms"
	case took <= time.Second:
		return "le_1s"
	case took <= 2*time.Second:
		return "le_2s"
	}
	return "gt_2s"
}

// passTimer keeps the passes' counts and writes their lines.
type passTimer struct {
	logf  func(string, ...any)
	every time.Duration
	// slow and lines are SlowPass and SlowPassLines; a test shortens them.
	slow  time.Duration
	lines int

	mu      sync.Mutex
	started time.Time // the period's first pass, on the real clock
	logged  int       // the period's slow-pass lines
	total   passTally
	period  passTally
}

// passTally is the passes' counts: the passes, by their connection (reused,
// new, none), a new connection's conn phase bucketed (setupBucket), the
// slow passes, by their slowest phase, and those not logged past the bound.
type passTally struct {
	passes, slow, unlogged int64
	connection, setup      map[string]int64
	slowBy                 map[string]int64
}

func newPassTally() passTally {
	return passTally{connection: map[string]int64{}, setup: map[string]int64{}, slowBy: map[string]int64{}}
}

func newPassTimer(logf func(string, ...any), every time.Duration) *passTimer {
	if every <= 0 {
		every = time.Minute
	}
	return &passTimer{logf: logf, every: every, slow: SlowPass, lines: SlowPassLines, total: newPassTally(),
		period: newPassTally()}
}

// start is an authorize's trace, nil for any other request (not timed).
func (pt *passTimer) start(r *http.Request, arrived time.Time) *passTrace {
	if r.Method != http.MethodPost || !authorizeRoutes[r.URL.Path] {
		return nil
	}
	return &passTrace{arrived: arrived}
}

// observe counts a pass whose answer was written back at done, logs the
// period's counts first if the period has ended, and logs the pass's line
// if it was slow and the period's bound allows.
func (pt *passTimer) observe(p *passTrace, done time.Time) {
	s := p.phases(done)
	pt.mu.Lock()
	var ended *passTally
	var seconds int64
	if pt.started.IsZero() {
		pt.started = done
	} else if took := done.Sub(pt.started); took >= pt.every {
		finished := pt.period
		ended, seconds = &finished, int64(took.Seconds())
		pt.period, pt.started, pt.logged = newPassTally(), done, 0
	}
	slow := s.total > pt.slow
	line := slow && pt.logged < pt.lines
	if line {
		pt.logged++
	}
	for _, t := range []*passTally{&pt.total, &pt.period} {
		t.passes++
		t.connection[s.connection]++
		if s.connection == "new" {
			t.setup[setupBucket(s.conn)]++
		}
		if slow {
			t.slow++
			t.slowBy[s.slowest]++
			if !line {
				t.unlogged++
			}
		}
	}
	pt.mu.Unlock()
	if ended != nil {
		pt.logf("fastpath edge pass: seconds=%d passes=%d connection=%s new_setup=%s slow=%d slow_by_phase=%s unlogged=%d",
			seconds, ended.passes, jsonOf(ended.connection), jsonOf(ended.setup), ended.slow, jsonOf(ended.slowBy),
			ended.unlogged)
	}
	if line {
		pt.logf("fastpath edge pass slow: status=%s total_ms=%d phase=%s edge_ms=%d conn_ms=%d write_ms=%d python_ms=%d "+
			"response_ms=%d connection=%s idle_ms=%d attempts=%d dns_ms=%d connect_ms=%d tls_ms=%d dial_failed=%t "+
			"tls_failed=%t", statusName(s.status), s.total.Milliseconds(), s.slowest, s.edge.Milliseconds(),
			s.conn.Milliseconds(), s.write.Milliseconds(), s.python.Milliseconds(), s.response.Milliseconds(),
			s.connection, s.idle.Milliseconds(), s.attempts, s.dns.Milliseconds(), s.connect.Milliseconds(),
			s.tls.Milliseconds(), s.dialFailed, s.tlsFailed)
	}
}

// statusName is Python's status as the line names it: unreached when no
// answer came.
func statusName(status int) string {
	if status == 0 {
		return "unreached"
	}
	return strconv.Itoa(status)
}

// PassCounts are the timed passes' counts since the edge started: the
// passes, by their connection (reused, new, none) and a new one's conn
// phase (setupBucket), and the slow passes (over SlowPass), by their
// slowest phase, with those not logged past SlowPassLines.
type PassCounts struct {
	Passes, Slow, Unlogged    int64
	Connection, Setup, SlowBy map[string]int64
}

// PassCounts are the counts now.
func (e *Edge) PassCounts() PassCounts {
	pt := e.passes
	pt.mu.Lock()
	defer pt.mu.Unlock()
	t := pt.total
	return PassCounts{Passes: t.passes, Slow: t.slow, Unlogged: t.unlogged, Connection: maps.Clone(t.connection),
		Setup: maps.Clone(t.setup), SlowBy: maps.Clone(t.slowBy)}
}

// tracedTransport attaches a pass's hooks to the request the proxy sends
// and records Python's status; a request with no pass in its context goes
// as it came.
type tracedTransport struct{ base http.RoundTripper }

func (t tracedTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	p, ok := r.Context().Value(passKey{}).(*passTrace)
	if !ok {
		return t.base.RoundTrip(r)
	}
	resp, err := t.base.RoundTrip(r.WithContext(httptrace.WithClientTrace(r.Context(), p.hooks())))
	if err == nil {
		p.at(func(time.Time) { p.status = resp.StatusCode })
	}
	return resp, err
}

// forward passes a request to Python, timing it when pass is not nil.
func (e *Edge) forward(w http.ResponseWriter, r *http.Request, pass *passTrace) {
	if pass == nil {
		e.proxy.ServeHTTP(w, r)
		return
	}
	pass.at(func(now time.Time) { pass.forwarded = now })
	e.proxy.ServeHTTP(w, r.WithContext(context.WithValue(r.Context(), passKey{}, pass)))
	e.passes.observe(pass, time.Now())
}
