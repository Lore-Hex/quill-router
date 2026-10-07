package store

import (
	"context"
	"fmt"
	"os"
	"testing"
	"time"

	"cloud.google.com/go/spanner"

	"github.com/Lore-Hex/quill-router/fastpath/internal/store/storetest"
)

// The test binary's emulator instance, and the spike database its tests
// share; tests use their own IDs (storetest.UniqueID), so they do not see
// one another's rows. The emulator runs one read-write transaction at a
// time, so the tests do not run in parallel.
var (
	emulator *storetest.Emulator
	skipped  string
	shared   *spanner.Client
)

func TestMain(m *testing.M) {
	ctx := context.Background()
	var err error
	if emulator, skipped, err = storetest.Start(ctx); err != nil {
		fmt.Fprintln(os.Stderr, "store tests:", err)
		os.Exit(1)
	}
	if emulator != nil {
		if shared, err = emulator.Database(ctx, "spike", nil); err != nil {
			fmt.Fprintln(os.Stderr, "store tests:", err)
			_ = emulator.Close(ctx)
			os.Exit(1)
		}
	}
	code := m.Run()
	if emulator != nil {
		shared.Close()
		if err := emulator.Close(ctx); err != nil {
			fmt.Fprintln(os.Stderr, "store tests: deleting the emulator's instance:", err)
			code = 1
		}
	}
	os.Exit(code)
}

// testConfig is a configuration every test can run with; a test changes
// what it is about.
func testConfig() Config {
	return Config{LiveFor: time.Hour, Window: 30 * time.Second, Allowance: 1_000_000, Floor: 0, RequiredTier: 3}
}

// spikeStore is a store of the shared database with testConfig as changes
// leave it, or skips the test without the emulator, saying why.
func spikeStore(t *testing.T, changes ...func(*Config)) *Store {
	t.Helper()
	if emulator == nil {
		t.Skip(skipped)
	}
	cfg := testConfig()
	for _, change := range changes {
		change(&cfg)
	}
	s, err := New(shared, cfg)
	if err != nil {
		t.Fatal(err)
	}
	return s
}
