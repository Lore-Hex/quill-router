package record

import (
	"bytes"
	"strings"
	"testing"
)

// TestAStreamsCeilingIsOptional (docs/design/fast-admission-streams.md
// section 12.2 item 2): a record without a ceiling encodes byte for byte as
// before, so an auditor from before the field reads every record an owner
// writes until it states one. A heartbeat, a reap and a hand-off's hold with
// one round-trip, and a running charge or a reap's charge above the
// estimate passes up to the ceiling and no further; without one, the
// estimate bounds it as before.
func TestAStreamsCeilingIsOptional(t *testing.T) {
	for kind, r := range one() {
		data, err := Encode(r)
		if err != nil {
			t.Fatal(err)
		}
		if bytes.Contains(data, []byte(`"ceil"`)) {
			t.Fatalf("a %s without a ceiling encodes one: %s", kind, data)
		}
	}

	// A heartbeat: a running charge of 700 on a hold of 500.
	hb := one()[Heartbeat]
	hb.Snapshot.Running = 700
	if _, err := Encode(hb); err == nil {
		t.Fatal("a running charge above the hold, with no ceiling, was encoded")
	}
	hb.Ceiling = 800
	data, err := Encode(hb)
	if err != nil {
		t.Fatalf("a running charge within the ceiling: %v", err)
	}
	if !strings.Contains(string(data), `"est":500,"ceil":800,`) {
		t.Fatalf("the ceiling's encoding: %s", data)
	}
	if back, err := Decode(data); err != nil || back.Ceiling != 800 || back.Snapshot.Running != 700 {
		t.Fatalf("the ceiling's round trip: %v %+v", err, back)
	}
	hb.Snapshot.Running = 800
	if _, err := Encode(hb); err != nil {
		t.Fatalf("a running charge at the ceiling: %v", err)
	}
	hb.Snapshot.Running = 801
	if _, err := Encode(hb); err == nil {
		t.Fatal("a running charge above the ceiling was encoded")
	}

	// A reap: a charge of 700 on a hold of 500.
	reap := one()[Reap]
	reap.Charge = 700
	if _, err := Encode(reap); err == nil {
		t.Fatal("a reap above its hold, with no ceiling, was encoded")
	}
	reap.Ceiling = 800
	if data, err := Encode(reap); err != nil {
		t.Fatalf("a reap within its ceiling: %v", err)
	} else if back, err := Decode(data); err != nil || back.Ceiling != 800 || back.Charge != 700 {
		t.Fatalf("a reap's ceiling round trip: %v %+v", err, back)
	}
	reap.Charge = 801
	if _, err := Encode(reap); err == nil {
		t.Fatal("a reap above its ceiling was encoded")
	}

	// A hand-off's hold: its snapshot's running charge of 700 on a hold of
	// 500.
	handoff := one()[Handoff]
	handoff.Holds[0].Snapshot.Running = 700
	if _, err := Encode(handoff); err == nil {
		t.Fatal("a held hold's running charge above its hold, with no ceiling, was encoded")
	}
	handoff.Holds[0].Ceiling = 800
	data, err = Encode(handoff)
	if err != nil {
		t.Fatalf("a held hold within its ceiling: %v", err)
	}
	if !strings.Contains(string(data), `"est":500,"ceil":800,`) {
		t.Fatalf("a held hold's ceiling's encoding: %s", data)
	}
	if back, err := Decode(data); err != nil || back.Holds[0].Ceiling != 800 {
		t.Fatalf("a held hold's ceiling round trip: %v %+v", err, back.Holds[0])
	}
	if _, err := HoldsDigest(handoff.Holds); err != nil {
		t.Fatalf("the digest of holds with a ceiling: %v", err)
	}
	handoff.Holds[0].Snapshot.Running = 801
	if _, err := Encode(handoff); err == nil {
		t.Fatal("a held hold's running charge above its ceiling was encoded")
	}
}

// TestACeilingIsAboveItsEstimateAndOnlyAStreamsRecords: a ceiling is
// written only when above the estimate, so a ceiling at or below it, or a
// negative one, is refused, as is one on a record that bounds no running
// charge: a settle, whose charge has no bound, a refund, a release, a
// checkpoint, a hand-off or a manifest themselves, and a tick.
func TestACeilingIsAboveItsEstimateAndOnlyAStreamsRecords(t *testing.T) {
	for _, ceiling := range []int64{500, 499, 1, -1} {
		hb := one()[Heartbeat]
		hb.Ceiling = ceiling
		if _, err := Encode(hb); err == nil {
			t.Errorf("a heartbeat with a ceiling of %d on a hold of 500 was encoded", ceiling)
		}
		reap := one()[Reap]
		reap.Ceiling = ceiling
		if _, err := Encode(reap); err == nil {
			t.Errorf("a reap with a ceiling of %d on a hold of 500 was encoded", ceiling)
		}
		handoff := one()[Handoff]
		handoff.Holds[0].Ceiling = ceiling
		if _, err := Encode(handoff); err == nil {
			t.Errorf("a held hold with a ceiling of %d on a hold of 500 was encoded", ceiling)
		}
		if _, err := HoldsDigest(handoff.Holds); err == nil {
			t.Errorf("the digest took a held hold with a ceiling of %d on a hold of 500", ceiling)
		}
	}
	for _, kind := range []Kind{Settle, Refund, Release, Checkpoint, Handoff, Manifest, Tick} {
		r := one()[kind]
		r.Ceiling = r.Estimate + 300
		if _, err := Encode(r); err == nil {
			t.Errorf("a %s with a ceiling was encoded", kind)
		}
	}
	// A canonical record is read only as its encoding: the field once, and
	// not zero.
	hb := one()[Heartbeat]
	plain, err := Encode(hb)
	if err != nil {
		t.Fatal(err)
	}
	zero := strings.Replace(string(plain), `"est":500,`, `"est":500,"ceil":0,`, 1)
	if _, err := Decode([]byte(zero)); err == nil {
		t.Fatal("a ceiling of zero, written out, was decoded")
	}
}
