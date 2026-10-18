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

// UpstreamDigests reads the accepted image digests from Python every Every
// and keeps the last set read for up to MaxAge: one source of truth for the
// policy, with the edge's exposure to a stale one bounded (§4.10's maximum
// age). A set older than MaxAge, or none, answers an error.
type UpstreamDigests struct {
	Client   *http.Client
	Upstream *url.URL
	Token    string
	Every    time.Duration
	MaxAge   time.Duration
	Clock    func() time.Time

	mu      sync.Mutex
	set     map[string]bool
	readAt  time.Time
	triedAt time.Time
}

type digestsAnswer struct {
	ImageDigests []string `json:"image_digests"`
}

// Accepted says whether a digest is in the set, reading it when due.
func (d *UpstreamDigests) Accepted(ctx context.Context, digest string) (bool, error) {
	clock := d.Clock
	if clock == nil {
		clock = time.Now
	}
	now := clock()
	d.mu.Lock()
	due := d.set == nil || now.Sub(d.triedAt) >= d.Every
	if due {
		d.triedAt = now
	}
	d.mu.Unlock()
	if due {
		set, err := d.read(ctx)
		d.mu.Lock()
		if err == nil {
			d.set, d.readAt = set, now
		}
		d.mu.Unlock()
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.set == nil || now.Sub(d.readAt) > d.MaxAge {
		return false, errors.New("edge: the accepted digests are not known")
	}
	return d.set[digest], nil
}

func (d *UpstreamDigests) read(ctx context.Context) (map[string]bool, error) {
	client := d.Client
	if client == nil {
		client = http.DefaultClient
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
