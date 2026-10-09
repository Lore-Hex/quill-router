package main

import (
	"context"
	"encoding/base64"
	"fmt"
	"regexp"
	"strings"

	"google.golang.org/api/option"
	secretmanager "google.golang.org/api/secretmanager/v1"
)

// pinnedVersion is a Secret Manager secret version named by its number,
// never "latest": every node, and every restart of one, reads the same key
// (docs/design/fast-admission-production-rollout.md, W6).
var pinnedVersion = regexp.MustCompile(`^projects/[^/]+/secrets/[^/]+/versions/[1-9][0-9]*$`)

// readSecret reads a pinned secret version's payload: the key's own bytes.
func readSecret(ctx context.Context, name string, opts ...option.ClientOption) ([]byte, error) {
	if !pinnedVersion.MatchString(name) {
		return nil, fmt.Errorf("%q is not a secret version pinned by its number", name)
	}
	svc, err := secretmanager.NewService(ctx, opts...)
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
