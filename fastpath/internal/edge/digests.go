package edge

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"sync"
	"time"
)

// DigestsPath is Python's route for the accepted image digests: the signed
// Stage D policy it resolves, with its break-glass override.
const DigestsPath = "/internal/gateway/stage-d/accepted-digests"

// UpstreamDigests keeps the accepted image digests read from Python: Run
// reads them every Every, off the request path, and a request reads the
// last set, for up to MaxAge after it was read (§4.10's maximum age). A set
// older than that, or none yet, answers an error; no request waits on
// Python for it. One source of truth for the policy, with the edge's
// exposure to a stale one bounded.
type UpstreamDigests struct {
	Client   *http.Client
	Upstream *url.URL
	Token    string
	Every    time.Duration
	MaxAge   time.Duration
	Clock    func() time.Time

	mu     sync.Mutex
	set    map[string]bool
	readAt time.Time
}

type digestsAnswer struct {
	ImageDigests []string `json:"image_digests"`
}

// Run reads the set now and then every Every until ctx ends. A read that
// fails keeps the last set, which ages out at MaxAge.
func (d *UpstreamDigests) Run(ctx context.Context) {
	d.Refresh(ctx)
	ticker := time.NewTicker(d.Every)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			d.Refresh(ctx)
		}
	}
}

// Refresh reads the set once; the time kept is the read's end, so the age
// a request sees counts from when the set was known.
func (d *UpstreamDigests) Refresh(ctx context.Context) error {
	set, err := d.read(ctx)
	if err != nil {
		return err
	}
	now := d.now()
	d.mu.Lock()
	d.set, d.readAt = set, now
	d.mu.Unlock()
	return nil
}

// Accepted says whether a digest is in the last set read, if that set is
// within MaxAge.
func (d *UpstreamDigests) Accepted(_ context.Context, digest string) (bool, error) {
	now := d.now()
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.set == nil || now.Sub(d.readAt) > d.MaxAge {
		return false, errors.New("edge: the accepted digests are not known")
	}
	return d.set[digest], nil
}

func (d *UpstreamDigests) now() time.Time {
	if d.Clock == nil {
		return time.Now()
	}
	return d.Clock()
}

func (d *UpstreamDigests) read(ctx context.Context) (map[string]bool, error) {
	client := d.Client
	if client == nil {
		client = &http.Client{Timeout: 5 * time.Second}
	}
	target := *d.Upstream
	target.Path, target.RawPath, target.RawQuery = DigestsPath, "", ""
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, target.String(), nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set(TokenHeader, d.Token)
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("edge: the digests answered %d", resp.StatusCode)
	}
	var answer digestsAnswer
	if err := json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(&answer); err != nil {
		return nil, err
	}
	set := make(map[string]bool, len(answer.ImageDigests))
	for _, digest := range answer.ImageDigests {
		set[digest] = true
	}
	return set, nil
}
