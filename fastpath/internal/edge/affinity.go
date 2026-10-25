package edge

import (
	"encoding/json"
	"slices"

	"github.com/Lore-Hex/quill-router/fastpath/internal/gwauth"
	"github.com/Lore-Hex/quill-router/fastpath/internal/routingsnapshot"
)

// Cache affinity (docs/design/fast-admission-e2-decide.md §1 item 5, G13).
// Python's authorize re-ranks the candidates of a request naming a
// cache_affinity_key through routing_state.rank: a session (key hash,
// model, region, affinity key) whose endpoint its own process remembered,
// at a settle with a discounted cache hit within the last 600 s, moves to
// the front of its model's run (the sort partitioned by model) or of the
// list, and with fallbacks off is then the one candidate kept. The table is
// per Python process, seeded only by settle, never stored: Go cannot read
// it, so Go composes the cold order and the full shadow measures Python's
// against it, cold (the same bytes) or warm (Python's bytes are Go's
// decision rendered with the remembered endpoint promoted). Neither admits.

// promotedWithinItsModel: py is cold with one endpoint moved to the front of
// its model's run, or to the front of the list, which is the order
// routing_state.rank gives a session's sticky endpoint (the warm cache)
// with the sort partitioned by model or not at all. modelOf is an
// endpoint's model, "" when not known.
func promotedWithinItsModel(cold, py []string, modelOf func(string) string) bool {
	if len(cold) != len(py) {
		return false
	}
	i := 0
	for i < len(cold) && cold[i] == py[i] {
		i++
	}
	if i == len(cold) {
		return false
	}
	j := slices.Index(cold, py[i])
	if j <= i {
		return false
	}
	moved := slices.Concat(cold[:i], []string{cold[j]}, cold[i:j], cold[j+1:])
	if !slices.Equal(moved, py) {
		return false
	}
	model := modelOf(cold[j])
	return i == 0 || (modelOf(cold[i-1]) != model && modelOf(cold[i]) == model)
}

// warmOf is Python's warm order over Go's decision: the candidates and
// the estimate Go would have composed had it held Python's session. ok is
// false unless py is a warm re-rank of the decision's cold order: its
// candidates with one promoted within its model (the estimate, the
// largest over the same candidates, unchanged), or, with fallbacks off,
// one endpoint of the same model that Go admits with fallbacks on, at the
// estimate Go names when pinned to that endpoint's provider.
func (d *Decision) warmOf(py []string) (candidates []gwauth.Candidate, estimate int64, ok bool) {
	if !d.Affinity || d.Snapshot == nil || len(py) == 0 {
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
	if promotedWithinItsModel(cold, py, modelOf) {
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
	on, ok := withFallbacks(d.Routing)
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
