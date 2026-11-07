package edge

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/tls"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/http/httptrace"
	"net/textproto"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
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

// last is the last line logged, or "" for none.
func (l *logLines) last() string {
	l.mu.Lock()
	defer l.mu.Unlock()
	if len(l.lines) == 0 {
		return ""
	}
	return l.lines[len(l.lines)-1]
}

const (
	slowPrefix   = "fastpath edge pass slow: "
	countsPrefix = "fastpath edge pass: "
)

// timedOpts shape the timed harness's way to Python.
type timedOpts struct {
	tls bool
	// dialDelay holds each dial before it connects; firstReadDelay holds
	// the first read of each of Python's new connections once its bytes
	// arrive (a TLS ClientHello, with tls); pythonDelay holds Python's
	// answer.
	dialDelay, firstReadDelay, pythonDelay time.Duration
	// closed leaves nothing listening where Python was.
	closed bool
	// logf, when set, is the edge's log instead of the collected one.
	logf func(string, ...any)
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
	logf := o.logf
	if logf == nil {
		logf = logs.logf
	}
	e, err := New(Config{Token: token, Upstream: target, Boots: h.boots, BootWait: 200 * time.Millisecond,
		Clock: func() time.Time { return h.now }, Transport: transport, Logf: logf})
	if err != nil {
		t.Fatal(err)
	}
	h.edge = e
	h.server = httptest.NewServer(e)
	t.Cleanup(h.server.Close)
	return h, logs
}

// slowFirstRead holds each accepted connection's first read once its bytes
// arrive.
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

// Read holds the first read after its bytes arrive. The transport runs the
// TLSHandshakeStart hooks before it sends the ClientHello, so the hold starts
// after the edge marks the handshake's start. Held from accept, it could
// start before that mark, while the handshake's goroutine waited to run.
func (c *heldConn) Read(b []byte) (int, error) {
	n, err := c.Conn.Read(b)
	c.once.Do(func() { time.Sleep(c.delay) })
	return n, err
}

// authorize sends one signed authorize and reads its answer whole.
func (h *harness) authorize(t *testing.T) {
	t.Helper()
	resp := h.signed(t, "boot-1", http.MethodPost, authorizePath, authorizeBody, nil)
	_, _ = io.ReadAll(resp.Body)
}

// signedRequest is a signed authorize with the token, for a caller of its own.
func (h *harness) signedRequest(t *testing.T, ctx context.Context) *http.Request {
	t.Helper()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, h.server.URL+authorizePath,
		strings.NewReader(authorizeBody))
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set(TokenHeader, token)
	req.Header.Set(BootHeader, h.header("boot-1", http.MethodPost, authorizePath, []byte(authorizeBody)))
	return req
}

// waitPasses waits for the edge's counts and slow lines (waitFor is
// decide_test.go's): a pass is counted once the proxy returns, after its
// answer reached the caller, and its lines are written by the writer
// goroutine.
func waitPasses(t *testing.T, h *harness, logs *logLines, passes int64, lines int) {
	t.Helper()
	waitFor(t, func() bool {
		return h.edge.PassCounts().Passes == passes && len(logs.with(slowPrefix)) == lines
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
		t.Fatalf("%s=%q is not a whole number", name, f[name])
	}
	return v
}

func total(m map[string]int64) int64 {
	var n int64
	for _, v := range m {
		n += v
	}
	return n
}

func TestTheFirstPassDialsAndTheNextTakesItIdle(t *testing.T) {
	h, logs := newTimedHarness(t, timedOpts{})
	h.edge.passes.slow = 0 // every pass gets its line
	h.authorize(t)
	waitPasses(t, h, logs, 1, 1)
	time.Sleep(50 * time.Millisecond) // the connection back in the pool
	h.authorize(t)
	waitPasses(t, h, logs, 2, 2)

	got := logs.with(slowPrefix)
	first, second := fields(t, got[0]), fields(t, got[1])
	if first["connection"] != PassConnNew || first["status"] != "402" || first["aborted"] != "false" {
		t.Fatalf("first pass: %s", got[0])
	}
	if second["connection"] != PassConnIdle || second["status"] != "402" || ms(t, second, "idle_ms") < 0 {
		t.Fatalf("second pass: %s", got[1])
	}
	c := h.edge.PassCounts()
	if c.Connection[PassConnNew] != 1 || c.Connection[PassConnIdle] != 1 || total(c.Wait) != 1 || c.Slow != 2 {
		t.Fatalf("counts: %+v", c)
	}
}

func TestEachLinesPhasesAddUpToItsTotal(t *testing.T) {
	h, logs := newTimedHarness(t, timedOpts{pythonDelay: 30 * time.Millisecond})
	h.edge.passes.slow = 0
	h.authorize(t)
	waitPasses(t, h, logs, 1, 1)
	f := fields(t, logs.with(slowPrefix)[0])
	sum := ms(t, f, "edge_ms") + ms(t, f, "conn_ms") + ms(t, f, "write_ms") + ms(t, f, "python_ms") +
		ms(t, f, "response_ms")
	// Each phase is rounded down to its millisecond.
	if all := ms(t, f, "total_ms"); sum > all || sum < all-5 {
		t.Fatalf("phases sum to %d ms, the total is %d: %v", sum, all, f)
	}
}

func TestASlowDialIsTheConnPhase(t *testing.T) {
	// The idle pass has the threshold's 400 ms to stay fast on a busy
	// runner; the dial is well past it.
	h, logs := newTimedHarness(t, timedOpts{dialDelay: 600 * time.Millisecond})
	h.edge.passes.slow = 400 * time.Millisecond
	h.authorize(t)
	waitPasses(t, h, logs, 1, 1)
	f := fields(t, logs.with(slowPrefix)[0])
	if f["phase"] != PassPhaseConn || f["connection"] != PassConnNew || ms(t, f, "conn_ms") < 600 {
		t.Fatalf("a slow dial: %v", f)
	}
	// The next pass takes the connection idle: fast, so counted, not logged.
	time.Sleep(50 * time.Millisecond)
	h.authorize(t)
	waitPasses(t, h, logs, 2, 1)
	c := h.edge.PassCounts()
	if c.Slow != 1 || c.SlowBy[PassPhaseConn] != 1 || c.Wait["le_1s"] != 1 || c.Connection[PassConnIdle] != 1 {
		t.Fatalf("counts: %+v", c)
	}
}

func TestASlowTLSHandshakeIsTheConnPhaseAndItsTLS(t *testing.T) {
	for _, c := range []struct {
		name string
		// late holds the edge's TLSHandshakeStart mark that long. heldConn's
		// Read says why the server's hold still starts after the mark.
		late time.Duration
	}{{"on time", 0}, {"the handshake started late", 20 * time.Millisecond}} {
		t.Run(c.name, func(t *testing.T) {
			h, logs := newTimedHarness(t, timedOpts{tls: true, firstReadDelay: 150 * time.Millisecond})
			h.edge.passes.slow = 100 * time.Millisecond
			if c.late > 0 {
				transport := h.edge.proxy.Transport.(tracedTransport)
				transport.base = delayedHeaderHandoff{RoundTripper: transport.base, tlsStartDelay: c.late}
				h.edge.proxy.Transport = transport
			}
			h.authorize(t)
			waitPasses(t, h, logs, 1, 1)
			f := fields(t, logs.with(slowPrefix)[0])
			if f["phase"] != PassPhaseConn || ms(t, f, "tls_ms") < 150 || f["tls_errors"] != "0" || f["status"] != "402" {
				t.Fatalf("a slow handshake: %v", f)
			}
		})
	}
}

func TestASlowPythonIsThePythonPhase(t *testing.T) {
	for _, c := range []struct {
		name string
		late bool
	}{{"on time", false}, {"the write marked late", true}} {
		t.Run(c.name, func(t *testing.T) {
			h, logs := newTimedHarness(t, timedOpts{})
			h.edge.passes.slow = 100 * time.Millisecond
			wait, respond := fromTheWritesEnd(t, h, c.late), h.respond
			h.respond = func(w http.ResponseWriter) {
				wait()
				time.Sleep(150 * time.Millisecond)
				respond(w)
			}
			h.authorize(t)
			waitPasses(t, h, logs, 1, 1)
			f := fields(t, logs.with(slowPrefix)[0])
			if f["phase"] != PassPhasePython || ms(t, f, "python_ms") < 150 {
				t.Fatalf("a slow Python: %v", f)
			}
			if counts := h.edge.PassCounts(); counts.SlowBy[PassPhasePython] != 1 {
				t.Fatalf("counts: %+v", counts)
			}
		})
	}
}

func TestA1xxReachesTheCallerAndDoesNotEndThePythonPhase(t *testing.T) {
	for _, c := range []struct {
		name string
		late bool
	}{{"on time", false}, {"the write marked late", true}} {
		t.Run(c.name, func(t *testing.T) {
			h, logs := newTimedHarness(t, timedOpts{})
			h.edge.passes.slow = 0
			wait := fromTheWritesEnd(t, h, c.late)
			h.respond = func(w http.ResponseWriter) {
				wait()
				w.Header().Set("Link", "</x>; rel=preload")
				w.WriteHeader(http.StatusEarlyHints)
				time.Sleep(200 * time.Millisecond)
				w.Header().Del("Link")
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(http.StatusPaymentRequired)
				_, _ = w.Write([]byte(`{"error":{"code":402}}`))
			}
			var hints atomic.Int32
			trace := &httptrace.ClientTrace{Got1xxResponse: func(code int, _ textproto.MIMEHeader) error {
				if code == http.StatusEarlyHints {
					hints.Add(1)
				}
				return nil
			}}
			resp, err := plainClient.Do(h.signedRequest(t, httptrace.WithClientTrace(context.Background(), trace)))
			if err != nil {
				t.Fatal(err)
			}
			_, _ = io.ReadAll(resp.Body)
			_ = resp.Body.Close()
			waitPasses(t, h, logs, 1, 1)
			if hints.Load() != 1 || resp.StatusCode != http.StatusPaymentRequired {
				t.Fatalf("the 103 or the 402 did not reach the caller: hints=%d status=%d", hints.Load(), resp.StatusCode)
			}
			f := fields(t, logs.with(slowPrefix)[0])
			if f["phase"] != PassPhasePython || ms(t, f, "python_ms") < 200 {
				t.Fatalf("Python's time after its 1xx: %v", f)
			}
		})
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
	f := fields(t, logs.with(slowPrefix)[0])
	// No phase is asserted: a refused dial on loopback takes about as long as
	// the edge's own work.
	if f["status"] != "unreached" || f["connection"] != PassConnNone || ms(t, f, "connect_errors") < 1 {
		t.Fatalf("an unreached Python: %v", f)
	}
	if c := h.edge.PassCounts(); c.Connection[PassConnNone] != 1 || len(c.Wait) != 0 {
		t.Fatalf("counts: %+v", c)
	}
}

func TestAnAnswerThatBreaksOffIsCountedAborted(t *testing.T) {
	h, logs := newTimedHarness(t, timedOpts{})
	h.edge.passes.slow = 0
	h.respond = func(w http.ResponseWriter) {
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Content-Length", "100")
		w.WriteHeader(http.StatusPaymentRequired)
		_, _ = w.Write([]byte(`{"error":`))
		w.(http.Flusher).Flush()
		panic(http.ErrAbortHandler) // the connection closes 91 bytes short
	}
	h.authorize(t)
	waitPasses(t, h, logs, 1, 1)
	f := fields(t, logs.with(slowPrefix)[0])
	if f["aborted"] != "true" || f["status"] != "402" {
		t.Fatalf("an answer that broke off: %v", f)
	}
	if c := h.edge.PassCounts(); c.Aborted != 1 {
		t.Fatalf("counts: %+v", c)
	}
}

func TestACallerThatHangsUpMidAnswerIsCountedAborted(t *testing.T) {
	h, logs := newTimedHarness(t, timedOpts{})
	h.edge.passes.slow = 0
	h.respond = func(w http.ResponseWriter) {
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Content-Length", "22")
		w.WriteHeader(http.StatusPaymentRequired)
		_, _ = w.Write([]byte(`{"error":`))
		w.(http.Flusher).Flush()
		time.Sleep(600 * time.Millisecond)
		_, _ = w.Write([]byte(`{"code":402}}`))
	}
	resp := h.signed(t, "boot-1", http.MethodPost, authorizePath, authorizeBody, nil)
	_ = resp.Body.Close() // the caller hangs up with the answer half read
	waitPasses(t, h, logs, 1, 1)
	if c := h.edge.PassCounts(); c.Aborted != 1 {
		t.Fatalf("counts: %+v", c)
	}
	if f := fields(t, logs.with(slowPrefix)[0]); f["aborted"] != "true" {
		t.Fatalf("a caller that hung up: %v", f)
	}
}

func TestAConnectionDialedForAnotherPassIsHanded(t *testing.T) {
	// A's caller gives up while A's dial is held; B arrives, finds no idle
	// connection, and is handed A's fresh one when it lands (the
	// transport's late binding: Reused but not WasIdle).
	h, logs := newTimedHarness(t, timedOpts{dialDelay: 800 * time.Millisecond})
	h.edge.passes.slow = 0
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	if resp, err := plainClient.Do(h.signedRequest(t, ctx)); err == nil {
		_ = resp.Body.Close()
		t.Fatal("A was answered")
	}
	waitPasses(t, h, logs, 1, 1)
	h.authorize(t)
	waitPasses(t, h, logs, 2, 2)
	b := fields(t, logs.with(slowPrefix)[1])
	if b["connection"] != PassConnHanded || ms(t, b, "conn_ms") < 200 {
		t.Fatalf("B, handed A's dial: %v", b)
	}
	c := h.edge.PassCounts()
	// A had no connection; B waited for one, and its wait is counted.
	if c.Connection[PassConnNone] != 1 || c.Connection[PassConnHanded] != 1 || total(c.Wait) != 1 {
		t.Fatalf("counts: %+v", c)
	}
}

func TestTheHooksCountErrorsAndAFailedWriteKeepsItsTime(t *testing.T) {
	t0 := time.Now()
	p := &passTrace{arrived: t0, forwarded: t0}
	hooks := p.hooks()
	failed := errors.New("refused")
	hooks.DNSStart(httptrace.DNSStartInfo{})
	hooks.DNSDone(httptrace.DNSDoneInfo{Err: failed})
	// A dial over two addresses: the first refused, the second connects.
	// The dial spans the first start to the last end.
	hooks.ConnectStart("tcp", "[::1]:1")
	hooks.ConnectDone("tcp", "[::1]:1", failed)
	time.Sleep(10 * time.Millisecond)
	hooks.ConnectStart("tcp", "127.0.0.1:1")
	time.Sleep(10 * time.Millisecond)
	hooks.ConnectDone("tcp", "127.0.0.1:1", nil)
	hooks.TLSHandshakeStart()
	hooks.TLSHandshakeDone(tls.ConnectionState{}, nil)
	hooks.GotConn(httptrace.GotConnInfo{})
	hooks.WroteRequest(httptrace.WroteRequestInfo{Err: failed})
	s := p.phases(time.Now().Add(time.Second))
	if s.connection != PassConnNew || s.dnsErrors != 1 || s.connectErrors != 1 || s.tlsErrors != 0 ||
		s.writeErrors != 1 || s.connect < 20*time.Millisecond {
		t.Fatalf("errors and the dial's span: %+v", s)
	}
	// The write never ended: the rest is the write's, none Python's.
	if s.slowest != PassPhaseWrite || s.python != 0 || s.response != 0 || s.write < time.Second {
		t.Fatalf("a failed write: %+v", s)
	}
}

func TestTheBranchesThatPassBeforeTheBootCheckAreTimed(t *testing.T) {
	h, logs := newTimedHarness(t, timedOpts{})
	h.edge.passes.slow = 0
	// No boot header: passed unread, timed.
	resp := h.send(t, http.MethodPost, authorizePath, authorizeBody, map[string]string{TokenHeader: token})
	_, _ = io.ReadAll(resp.Body)
	waitPasses(t, h, logs, 1, 1)
	// A body past the bound: passed unchecked, timed.
	resp = h.send(t, http.MethodPost, authorizePath, strings.Repeat("a", maxBody+1),
		map[string]string{TokenHeader: token, BootHeader: "kid=boot-1,sig=x"})
	_, _ = io.ReadAll(resp.Body)
	waitPasses(t, h, logs, 2, 2)
}

func TestAHookThatFiresLateRacesNothing(t *testing.T) {
	// A dial the transport keeps for its pool can report after the pass has
	// ended; run with -race, this fails without the trace's lock.
	p := &passTrace{arrived: time.Now(), forwarded: time.Now()}
	hooks := p.hooks()
	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		for range 200 {
			hooks.DNSStart(httptrace.DNSStartInfo{})
			hooks.DNSDone(httptrace.DNSDoneInfo{})
			hooks.ConnectStart("tcp", "127.0.0.1:1")
			hooks.ConnectDone("tcp", "127.0.0.1:1", nil)
			hooks.TLSHandshakeStart()
			hooks.TLSHandshakeDone(tls.ConnectionState{}, nil)
		}
	}()
	for range 200 {
		_ = p.phases(time.Now())
	}
	wg.Wait()
}

func TestASlowLogHoldsNoPass(t *testing.T) {
	release := make(chan struct{})
	logs := &logLines{}
	blocked := func(format string, args ...any) {
		<-release
		logs.logf(format, args...)
	}
	h, _ := newTimedHarness(t, timedOpts{logf: blocked})
	defer close(release)
	h.edge.passes.slow = 0
	h.edge.passes.out = make(chan string, 1)
	// The handlers' returns, which a log written on them would hold.
	returned := make(chan struct{}, 3)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		h.edge.ServeHTTP(w, r)
		returned <- struct{}{}
	}))
	// Closed after the deferred release: Close waits for the handlers,
	// which a log written on them would hold until then.
	t.Cleanup(server.Close)
	h.server = server
	// A connection each, so that a handler held by the log fails the test
	// below within seconds instead of holding the next request behind it.
	client := &http.Client{Timeout: 10 * time.Second,
		Transport: &http.Transport{DisableKeepAlives: true, DisableCompression: true}}
	for range 3 {
		resp, err := client.Do(h.signedRequest(t, context.Background()))
		if err != nil {
			t.Fatal(err)
		}
		_, _ = io.ReadAll(resp.Body)
		_ = resp.Body.Close()
	}
	for range 3 {
		select {
		case <-returned:
		case <-time.After(5 * time.Second):
			t.Fatal("a handler waited for the log")
		}
	}
	// One line is with the blocked writer, at most one in the queue, the
	// rest dropped.
	if c := h.edge.PassCounts(); c.Passes != 3 || c.Dropped < 1 {
		t.Fatalf("counts: %+v", c)
	}
}

func TestFlushPassesWritesTheQueueThenTheLastPeriod(t *testing.T) {
	h, logs := newTimedHarness(t, timedOpts{})
	h.edge.passes.slow = 0
	h.authorize(t)
	h.authorize(t)
	waitFor(t, func() bool { return h.edge.PassCounts().Passes == 2 })
	h.edge.FlushPasses()
	// FlushPasses returns once every line is written, the counts last.
	if got := logs.with(slowPrefix); len(got) != 2 {
		t.Fatalf("slow lines: %v", got)
	}
	counts := logs.with(countsPrefix)
	if len(counts) != 1 || fields(t, counts[0])["passes"] != "2" || !strings.HasPrefix(logs.last(), countsPrefix) {
		t.Fatalf("the last period's counts: %v", logs.all())
	}
	// A pass after it is still counted; its line is dropped.
	h.authorize(t)
	waitFor(t, func() bool { return h.edge.PassCounts().Passes == 3 })
	if c := h.edge.PassCounts(); c.Dropped != 1 {
		t.Fatalf("counts: %+v", c)
	}
	h.edge.FlushPasses() // once is enough; again is nothing
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

func TestAPeriodsCountsAreWrittenByThePassAfterIt(t *testing.T) {
	logs := &logLines{}
	pt := newPassTimer(logs.logf, time.Minute)
	pt.slow = time.Hour // no slow lines
	t0 := time.Now()
	pass := func(at time.Duration) *passTrace {
		i := t0.Add(at)
		return &passTrace{arrived: i, forwarded: i, gotConn: i, wrote: i, headers: i, reused: true, wasIdle: true}
	}
	pt.observe(pass(0), t0.Add(10*time.Millisecond), false)
	pt.observe(pass(30*time.Second), t0.Add(30*time.Second), true)
	pt.mu.Lock()
	queued, writing := len(pt.out), pt.writing
	pt.mu.Unlock()
	if queued != 0 || writing {
		t.Fatal("a line was written inside the period")
	}
	pt.observe(pass(61*time.Second), t0.Add(61*time.Second), false)
	waitFor(t, func() bool { return len(logs.with(countsPrefix)) == 1 })
	line := logs.with(countsPrefix)[0]
	f := fields(t, line)
	if f["passes"] != "2" || f["aborted"] != "1" || f["seconds"] != "60" || f["slow"] != "0" ||
		!strings.Contains(line, `connection={"idle":2}`) {
		t.Fatalf("the period's counts: %s", line)
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
	waitFor(t, func() bool { return len(logs.with(countsPrefix)) == 1 })
	upstream, _ := url.Parse(h.upstream.URL)
	edgeHost, _ := url.Parse(h.server.URL)
	for _, line := range append(logs.with(slowPrefix), logs.with(countsPrefix)...) {
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
		{"answered on an idle connection", &passTrace{arrived: at(0), forwarded: at(1), gotConn: at(3), wrote: at(4),
			headers: at(10), reused: true, wasIdle: true}, at(12), 1, 2, 1, 6, 2, PassPhasePython, PassConnIdle},
		{"handed a connection another pass put back", &passTrace{arrived: at(0), forwarded: at(1), gotConn: at(900),
			wrote: at(901), headers: at(905), reused: true}, at(906), 1, 899, 1, 4, 1, PassPhaseConn, PassConnHanded},
		{"no connection: the rest is conn's", &passTrace{arrived: at(0), forwarded: at(1)},
			at(3001), 1, 3000, 0, 0, 0, PassPhaseConn, PassConnNone},
		{"no final headers: the rest is Python's", &passTrace{arrived: at(0), forwarded: at(1), gotConn: at(2),
			wrote: at(3)}, at(2003), 1, 1, 1, 2000, 0, PassPhasePython, PassConnNew},
		{"a tie goes to the earlier phase", &passTrace{arrived: at(0), forwarded: at(5), gotConn: at(10), wrote: at(10),
			headers: at(12)}, at(14), 5, 5, 0, 2, 2, PassPhaseEdge, PassConnNew},
		{"a mark before the last is not an end", &passTrace{arrived: at(0), forwarded: at(2), gotConn: at(1)},
			at(9), 2, 7, 0, 0, 0, PassPhaseConn, PassConnNew},
		{"a write marked after the headers ended by them", &passTrace{arrived: at(0), forwarded: at(1), gotConn: at(2),
			wrote: at(11), headers: at(10)}, at(210), 1, 1, 8, 0, 200, PassPhaseResponse, PassConnNew},
		{"a write not yet marked ended by the headers", &passTrace{arrived: at(0), forwarded: at(1), gotConn: at(2),
			headers: at(10)}, at(210), 1, 1, 8, 0, 200, PassPhaseResponse, PassConnNew},
		{"a write that failed keeps the rest", &passTrace{arrived: at(0), forwarded: at(1), gotConn: at(2),
			headers: at(10), writeErrors: 1}, at(210), 1, 1, 208, 0, 0, PassPhaseWrite, PassConnNew},
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

func TestAWaitForAConnectionIsBucketedAtTheSYNRetries(t *testing.T) {
	for took, want := range map[time.Duration]string{
		10 * time.Millisecond:           "le_10ms",
		11 * time.Millisecond:           "le_100ms",
		100 * time.Millisecond:          "le_100ms",
		101 * time.Millisecond:          "le_1s",
		time.Second:                     "le_1s",
		1200 * time.Millisecond:         "le_2s",
		2 * time.Second:                 "le_2s",
		2*time.Second + time.Nanosecond: "gt_2s",
		3050 * time.Millisecond:         "gt_2s",
	} {
		if got := waitBucket(took); got != want {
			t.Fatalf("waitBucket(%v) = %q, want %q", took, got, want)
		}
	}
}

// idlePass is a pass answered on an idle connection, all its marks at at.
func idlePass(at time.Time) *passTrace {
	return &passTrace{arrived: at, forwarded: at, gotConn: at, wrote: at, headers: at, reused: true, wasIdle: true}
}

func TestFlushPassesDrainsABacklogThenWritesTheCounts(t *testing.T) {
	release := make(chan struct{})
	logs := &logLines{}
	held := func(format string, args ...any) {
		<-release
		logs.logf(format, args...)
	}
	pt := newPassTimer(held, time.Hour)
	pt.slow = 0
	for range 5 {
		pt.observe(idlePass(time.Now()), time.Now(), false)
	}
	e := &Edge{passes: pt}
	flushed := make(chan struct{})
	go func() {
		e.FlushPasses()
		close(flushed)
	}()
	select {
	case <-flushed:
		t.Fatal("FlushPasses returned with the writer held and lines queued")
	case <-time.After(100 * time.Millisecond):
	}
	close(release)
	select {
	case <-flushed:
	case <-time.After(5 * time.Second):
		t.Fatal("FlushPasses did not return once the writer was released")
	}
	if got := logs.with(slowPrefix); len(got) != 5 || !strings.HasPrefix(logs.last(), countsPrefix) {
		t.Fatalf("after the flush: %v", logs.all())
	}
}

// delayedHeaderHandoff is the edge's base transport, with its hooks running
// before the edge's own.
type delayedHeaderHandoff struct {
	http.RoundTripper
	delay time.Duration
	// wroteAfter, when set, holds the transport's WroteRequest hook until it
	// is closed, as a write goroutine scheduled late does.
	wroteAfter <-chan struct{}
	// tlsStartDelay holds the TLSHandshakeStart hook that long, as a
	// handshake goroutine scheduled late does.
	tlsStartDelay time.Duration
}

func (d delayedHeaderHandoff) RoundTrip(r *http.Request) (*http.Response, error) {
	trace := &httptrace.ClientTrace{}
	if d.wroteAfter != nil {
		trace.WroteRequest = func(httptrace.WroteRequestInfo) {
			select {
			case <-d.wroteAfter:
			case <-time.After(5 * time.Second):
			}
		}
	}
	if d.tlsStartDelay > 0 {
		trace.TLSHandshakeStart = func() { time.Sleep(d.tlsStartDelay) }
	}
	if trace.WroteRequest != nil || trace.TLSHandshakeStart != nil {
		r = r.WithContext(httptrace.WithClientTrace(r.Context(), trace))
	}
	resp, err := d.RoundTripper.RoundTrip(r)
	if err == nil {
		time.Sleep(d.delay)
	}
	return resp, err
}

// afterTheEdge is the edge's tracedTransport with a fresh trace's hooks
// running after the edge's own, so a test can see a mark the edge has made.
type afterTheEdge struct {
	http.RoundTripper
	trace func() *httptrace.ClientTrace
}

func (a afterTheEdge) RoundTrip(r *http.Request) (*http.Response, error) {
	return a.RoundTripper.RoundTrip(r.WithContext(httptrace.WithClientTrace(r.Context(), a.trace())))
}

// fromTheWritesEnd returns a wait for the start of h.respond, so that
// Python's delay starts once the edge has marked its write's end, as
// TestAnAnswerSlowOnItsWayBackIsTheResponsePhase starts its delay once the
// edge has the headers. The transport runs the WroteRequest hook on its write
// goroutine after the flush, which can be after Python has read the request:
// a delay from that read lost a millisecond to the write phase on CI
// (write_ms:1 python_ms:149). With late, the edge's mark itself is held until
// 20 ms after Python has read the request. For a test of one pass.
func fromTheWritesEnd(t *testing.T, h *harness, late bool) (wait func()) {
	t.Helper()
	release, marked := make(chan struct{}), make(chan struct{})
	if late {
		transport := h.edge.proxy.Transport.(tracedTransport)
		transport.base = delayedHeaderHandoff{RoundTripper: transport.base, wroteAfter: release}
		h.edge.proxy.Transport = transport
	}
	var markedOnce, releasedOnce sync.Once
	h.edge.proxy.Transport = afterTheEdge{RoundTripper: h.edge.proxy.Transport, trace: func() *httptrace.ClientTrace {
		return &httptrace.ClientTrace{WroteRequest: func(httptrace.WroteRequestInfo) {
			markedOnce.Do(func() { close(marked) })
		}}
	}}
	return func() {
		time.AfterFunc(20*time.Millisecond, func() { releasedOnce.Do(func() { close(release) }) })
		select {
		case <-marked:
		case <-time.After(5 * time.Second):
			t.Error("the edge did not mark its write's end")
		}
	}
}

func TestAnAnswerSlowOnItsWayBackIsTheResponsePhase(t *testing.T) {
	for _, c := range []struct {
		name      string
		delay     time.Duration
		lateWrote bool
	}{{"0s", 0, false}, {"100ms", 100 * time.Millisecond, false}, {"the write marked after the headers", 0, true}} {
		t.Run(c.name, func(t *testing.T) {
			h, logs := newTimedHarness(t, timedOpts{})
			h.edge.passes.slow = 0
			headersReceived := make(chan struct{})
			handoff := delayedHeaderHandoff{delay: c.delay}
			if c.lateWrote {
				handoff.wroteAfter = headersReceived
			}
			transport := h.edge.proxy.Transport.(tracedTransport)
			handoff.RoundTripper = transport.base
			transport.base = handoff
			h.edge.proxy.Transport = transport
			modifyResponse := h.edge.proxy.ModifyResponse
			h.edge.proxy.ModifyResponse = func(resp *http.Response) error {
				if err := modifyResponse(resp); err != nil {
					return err
				}
				close(headersReceived)
				return nil
			}
			h.respond = func(w http.ResponseWriter) {
				w.Header().Set("Content-Type", "application/json")
				w.Header().Set("Content-Length", "22")
				w.WriteHeader(http.StatusPaymentRequired)
				w.(http.Flusher).Flush()
				// Start the body delay after the proxy marks its final headers,
				// not while the transport may still be handing them back.
				select {
				case <-headersReceived:
				case <-time.After(5 * time.Second):
					t.Error("the proxy did not receive the final headers")
					return
				}
				time.Sleep(200 * time.Millisecond)
				_, _ = w.Write([]byte(`{"error":{"code":402}}`))
			}
			h.authorize(t)
			waitPasses(t, h, logs, 1, 1)
			f := fields(t, logs.with(slowPrefix)[0])
			if f["phase"] != PassPhaseResponse || ms(t, f, "response_ms") < 200 || ms(t, f, "python_ms") >= 200 {
				t.Fatalf("an answer slow on its way back: %v", f)
			}
		})
	}
}
