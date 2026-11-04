package edge

import (
	"context"
	"crypto/tls"
	"fmt"
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
// five phases that follow one another from its arrival at the handler to
// its answer written back:
//
//   - edge: the arrival to the forwarding (the body's read, the boot check
//     and the decide path, which count their own slow passes);
//   - conn: the forwarding to a connection to Python;
//   - write: the connection to the request written;
//   - python: the request written to Python's final response headers;
//   - response: those headers to the answer written back, or to the proxy's
//     abort when the copy broke off (the caller hung up, or Python's answer
//     stopped short).
//
// A phase whose end never came (Python not reached, a write that failed,
// no answer) takes the rest of the time, so the phases add up to the total.
// A connection is idle (taken from the pool, with how long it sat there),
// handed (given over as another pass put it back, freed or freshly dialed:
// this pass found no idle one and waited), new (this pass's own dial), or
// none. The DNS, dial and TLS times are this pass's own dial's, if it made
// one: they break down a new connection's conn phase; on another line they
// are a dial that lost the race (kept for the pool), and a dial still
// running when the pass ended reads 0.
//
// A pass slower than SlowPass in all gets one line of its phases, at most
// SlowPassLines a period. Each period's counts are one line, written when
// the first pass after the period ends, and the last period's by
// FlushPasses; so a quiet node writes none, and Run keeps no goroutine for
// them. The lines are handed to one writer goroutine through a bounded
// queue, so a slow log never holds a pass; a line the full queue cannot
// take is counted (dropped). No line carries a body, an id, a key, a path or
// an address.

const (
	// SlowPass is the time in all, from the arrival, past which a pass's
	// phases are logged: Python answers an authorize in tens of
	// milliseconds, so a pass over it spent its time somewhere to look at.
	SlowPass = 500 * time.Millisecond
	// SlowPassLines bounds a period's slow-pass lines; past it a slow pass
	// is counted (unlogged) in the period's counts.
	SlowPassLines = 30
	// passQueue is how many lines wait for the writer before more are
	// dropped: two periods' slow lines and their counts.
	passQueue = 2*SlowPassLines + 4
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

// The connections a pass can have had (GotConnInfo).
const (
	PassConnIdle   = "idle"
	PassConnHanded = "handed"
	PassConnNew    = "new"
	PassConnNone   = "none"
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
	gotConn                   time.Time
	dnsStart, dnsDone         time.Time
	connectStart, connectDone time.Time
	tlsStart, tlsDone         time.Time
	wrote, headers            time.Time
	// reused, wasIdle and idle are the connection's (GotConnInfo).
	reused, wasIdle bool
	idle            time.Duration
	// The errors of this pass's own dial and of its write: a dial over
	// several addresses can fail on one and connect on another.
	dnsErrors, connectErrors, tlsErrors, writeErrors int
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
// addresses spans them all. The end of the python phase is not a hook's:
// GotFirstResponseByte fires on a 1xx too, so the transport marks it when
// the final headers are read (tracedTransport).
func (p *passTrace) hooks() *httptrace.ClientTrace {
	return &httptrace.ClientTrace{
		GotConn: func(info httptrace.GotConnInfo) {
			p.at(func(now time.Time) {
				p.gotConn, p.reused, p.wasIdle, p.idle = now, info.Reused, info.WasIdle, info.IdleTime
			})
		},
		DNSStart: func(httptrace.DNSStartInfo) {
			p.at(func(now time.Time) {
				if p.dnsStart.IsZero() {
					p.dnsStart = now
				}
			})
		},
		DNSDone: func(info httptrace.DNSDoneInfo) {
			p.at(func(now time.Time) {
				p.dnsDone = now
				if info.Err != nil {
					p.dnsErrors++
				}
			})
		},
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
				if err != nil {
					p.connectErrors++
				}
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
				if err != nil {
					p.tlsErrors++
				}
			})
		},
		WroteRequest: func(info httptrace.WroteRequestInfo) {
			p.at(func(now time.Time) {
				// A write that failed has no end: its time stays the write's.
				if info.Err != nil {
					p.writeErrors++
					return
				}
				p.wrote = now
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
	connection                          string
	status                              int
	dnsErrors, connectErrors            int
	tlsErrors, writeErrors              int
	slowest                             string
}

// phases reads a pass's phases as of done, its answer written back or its
// copy aborted.
func (p *passTrace) phases(done time.Time) passPhases {
	p.mu.Lock()
	defer p.mu.Unlock()
	s := passPhases{total: span(p.arrived, done), dns: span(p.dnsStart, p.dnsDone),
		connect: span(p.connectStart, p.connectDone), tls: span(p.tlsStart, p.tlsDone), status: p.status,
		dnsErrors: p.dnsErrors, connectErrors: p.connectErrors, tlsErrors: p.tlsErrors, writeErrors: p.writeErrors}
	switch {
	case p.gotConn.IsZero():
		s.connection = PassConnNone
	case p.wasIdle:
		s.connection, s.idle = PassConnIdle, p.idle
	case p.reused:
		s.connection = PassConnHanded
	default:
		s.connection = PassConnNew
	}
	from := p.arrived
	for _, phase := range []struct {
		to   time.Time
		took *time.Duration
	}{{p.forwarded, &s.edge}, {p.gotConn, &s.conn}, {p.wrote, &s.write}, {p.headers, &s.python}, {done, &s.response}} {
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

// waitBucket is the conn phase, as the counts name it, of a pass that found
// no idle connection (new or handed): a lost SYN is resent after a second,
// and again two seconds later.
func waitBucket(took time.Duration) string {
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
	// out is the writer's queue: its goroutine starts with the first line
	// and ends once FlushPasses has closed the queue (closed).
	out           chan string
	writing       bool
	closed        bool
	writerStopped chan struct{}
}

// passTally is the passes' counts: the passes, of which aborted (the
// proxy's copy broke off), by their connection, the conn phase of those
// that found no idle connection (wait, by waitBucket), the slow passes, by
// their slowest phase, those not logged past the bound, and the lines the
// writer's queue could not take.
type passTally struct {
	passes, aborted, slow, unlogged, dropped int64
	connection, wait, slowBy                 map[string]int64
}

func newPassTally() passTally {
	return passTally{connection: map[string]int64{}, wait: map[string]int64{}, slowBy: map[string]int64{}}
}

func newPassTimer(logf func(string, ...any), every time.Duration) *passTimer {
	if every <= 0 {
		every = time.Minute
	}
	return &passTimer{logf: logf, every: every, slow: SlowPass, lines: SlowPassLines, total: newPassTally(),
		period: newPassTally(), out: make(chan string, passQueue), writerStopped: make(chan struct{})}
}

// start is an authorize's trace, nil for any other request (not timed).
func (pt *passTimer) start(r *http.Request, arrived time.Time) *passTrace {
	if r.Method != http.MethodPost || !authorizeRoutes[r.URL.Path] {
		return nil
	}
	return &passTrace{arrived: arrived}
}

// observe counts a pass that ended at done (aborted when the proxy's copy
// broke off), queues the period's counts line first if the period has
// ended, and the pass's line if it was slow and the period's bound allows.
func (pt *passTimer) observe(p *passTrace, done time.Time, aborted bool) {
	s := p.phases(done)
	pt.mu.Lock()
	var counts string
	if pt.started.IsZero() {
		pt.started = done
	} else if took := done.Sub(pt.started); took >= pt.every {
		counts = countsLine(pt.period, took)
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
		if s.connection == PassConnNew || s.connection == PassConnHanded {
			t.wait[waitBucket(s.conn)]++
		}
		if aborted {
			t.aborted++
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
	if counts != "" {
		pt.emit(counts)
	}
	if line {
		pt.emit(fmt.Sprintf("fastpath edge pass slow: status=%s aborted=%t total_ms=%d phase=%s edge_ms=%d "+
			"conn_ms=%d write_ms=%d python_ms=%d response_ms=%d connection=%s idle_ms=%d dns_ms=%d connect_ms=%d "+
			"tls_ms=%d dns_errors=%d connect_errors=%d tls_errors=%d write_errors=%d", statusName(s.status), aborted,
			s.total.Milliseconds(), s.slowest, s.edge.Milliseconds(), s.conn.Milliseconds(), s.write.Milliseconds(),
			s.python.Milliseconds(), s.response.Milliseconds(), s.connection, s.idle.Milliseconds(),
			s.dns.Milliseconds(), s.connect.Milliseconds(), s.tls.Milliseconds(), s.dnsErrors, s.connectErrors,
			s.tlsErrors, s.writeErrors))
	}
}

// countsLine is a period's counts, over took (its first pass to the pass
// that ended it).
func countsLine(t passTally, took time.Duration) string {
	return fmt.Sprintf("fastpath edge pass: seconds=%d passes=%d aborted=%d connection=%s conn_wait=%s slow=%d "+
		"slow_by_phase=%s unlogged=%d dropped=%d", int64(took.Seconds()), t.passes, t.aborted, jsonOf(t.connection),
		jsonOf(t.wait), t.slow, jsonOf(t.slowBy), t.unlogged, t.dropped)
}

// emit hands a line to the writer, starting it with the first; a line the
// full queue cannot take, or one after FlushPasses, is counted dropped.
func (pt *passTimer) emit(line string) {
	pt.mu.Lock()
	defer pt.mu.Unlock()
	if !pt.closed {
		if !pt.writing {
			pt.writing = true
			go pt.write()
		}
		select {
		case pt.out <- line:
			return
		default:
		}
	}
	pt.total.dropped++
	pt.period.dropped++
}

// write logs the queued lines until the queue is closed.
func (pt *passTimer) write() {
	defer close(pt.writerStopped)
	for line := range pt.out {
		pt.logf("%s", line)
	}
}

// FlushPasses ends the passes' timing at shutdown, once the edge's handlers
// have returned: the lines still queued are written, then the last
// period's counts. A pass after it is counted, its lines dropped.
func (e *Edge) FlushPasses() {
	pt := e.passes
	pt.mu.Lock()
	if pt.closed {
		pt.mu.Unlock()
		return
	}
	pt.closed = true
	var counts string
	if pt.period.passes > 0 {
		counts = countsLine(pt.period, time.Since(pt.started))
		pt.period = newPassTally()
	}
	writing := pt.writing
	pt.mu.Unlock()
	// No line is queued once closed is set: emit checks it under the lock.
	close(pt.out)
	if writing {
		<-pt.writerStopped
	}
	if counts != "" {
		pt.logf("%s", counts)
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

// PassCounts are the timed passes' counts since the edge started (see
// passTally).
type PassCounts struct {
	Passes, Aborted, Slow, Unlogged, Dropped int64
	Connection, Wait, SlowBy                 map[string]int64
}

// PassCounts are the counts now.
func (e *Edge) PassCounts() PassCounts {
	pt := e.passes
	pt.mu.Lock()
	defer pt.mu.Unlock()
	t := pt.total
	return PassCounts{Passes: t.passes, Aborted: t.aborted, Slow: t.slow, Unlogged: t.unlogged, Dropped: t.dropped,
		Connection: maps.Clone(t.connection), Wait: maps.Clone(t.wait), SlowBy: maps.Clone(t.slowBy)}
}

// tracedTransport attaches a pass's hooks to the request the proxy sends,
// and marks the final headers and Python's status; a request with no pass
// in its context goes as it came.
type tracedTransport struct{ base http.RoundTripper }

func (t tracedTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	p, ok := r.Context().Value(passKey{}).(*passTrace)
	if !ok {
		return t.base.RoundTrip(r)
	}
	resp, err := t.base.RoundTrip(r.WithContext(httptrace.WithClientTrace(r.Context(), p.hooks())))
	if err == nil {
		p.at(func(now time.Time) { p.headers, p.status = now, resp.StatusCode })
	}
	return resp, err
}

// forward passes a request to Python, timing it when pass is not nil. A
// copy that breaks off makes the proxy panic with http.ErrAbortHandler,
// which the server handles; the pass is counted, aborted, on the way.
func (e *Edge) forward(w http.ResponseWriter, r *http.Request, pass *passTrace) {
	if pass == nil {
		e.proxy.ServeHTTP(w, r)
		return
	}
	pass.at(func(now time.Time) { pass.forwarded = now })
	completed := false
	defer func() { e.passes.observe(pass, time.Now(), !completed) }()
	e.proxy.ServeHTTP(w, r.WithContext(context.WithValue(r.Context(), passKey{}, pass)))
	completed = true
}
