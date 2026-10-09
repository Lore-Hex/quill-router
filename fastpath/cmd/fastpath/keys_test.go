package main

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"

	"google.golang.org/api/option"
	secretmanager "google.golang.org/api/secretmanager/v1"
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
	// Every key accepted is read, in order.
	key, accepted, err = readKeys(ctx, "projects/p/secrets/key/versions/2",
		"projects/p/secrets/key/versions/1,projects/p/secrets/key/versions/2", opts...)
	if err != nil || !bytes.Equal(key, next) || len(accepted) != 2 || !bytes.Equal(accepted[0], old) ||
		!bytes.Equal(accepted[1], next) {
		t.Fatalf("two keys accepted: %x %x %v", key, accepted, err)
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

// TestNoKeyIsLogged: with the client library's debug logging on, as
// GOOGLE_SDK_GO_LOGGING_LEVEL=debug turns it on, reading the keys writes
// none of them to the log. The same read through a client made without the
// quiet logger does, so the test sees what the library logs.
func TestNoKeyIsLogged(t *testing.T) {
	t.Setenv("GOOGLE_SDK_GO_LOGGING_LEVEL", "debug")
	key := bytes.Repeat([]byte{7}, 32)
	name := "projects/p/secrets/key/versions/1"
	srv, _ := secrets(t, map[string][]byte{name: key})
	opts := []option.ClientOption{option.WithEndpoint(srv.URL + "/"), option.WithHTTPClient(srv.Client())}
	ctx := context.Background()
	encoded := base64.StdEncoding.EncodeToString(key)
	// logged is what read writes to standard error, where the library's
	// default logger writes.
	logged := func(read func()) string {
		t.Helper()
		r, w, err := os.Pipe()
		if err != nil {
			t.Fatal(err)
		}
		out := make(chan []byte)
		go func() {
			b, _ := io.ReadAll(r)
			out <- b
		}()
		saved := os.Stderr
		os.Stderr = w
		read()
		os.Stderr = saved
		_ = w.Close()
		return string(<-out)
	}
	unquiet := logged(func() {
		svc, err := secretmanager.NewService(ctx, opts...)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := svc.Projects.Secrets.Versions.Access(name).Context(ctx).Do(); err != nil {
			t.Fatal(err)
		}
	})
	if !strings.Contains(unquiet, encoded) {
		t.Fatalf("the library's debug log does not show the payload, so this test would see nothing: %q", unquiet)
	}
	if out := logged(func() {
		if _, _, err := readKeys(ctx, name, name, opts...); err != nil {
			t.Fatal(err)
		}
	}); strings.Contains(out, encoded) {
		t.Fatal("a key was written to the log")
	}
}

// TestTheKeyFlagsGoTogether: a file's key or a secret's, not both; keys
// accepted only beside a secret's; and neither flag is no key.
func TestTheKeyFlagsGoTogether(t *testing.T) {
	key := bytes.Repeat([]byte{7}, 32)
	name := "projects/p/secrets/key/versions/1"
	srv, _ := secrets(t, map[string][]byte{name: key})
	opts := []option.ClientOption{option.WithEndpoint(srv.URL + "/"), option.WithHTTPClient(srv.Client())}
	ctx := context.Background()
	file := filepath.Join(t.TempDir(), "key")
	if err := os.WriteFile(file, key, 0o600); err != nil {
		t.Fatal(err)
	}
	for _, c := range []struct{ path, secret, accept string }{{file, name, ""}, {file, "", name}, {"", "", name}} {
		if _, _, err := keysOf(ctx, c.path, c.secret, c.accept, opts...); err == nil {
			t.Errorf("-key %q -key-secret %q -accept-key-secrets %q taken", c.path, c.secret, c.accept)
		}
	}
	for _, c := range []struct {
		path, secret, accept string
		accepted             int
	}{{file, "", "", 0}, {"", name, "", 0}, {"", name, name, 1}} {
		got, accepted, err := keysOf(ctx, c.path, c.secret, c.accept, opts...)
		if err != nil || !bytes.Equal(got, key) || len(accepted) != c.accepted {
			t.Errorf("-key %q -key-secret %q -accept-key-secrets %q: %x %x %v", c.path, c.secret, c.accept, got,
				accepted, err)
		}
	}
	if got, accepted, err := keysOf(ctx, "", "", "", opts...); got != nil || accepted != nil || err != nil {
		t.Errorf("no key flag: %x %x %v", got, accepted, err)
	}
}
