package edge

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
)

// with is the lines that begin with prefix (logLines is shadow_full_test.go's).
func (l *logLines) with(prefix string) []string {
	l.mu.Lock()
	defer l.mu.Unlock()
	var out []string
	for _, line := range l.lines {
		if strings.HasPrefix(line, prefix) {
			out = append(out, line)
		}
	}
	return out
}

const (
	slowLine   = "fastpath edge pass slow: "
	countsLine = "fastpath edge pass: "
)

// timedOpts shape the timed harness's way to Python.
type timedOpts struct {
	tls bool
	// dialDelay holds each dial before it connects; firstReadDelay holds
	// each of Python's new connections before its first read (a TLS
	// handshake's, with tls); pythonDelay holds Python's answer.
	dialDelay, firstReadDelay, pythonDelay time.Duration
	// closed leaves nothing listening where Python was.
	closed bool
}

// newTimedHarness is newHarness with the edge's log collected and the way to
// Python shaped by o.
func newTimedHarness(t *testing.T, o timedOpts) (*harness, *logLines) {
	t.Helper()
	h := &harness{now: time.Date(2026, 11, 4, 16, 0, 0, 0, time.UTC)}
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	h.private = private
	h.boots = &boots{by: map[string]Boot{"boot-1": {Kid: "boot-1", PublicKey: public, Verified: true}}}
	h.respond = func(w http.ResponseWriter) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusPaymentRequired)
		_, _ = w.Write([]byte(`{"error":{"code":402}}`))
	}
	upstream := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.ReadAll(r.Body)
		time.Sleep(o.pythonDelay)
		h.respond(w)
	}))
	if o.firstReadDelay > 0 {
		upstream.Listener = slowFirstRead{Listener: upstream.Listener, delay: o.firstReadDelay}
	}
	if o.tls {
		upstream.StartTLS()
	} else {
		upstream.Start()
	}
	t.Cleanup(upstream.Close)
	h.upstream = upstream
	target, _ := url.Parse(upstream.URL)
	dialer := &net.Dialer{Timeout: 5 * time.Second}
	transport := &http.Transport{DisableCompression: true, MaxIdleConnsPerHost: 16,
		DialContext: func(ctx context.Context, network, addr string) (net.Conn, error) {
			time.Sleep(o.dialDelay)
			return dialer.DialContext(ctx, network, addr)
		}}
	if o.tls {
		transport.TLSClientConfig = upstream.Client().Transport.(*http.Transport).TLSClientConfig.Clone()
	}
	t.Cleanup(transport.CloseIdleConnections)
	if o.closed {
		upstream.Close()
	}
	logs := &logLines{}
	e, err := New(Config{Token: token, Upstream: target, Boots: h.boots, BootWait: 200 * time.Millisecond,
		Clock: func() time.Time { return h.now }, Transport: transport, Logf: logs.logf})
	if err != nil {
		t.Fatal(err)
	}
	h.edge = e
	h.server = httptest.NewServer(e)
	t.Cleanup(h.server.Close)
	return h, logs
}

// slowFirstRead holds each accepted connection's first read.
type slowFirstRead struct {
	net.Listener
	delay time.Duration
}

func (l slowFirstRead) Accept() (net.Conn, error) {
	c, err := l.Listener.Accept()
	if err != nil {
		return nil, err
	}
	return &heldConn{Conn: c, delay: l.delay}, nil
}

type heldConn struct {
	net.Conn
	delay time.Duration
	once  sync.Once
}

func (c *heldConn) Read(b []byte) (int, error) {
	c.once.Do(func() { time.Sleep(c.delay) })
	return c.Conn.Read(b)
}

// authorize sends one signed authorize and reads its answer whole.
func (h *harness) authorize(t *testing.T) {
	t.Helper()
	resp := h.signed(t, "boot-1", http.MethodPost, authorizePath, authorizeBody, nil)
	_, _ = io.ReadAll(resp.Body)
}

// waitPasses waits for the edge's counts and slow lines (waitFor is
// decide_test.go's): a pass is counted once the proxy returns, which is
// after its answer reached the caller.
func waitPasses(t *testing.T, h *harness, logs *logLines, passes int64, lines int) {
	t.Helper()
	waitFor(t, func() bool {
		return h.edge.PassCounts().Passes == passes && len(logs.with(slowLine)) == lines
	})
}

var fieldPattern = regexp.MustCompile(`(\w+)=(\S+)`)

// fields reads a line's name=value pairs.
func fields(t *testing.T, line string) map[string]string {
	t.Helper()
	out := map[string]string{}
	for _, m := range fieldPattern.FindAllStringSubmatch(line, -1) {
		out[m[1]] = m[2]
	}
	return out
}

func ms(t *testing.T, f map[string]string, name string) int64 {
	t.Helper()
	v, err := strconv.ParseInt(f[name], 10, 64)
	if err != nil {
		t.Fatalf("%s=%q is not a whole number of milliseconds", name, f[name])
	}
	return v
}

func TestTheFirstPassDialsAndTheNextReusesItsConnection(t *testing.T) {
	h, logs := newTimedHarness(t, timedOpts{})
	h.edge.passes.slow = 0 // every pass gets its line
	h.authorize(t)
	waitPasses(t, h, logs, 1, 1)
	time.Sleep(50 * time.Millisecond) // the connection back in the pool
	h.authorize(t)
	waitPasses(t, h, logs, 2, 2)

	got := logs.with(slowLine)
	first, second := fields(t, got[0]), fields(t, got[1])
	if first["connection"] != "new" || first["attempts"] != "1" || first["status"] != "402" {
		t.Fatalf("first pass: %s", got[0])
	}
	if second["connection"] != "reused" || second["attempts"] != "1" || second["status"] != "402" {
		t.Fatalf("second pass: %s", got[1])
	}
	c := h.edge.PassCounts()
	if c.Connection["new"] != 1 || c.Connection["reused"] != 1 || len(c.Setup) != 1 || c.Slow != 2 {
		t.Fatalf("counts: %+v", c)
	}
}

func TestEachLinesPhasesAddUpToItsTotal(t *testing.T) {
	h, logs := newTimedHarness(t, timedOpts{pythonDelay: 30 * time.Millisecond})
	h.edge.passes.slow = 0
	h.authorize(t)
	waitPasses(t, h, logs, 1, 1)
	f := fields(t, logs.with(slowLine)[0])
	sum := ms(t, f, "edge_ms") + ms(t, f, "conn_ms") + ms(t, f, "write_ms") + ms(t, f, "python_ms") +
		ms(t, f, "response_ms")
	// Each phase is rounded down to its millisecond.
	if total := ms(t, f, "total_ms"); sum > total || sum < total-5 {
		t.Fatalf("phases sum to %d ms, the total is %d: %v", sum, total, f)
	}
}

func TestASlowDialIsTheConnPhase(t *testing.T) {
	// The reused pass has the threshold's 250 ms to stay fast on a busy
	// runner; the dial is well past it.
	h, logs := newTimedHarness(t, timedOpts{dialDelay: 400 * time.Millisecond})
	h.edge.passes.slow = 250 * time.Millisecond
	h.authorize(t)
	waitPasses(t, h, logs, 1, 1)
	f := fields(t, logs.with(slowLine)[0])
	if f["phase"] != PassPhaseConn || f["connection"] != "new" || ms(t, f, "conn_ms") < 400 {
		t.Fatalf("a slow dial: %v", f)
	}
	// The next pass reuses the connection: fast, so counted, not logged.
	time.Sleep(50 * time.Millisecond)
	h.authorize(t)
	waitPasses(t, h, logs, 2, 1)
	c := h.edge.PassCounts()
	if c.Slow != 1 || c.SlowBy[PassPhaseConn] != 1 || c.Setup["le_1s"] != 1 || c.Connection["reused"] != 1 {
		t.Fatalf("counts: %+v", c)
	}
}

func TestASlowTLSHandshakeIsTheConnPhaseAndItsTLS(t *testing.T) {
	h, logs := newTimedHarness(t, timedOpts{tls: true, firstReadDelay: 150 * time.Millisecond})
	h.edge.passes.slow = 100 * time.Millisecond
	h.authorize(t)
	waitPasses(t, h, logs, 1, 1)
	f := fields(t, logs.with(slowLine)[0])
	if f["phase"] != PassPhaseConn || ms(t, f, "tls_ms") < 150 || f["tls_failed"] != "false" || f["status"] != "402" {
		t.Fatalf("a slow handshake: %v", f)
	}
}

func TestASlowPythonIsThePythonPhase(t *testing.T) {
	h, logs := newTimedHarness(t, timedOpts{pythonDelay: 150 * time.Millisecond})
	h.edge.passes.slow = 100 * time.Millisecond
	h.authorize(t)
	waitPasses(t, h, logs, 1, 1)
	f := fields(t, logs.with(slowLine)[0])
	if f["phase"] != PassPhasePython || ms(t, f, "python_ms") < 150 {
		t.Fatalf("a slow Python: %v", f)
	}
	if c := h.edge.PassCounts(); c.SlowBy[PassPhasePython] != 1 {
		t.Fatalf("counts: %+v", c)
	}
}

func TestAPythonNotReachedIsUnreachedWithNoConnection(t *testing.T) {
	h, logs := newTimedHarness(t, timedOpts{closed: true})
	h.edge.passes.slow = 0
	resp := h.signed(t, "boot-1", http.MethodPost, authorizePath, authorizeBody, nil)
	if resp.StatusCode != http.StatusBadGateway {
		t.Fatalf("status %d, want the proxy's 502", resp.StatusCode)
	}
	waitPasses(t, h, logs, 1, 1)
	f := fields(t, logs.with(slowLine)[0])
	if f["status"] != "unreached" || f["connection"] != "none" || f["dial_failed"] != "true" {
		t.Fatalf("an unreached Python: %v", f)
	}
	if c := h.edge.PassCounts(); c.Connection["none"] != 1 || len(c.Setup) != 0 {
		t.Fatalf("counts: %+v", c)
	}
}

func TestAPeriodsSlowLinesAreBoundedAndTheRestCounted(t *testing.T) {
	h, logs := newTimedHarness(t, timedOpts{})
	h.edge.passes.slow = 0
	h.edge.passes.lines = 2
	for range 5 {
		h.authorize(t)
	}
	waitPasses(t, h, logs, 5, 2)
	if c := h.edge.PassCounts(); c.Slow != 5 || c.Unlogged != 3 {
		t.Fatalf("counts: %+v", c)
	}
}

func TestAPeriodsCountsAreLoggedByThePassAfterIt(t *testing.T) {
	h, logs := newTimedHarness(t, timedOpts{})
	h.edge.passes.every = time.Second
	h.edge.passes.slow = 0
	h.edge.passes.lines = 1
	h.authorize(t)
	h.authorize(t)
	waitPasses(t, h, logs, 2, 1)
	if got := logs.with(countsLine); len(got) != 0 {
		t.Fatalf("counts logged before the period ended: %v", got)
	}
	time.Sleep(1100 * time.Millisecond)
	h.authorize(t)
	// The new period logs its own first slow line.
	waitPasses(t, h, logs, 3, 2)
	got := logs.with(countsLine)
	if len(got) != 1 {
		t.Fatalf("counts lines: %v", got)
	}
	f := fields(t, got[0])
	if f["passes"] != "2" || f["slow"] != "2" || f["unlogged"] != "1" || ms(t, f, "seconds") < 1 {
		t.Fatalf("the period's counts: %s", got[0])
	}
	if !strings.Contains(got[0], `connection={"new":1,"reused":1}`) &&
		!strings.Contains(got[0], `connection={"new":2}`) {
		t.Fatalf("the period's connections: %s", got[0])
	}
}

func TestOnlyAnAuthorizeWithTheTokenIsTimed(t *testing.T) {
	h, logs := newTimedHarness(t, timedOpts{})
	h.edge.passes.slow = 0
	// Another path, signed and with the token, passes untimed.
	resp := h.signed(t, "boot-1", http.MethodPost, heartbeatPath, authorizeBody, nil)
	_, _ = io.ReadAll(resp.Body)
	// An authorize without the token passes as it is, untimed.
	resp = h.send(t, http.MethodPost, authorizePath, authorizeBody, nil)
	_, _ = io.ReadAll(resp.Body)
	// A timed one, so the untimed ones have had their time to be counted.
	h.authorize(t)
	waitPasses(t, h, logs, 1, 1)
	if c := h.edge.PassCounts(); c.Passes != 1 {
		t.Fatalf("counts: %+v", c)
	}
}

func TestTheLinesNameNoRequest(t *testing.T) {
	h, logs := newTimedHarness(t, timedOpts{})
	h.edge.passes.every = time.Nanosecond // each pass ends the period before it
	h.edge.passes.slow = 0
	h.authorize(t)
	h.authorize(t)
	waitPasses(t, h, logs, 2, 2)
	waitFor(t, func() bool { return len(logs.with(countsLine)) == 1 })
	upstream, _ := url.Parse(h.upstream.URL)
	edgeHost, _ := url.Parse(h.server.URL)
	for _, line := range append(logs.with(slowLine), logs.with(countsLine)...) {
		for _, secret := range []string{"lookup-1", "boot-1", token, "/internal", upstream.Host, upstream.Hostname(),
			edgeHost.Host, `"model"`} {
			if strings.Contains(line, secret) {
				t.Fatalf("%q names %q", line, secret)
			}
		}
	}
}

func TestPhasesTellAPassApart(t *testing.T) {
	t0 := time.Date(2026, 11, 4, 16, 21, 40, 0, time.UTC)
	at := func(ms int) time.Time { return t0.Add(time.Duration(ms) * time.Millisecond) }
	for _, c := range []struct {
		name                                string
		p                                   *passTrace
		done                                time.Time
		edge, conn, write, python, response int
		slowest, connection                 string
	}{
		{"answered", &passTrace{arrived: at(0), forwarded: at(1), gotConn: at(3), wrote: at(4), firstByte: at(10),
			reused: true}, at(12), 1, 2, 1, 6, 2, PassPhasePython, "reused"},
		{"no connection: the rest is conn's", &passTrace{arrived: at(0), forwarded: at(1), getConn: at(1)},
			at(3001), 1, 3000, 0, 0, 0, PassPhaseConn, "none"},
		{"no first byte: the rest is Python's", &passTrace{arrived: at(0), forwarded: at(1), gotConn: at(2),
			wrote: at(3)}, at(2003), 1, 1, 1, 2000, 0, PassPhasePython, "new"},
		{"a tie goes to the earlier phase", &passTrace{arrived: at(0), forwarded: at(5), gotConn: at(10), wrote: at(10),
			firstByte: at(12)}, at(14), 5, 5, 0, 2, 2, PassPhaseEdge, "new"},
		{"a mark before the last is not an end", &passTrace{arrived: at(0), forwarded: at(2), gotConn: at(1)},
			at(9), 2, 7, 0, 0, 0, PassPhaseConn, "new"},
	} {
		t.Run(c.name, func(t *testing.T) {
			s := c.p.phases(c.done)
			got := []time.Duration{s.edge, s.conn, s.write, s.python, s.response}
			want := []int{c.edge, c.conn, c.write, c.python, c.response}
			for i := range got {
				if got[i] != time.Duration(want[i])*time.Millisecond {
					t.Fatalf("phases %v, want %v ms", got, want)
				}
			}
			if s.slowest != c.slowest || s.connection != c.connection || s.total != c.done.Sub(t0) {
				t.Fatalf("slowest %q connection %q total %v", s.slowest, s.connection, s.total)
			}
		})
	}
}

func TestANewConnectionsSetupIsBucketedAtTheSYNRetries(t *testing.T) {
	for took, want := range map[time.Duration]string{
		10 * time.Millisecond:   "le_10ms",
		11 * time.Millisecond:   "le_100ms",
		time.Second:             "le_1s",
		1200 * time.Millisecond: "le_2s",
		3050 * time.Millisecond: "gt_2s",
	} {
		if got := setupBucket(took); got != want {
			t.Fatalf("setupBucket(%v) = %q, want %q", took, got, want)
		}
	}
}
