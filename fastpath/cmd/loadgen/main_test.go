package main

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"testing"

	"github.com/Lore-Hex/quill-router/fastpath/internal/frontdoor"
)

// TestHelpIsNoFailure: -h prints the flags and is done, which main takes
// for success, as the flag package's own handling of it was; an unknown
// flag is a failure.
func TestHelpIsNoFailure(t *testing.T) {
	if err := run([]string{"-h"}, io.Discard, nil); err != nil {
		t.Fatalf("-h: %v", err)
	}
	if err := run([]string{"-no-such-flag"}, io.Discard, nil); err == nil {
		t.Fatal("an unknown flag was taken")
	}
}

// TestTheOpenHeartbeatFlagDeclaresStreams: with -open-heartbeat, each
// stream's authorize declares the stream-open heartbeat; without it, none
// does.
func TestTheOpenHeartbeatFlagDeclaresStreams(t *testing.T) {
	mix := filepath.Join(t.TempDir(), "mix.json")
	if err := os.WriteFile(mix, []byte(`{"stream_share":1,"heartbeats":[{"value":1,"weight":1}],`+
		`"refund_share":0,"estimates":[{"value":100,"weight":1}],"bill_permill":[{"value":500,"weight":1}]}`),
		0o600); err != nil {
		t.Fatal(err)
	}
	for _, open := range []bool{false, true} {
		var mu sync.Mutex
		var declared []bool
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path == "/v1/authorize" {
				var a frontdoor.AuthorizeOf
				if err := json.NewDecoder(r.Body).Decode(&a); err != nil || !a.Stream {
					t.Errorf("an authorize: %+v %v", a, err)
				}
				mu.Lock()
				declared = append(declared, a.OpenHeartbeat)
				mu.Unlock()
			}
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(frontdoor.Authorized{Status: frontdoor.Busy})
		}))
		args := []string{"-front", srv.URL, "-mix", mix, "-rate", "100", "-duration", "50ms", "-workspaces", "1",
			"-seed", "1"}
		if open {
			args = append(args, "-open-heartbeat")
		}
		err := run(args, io.Discard, nil)
		srv.Close()
		if err != nil {
			t.Fatal(err)
		}
		mu.Lock()
		if len(declared) == 0 {
			t.Fatalf("-open-heartbeat %v: no authorize", open)
		}
		for _, d := range declared {
			if d != open {
				t.Fatalf("-open-heartbeat %v: an authorize declared %v", open, d)
			}
		}
		mu.Unlock()
	}
}

// TestTheRunStartsOnlyOnceTheWorkspacesAreReady: with -database, the
// workspaces named by -workspace are asked of the store before any
// authorize, and a workspace not ready stops the run with its reason, with
// no authorize sent; ready, the run goes to the workspaces named; and
// -workspace beside -workspaces is refused.
func TestTheRunStartsOnlyOnceTheWorkspacesAreReady(t *testing.T) {
	mix := filepath.Join(t.TempDir(), "mix.json")
	if err := os.WriteFile(mix, []byte(`{"stream_share":0,"heartbeats":[{"value":1,"weight":1}],`+
		`"refund_share":0,"estimates":[{"value":100,"weight":1}],"bill_permill":[{"value":500,"weight":1}]}`),
		0o600); err != nil {
		t.Fatal(err)
	}
	var mu sync.Mutex
	var seen []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/v1/authorize" {
			var a frontdoor.AuthorizeOf
			if err := json.NewDecoder(r.Body).Decode(&a); err != nil {
				t.Errorf("an authorize: %v", err)
			}
			mu.Lock()
			seen = append(seen, a.Workspace)
			mu.Unlock()
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(frontdoor.Authorized{Status: frontdoor.Busy})
	}))
	defer srv.Close()
	args := []string{"-front", srv.URL, "-mix", mix, "-rate", "100", "-duration", "50ms", "-seed", "1",
		"-workspace", "ws-a, ws-b", "-database", "projects/p/instances/i/databases/d"}
	var asked []string
	notReady := func(_ context.Context, database string, workspaces []string) error {
		asked = append(asked, database)
		asked = append(asked, workspaces...)
		return errors.New("workspace ws-b is not ready for the load: the workspace is not enabled for the fast path")
	}
	err := run(args, io.Discard, notReady)
	if err == nil || !strings.Contains(err.Error(), "ws-b is not ready") ||
		!slices.Equal(asked, []string{"projects/p/instances/i/databases/d", "ws-a", "ws-b"}) {
		t.Fatalf("a workspace not ready: %v, asked %q", err, asked)
	}
	mu.Lock()
	if len(seen) != 0 {
		t.Fatalf("authorizes were sent before the readiness: %q", seen)
	}
	mu.Unlock()
	if err := run(args, io.Discard, func(context.Context, string, []string) error { return nil }); err != nil {
		t.Fatal(err)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(seen) == 0 || slices.ContainsFunc(seen, func(ws string) bool { return ws != "ws-a" && ws != "ws-b" }) {
		t.Fatalf("the workspaces the load went to: %q", seen)
	}
	if err := run(append(args, "-workspaces", "2"), io.Discard, nil); err == nil ||
		!strings.Contains(err.Error(), "not both") {
		t.Fatalf("-workspace beside -workspaces: %v", err)
	}
	if err := run([]string{"-front", srv.URL, "-mix", mix, "-duration", "1ms", "-database", "d"}, io.Discard, nil); err == nil {
		t.Fatal("-database with no store to ask was taken")
	}
}
