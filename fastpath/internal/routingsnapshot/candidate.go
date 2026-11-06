package routingsnapshot

import "slices"

// What the authorize 200 says of each route candidate, read from the
// snapshot (docs/design/fast-admission-e2-decide.md §6): the decide
// pipeline composes the response from these, never from the request. The
// mapping lives here, beside the snapshot's own types, so the pipeline and
// the compose goldens read an endpoint one way.

// EndpointFacts are an endpoint's fields the 200 writes: its id, model,
// upstream model, provider and the provider's name, its usage type,
// its inference location (the snapshot's, which the response re-orders) and
// whether a Wafer endpoint requires ZDR; and its price epochs, from which
// the async-v1 additions' billing snapshot prices it at the compose's
// instant (docs/design/fast-admission-e2-g15-async.md section 3.2). Epochs
// is the snapshot's own slice, never written.
type EndpointFacts struct {
	ID, ModelID, UpstreamModel, Provider, ProviderName, UsageType string
	InferenceLocation                                             InferenceLocation
	WaferZDRRequired                                              bool
	Epochs                                                        []Epoch
}

// Endpoint is an endpoint's facts by id; ok is false for an id the snapshot
// does not hold. ProviderName is the provider's name, or its slug when the
// provider has no entry (gateway.py, _gateway_provider_route_payload's
// fallback).
func (s *Snapshot) Endpoint(id string) (EndpointFacts, bool) {
	i, ok := s.endpointIndex[id]
	if !ok {
		return EndpointFacts{}, false
	}
	e := &s.Endpoints[i]
	name := e.Provider
	if p := s.providers[e.Provider]; p != nil {
		name = p.Name
	}
	return EndpointFacts{
		ID:                e.ID,
		ModelID:           e.ModelID,
		UpstreamModel:     e.UpstreamModel,
		Provider:          e.Provider,
		ProviderName:      name,
		UsageType:         e.UsageType,
		InferenceLocation: e.Response.InferenceLocation,
		WaferZDRRequired:  e.Response.WaferZDRRequired,
		Epochs:            e.Epochs,
	}, true
}

// CreditsOnly reports every candidate priced in credits (decision 3: a
// BYOK candidate passes).
func (s *Snapshot) CreditsOnly(candidates []string) bool {
	for _, id := range candidates {
		i, ok := s.endpointIndex[id]
		if !ok || s.Endpoints[i].UsageType != usageCredits {
			return false
		}
	}
	return true
}

// BYOKProviders are the provider slugs whose BYOK credentials a workspace
// holds, from the slugs of its BYOK entity ids ("ws#slug"): a provider is
// present when one of its byok_storage_slugs is, as Evaluate's
// Request.BYOKProviders takes them. Sorted, unique.
func (s *Snapshot) BYOKProviders(storageSlugs []string) []string {
	var out []string
	for _, p := range s.Providers {
		for _, slug := range p.BYOKStorageSlugs {
			if slices.Contains(storageSlugs, slug) {
				out = append(out, p.Slug)
				break
			}
		}
	}
	return out
}

// PrivateProxy reports a requested model the snapshot marks as a private
// proxy, for which Python's response_model is not null (§3,
// candidates.private_proxy). ids are stripped and identity under
// canonical_router_model_id.
func (s *Snapshot) PrivateProxy(ids ...string) bool {
	for _, r := range s.RequestedModels {
		if r.PrivateProxy && slices.Contains(ids, r.ID) {
			return true
		}
	}
	return false
}
