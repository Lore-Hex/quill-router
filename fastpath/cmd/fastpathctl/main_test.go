package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"testing"

	"cloud.google.com/go/spanner"

	"github.com/Lore-Hex/quill-router/fastpath/internal/service"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store/storetest"
)

var (
	emulator *storetest.Emulator
	skipped  string
	shared   *spanner.Client
)

func TestMain(m *testing.M) {
	ctx := context.Background()
	var err error
	if emulator, skipped, err = storetest.Start(ctx); err != nil {
		fmt.Fprintln(os.Stderr, "fastpathctl tests:", err)
		os.Exit(1)
	}
	if emulator != nil {
		if shared, err = emulator.Database(ctx, "spike", nil); err != nil {
			fmt.Fprintln(os.Stderr, "fastpathctl tests:", err)
			_ = emulator.Close(ctx)
			os.Exit(1)
		}
	}
	code := m.Run()
	if emulator != nil {
		shared.Close()
		if err := emulator.Close(ctx); err != nil {
			fmt.Fprintln(os.Stderr, "fastpathctl tests: deleting the emulator's instance:", err)
			code = 1
		}
	}
	os.Exit(code)
}

// onShared is the command's store, on the test's database.
func onShared(t *testing.T) func(context.Context, string) (*store.Store, func(), error) {
	t.Helper()
	if shared == nil {
		t.Skip(skipped)
	}
	return func(context.Context, string) (*store.Store, func(), error) {
		s, err := store.New(shared, service.Defaults().Store)
		return s, func() {}, err
	}
}

// TestTheCommandRefusesWhatItCannotDo: no database, no workspace, another
// command, each exit 2, and none reaches the store.
func TestTheCommandRefusesWhatItCannotDo(t *testing.T) {
	never := func(context.Context, string) (*store.Store, func(), error) {
		t.Fatal("the store was opened")
		return nil, nil, nil
	}
	for _, args := range [][]string{{"status", "ws"}, {"-database", "d", "status"}, {"-database", "d", "drop", "ws"},
		{"-database", "d", "status", ""}} {
		if code, err := run(context.Background(), args, &bytes.Buffer{}, never); code != 2 || err == nil {
			t.Errorf("%q: exit %d, %v", args, code, err)
		}
	}
}

// TestStatusSaysWhenAWorkspaceIsDone: a workspace enabled is not done, exit
// 3, and once disabled, with nothing of the fast path left of it, it is,
// exit 0; enable and disable answer what they did.
func TestStatusSaysWhenAWorkspaceIsDone(t *testing.T) {
	open := onShared(t)
	ctx := context.Background()
	ws := storetest.UniqueID("ws")
	do := func(command string) (int, map[string]any) {
		t.Helper()
		var out bytes.Buffer
		code, err := run(ctx, []string{"-database", "d", command, ws}, &out, open)
		if err != nil {
			t.Fatalf("%s: %v", command, err)
		}
		var got map[string]any
		if err := json.Unmarshal(out.Bytes(), &got); err != nil {
			t.Fatalf("%s wrote %q: %v", command, out.String(), err)
		}
		return code, got
	}
	if code, got := do("enable"); code != 0 || got["enabled"] != true {
		t.Fatalf("enable: exit %d, %v", code, got)
	}
	code, got := do("status")
	if why, _ := json.Marshal(got["why_not"]); code != notDone || got["done"] != false ||
		!strings.Contains(string(why), "enabled") {
		t.Fatalf("an enabled workspace's status: exit %d, %v", code, got)
	}
	if code, got := do("disable"); code != 0 || got["enabled"] != false || got["revoked_leases"] != float64(0) {
		t.Fatalf("disable: exit %d, %v", code, got)
	}
	if code, got := do("status"); code != 0 || got["done"] != true {
		t.Fatalf("a disabled workspace with nothing left: exit %d, %v", code, got)
	}
}
