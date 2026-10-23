package edge

import (
	"context"
	"fmt"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

// The shared fetcher (poll.go) carries the live routing snapshot's poll
// too, so these hold its behaviour on the snapshot poller and, where the
// policy poller shares it, on that one as well.

// answerCutShort writes a response head claiming more body than it sends,
// then the first part of the body, then closes the connection: the body
// fails mid-read.
func answerCutShort(t *testing.T, w http.ResponseWriter, status int, retryAfter, body string) {
	t.Helper()
	hijacker, ok := w.(http.Hijacker)
	if !ok {
		t.Error("the test server cannot hijack")
		return
	}
	conn, buf, err := hijacker.Hijack()
	if err != nil {
		t.Error(err)
		return
	}
	defer conn.Close()
	_, _ = fmt.Fprintf(buf, "HTTP/1.1 %d %s\r\nContent-Type: application/json\r\nRetry-After: %s\r\n"+
		"Content-Length: %d\r\n\r\n%s", status, http.StatusText(status), retryAfter, len(body)+4000, body)
	_ = buf.Flush()
}

// TestANon200BodyThatFailsMidReadIsStillAnAnswer: a 503 whose body breaks
// off is still the publisher's answer: its status word (from what was read)
// and its Retry-After are honoured, as an unavailable poll, not a failed
// one. A 200 whose body breaks off is a failed read.
func TestANon200BodyThatFailsMidReadIsStillAnAnswer(t *testing.T) {
	h := newSnapshotEdge(t, false, nil)
	h.python.onSnapshot(func(w http.ResponseWriter, _ *http.Request) {
		answerCutShort(t, w, http.StatusServiceUnavailable, "7", `{"status":"compiling"}`)
	})
	if wait := h.poll(t); wait != 7*time.Second {
		t.Fatalf("a cut-short 503 waited %v, want its Retry-After", wait)
	}
	if !strings.Contains(h.logs.text(), "poll status=503 outcome=unavailable publisher_status=compiling retry_after=7s kept=none") {
		t.Fatalf("the cut-short 503 is not an unavailable poll:\n%s", h.logs.text())
	}
	h.python.onSnapshot(func(w http.ResponseWriter, _ *http.Request) {
		answerCutShort(t, w, http.StatusOK, "1", `{"v":1,`)
	})
	h.poll(t)
	if !strings.Contains(h.logs.text(), "fastpath edge snapshot: poll outcome=failed stage=read") {
		t.Fatalf("a cut-short 200 is not a failed read:\n%s", h.logs.text())
	}

	p := newPolicyEdge(t, nil)
	f := func(w http.ResponseWriter, _ *http.Request) {
		answerCutShort(t, w, http.StatusServiceUnavailable, "5", `{"status":"digest-policy-unavailable"}`)
	}
	p.handler.Store(&f)
	if wait := p.poll(); wait != 5*time.Second || p.edge.PolicyCounts().Unavailable != 1 ||
		!strings.Contains(p.logs.text(), "publisher_status=digest-policy-unavailable retry_after=5s") {
		t.Fatalf("the policy's cut-short 503 waited %v:\n%s", wait, p.logs.text())
	}
}

// TestA503IsReadToItsFirst4KiB: the status word comes from the first 4 KiB
// of a non-200 answer, as before the fetcher was shared; a status past
// them is "other".
func TestA503IsReadToItsFirst4KiB(t *testing.T) {
	h := newSnapshotEdge(t, false, nil)
	answer := func(pad int) {
		h.python.onSnapshot(func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusServiceUnavailable)
			_, _ = fmt.Fprintf(w, `{"pad":%q,"status":"compiling"}`, strings.Repeat("x", pad))
		})
	}
	answer(4000)
	h.poll(t)
	answer(5000)
	h.poll(t)
	logs := h.logs.text()
	if !strings.Contains(logs, "publisher_status=compiling") || !strings.Contains(logs, "publisher_status=other") {
		t.Fatalf("the status word is not read from the first 4 KiB alone:\n%s", logs)
	}
}

// TestTheSnapshotPollBacksOffAndCountsFailures: each failed poll counts
// (a 503, a refusal, an unreachable Python), the waits double from the
// shortest to the longest, and a poll that verifies resets both.
func TestTheSnapshotPollBacksOffAndCountsFailures(t *testing.T) {
	h := newSnapshotEdge(t, false, nil)
	h.python.onSnapshot(func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusServiceUnavailable) })
	var waits []time.Duration
	for i := 1; i <= 6; i++ {
		waits = append(waits, h.poll(t))
		if h.edge.poller.failures != i {
			t.Fatalf("after %d failed polls, failures is %d", i, h.edge.poller.failures)
		}
	}
	want := []time.Duration{5 * time.Second, 10 * time.Second, 20 * time.Second, 40 * time.Second, time.Minute, time.Minute}
	if fmt.Sprint(waits) != fmt.Sprint(want) {
		t.Fatalf("waits %v, want %v", waits, want)
	}
	h.python.onSnapshot(func(w http.ResponseWriter, _ *http.Request) { _, _ = w.Write([]byte(`{}`)) })
	h.poll(t)
	if h.edge.poller.failures != 7 {
		t.Fatalf("a refusal is not counted: %d", h.edge.poller.failures)
	}
	h.serveGolden(t, h.clock.now().Unix()+60)
	h.poll(t)
	if h.edge.poller.failures != 0 {
		t.Fatalf("a verified poll did not reset the count: %d", h.edge.poller.failures)
	}
	h.python.server.Close()
	if wait := h.poll(t); wait != 5*time.Second || h.edge.poller.failures != 1 ||
		!strings.Contains(h.logs.text(), "outcome=failed stage=transport") {
		t.Fatalf("an unreachable Python waited %v, failures %d:\n%s", wait, h.edge.poller.failures, h.logs.text())
	}
}

// TestACancelledPollIsNotAFailure: a poll cut short because the edge is
// stopping is neither counted nor logged as a failure, for either poller.
func TestACancelledPollIsNotAFailure(t *testing.T) {
	h := newSnapshotEdge(t, false, nil)
	release := make(chan struct{})
	t.Cleanup(func() { close(release) })
	hang := func(_ http.ResponseWriter, r *http.Request) {
		select {
		case <-r.Context().Done():
		case <-release:
		}
	}
	h.python.onSnapshot(hang)
	ctx, cancel := context.WithCancel(context.Background())
	time.AfterFunc(50*time.Millisecond, cancel)
	if wait := h.edge.poller.poll(ctx); wait != time.Minute || h.edge.poller.failures != 0 ||
		strings.Contains(h.logs.text(), "outcome=failed") {
		t.Fatalf("a cancelled snapshot poll waited %v, failures %d:\n%s", wait, h.edge.poller.failures, h.logs.text())
	}

	p := newPolicyEdge(t, nil)
	p.handler.Store(&hang)
	ctx, cancel = context.WithCancel(context.Background())
	time.AfterFunc(50*time.Millisecond, cancel)
	if wait := p.edge.policy.poll(ctx); wait != 15*time.Second || p.edge.policy.failures != 0 ||
		p.edge.PolicyCounts() != (PolicyCounts{}) {
		t.Fatalf("a cancelled policy poll waited %v, counts %+v", wait, p.edge.PolicyCounts())
	}
	// The control: the same hang, the poll's own timeout, is a failure.
	p2 := newPolicyEdge(t, func(c *Config) { c.Policy.Timeout = 50 * time.Millisecond })
	p2.handler.Store(&hang)
	p2.poll()
	if p2.edge.policy.failures != 1 || p2.edge.PolicyCounts().Failed != 1 {
		t.Fatalf("a timed-out poll is not a failure: %+v", p2.edge.PolicyCounts())
	}
}

// TestTheSnapshotPollTimesOut: the snapshot's configured timeout ends a
// poll Python does not answer.
func TestTheSnapshotPollTimesOut(t *testing.T) {
	h := newSnapshotEdge(t, false, func(c *Config) { c.Snapshot.Timeout = 100 * time.Millisecond })
	release := make(chan struct{})
	t.Cleanup(func() { close(release) })
	h.python.onSnapshot(func(_ http.ResponseWriter, r *http.Request) {
		select {
		case <-r.Context().Done():
		case <-release:
		case <-time.After(4 * time.Second): // an ignored timeout fails, not hangs
		}
	})
	started := time.Now()
	h.poll(t)
	if elapsed := time.Since(started); elapsed > 2*time.Second || h.edge.poller.failures != 1 ||
		!strings.Contains(h.logs.text(), "outcome=failed stage=transport") {
		t.Fatalf("the poll took %v, failures %d:\n%s", elapsed, h.edge.poller.failures, h.logs.text())
	}
}

// TestTheLoopWakesAtNotAfter: with the next poll far off, the loop still
// wakes at the held copy's not_after to drop it.
func TestTheLoopWakesAtNotAfter(t *testing.T) {
	f := &fetcher{clock: time.Now}
	var expires atomic.Int64
	notAfter := time.Now().Unix() + 1
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		f.loop(ctx, func() { expires.Add(1) }, func(context.Context) time.Duration { return time.Hour },
			func() (int64, bool) { return notAfter, true })
		close(done)
	}()
	deadline := time.Now().Add(5 * time.Second)
	for expires.Load() < 2 {
		if time.Now().After(deadline) {
			t.Fatal("the loop did not wake at not_after")
		}
		time.Sleep(5 * time.Millisecond)
	}
	cancel()
	<-done
}

// TestRetryAfterIsCapped: seconds or a date, at most a day, never negative;
// anything else is no Retry-After.
func TestRetryAfterIsCapped(t *testing.T) {
	now := time.Unix(1792368000, 0)
	for _, c := range []struct {
		value string
		want  time.Duration
		ok    bool
	}{
		{"7", 7 * time.Second, true},
		{"0", 0, true},
		{"99999999999", 24 * time.Hour, true},
		{now.Add(30 * time.Second).UTC().Format(http.TimeFormat), 30 * time.Second, true},
		{now.Add(400 * 24 * time.Hour).UTC().Format(http.TimeFormat), 24 * time.Hour, true},
		{now.Add(-time.Hour).UTC().Format(http.TimeFormat), 0, true},
		{"-5", 0, false},
		{"soon", 0, false},
		{"", 0, false},
	} {
		if got, ok := retryAfter(c.value, now); got != c.want || ok != c.ok {
			t.Errorf("Retry-After %q: %v %v, want %v %v", c.value, got, ok, c.want, c.ok)
		}
	}
}
