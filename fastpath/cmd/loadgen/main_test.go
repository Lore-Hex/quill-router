package main

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync"
	"testing"

	"github.com/Lore-Hex/quill-router/fastpath/internal/frontdoor"
)

// TestHelpIsNoFailure: -h prints the flags and is done, which main takes
// for success, as the flag package's own handling of it was; an unknown
// flag is a failure.
func TestHelpIsNoFailure(t *testing.T) {
	if err := run([]string{"-h"}, io.Discard); err != nil {
		t.Fatalf("-h: %v", err)
	}
	if err := run([]string{"-no-such-flag"}, io.Discard); err == nil {
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
		err := run(args, io.Discard)
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
