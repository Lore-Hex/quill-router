package record

import (
	"bytes"
	"encoding/json"
	"fmt"
	"reflect"
	"strings"
	"testing"
	"time"
)

var (
	deadline = time.Date(2026, 10, 8, 2, 20, 0, 0, time.UTC)
	digest   = bytes.Repeat([]byte{0xab}, DigestSize)
	snapHash = bytes.Repeat([]byte{0xcd}, DigestSize)
)

func snapshot() *Snapshot {
	return &Snapshot{GatewaySeq: 3, Hash: snapHash, Usage: []byte(`{"out":40}`), Running: 120, Deadline: deadline}
}

// one is a valid record of each kind.
func one() map[Kind]Record {
	base := func(k Kind, seq int64) Record {
		return Record{Version: Version, Lease: "L0oJBwYFBAMCAQ0ODw4ODw", Epoch: 2, Seq: seq, Kind: k}
	}
	hb := base(Heartbeat, 1)
	hb.Auth, hb.Estimate, hb.Snapshot, hb.First, hb.Basis = "gwa-1", 500, snapshot(), true, []byte(`{"model":"m"}`)
	settle := base(Settle, 2)
	settle.Auth, settle.Estimate, settle.Charge, settle.Shortfall, settle.Digest = "gwa-1", 500, 640, 140, digest
	adopted := base(Refund, 3)
	adopted.Auth, adopted.Estimate, adopted.Shortfall, adopted.Drain, adopted.Boot = "gwa-2", 300, 140, "d-7", []byte("boot")
	reap := base(Reap, 4)
	reap.Auth, reap.Estimate, reap.Charge, reap.Shortfall, reap.Digest, reap.SnapshotSeq = "gwa-3", 500, 120, 140, digest, 1
	release := base(Release, 5)
	release.Auth, release.Estimate, release.Shortfall, release.Boot = "gwa-4", 200, 140, []byte("boot")
	ckpt := base(Checkpoint, 6)
	ckpt.Checkpoint = &CheckpointOf{Consumed: 760, Open: 2, OpenSum: 800, LatestEnd: deadline, KeyStatus: 9, Return: 50}
	handoff := base(Handoff, 7)
	handoff.Holds = []HeldHold{{Auth: "gwa-5", Estimate: 500, Deadline: deadline, Boot: []byte("boot"), Snapshot: snapshot(),
		SnapshotSeq: 1, Basis: []byte(`{"model":"m"}`)}, {Auth: "gwa-6", Estimate: 300, Deadline: deadline, Boot: []byte("boot")}}
	sum, err := HoldsDigest(handoff.Holds)
	if err != nil {
		panic(err)
	}
	manifest := base(Manifest, 8)
	manifest.Manifest = &ManifestOf{Chunks: 1, HoldsDigest: sum, Seqs: []int64{7}}
	return map[Kind]Record{Heartbeat: hb, Settle: settle, Refund: adopted, Reap: reap, Release: release,
		Checkpoint: ckpt, Handoff: handoff, Manifest: manifest,
		Tick: {Version: Version, Lease: "L0oJBwYFBAMCAQ0ODw4ODw", Kind: Tick, TickNumber: 1, TickAt: deadline}}
}

// others are valid records the kinds' fields allow besides one's: a later
// heartbeat, and a final checkpoint.
func others() map[string]Record {
	later := one()[Heartbeat]
	later.Seq, later.First, later.Basis = 2, false, nil
	final := one()[Checkpoint]
	final.Seq, final.Checkpoint = 9, &CheckpointOf{Consumed: 760, KeyStatus: 9, Return: 50, Final: true}
	return map[string]Record{"a later heartbeat": later, "a final checkpoint": final}
}

func TestEachKindRoundTrips(t *testing.T) {
	all := map[string]Record{}
	for kind, r := range one() {
		all[string(kind)] = r
	}
	for name, r := range others() {
		all[name] = r
	}
	for name, r := range all {
		b, err := Encode(r)
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		got, err := Decode(b)
		if err != nil || !reflect.DeepEqual(got, r) {
			t.Fatalf("%s came back as %+v, %v", name, got, err)
		}
	}
	for kind, terminal := range map[Kind]bool{Settle: true, Refund: true, Reap: true, Release: true,
		Heartbeat: false, Checkpoint: false, Handoff: false, Manifest: false, Tick: false} {
		if kind.Terminal() != terminal {
			t.Errorf("%s.Terminal() is %v", kind, kind.Terminal())
		}
	}
	if len(one()) != 9 {
		t.Fatalf("%d kinds, and the test names 9", len(one()))
	}
}

// TestTheFormatIsPinned: the auditor of another release reads these bytes,
// every kind's, and this digest of a hand-off's holds.
func TestTheFormatIsPinned(t *testing.T) {
	for kind, want := range map[Kind]string{
		Heartbeat: `{"v":1,"lease":"L0oJBwYFBAMCAQ0ODw4ODw","epoch":2,"seq":1,"kind":"hb","a":"gwa-1","est":500,"first":true,` +
			`"basis":"eyJtb2RlbCI6Im0ifQ==","hb":{"gseq":3,"hash":"zc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc0=",` +
			`"usage":"eyJvdXQiOjQwfQ==","run":120,"deadline":"2026-10-08T02:20:00Z"}}`,
		Settle: `{"v":1,"lease":"L0oJBwYFBAMCAQ0ODw4ODw","epoch":2,"seq":2,"kind":"settle","a":"gwa-1","est":500,` +
			`"charge":640,"sf":140,"digest":"q6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6s="}`,
		Refund: `{"v":1,"lease":"L0oJBwYFBAMCAQ0ODw4ODw","epoch":2,"seq":3,"kind":"refund","a":"gwa-2","est":300,` +
			`"sf":140,"drain":"d-7","boot":"Ym9vdA=="}`,
		Reap: `{"v":1,"lease":"L0oJBwYFBAMCAQ0ODw4ODw","epoch":2,"seq":4,"kind":"reap","a":"gwa-3","est":500,"charge":120,` +
			`"sf":140,"digest":"q6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6s=","snap":1}`,
		Release: `{"v":1,"lease":"L0oJBwYFBAMCAQ0ODw4ODw","epoch":2,"seq":5,"kind":"release","a":"gwa-4","est":200,` +
			`"sf":140,"boot":"Ym9vdA=="}`,
		Checkpoint: `{"v":1,"lease":"L0oJBwYFBAMCAQ0ODw4ODw","epoch":2,"seq":6,"kind":"ckpt","ckpt":{"consumed":760,"open":2,` +
			`"open_sum":800,"latest_end":"2026-10-08T02:20:00Z","ks":9,"return":50}}`,
		Handoff: `{"v":1,"lease":"L0oJBwYFBAMCAQ0ODw4ODw","epoch":2,"seq":7,"kind":"handoff","holds":[{"a":"gwa-5",` +
			`"est":500,"deadline":"2026-10-08T02:20:00Z","boot":"Ym9vdA==","hb":{"gseq":3,"hash":"zc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc0=",` +
			`"usage":"eyJvdXQiOjQwfQ==","run":120,"deadline":"2026-10-08T02:20:00Z"},"snap":1,"basis":"eyJtb2RlbCI6Im0ifQ=="},` +
			`{"a":"gwa-6","est":300,"deadline":"2026-10-08T02:20:00Z","boot":"Ym9vdA=="}]}`,
		Manifest: `{"v":1,"lease":"L0oJBwYFBAMCAQ0ODw4ODw","epoch":2,"seq":8,"kind":"manifest","manifest":{"chunks":1,` +
			`"holds_digest":"NHimSbLdv7cIqrwXmomrJ4yfs42LWw3s0N6JCRDyTUw=","seqs":[7]}}`,
		Tick: `{"v":1,"lease":"L0oJBwYFBAMCAQ0ODw4ODw","kind":"tick","tick":1,"at":"2026-10-08T02:20:00Z"}`,
	} {
		got, err := Encode(one()[kind])
		if err != nil || string(got) != want {
			t.Errorf("%s encodes as\n%s, %v; want\n%s", kind, got, err, want)
		}
	}
	for name, want := range map[string]string{
		"a later heartbeat": `{"v":1,"lease":"L0oJBwYFBAMCAQ0ODw4ODw","epoch":2,"seq":2,"kind":"hb","a":"gwa-1","est":500,` +
			`"hb":{"gseq":3,"hash":"zc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc0=","usage":"eyJvdXQiOjQwfQ==","run":120,` +
			`"deadline":"2026-10-08T02:20:00Z"}}`,
		"a final checkpoint": `{"v":1,"lease":"L0oJBwYFBAMCAQ0ODw4ODw","epoch":2,"seq":9,"kind":"ckpt","ckpt":{"consumed":760,` +
			`"open":0,"open_sum":0,"ks":9,"return":50,"final":true}}`,
	} {
		got, err := Encode(others()[name])
		if err != nil || string(got) != want {
			t.Errorf("%s encodes as\n%s, %v; want\n%s", name, got, err, want)
		}
	}
	sum, err := HoldsDigest(one()[Handoff].Holds)
	if err != nil || fmt.Sprintf("%x", sum) != "3478a649b2ddbfb708aabc179a89ab278c9fb38d8b5b0decd0de890910f24d4c" {
		t.Errorf("the holds digest is %x, %v", sum, err)
	}
}

func TestRecordsTheirKindsRefuse(t *testing.T) {
	valid := one()
	change := func(k Kind, f func(*Record)) Record {
		r := valid[k]
		if r.Snapshot != nil {
			c := *r.Snapshot
			r.Snapshot = &c
		}
		if r.Checkpoint != nil {
			c := *r.Checkpoint
			r.Checkpoint = &c
		}
		if r.Manifest != nil {
			c := *r.Manifest
			c.Seqs = append([]int64(nil), c.Seqs...)
			r.Manifest = &c
		}
		r.Holds = append([]HeldHold(nil), r.Holds...)
		for i := range r.Holds {
			if r.Holds[i].Snapshot != nil {
				c := *r.Holds[i].Snapshot
				r.Holds[i].Snapshot = &c
			}
		}
		f(&r)
		return r
	}
	elsewhere := time.FixedZone("UTC-7", -7*3600)
	yearZero := time.Date(0, 12, 31, 0, 0, 0, 0, time.UTC)
	for name, r := range map[string]Record{
		"another version":                      change(Settle, func(r *Record) { r.Version = 2 }),
		"no lease":                             change(Settle, func(r *Record) { r.Lease = "" }),
		"a lease that is not printable ASCII":  change(Settle, func(r *Record) { r.Lease = "L\xff" }),
		"no such kind":                         change(Settle, func(r *Record) { r.Kind = "bonus" }),
		"an owner record without a seq":        change(Settle, func(r *Record) { r.Seq = 0 }),
		"an owner record without an epoch":     change(Refund, func(r *Record) { r.Epoch = 0 }),
		"a tick with an owner's seq":           change(Tick, func(r *Record) { r.Seq = 1 }),
		"a tick with an authorization":         change(Tick, func(r *Record) { r.Auth = "gwa-1" }),
		"a tick without its number":            change(Tick, func(r *Record) { r.TickNumber = 0 }),
		"a tick without its time":              change(Tick, func(r *Record) { r.TickAt = time.Time{} }),
		"a tick's time not in UTC":             change(Tick, func(r *Record) { r.TickAt = deadline.In(elsewhere) }),
		"an owner record with a tick's number": change(Settle, func(r *Record) { r.TickNumber = 1 }),
		"a settle with no authorization":       change(Settle, func(r *Record) { r.Auth = "" }),
		"an authorization not printable ASCII": change(Settle, func(r *Record) { r.Auth = "gwa-\xff" }),
		"a settle with no digest":              change(Settle, func(r *Record) { r.Digest = nil }),
		"a digest of 33 bytes":                 change(Settle, func(r *Record) { r.Digest = append(r.Digest, 1) }),
		"a negative charge":                    change(Settle, func(r *Record) { r.Charge = -1 }),
		"a negative shortfall":                 change(Settle, func(r *Record) { r.Shortfall = -1 }),
		"a refund that charges":                change(Refund, func(r *Record) { r.Charge = 1 }),
		"a refund without its boot binding":    change(Refund, func(r *Record) { r.Boot = nil }),
		"a release without its boot binding":   change(Release, func(r *Record) { r.Boot = nil }),
		"a settle with a boot binding":         change(Settle, func(r *Record) { r.Boot = []byte("b") }),
		"a release that adopts":                change(Release, func(r *Record) { r.Drain = "d-1" }),
		"a reap above its hold":                change(Reap, func(r *Record) { r.Charge = 501 }),
		"a reap with no snapshot":              change(Reap, func(r *Record) { r.SnapshotSeq = 0 }),
		"a reap of its own record":             change(Reap, func(r *Record) { r.SnapshotSeq = r.Seq }),
		"a reap that adopts":                   change(Reap, func(r *Record) { r.Drain = "d-1" }),
		"a hand-off with a charge":             change(Handoff, func(r *Record) { r.Charge = 1 }),
		"a manifest with a shortfall":          change(Manifest, func(r *Record) { r.Shortfall = 1 }),
		"a checkpoint with an adopted row":     change(Checkpoint, func(r *Record) { r.Drain = "d-1" }),
		"a heartbeat over its cap":             change(Heartbeat, func(r *Record) { r.Snapshot.Running = 501 }),
		"a heartbeat with no hash":             change(Heartbeat, func(r *Record) { r.Snapshot.Hash = nil }),
		"a hash of 33 bytes":                   change(Heartbeat, func(r *Record) { r.Snapshot.Hash = append(r.Snapshot.Hash, 1) }),
		"a heartbeat with no usage":            change(Heartbeat, func(r *Record) { r.Snapshot.Usage = nil }),
		"a gateway sequence of zero":           change(Heartbeat, func(r *Record) { r.Snapshot.GatewaySeq = 0 }),
		"a deadline not in UTC":                change(Heartbeat, func(r *Record) { r.Snapshot.Deadline = deadline.In(elsewhere) }),
		"a first heartbeat without its basis":  change(Heartbeat, func(r *Record) { r.Basis = nil }),
		"a later heartbeat with a basis":       change(Heartbeat, func(r *Record) { r.Seq, r.First = 2, false }),
		"the lease's first record not a first": change(Heartbeat, func(r *Record) { r.First, r.Basis = false, nil }),
		"a gateway's first heartbeat not a first": change(Heartbeat, func(r *Record) {
			r.Seq, r.First, r.Basis, r.Snapshot.GatewaySeq = 2, false, nil, 1
		}),
		"a deadline in year 0":             change(Heartbeat, func(r *Record) { r.Snapshot.Deadline = yearZero }),
		"a held hold's deadline in year 0": change(Handoff, func(r *Record) { r.Holds[1].Deadline = yearZero }),
		"a tick in year 0":                 change(Tick, func(r *Record) { r.TickAt = yearZero }),
		"a latest end in year 0":           change(Checkpoint, func(r *Record) { r.Checkpoint.LatestEnd = yearZero }),
		"a heartbeat with a shortfall":     change(Heartbeat, func(r *Record) { r.Shortfall = 1 }),
		"a heartbeat with a charge":        change(Heartbeat, func(r *Record) { r.Charge = 1 }),
		"a checkpoint with an auth":        change(Checkpoint, func(r *Record) { r.Auth = "gwa-1" }),
		"a final checkpoint with holds":    change(Checkpoint, func(r *Record) { r.Checkpoint.Final = true }),
		"open holds with no end of life":   change(Checkpoint, func(r *Record) { r.Checkpoint.LatestEnd = time.Time{} }),
		"no open holds with a sum": change(Checkpoint, func(r *Record) {
			r.Checkpoint.Open, r.Checkpoint.LatestEnd = 0, time.Time{}
		}),
		"a checkpoint carrying a hand-off":     change(Checkpoint, func(r *Record) { r.Holds = valid[Handoff].Holds }),
		"a settle carrying a reap's basis":     change(Settle, func(r *Record) { r.Basis = []byte("x") }),
		"a hand-off naming a hold twice":       change(Handoff, func(r *Record) { r.Holds[1].Auth = r.Holds[0].Auth }),
		"a held snapshot without its seq":      change(Handoff, func(r *Record) { r.Holds[0].SnapshotSeq = 0 }),
		"a held snapshot from a later record":  change(Handoff, func(r *Record) { r.Holds[0].SnapshotSeq = 8 }),
		"a held snapshot without its basis":    change(Handoff, func(r *Record) { r.Holds[0].Basis = nil }),
		"a held hold with no deadline":         change(Handoff, func(r *Record) { r.Holds[1].Deadline = time.Time{} }),
		"a held hold's two deadlines":          change(Handoff, func(r *Record) { r.Holds[0].Deadline = deadline.Add(-time.Hour) }),
		"a held hold with a basis alone":       change(Handoff, func(r *Record) { r.Holds[1].Basis = []byte("x") }),
		"a held hold without its boot binding": change(Handoff, func(r *Record) { r.Holds[1].Boot = nil }),
		"a held snapshot of record -1":         change(Handoff, func(r *Record) { r.Holds[0].SnapshotSeq = -1 }),
		"an authorization of 65 characters":    change(Settle, func(r *Record) { r.Auth = strings.Repeat("a", 65) }),
		"a lease of 33 characters":             change(Settle, func(r *Record) { r.Lease = strings.Repeat("l", 33) }),
		"a drain record ID of 65 characters":   change(Refund, func(r *Record) { r.Drain = strings.Repeat("d", 65) }),
		"a held hold's authorization of 65":    change(Handoff, func(r *Record) { r.Holds[1].Auth = strings.Repeat("a", 65) }),
		"a manifest's seqs out of order":       change(Manifest, func(r *Record) { r.Manifest.Chunks, r.Manifest.Seqs = 2, []int64{7, 6} }),
		"a manifest's seqs miscounted":         change(Manifest, func(r *Record) { r.Manifest.Chunks = 2 }),
		"a manifest naming itself":             change(Manifest, func(r *Record) { r.Manifest.Seqs = []int64{8} }),
		"a manifest with a short digest":       change(Manifest, func(r *Record) { r.Manifest.HoldsDigest = []byte{1} }),
	} {
		if _, err := Encode(r); err == nil {
			t.Errorf("%s is encoded", name)
		}
		// Written by another writer, as JSON, the record is not read.
		if raw, err := json.Marshal(r); err == nil {
			if _, err := Decode(raw); err == nil {
				t.Errorf("%s is decoded", name)
			}
		}
	}
	refund, err := Encode(valid[Refund])
	if err != nil {
		t.Fatal(err)
	}
	r := string(refund)
	for name, raw := range map[string]string{
		"an unknown field":          strings.Replace(r, `"v":1,`, `"v":1,"bonus":1,`, 1),
		"a field named twice":       strings.Replace(r, `"est":300,`, `"est":300,"est":300,`, 1),
		"a field cased otherwise":   strings.Replace(r, `"est":300`, `"Est":300`, 1),
		"a null field":              strings.Replace(r, `"v":1,`, `"v":1,"hb":null,`, 1),
		"a zero field Encode omits": strings.Replace(r, `"est":300,`, `"est":300,"charge":0,`, 1),
		"a closing brace after":     r + `}`,
		"a bracket after":           r + `]`,
		"a second value":            r + r,
		"white space":               " " + r,
		"not a record":              `[1,2]`,
	} {
		if raw == r {
			t.Fatalf("%s: the case left the record as it was", name)
		}
		if _, err := Decode([]byte(raw)); err == nil {
			t.Errorf("%s is decoded", name)
		}
	}
}

// TestASettleIsAbout250Bytes: the design's size, which with the per-key
// limit of 1 MBps sets a hot workspace's shard count (§4.1, §6).
func TestASettleIsAbout250Bytes(t *testing.T) {
	r := one()[Settle]
	r.Auth = "gwa-" + strings.Repeat("x", 22) + strings.Repeat("y", 22)
	r.Drain = strings.Repeat("r", 32)
	b, err := Encode(r)
	if err != nil || len(b) > 300 {
		t.Fatalf("a settle is %d bytes, %v", len(b), err)
	}
}

func TestHoldsDigestIgnoresTheirOrder(t *testing.T) {
	holds := one()[Handoff].Holds
	var digests [][]byte
	for _, hs := range [][]HeldHold{holds, {holds[1], holds[0]}, holds[:1]} {
		d, err := HoldsDigest(hs)
		if err != nil || len(d) != DigestSize {
			t.Fatalf("the digest of %+v: %x, %v", hs, d, err)
		}
		digests = append(digests, d)
	}
	if a, b, c := digests[0], digests[1], digests[2]; !bytes.Equal(a, b) || bytes.Equal(a, c) {
		t.Fatalf("digests %x, %x, %x", a, b, c)
	}
	elsewhere := holds[1]
	elsewhere.Deadline = elsewhere.Deadline.In(time.FixedZone("UTC-7", -7*3600))
	if _, err := HoldsDigest([]HeldHold{holds[0], elsewhere}); err == nil {
		t.Fatal("a hold whose deadline is not in UTC is digested")
	}
	if _, err := HoldsDigest([]HeldHold{holds[1], holds[1]}); err == nil {
		t.Fatal("a hold named twice is digested")
	}
	// Any hand-off the format carries has its digest.
	high := one()[Handoff]
	high.Seq, high.Holds[0].SnapshotSeq = 1<<62+1, 1<<62
	if _, err := Encode(high); err != nil {
		t.Fatal(err)
	}
	if _, err := HoldsDigest(high.Holds); err != nil {
		t.Fatalf("a hand-off the format carries has no digest: %v", err)
	}
}
