package gwauth

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"

	"github.com/Lore-Hex/quill-router/fastpath/internal/pyjson"
	"github.com/Lore-Hex/quill-router/fastpath/internal/routingsnapshot"
)

// What Compose takes from the routing snapshot and the edge policy, mapped
// one way for the decide pipeline and the goldens
// (docs/design/fast-admission-e2-decide.md §6).

// CandidateOf is a route candidate as the snapshot describes its endpoint:
// the endpoint's model, upstream model and provider, the provider's name,
// its inference location (re-ordered by Stage), its Wafer ZDR flag, and its
// usage type and price epochs (the async-v1 billing snapshot's).
func CandidateOf(e routingsnapshot.EndpointFacts) Candidate {
	l := e.InferenceLocation
	return Candidate{
		EndpointID:    e.ID,
		Model:         e.ModelID,
		UpstreamModel: e.UpstreamModel,
		Provider:      e.Provider,
		ProviderName:  e.ProviderName,
		// The snapshot's keys, sorted as its body is; Stage writes them in
		// inference_location_metadata's order.
		InferenceLocation: pyjson.Object{
			{Key: "advertised_region_scope", Value: l.AdvertisedRegionScope},
			{Key: "advertised_regions", Value: stringsOrEmpty(l.AdvertisedRegions)},
			{Key: "catalog_updated_at", Value: orNull(l.CatalogUpdatedAt)},
			{Key: "documentation_url", Value: orNull(l.DocumentationURL)},
			{Key: "provider_declaration_reviewed_on", Value: orNull(l.ProviderDeclarationReviewedOn)},
			{Key: "provider_declared_locations", Value: stringsOrEmpty(l.ProviderDeclaredLocations)},
			{Key: "region_pinning_enforced", Value: l.RegionPinningEnforced},
			{Key: "serving_region", Value: orNull(l.ServingRegion)},
			{Key: "serving_region_status", Value: l.ServingRegionStatus},
		},
		WaferZDRRequired: e.WaferZDRRequired,
		UsageType:        e.UsageType,
		Epochs:           e.Epochs,
	}
}

func orNull(s *string) any {
	if s == nil {
		return nil
	}
	return *s
}

func stringsOrEmpty(s []string) []string {
	if s == nil {
		return []string{}
	}
	return s
}

// RegionsOf is the snapshot's settings.region_payload as the 200's regions.
func RegionsOf(payload []routingsnapshot.RegionEntry) []Region {
	out := make([]Region, len(payload))
	for i, r := range payload {
		out[i] = Region{ID: r.ID, Name: r.Name, Primary: r.Primary, Enabled: r.Enabled, APIBaseURL: r.APIBaseURL}
		if r.ControlPlaneURL != nil {
			u := *r.ControlPlaneURL
			out[i].ControlPlaneURL = &u
		}
	}
	return out
}

// ChooseRegion is regions.choose_region(settings, requested): the
// requested region when it is configured (the schema has stripped it),
// else the primary when configured, else the first configured region.
// configured must not be empty.
func ChooseRegion(configured []string, primary string, requested *string) string {
	contains := func(r string) bool {
		for _, c := range configured {
			if c == r {
				return true
			}
		}
		return false
	}
	switch {
	case requested != nil && *requested != "" && contains(*requested):
		return *requested
	case contains(primary):
		return primary
	}
	return configured[0]
}

// ScopeOf is a reservation's idempotency_scope (storage_gcp_keys.py,
// _gateway_authorization_idempotency_index_id):
// f"{workspace_id}#{key_hash}#{sha256(idempotency_key).hexdigest()}".
func ScopeOf(workspaceID, keyHash, idempotencyKey string) string {
	sum := sha256.Sum256([]byte(idempotencyKey))
	return fmt.Sprintf("%s#%s#%s", workspaceID, keyHash, hex.EncodeToString(sum[:]))
}
