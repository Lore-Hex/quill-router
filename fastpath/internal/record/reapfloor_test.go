package record

import (
	"bytes"
	"strings"
	"testing"
	"time"
)

// TestAStreamsReapFloorIsOptional (docs/design/fast-admission-streams.md
// section 4.6 item 4): a snapshot without a reap deadline encodes byte for
// byte as before, so an auditor from before the field reads every record an
// owner writes until it states one; a heartbeat and a hand-off's hold with
// one round-trip and say when a reap may come; one not after the deadline,
// or not in UTC, is refused.
func TestAStreamsReapFloorIsOptional(t *testing.T) {
	hb := one()[Heartbeat]
	plain, err := Encode(hb)
	if err != nil {
		t.Fatal(err)
	}
	if bytes.Contains(plain, []byte("reap_deadline")) {
		t.Fatalf("a snapshot without a floor encodes it: %s", plain)
	}
	if got := hb.Snapshot.ReapAfter(); !got.Equal(deadline) {
		t.Fatalf("without a floor a reap waits for %v, want the deadline %v", got, deadline)
	}
	floor := deadline.Add(4 * time.Minute)
	hb.Snapshot.ReapDeadline = floor
	data, err := Encode(hb)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(data), `"reap_deadline":"2026-10-08T02:24:00Z"`) {
		t.Fatalf("the floor's encoding: %s", data)
	}
	back, err := Decode(data)
	if err != nil || !back.Snapshot.ReapDeadline.Equal(floor) || !back.Snapshot.ReapAfter().Equal(floor) {
		t.Fatalf("the floor's round trip: %v %+v", err, back.Snapshot)
	}
	handoff := one()[Handoff]
	handoff.Holds[0].Snapshot.ReapDeadline = floor
	if data, err := Encode(handoff); err != nil {
		t.Fatalf("a hand-off's hold with a floor: %v", err)
	} else if back, err := Decode(data); err != nil || !back.Holds[0].Snapshot.ReapAfter().Equal(floor) {
		t.Fatalf("a hand-off's floor round trip: %v %+v", err, back.Holds[0].Snapshot)
	}
	for name, at := range map[string]time.Time{
		"at the deadline":     deadline,
		"before the deadline": deadline.Add(-time.Second),
		"not in UTC":          floor.In(time.FixedZone("x", 3600)),
	} {
		bad := one()[Heartbeat]
		bad.Snapshot.ReapDeadline = at
		if _, err := Encode(bad); err == nil {
			t.Errorf("a reap deadline %s was encoded", name)
		}
	}
}
