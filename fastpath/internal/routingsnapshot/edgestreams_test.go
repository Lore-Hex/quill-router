package routingsnapshot

import (
	"bytes"
	"regexp"
	"strings"
	"testing"
)

// edgeStreamsMember matches a fastpath_edge_streams member, whatever its
// value, so the test runs on a golden body with or without one.
var edgeStreamsMember = regexp.MustCompile(`"fastpath_edge_streams":([^,]*),`)

// withEdgeStreams is the golden body with its fastpath_edge_streams member
// set to value (raw JSON) in its canonical place, just before
// fastpath_pilot_enabled; "" removes it.
func withEdgeStreams(t *testing.T, raw []byte, value string) []byte {
	t.Helper()
	out := edgeStreamsMember.ReplaceAll(raw, nil)
	if value == "" {
		return out
	}
	at := []byte(`"fastpath_pilot_enabled":`)
	if bytes.Count(out, at) != 1 {
		t.Fatal("the golden body has no single fastpath_pilot_enabled")
	}
	return bytes.Replace(out, at, append([]byte(`"fastpath_edge_streams":`+value+`,`), at...), 1)
}

// TestTheEdgeStreamsSwitchIsOptional (docs/design/fast-admission-streams.md
// section 5.4): Go ships before the control plane publishes the member, so
// a body without fastpath_edge_streams parses and reads as off, the only
// form of off; a body with true parses as on and verifies under a manifest
// signed for it; false, null, another type and a member out of canonical
// order are refused.
func TestTheEdgeStreamsSwitchIsOptional(t *testing.T) {
	m, published := policyGolden(t)
	raw := withEdgeStreams(t, published, "")
	p, err := ParseEdgePolicy(raw)
	if err != nil || p.FastpathEdgeStreams != nil || p.EdgeStreams() {
		t.Fatalf("absent fastpath_edge_streams: %v, %+v", err, p)
	}
	on := withEdgeStreams(t, raw, `true`)
	got, err := ParseEdgePolicy(on)
	if err != nil || !got.EdgeStreams() {
		t.Fatalf("fastpath_edge_streams true: %v, %+v", err, got)
	}
	got.FastpathEdgeStreams = nil
	if a, b := canonical(got), canonical(p); !bytes.Equal(a, b) {
		t.Fatalf("the member changed another field:\n%s\n%s", a, b)
	}
	s := testKeySigner(t, m)
	verified, _, err := VerifyEdgePolicy(s.signPolicy(t, on), []TrustedKey{goldenKey(t, m)}, on,
		policyOptions(m, policyIssuedAt))
	if err != nil || !verified.EdgeStreams() {
		t.Fatalf("a signed body with fastpath_edge_streams: %v, %+v", err, verified)
	}
	for name, value := range map[string]string{
		"false":       `false`,
		"null":        `null`,
		"a string":    `"true"`,
		"an integer":  `1`,
		"an object":   `{"on":true}`,
		"an array":    `[true]`,
		"unknown key": `true,"fastpath_edge_streams_x":true`,
	} {
		if got, err := ParseEdgePolicy(withEdgeStreams(t, raw, value)); err == nil {
			t.Errorf("%s: accepted as %v", name, got.EdgeStreams())
		}
	}
	at := []byte(`"fastpath_pilot_regions":`)
	misplaced := bytes.Replace(raw, at, append([]byte(`"fastpath_edge_streams":true,`), at...), 1)
	if _, err := ParseEdgePolicy(misplaced); err == nil || !strings.Contains(err.Error(), "canonical") {
		t.Errorf("a misplaced member: %v", err)
	}
}
