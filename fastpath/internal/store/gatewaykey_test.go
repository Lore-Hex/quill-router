package store

import (
	"context"
	"errors"
	"regexp"
	"slices"
	"strings"
	"testing"
	"time"

	"cloud.google.com/go/spanner"

	"github.com/Lore-Hex/quill-router/fastpath/internal/store/storetest"
)

func entity(kind, id, body string) *spanner.Mutation {
	return spanner.Insert("tr_entities", []string{"kind", "id", "body", "updated_at"},
		[]any{kind, id, body, spanner.CommitTimestamp})
}

// TestTheKeyContextIsOneReadOfTheKeyItsWorkspaceAndTheirIds: the key the
// lookup pointer names, the workspace the key names, the workspace's BYOK
// ids whole and in order (never a body), and whether a broadcast pointer is
// in the workspace's range; a neighbour whose id only shares a prefix is
// outside it.
func TestTheKeyContextIsOneReadOfTheKeyItsWorkspaceAndTheirIds(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	ws, other := storetest.UniqueID("ws"), storetest.UniqueID("ws")
	lookup, keyHash := storetest.UniqueID("lh"), storetest.UniqueID("kh")
	keyBody := `{"hash":"` + keyHash + `","lookup_hash":"` + lookup + `","workspace_id":"` + ws + `","salt":"s","secret_hash":"x",` +
		`"name":"n","label":"l","creator_user_id":null}`
	workspaceBody := `{"id":"` + ws + `","name":"w","owner_user_id":"u"}`
	const canary = "byok-secret-canary"
	mutations := []*spanner.Mutation{
		entity("api_key_lookup", lookup, `{"key_id":"`+keyHash+`"}`),
		entity("api_key", keyHash, keyBody),
		entity("workspace", ws, workspaceBody),
		entity("byok", ws+"#openai", `{"secret_ref":"`+canary+`"}`),
		entity("byok", ws+"#anthropic", `{"secret_ref":"`+canary+`"}`),
		// An imported workspace "ws#x" shares the range; its id is returned
		// whole, for the caller to match complete ids.
		entity("byok", ws+"#x#mistral", `{"secret_ref":"`+canary+`"}`),
		// Outside the range: another workspace, a longer id with the same
		// prefix, and another kind with an id in the range.
		entity("byok", other+"#openai", `{}`),
		entity("byok", ws+"z#openai", `{}`),
		entity("byok_other", ws+"#google", `{}`),
		entity("broadcast_destination_by_workspace", ws+"#bdst_1", `{"destination_id":"bdst_1"}`),
		entity("broadcast_destination_by_workspace", other+"z#bdst_2", `{"destination_id":"bdst_2"}`),
	}
	if _, err := shared.Apply(ctx, mutations); err != nil {
		t.Fatal(err)
	}
	got, found, err := s.ReadKeyContext(ctx, lookup)
	if err != nil || !found {
		t.Fatalf("the key context: %v %v", found, err)
	}
	if got.KeyBody != keyBody || got.WorkspaceBody != workspaceBody || !got.WorkspaceFound {
		t.Fatalf("the bodies: %+v", got)
	}
	if want := []string{ws + "#anthropic", ws + "#openai", ws + "#x#mistral"}; !slices.Equal(got.BYOKIDs, want) {
		t.Fatalf("the BYOK ids: %q, want %q", got.BYOKIDs, want)
	}
	if !got.BroadcastDestination {
		t.Fatal("the workspace's broadcast pointer is not seen")
	}
	if strings.Contains(got.KeyBody+got.WorkspaceBody+strings.Join(got.BYOKIDs, ","), canary) {
		t.Fatal("a BYOK body was read")
	}

	// The other workspace: no BYOK of its own in range ("other#openai" is
	// its), and the pointer "otherz#..." is a neighbour's, not its.
	otherLookup, otherKey := storetest.UniqueID("lh"), storetest.UniqueID("kh")
	if _, err := shared.Apply(ctx, []*spanner.Mutation{
		entity("api_key_lookup", otherLookup, `{"key_id":"`+otherKey+`"}`),
		entity("api_key", otherKey, `{"hash":"`+otherKey+`","workspace_id":"`+other+`"}`),
	}); err != nil {
		t.Fatal(err)
	}
	got, found, err = s.ReadKeyContext(ctx, otherLookup)
	if err != nil || !found {
		t.Fatalf("the other key: %v %v", found, err)
	}
	if got.WorkspaceFound || got.WorkspaceBody != "" || got.BroadcastDestination ||
		!slices.Equal(got.BYOKIDs, []string{other + "#openai"}) {
		t.Fatalf("a key whose workspace has no row and a neighbour's pointer: %+v", got)
	}
}

// TestAKeyContextIsNotFoundWithoutAPointerAndAKey: no pointer, a pointer to
// no key, a pointer of another kind, and a key whose workspace has nothing in
// range.
func TestAKeyContextIsNotFoundWithoutAPointerAndAKey(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	dangling, wrongKind, bare, bareKey := storetest.UniqueID("lh"), storetest.UniqueID("lh"), storetest.UniqueID("lh"),
		storetest.UniqueID("kh")
	bareWS := storetest.UniqueID("ws")
	if _, err := shared.Apply(ctx, []*spanner.Mutation{
		entity("api_key_lookup", dangling, `{"key_id":"`+storetest.UniqueID("kh")+`"}`),
		entity("api_key_lookup_other", wrongKind, `{"key_id":"`+bareKey+`"}`),
		entity("api_key_lookup", bare, `{"key_id":"`+bareKey+`"}`),
		entity("api_key", bareKey, `{"hash":"`+bareKey+`","workspace_id":"`+bareWS+`"}`),
		entity("workspace", bareWS, `{"id":"`+bareWS+`"}`),
	}); err != nil {
		t.Fatal(err)
	}
	for _, lookup := range []string{storetest.UniqueID("lh"), dangling, wrongKind} {
		if got, found, err := s.ReadKeyContext(ctx, lookup); err != nil || found {
			t.Fatalf("%s: %+v %v %v", lookup, got, found, err)
		}
	}
	got, found, err := s.ReadKeyContext(ctx, bare)
	if err != nil || !found || !got.WorkspaceFound || len(got.BYOKIDs) != 0 || got.BroadcastDestination {
		t.Fatalf("a key with nothing in its workspace's range: %+v %v %v", got, found, err)
	}
	if _, _, err := s.ReadKeyContext(ctx, ""); err == nil {
		t.Fatal("an empty lookup hash")
	}
}

// TestTheKeyContextQueryReadsByCompletePrimaryKeys holds the query to
// AGENTS.md's rule for tr_entities: no search of a body, and every alias of
// the table read by its kind and by its id (an equality, or a range of ids).
func TestTheKeyContextQueryReadsByCompletePrimaryKeys(t *testing.T) {
	upper := strings.ToUpper(keyContextSQL)
	for _, banned := range []string{" LIKE ", "REGEXP", "STARTS_WITH", "JSON_QUERY", "SEARCH(", "STRPOS"} {
		if strings.Contains(upper, banned) {
			t.Errorf("the query uses %s", banned)
		}
	}
	aliases := regexp.MustCompile(`tr_entities AS (\w+)`).FindAllStringSubmatch(keyContextSQL, -1)
	if len(aliases) != 5 {
		t.Fatalf("the query reads tr_entities as %d aliases, want 5", len(aliases))
	}
	for _, a := range aliases {
		alias := a[1]
		if !strings.Contains(keyContextSQL, alias+".kind = '") {
			t.Errorf("%s is not read by its kind", alias)
		}
		if !regexp.MustCompile(alias + `\.id (=|>=) `).MatchString(keyContextSQL) {
			t.Errorf("%s is not read by its id", alias)
		}
		// A body is read only for its value or for an id it names.
		for _, use := range regexp.MustCompile(alias+`\.body[^,\n]*`).FindAllString(keyContextSQL, -1) {
			if strings.Contains(use, "=") || strings.Contains(use, "<") || strings.Contains(use, ">") {
				t.Errorf("%s's body is compared: %q", alias, use)
			}
		}
	}
}

// TestTheCreditStateIsWhatAGrantReads: the shards, the debt mark, a pause, a
// latch and the least tier, and the refusal a grant meets from the rows.
func TestTheCreditStateIsWhatAGrantReads(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	state := func(ws string) CreditState {
		t.Helper()
		got, err := s.ReadCreditState(ctx, ws)
		if err != nil {
			t.Fatal(err)
		}
		return got
	}
	if got := state(seedWorkspace(t, 50, 30)); got != (CreditState{Shards: 2, Tier: 3}) {
		t.Fatalf("a funded workspace at the tier: %+v", got)
	}
	tier := seedWorkspace(t, 50, 30)
	setRow(t, tier, 1, map[string]any{"trust_tier": int64(2)})
	if got := state(tier); got != (CreditState{Shards: 2, Tier: 2, Refused: RefusedTier}) {
		t.Fatalf("a shard below the tier: %+v", got)
	}
	latched := seedWorkspace(t, 50)
	setRows(t, latched, map[string]any{"trust_latched_at": time.Now()})
	if got := state(latched); got != (CreditState{Shards: 1, Latched: true, Tier: 0, Refused: RefusedTier}) {
		t.Fatalf("a latched shard: %+v", got)
	}
	paused := seedWorkspace(t, 50, 0)
	setRow(t, paused, 1, map[string]any{"billing_pause_causes": []string{"manual"}})
	if got := state(paused); got != (CreditState{Shards: 2, Paused: true, Tier: 3, Refused: RefusedPaused}) {
		t.Fatalf("a paused shard: %+v", got)
	}
	// Production's reading: an empty list of causes is no pause.
	emptied := seedWorkspace(t, 50)
	setRows(t, emptied, map[string]any{"billing_pause_causes": []string{}})
	if got := state(emptied); got != (CreditState{Shards: 1, Tier: 3}) {
		t.Fatalf("an empty list of causes: %+v", got)
	}
	debt := seedWorkspace(t, 50)
	setRows(t, debt, map[string]any{"in_debt": true, "total_usage": int64(60)})
	if got := state(debt); got != (CreditState{Shards: 1, InDebt: true, Tier: 3, Refused: RefusedInDebt}) {
		t.Fatalf("a workspace in debt: %+v", got)
	}
	if _, err := s.ReadCreditState(ctx, storetest.UniqueID("ws")); !errors.Is(err, ErrCreditRowsIncomplete) {
		t.Fatalf("a workspace without rows: %v", err)
	}
	if _, err := s.ReadCreditState(ctx, ""); err == nil {
		t.Fatal("an empty workspace")
	}
}
