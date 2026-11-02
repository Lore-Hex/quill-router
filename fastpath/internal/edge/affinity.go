package edge

import (
	"cmp"
	"encoding/json"
	"slices"

	"github.com/Lore-Hex/quill-router/fastpath/internal/gwauth"
	"github.com/Lore-Hex/quill-router/fastpath/internal/routingsnapshot"
)

// Cache affinity (docs/design/fast-admission-e2-decide.md §1 item 5, G13).
// Python's authorize re-ranks the candidates of a request naming a
// cache_affinity_key through routing_state.rank: unless provider.order
// names a provider, the candidates are sorted, stably, by their model's
// first-seen rank (unless provider.sort.partition is none) and then with
// the session's remembered endpoint first, where the session (key hash,
// model, region, affinity key) is one the process itself remembered at a
// settle with a discounted cache hit within the last 600 s; with fallbacks
// off the first candidate of that order is then the one kept. The table is
// per Python process, seeded only by settle, never stored: Go cannot read
// it, and Go decides such a request in the cold order, which is Python's
// own answer with no session. The full shadow compares Python's 200 with
// Go's like any other; when it is Go's decision rendered in rank's order
// for some remembered endpoint among the candidates (warmOf), the differ
// is named differ.affinity_warm, so a warm re-rank is told from a defect.

// rerankOrder is routing_state.rank's order over cold with sticky
// remembered: a stable sort by the model's first-seen rank (unless byModel
// is false), then the remembered endpoint before the others.
func rerankOrder(cold []string, sticky string, byModel bool, modelOf func(string) string) []string {
	rank := map[string]int{}
	for _, id := range cold {
		if m := modelOf(id); !byModel {
			rank[m] = 0
		} else if _, seen := rank[m]; !seen {
			rank[m] = len(rank)
		}
	}
	notSticky := func(id string) int {
		if id == sticky {
			return 0
		}
		return 1
	}
	out := slices.Clone(cold)
	slices.SortStableFunc(out, func(a, b string) int {
		if c := cmp.Compare(rank[modelOf(a)], rank[modelOf(b)]); c != 0 {
			return c
		}
		return cmp.Compare(notSticky(a), notSticky(b))
	})
	return out
}

// pythonsWarm reports py as the order routing_state.rank gives cold for
// some endpoint of cold remembered, other than cold itself, when ordered
// is false (a provider.order keeps the cold order); sticky is that
// endpoint.
func pythonsWarm(ordered, byModel bool, cold, py []string, modelOf func(string) string) (sticky string, ok bool) {
	if ordered || len(cold) != len(py) || slices.Equal(cold, py) {
		return "", false
	}
	for _, id := range cold {
		if slices.Equal(rerankOrder(cold, id, byModel, modelOf), py) {
			return id, true
		}
	}
	return "", false
}

// warmOf is Go's decision in Python's warm order: the candidates and the
// estimate Go would have composed had it held Python's session, for the
// shadow's comparison alone (never admitted). ok is false unless py is
// that order for some remembered endpoint (pythonsWarm: the estimate, the
// largest over the same candidates, unchanged), or, with fallbacks off,
// one endpoint of the primary model that Go admits with fallbacks on
// (Python re-ranks the model's endpoints and keeps the first), at the
// estimate Go names when pinned to that endpoint's provider.
func (d *Decision) warmOf(py []string) (*Decision, bool) {
	candidates, estimate, ok := d.warmCandidates(py)
	if !ok {
		return nil, false
	}
	warm := *d
	warm.Input.Candidates, warm.Input.Estimate, warm.Estimate = candidates, estimate, estimate
	warm.Candidates = slices.Clone(py)
	return &warm, true
}

// warmCandidates are warmOf's candidates and estimate.
func (d *Decision) warmCandidates(py []string) (candidates []gwauth.Candidate, estimate int64, ok bool) {
	if !d.Affinity || d.Snapshot == nil || len(py) == 0 {
		return nil, 0, false
	}
	ordered, byModel, ok := d.Snapshot.Rerank(d.Routing)
	if !ok || ordered {
		return nil, 0, false
	}
	modelOf := func(id string) string {
		e, found := d.Snapshot.Endpoint(id)
		if !found {
			return ""
		}
		return e.ModelID
	}
	cold := d.Candidates
	if _, warm := pythonsWarm(ordered, byModel, cold, py, modelOf); warm {
		byID := make(map[string]gwauth.Candidate, len(d.Input.Candidates))
		for _, c := range d.Input.Candidates {
			byID[c.EndpointID] = c
		}
		candidates = make([]gwauth.Candidate, len(py))
		for i, id := range py {
			c, found := byID[id]
			if !found {
				return nil, 0, false
			}
			candidates[i] = c
		}
		return candidates, d.Input.Estimate, true
	}
	if len(cold) != 1 || len(py) != 1 || py[0] == cold[0] || modelOf(py[0]) == "" || modelOf(py[0]) != modelOf(cold[0]) {
		return nil, 0, false
	}
	// The cohort is credits only (candidates.byok); a BYOK endpoint Python
	// kept is not one Go would have composed.
	if !d.Snapshot.CreditsOnly(py) {
		return nil, 0, false
	}
	on, ok := withFallbacks(d.Snapshot, d.Routing)
	if !ok {
		return nil, 0, false
	}
	if r := d.Snapshot.Evaluate(on); r.Outcome != routingsnapshot.Answered || !slices.Contains(r.Candidates, py[0]) {
		return nil, 0, false
	}
	e, _ := d.Snapshot.Endpoint(py[0])
	pinned, ok := withProviderOnly(d.Routing, e.Provider)
	if !ok {
		return nil, 0, false
	}
	r := d.Snapshot.Evaluate(pinned)
	if r.Outcome != routingsnapshot.Answered || !slices.Equal(r.Candidates, py) {
		return nil, 0, false
	}
	return []gwauth.Candidate{gwauth.CandidateOf(e)}, r.ModelEstimate, true
}

// withFallbacks is req with allow_fallbacks unset, top-level and in
// provider; ok is false when provider.order names a provider (Python then
// does not re-rank), or provider is not an object.
func withFallbacks(s *routingsnapshot.Snapshot, req routingsnapshot.Request) (routingsnapshot.Request, bool) {
	if ordered, _, ok := s.Rerank(req); !ok || ordered {
		return req, false
	}
	req.AllowFallbacks = nil
	if isJSONNull(req.Provider) {
		return req, true
	}
	var fields map[string]json.RawMessage
	if json.Unmarshal(req.Provider, &fields) != nil || fields == nil {
		return req, false
	}
	delete(fields, "allow_fallbacks")
	provider, err := json.Marshal(fields)
	if err != nil {
		return req, false
	}
	req.Provider = provider
	return req, true
}

// withProviderOnly is req with provider.only set to the one provider, its
// other provider fields kept; ok is false when provider is not an object.
func withProviderOnly(req routingsnapshot.Request, provider string) (routingsnapshot.Request, bool) {
	fields := map[string]json.RawMessage{}
	if !isJSONNull(req.Provider) && (json.Unmarshal(req.Provider, &fields) != nil || fields == nil) {
		return req, false
	}
	only, err := json.Marshal([]string{provider})
	if err != nil {
		return req, false
	}
	fields["only"] = only
	raw, err := json.Marshal(fields)
	if err != nil {
		return req, false
	}
	req.Provider = raw
	return req, true
}
