package edge

import (
	"context"
	"net/http"
	"net/http/httptest"
	"net/url"
	"sync/atomic"
	"testing"
	"time"
)

func TestTheAcceptedDigestsAreReadFromPythonOffTheRequestPathAndAgeOut(t *testing.T) {
	var reads atomic.Int64
	var answer atomic.Value
	answer.Store(`{"image_digests":["sha256:a","sha256:b"]}`)
	var status atomic.Int64
	status.Store(http.StatusOK)
	var tokenSeen atomic.Value
	python := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		reads.Add(1)
		tokenSeen.Store(r.Header.Get(TokenHeader))
		if r.URL.Path != DigestsPath {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		w.WriteHeader(int(status.Load()))
		_, _ = w.Write([]byte(answer.Load().(string)))
	}))
	defer python.Close()
	upstream, _ := url.Parse(python.URL)
	now := time.Date(2026, 10, 18, 20, 0, 0, 0, time.UTC)
	d := &UpstreamDigests{Upstream: upstream, Token: token, Every: time.Minute, MaxAge: 10 * time.Minute,
		Clock: func() time.Time { return now }}
	ctx := context.Background()
	if _, err := d.Accepted(ctx, "sha256:a"); err == nil || reads.Load() != 0 {
		t.Fatalf("before any read: err %v reads %d", err, reads.Load())
	}
	if err := d.Refresh(ctx); err != nil || tokenSeen.Load() != token {
		t.Fatalf("the first read: %v token %v", err, tokenSeen.Load())
	}
	a, _ := d.Accepted(ctx, "sha256:a")
	c, _ := d.Accepted(ctx, "sha256:c")
	if !a || c || reads.Load() != 1 {
		t.Fatalf("the set: a %v c %v reads %d", a, c, reads.Load())
	}
	// A read that fails keeps the last set.
	status.Store(http.StatusServiceUnavailable)
	if err := d.Refresh(ctx); err == nil {
		t.Fatal("a failed read answered nothing")
	}
	if a, err := d.Accepted(ctx, "sha256:a"); !a || err != nil {
		t.Fatalf("after a failed read: %v %v", a, err)
	}
	// Until it is older than MaxAge.
	now = now.Add(10*time.Minute + time.Second)
	if _, err := d.Accepted(ctx, "sha256:a"); err == nil {
		t.Fatal("an aged set answered")
	}
	// A read that succeeds makes it current again, with the new set.
	status.Store(http.StatusOK)
	answer.Store(`{"image_digests":["sha256:c"]}`)
	if err := d.Refresh(ctx); err != nil {
		t.Fatal(err)
	}
	a, _ = d.Accepted(ctx, "sha256:a")
	c, _ = d.Accepted(ctx, "sha256:c")
	if a || !c {
		t.Fatalf("the new set: a %v c %v", a, c)
	}
}

func TestRunReadsOnItsPeriodUntilStopped(t *testing.T) {
	var reads atomic.Int64
	python := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		reads.Add(1)
		_, _ = w.Write([]byte(`{"image_digests":[]}`))
	}))
	defer python.Close()
	upstream, _ := url.Parse(python.URL)
	d := &UpstreamDigests{Upstream: upstream, Token: token, Every: 20 * time.Millisecond, MaxAge: time.Minute}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { d.Run(ctx); close(done) }()
	deadline := time.Now().Add(2 * time.Second)
	for reads.Load() < 3 && time.Now().Before(deadline) {
		time.Sleep(5 * time.Millisecond)
	}
	cancel()
	<-done
	if reads.Load() < 3 {
		t.Fatalf("reads: %d", reads.Load())
	}
}
