package edge

import (
	"strings"
	"sync"
)

// inFlightKeys is the decide path's per-key in-flight limit (decision 14):
// how many requests of each lookup hash the edge is deciding at once, from
// the admit to the answer. A request that would go past the edge policy's
// gateway_authorize_max_in_flight_per_key passes to Python
// (inflight.key), which applies its own limit to what it serves; Go never
// answers Python's 429 itself. A key with nothing in flight holds no entry.
type inFlightKeys struct {
	mu sync.Mutex
	n  map[string]int64
}

// enter counts a request of the key in, unless limit are in flight already
// (or the limit is not positive); leave counts it out.
func (k *inFlightKeys) enter(key string, limit int64) bool {
	k.mu.Lock()
	defer k.mu.Unlock()
	if limit < 1 || k.n[key] >= limit {
		return false
	}
	if k.n == nil {
		k.n = map[string]int64{}
	}
	if _, ok := k.n[key]; !ok {
		// Its own string: the key may be a slice of the request's body.
		key = strings.Clone(key)
	}
	k.n[key]++
	return true
}

func (k *inFlightKeys) leave(key string) {
	k.mu.Lock()
	defer k.mu.Unlock()
	if k.n[key] <= 1 {
		delete(k.n, key)
		return
	}
	k.n[key]--
}

// inFlight is how many requests of the key are in flight now.
func (k *inFlightKeys) inFlight(key string) int64 {
	k.mu.Lock()
	defer k.mu.Unlock()
	return k.n[key]
}
