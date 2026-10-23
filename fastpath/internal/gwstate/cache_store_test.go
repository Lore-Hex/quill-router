package gwstate

import (
	"context"
	"fmt"
	"os"
	"testing"
	"time"

	"cloud.google.com/go/spanner"

	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store/storetest"
)

// The cache's test against the store runs on the Spanner emulator, as the
// store's own do (fastpath/README.md); without it it skips, and the rest
// run on a fake reader.
var (
	emulator *storetest.Emulator
	skipped  string
	shared   *spanner.Client
)

func TestMain(m *testing.M) {
	ctx := context.Background()
	var err error
	if emulator, skipped, err = storetest.Start(ctx); err != nil {
		fmt.Fprintln(os.Stderr, "gwstate tests:", err)
		os.Exit(1)
	}
	if emulator != nil {
		if shared, err = emulator.Database(ctx, "spike", nil); err != nil {
			fmt.Fprintln(os.Stderr, "gwstate tests:", err)
			_ = emulator.Close(ctx)
			os.Exit(1)
		}
	}
	code := m.Run()
	if emulator != nil {
		shared.Close()
		if err := emulator.Close(ctx); err != nil {
			fmt.Fprintln(os.Stderr, "gwstate tests: deleting the emulator's instance:", err)
			code = 1
		}
	}
	os.Exit(code)
}

// TestTheCacheAgainstTheStore: a key the store holds is a miss, filled with
// its workspace's credit state by the store's own reads; a refresh sees the
// key disabled and the workspace paused in the store, and the key's lookup
// pointer gone; Python's refusal evicts what is held.
func TestTheCacheAgainstTheStore(t *testing.T) {
	if emulator == nil {
		t.Skip(skipped)
	}
	ctx := context.Background()
	s, err := store.New(shared, store.Config{LiveFor: time.Hour, Window: 30 * time.Second, Skew: 2 * time.Second,
		PublishDeadline: 5 * time.Second, MaxLife: 5 * time.Minute, Grace: time.Minute, Allowance: 1_000_000,
		RequiredTier: 3})
	if err != nil {
		t.Fatal(err)
	}
	ws, lookup, keyHash := storetest.UniqueID("ws"), storetest.UniqueID("lh"), storetest.UniqueID("kh")
	keyBody := func(extra string) string {
		return `{"hash":"` + keyHash + `","salt":"s","secret_hash":"x","lookup_hash":"` + lookup +
			`","name":"n","label":"l","workspace_id":"` + ws + `","creator_user_id":null` + extra + `}`
	}
	entity := func(kind, id, body string) *spanner.Mutation {
		return spanner.InsertOrUpdate("tr_entities", []string{"kind", "id", "body", "updated_at"},
			[]any{kind, id, body, spanner.CommitTimestamp})
	}
	if _, err := shared.Apply(ctx, []*spanner.Mutation{
		entity("api_key_lookup", lookup, `{"key_id":"`+keyHash+`"}`),
		entity("api_key", keyHash, keyBody("")),
		entity("workspace", ws, `{"id":"`+ws+`","name":"w","owner_user_id":"u"}`),
		entity("byok", ws+"#openai", `{}`),
		spanner.InsertMap("tr_credit_balance", map[string]any{
			"workspace_id": ws, "shard": int64(0), "total_credits": int64(100_000), "trust_tier": int64(3)}),
	}); err != nil {
		t.Fatal(err)
	}
	c := start()
	cache := newTestCache(t, nil, c, func(cfg *CacheConfig) { cfg.Reader = s })
	if got := cache.Lookup(lookup, c.Now()); got.Key != Cold {
		t.Fatalf("the first lookup: %+v", got)
	}
	cache.settle()
	got := cache.Lookup(lookup, c.Now())
	if got.Key != Fresh || !got.Found || !got.Facts.Key.Eligible || got.Facts.Key.Hash != keyHash ||
		!got.Facts.Workspace.Eligible || len(got.Facts.BYOKProviders) != 1 || got.Credit != Fresh ||
		!got.CreditRows || got.CreditState != (store.CreditState{Shards: 1, Tier: 3}) {
		t.Fatalf("filled: %+v", got)
	}

	// The workspace paused on its credit row (DML: on the emulator an
	// update mutation resets the columns it does not name).
	if _, err := shared.ReadWriteTransaction(ctx, func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		_, err := txn.Update(ctx, spanner.Statement{
			SQL:    `UPDATE tr_credit_balance SET billing_pause_causes = ['manual'] WHERE workspace_id = @w`,
			Params: map[string]any{"w": ws}})
		return err
	}); err != nil {
		t.Fatal(err)
	}
	c.advance(DefaultRefreshEvery)
	cache.Refresh(ctx)
	if got := cache.Lookup(lookup, c.Now()); got.Credit != Fresh || !got.CreditState.Paused ||
		got.CreditState.Refused != store.RefusedPaused {
		t.Fatalf("paused: %+v", got)
	}
	if _, err := shared.Apply(ctx, []*spanner.Mutation{entity("api_key", keyHash, keyBody(`,"disabled":true`))}); err != nil {
		t.Fatal(err)
	}
	c.advance(DefaultRefreshEvery)
	cache.Refresh(ctx)
	if got := cache.Lookup(lookup, c.Now()); got.Key != Fresh || got.Facts.Key.Reason != ReasonKeyDisabled ||
		got.Credit != Unneeded {
		t.Fatalf("disabled: %+v", got)
	}
	if _, err := shared.Apply(ctx, []*spanner.Mutation{
		spanner.Delete("tr_entities", spanner.Key{"api_key_lookup", lookup})}); err != nil {
		t.Fatal(err)
	}
	c.advance(DefaultRefreshEvery)
	cache.Refresh(ctx)
	if got := cache.Lookup(lookup, c.Now()); got.Key != Fresh || got.Found {
		t.Fatalf("the lookup pointer gone: %+v", got)
	}
	cache.Evict(lookup)
	if keys, credits := cache.Len(); keys != 0 || credits != 0 {
		t.Fatalf("evicted: %d keys, %d credit entries", keys, credits)
	}
}
