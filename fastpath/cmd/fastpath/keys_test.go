package main

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"

	"google.golang.org/api/option"
)

// secrets is a stand-in for Secret Manager's access call, with the versions
// it holds and a count of the calls it answered.
func secrets(t *testing.T, held map[string][]byte) (*httptest.Server, *atomic.Int64) {
	t.Helper()
	var calls atomic.Int64
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		name := strings.TrimSuffix(strings.TrimPrefix(r.URL.Path, "/v1/"), ":access")
		key, ok := held[name]
		if r.Method != http.MethodGet || !ok {
			http.Error(w, `{"error":{"code":404,"message":"no such version"}}`, http.StatusNotFound)
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"name": name,
			"payload": map[string]string{"data": base64.StdEncoding.EncodeToString(key)}})
	}))
	t.Cleanup(srv.Close)
	return srv, &calls
}

// TestKeysAreReadFromPinnedVersions: the sealing key and the keys accepted
// beside it are read from secret versions pinned by their numbers; a
// version not pinned is refused before any call, and one not held is an
// error.
func TestKeysAreReadFromPinnedVersions(t *testing.T) {
	old, next := bytes.Repeat([]byte{7}, 32), bytes.Repeat([]byte{9}, 32)
	srv, calls := secrets(t, map[string][]byte{"projects/p/secrets/key/versions/1": old,
		"projects/p/secrets/key/versions/2": next})
	opts := []option.ClientOption{option.WithEndpoint(srv.URL + "/"), option.WithHTTPClient(srv.Client())}
	ctx := context.Background()
	key, accepted, err := readKeys(ctx, "projects/p/secrets/key/versions/2", "projects/p/secrets/key/versions/1", opts...)
	if err != nil || !bytes.Equal(key, next) || len(accepted) != 1 || !bytes.Equal(accepted[0], old) {
		t.Fatalf("the keys: %x %x %v", key, accepted, err)
	}
	if key, accepted, err := readKeys(ctx, "projects/p/secrets/key/versions/1", "", opts...); err != nil ||
		!bytes.Equal(key, old) || len(accepted) != 0 {
		t.Fatalf("a key with none accepted beside it: %x %x %v", key, accepted, err)
	}
	before := calls.Load()
	for _, name := range []string{"projects/p/secrets/key/versions/latest", "projects/p/secrets/key/versions/0",
		"projects/p/secrets/key", "key"} {
		if _, err := readSecret(ctx, name, opts...); err == nil {
			t.Errorf("%q is read", name)
		}
	}
	if calls.Load() != before {
		t.Fatal("a version not pinned was asked for")
	}
	if _, err := readSecret(ctx, "projects/p/secrets/key/versions/3", opts...); err == nil {
		t.Fatal("a version not held is read")
	}
	if _, _, err := readKeys(ctx, "projects/p/secrets/key/versions/2", "projects/p/secrets/key/versions/3",
		opts...); err == nil {
		t.Fatal("an accepted version not held is read")
	}
}
