package main

import (
	"context"
	"encoding/base64"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"regexp"
	"strings"

	"google.golang.org/api/option"
	secretmanager "google.golang.org/api/secretmanager/v1"
)

// pinnedVersion is a Secret Manager secret version named by its number,
// never "latest": every node, and every restart of one, reads the same key
// (docs/design/fast-admission-production-rollout.md, W6).
var pinnedVersion = regexp.MustCompile(`^projects/[^/]+/secrets/[^/]+/versions/[1-9][0-9]*$`)

// quiet is the Secret Manager client's logger, which logs nothing, whatever
// GOOGLE_SDK_GO_LOGGING_LEVEL says: the client's debug logging prints each
// response, and an access's response is the key's own bytes.
var quiet = slog.New(slog.DiscardHandler)

// sdkLogging is the variable that turns the Google client libraries' logs
// on; each library reads it as it makes a client's logger.
const sdkLogging = "GOOGLE_SDK_GO_LOGGING_LEVEL"

// quietLibraries keeps every Google client library the node uses from
// logging, whatever sdkLogging says: their debug logs print each request and
// response, with the credentials that can read the envelope keys, and the
// keys. It runs before the node makes any client.
func quietLibraries() {
	if os.Getenv(sdkLogging) == "" {
		return
	}
	fmt.Fprintf(os.Stderr, "fastpath: %s is ignored: the client libraries' logs print credentials and keys\n",
		sdkLogging)
	_ = os.Unsetenv(sdkLogging)
}

// readSecret reads a pinned secret version's payload: the key's own bytes.
func readSecret(ctx context.Context, name string, opts ...option.ClientOption) ([]byte, error) {
	if !pinnedVersion.MatchString(name) {
		return nil, fmt.Errorf("%q is not a secret version pinned by its number", name)
	}
	// The quiet logger comes last, so no option given can replace it.
	svc, err := secretmanager.NewService(ctx, append(opts[:len(opts):len(opts)], option.WithLogger(quiet))...)
	if err != nil {
		return nil, err
	}
	resp, err := svc.Projects.Secrets.Versions.Access(name).Context(ctx).Do()
	if err != nil {
		return nil, fmt.Errorf("reading %s: %w", name, err)
	}
	if resp.Payload == nil {
		return nil, fmt.Errorf("%s has no payload", name)
	}
	key, err := base64.StdEncoding.DecodeString(resp.Payload.Data)
	if err != nil {
		return nil, fmt.Errorf("%s's payload: %w", name, err)
	}
	return key, nil
}

// keysOf is the node's keys as its flags give them: a file's, for
// development, or a pinned secret version's with the keys accepted beside
// it, which go only with a secret. With neither, the node has no key, as an
// auditor alone needs none.
func keysOf(ctx context.Context, keyPath, keySecret, accept string, opts ...option.ClientOption) ([]byte, [][]byte,
	error) {
	switch {
	case keyPath != "" && keySecret != "":
		return nil, nil, errors.New("-key or -key-secret, not both")
	case accept != "" && keySecret == "":
		return nil, nil, errors.New("-accept-key-secrets goes with -key-secret")
	case keyPath != "":
		key, err := os.ReadFile(keyPath)
		return key, nil, err
	case keySecret != "":
		return readKeys(ctx, keySecret, accept, opts...)
	}
	return nil, nil, nil
}

// readKeys reads the sealing key, from one pinned secret version, and the
// keys accepted beside it, from a comma-separated list of others.
func readKeys(ctx context.Context, seal, accept string, opts ...option.ClientOption) ([]byte, [][]byte, error) {
	key, err := readSecret(ctx, seal, opts...)
	if err != nil {
		return nil, nil, err
	}
	var accepted [][]byte
	if accept != "" {
		for _, name := range strings.Split(accept, ",") {
			k, err := readSecret(ctx, name, opts...)
			if err != nil {
				return nil, nil, err
			}
			accepted = append(accepted, k)
		}
	}
	return key, accepted, nil
}
