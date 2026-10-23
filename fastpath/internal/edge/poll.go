package edge

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/url"
	"slices"
	"strconv"
	"time"
)

// fetcher is what the routing snapshot's poller (snapshot.go) and the edge
// policy's (policy.go) share: one GET of a Python publication on the edge's
// own upstream with the internal token, the conditional ETag and a timeout,
// never following a redirect; the loop that wakes for the next poll or the
// copy's not_after; and the waits between polls. What a publication is, how
// it verifies and which copy is kept is each poller's own.
type fetcher struct {
	// name is the document in the log lines: `fastpath edge <name>: ...`.
	name   string
	url    *url.URL
	token  string
	client *http.Client
	clock  func() time.Time
	logf   func(string, ...any)
	// interval is the longest wait between polls, minInterval the
	// shortest and the first backoff, timeout a poll's.
	interval, minInterval, timeout time.Duration
	// failures counts the polls since the last that verified; the poll
	// loop's own.
	failures int
}

func newFetcher(name string, u, upstream *url.URL, token string, transport http.RoundTripper, clock func() time.Time,
	logf func(string, ...any), interval, minInterval, timeout time.Duration) (fetcher, error) {
	if u == nil || u.Scheme != upstream.Scheme || u.Host != upstream.Host || u.User != nil || upstream.User != nil {
		return fetcher{}, errors.New("edge: the " + name + " is polled from the edge's own upstream")
	}
	return fetcher{name: name, url: u, token: token, clock: clock, logf: logf,
		interval: interval, minInterval: minInterval, timeout: timeout,
		client: &http.Client{Transport: transport,
			// A redirect would carry the token elsewhere: the answer is the
			// redirect, which is not a publication.
			CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}}, nil
}

// pollAnswer is one poll's response: its status, headers and at most limit+1
// bytes of its body.
type pollAnswer struct {
	status int
	header http.Header
	body   []byte
}

// errStopped is a poll cut short because the edge is stopping.
var errStopped = errors.New("the edge is stopping")

// get asks Python once, with If-None-Match when etag is set, within the
// poll timeout. A failure names its stage ("request", "transport",
// "read"); errStopped is the edge stopping, not a failure.
func (f *fetcher) get(ctx context.Context, etag string, limit int64) (pollAnswer, string, error) {
	ctx, cancel := context.WithTimeout(ctx, f.timeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, f.url.String(), nil)
	if err != nil {
		return pollAnswer{}, "request", err
	}
	req.Header.Set(TokenHeader, f.token)
	if etag != "" {
		req.Header.Set("If-None-Match", etag)
	}
	resp, err := f.client.Do(req)
	if err != nil {
		if ctx.Err() != nil && errors.Is(err, context.Canceled) {
			return pollAnswer{}, "", errStopped
		}
		return pollAnswer{}, "transport", err
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, limit+1))
	if err != nil {
		return pollAnswer{}, "read", err
	}
	return pollAnswer{status: resp.StatusCode, header: resp.Header, body: raw}, "", nil
}

// loop polls until ctx ends, in its own goroutine: nothing on the proxy
// path waits for it. Before each poll it drops a copy past its not_after;
// after one it sleeps until the next poll, or the held copy's not_after
// when that comes first, so a copy is dropped on time.
func (f *fetcher) loop(ctx context.Context, expire func(), poll func(context.Context) time.Duration,
	notAfter func() (int64, bool)) {
	for {
		expire()
		wait := poll(ctx)
		if at, ok := notAfter(); ok {
			wait = min(wait, max(time.Unix(at, 0).Sub(f.clock()), 0))
		}
		timer := time.NewTimer(wait)
		select {
		case <-ctx.Done():
			timer.Stop()
			return
		case <-timer.C:
		}
	}
}

// until is the wait for a copy's refresh_at, between the shortest and
// longest waits.
func (f *fetcher) until(refreshAt int64) time.Duration {
	wait := time.Unix(refreshAt, 0).Sub(f.clock())
	return min(max(wait, f.minInterval), f.interval)
}

// backoffAfter doubles the shortest wait n-1 times, up to the longest.
func (f *fetcher) backoffAfter(n int) time.Duration {
	wait := f.minInterval
	for i := 1; i < n && wait < f.interval; i++ {
		wait *= 2
	}
	return min(wait, f.interval)
}

func (f *fetcher) backoff() time.Duration { return f.backoffAfter(f.failures) }

// failed counts a poll that reached no answer and logs it; kept is the
// copy still held.
func (f *fetcher) failed(stage string, err error, kept string) time.Duration {
	f.failures++
	f.logf("fastpath edge %s: poll outcome=failed stage=%s error=%q kept=%s", f.name, stage, err.Error(), kept)
	return f.backoff()
}

// publisherStatus is a 503's status, one of the publisher's own words;
// anything else is "other".
func publisherStatus(body []byte, words []string) string {
	var answer struct {
		Status string `json:"status"`
	}
	if json.Unmarshal(body, &answer) == nil && slices.Contains(words, answer.Status) {
		return answer.Status
	}
	return "other"
}

// retryAfter reads Retry-After: seconds, or an HTTP date. A longest wait of
// a day keeps a wrong date from stopping the poll.
func retryAfter(value string, now time.Time) (time.Duration, bool) {
	if value == "" {
		return 0, false
	}
	var wait time.Duration
	if seconds, err := strconv.ParseInt(value, 10, 64); err == nil && seconds >= 0 {
		wait = time.Duration(min(seconds, int64(24*time.Hour/time.Second))) * time.Second
	} else if at, err := http.ParseTime(value); err == nil {
		wait = max(at.Sub(now), 0)
	} else {
		return 0, false
	}
	return min(wait, 24*time.Hour), true
}
