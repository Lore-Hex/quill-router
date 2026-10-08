package record

import (
	"bytes"
	"reflect"
	"strings"
	"testing"
	"time"
)

var (
	deadline = time.Date(2026, 10, 8, 2, 20, 0, 0, time.UTC)
	digest   = bytes.Repeat([]byte{0xab}, 32)
	snapshot = &Snapshot{GatewaySeq: 3, Hash: []byte{1, 2, 3}, Usage: []byte(`{"out":40}`), Running: 120, Deadline: deadline}
)

// one is a valid record of each kind.
func one() map[Kind]Record {
	base := func(k Kind, seq int64) Record {
		return Record{Version: Version, Lease: "L0oJBwYFBAMCAQ0ODw4ODw", Epoch: 2, Seq: seq, Kind: k}
	}
	hb := base(Heartbeat, 1)
	hb.Auth, hb.Estimate, hb.Snapshot, hb.Basis = "gwa-1", 500, snapshot, []byte(`{"model":"m"}`)
	settle := base(Settle, 2)
	settle.Auth, settle.Estimate, settle.Charge, settle.Shortfall, settle.Digest = "gwa-1", 500, 640, 140, digest
	adopted := base(Refund, 3)
	adopted.Auth, adopted.Estimate, adopted.Shortfall, adopted.Drain = "gwa-2", 300, 140, "d-7"
	reap := base(Reap, 4)
	reap.Auth, reap.Estimate, reap.Charge, reap.Shortfall, reap.Digest, reap.SnapshotSeq = "gwa-3", 500, 120, 140, digest, 1
	release := base(Release, 5)
	release.Auth, release.Estimate, release.Shortfall = "gwa-4", 200, 140
	ckpt := base(Checkpoint, 6)
	ckpt.Checkpoint = &CheckpointOf{Consumed: 760, Open: 2, OpenSum: 800, LatestEnd: deadline, KeyStatus: 9, Return: 50}
	handoff := base(Handoff, 7)
	handoff.Holds = []HeldHold{{Auth: "gwa-5", Estimate: 500, Deadline: deadline, Snapshot: snapshot, SnapshotSeq: 1,
		Basis: []byte(`{"model":"m"}`)},
		{Auth: "gwa-6", Estimate: 300, Deadline: deadline}}
	sum, err := HoldsDigest(handoff.Holds)
	if err != nil {
		panic(err)
	}
	manifest := base(Manifest, 8)
	manifest.Manifest = &ManifestOf{Chunks: 1, HoldsDigest: sum, Seqs: []int64{7}}
	return map[Kind]Record{Heartbeat: hb, Settle: settle, Refund: adopted, Reap: reap, Release: release,
		Checkpoint: ckpt, Handoff: handoff, Manifest: manifest,
		Tick: {Version: Version, Lease: "L0oJBwYFBAMCAQ0ODw4ODw", Kind: Tick}}
}

func TestEachKindRoundTrips(t *testing.T) {
	for kind, r := range one() {
		b, err := Encode(r)
		if err != nil {
			t.Fatalf("%s: %v", kind, err)
		}
		got, err := Decode(b)
		if err != nil || !reflect.DeepEqual(got, r) {
			t.Fatalf("%s came back as %+v, %v", kind, got, err)
		}
	}
	if !Settle.Terminal() || !Reap.Terminal() || Heartbeat.Terminal() || Tick.Terminal() || Checkpoint.Terminal() {
		t.Fatal("Terminal")
	}
}

// TestTheFormatIsPinned: the auditor of another release reads these bytes.
func TestTheFormatIsPinned(t *testing.T) {
	for kind, want := range map[Kind]string{
		Settle: `{"v":1,"lease":"L0oJBwYFBAMCAQ0ODw4ODw","epoch":2,"seq":2,"kind":"settle","a":"gwa-1","est":500,` +
			`"charge":640,"sf":140,"digest":"q6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6s="}`,
		Tick: `{"v":1,"lease":"L0oJBwYFBAMCAQ0ODw4ODw","kind":"tick"}`,
		Heartbeat: `{"v":1,"lease":"L0oJBwYFBAMCAQ0ODw4ODw","epoch":2,"seq":1,"kind":"hb","a":"gwa-1","est":500,` +
			`"basis":"eyJtb2RlbCI6Im0ifQ==","hb":{"gseq":3,"hash":"AQID","usage":"eyJvdXQiOjQwfQ==","run":120,` +
			`"deadline":"2026-10-08T02:20:00Z"}}`,
	} {
		got, err := Encode(one()[kind])
		if err != nil || string(got) != want {
			t.Errorf("%s encodes as\n%s, %v; want\n%s", kind, got, err, want)
		}
	}
}

func TestRecordsTheirKindsRefuse(t *testing.T) {
	valid := one()
	change := func(k Kind, f func(*Record)) Record {
		r := valid[k]
		r.Snapshot, r.Checkpoint, r.Manifest = clone(r.Snapshot), cloneCkpt(r.Checkpoint), cloneManifest(r.Manifest)
		r.Holds = append([]HeldHold(nil), r.Holds...)
		f(&r)
		return r
	}
	for name, r := range map[string]Record{
		"another version":                  change(Settle, func(r *Record) { r.Version = 2 }),
		"no lease":                         change(Settle, func(r *Record) { r.Lease = "" }),
		"no such kind":                     change(Settle, func(r *Record) { r.Kind = "bonus" }),
		"an owner record without a seq":    change(Settle, func(r *Record) { r.Seq = 0 }),
		"an owner record without epoch":    change(Refund, func(r *Record) { r.Epoch = 0 }),
		"a tick with a seq":                change(Tick, func(r *Record) { r.Seq = 1 }),
		"a tick with an authorization":     change(Tick, func(r *Record) { r.Auth = "gwa-1" }),
		"a settle with no authorization":   change(Settle, func(r *Record) { r.Auth = "" }),
		"a settle with no digest":          change(Settle, func(r *Record) { r.Digest = nil }),
		"a negative charge":                change(Settle, func(r *Record) { r.Charge = -1 }),
		"a negative shortfall":             change(Settle, func(r *Record) { r.Shortfall = -1 }),
		"a refund that charges":            change(Refund, func(r *Record) { r.Charge = 1 }),
		"a release that adopts":            change(Release, func(r *Record) { r.Drain = "d-1" }),
		"a reap above its hold":            change(Reap, func(r *Record) { r.Charge = 501 }),
		"a reap with no snapshot":          change(Reap, func(r *Record) { r.SnapshotSeq = 0 }),
		"a reap that adopts":               change(Reap, func(r *Record) { r.Drain = "d-1" }),
		"a heartbeat over its cap":         change(Heartbeat, func(r *Record) { r.Snapshot.Running = 501 }),
		"a heartbeat with no hash":         change(Heartbeat, func(r *Record) { r.Snapshot.Hash = nil }),
		"a heartbeat with a shortfall":     change(Heartbeat, func(r *Record) { r.Shortfall = 1 }),
		"a heartbeat with a charge":        change(Heartbeat, func(r *Record) { r.Charge = 1 }),
		"a checkpoint with an auth":        change(Checkpoint, func(r *Record) { r.Auth = "gwa-1" }),
		"a final checkpoint with holds":    change(Checkpoint, func(r *Record) { r.Checkpoint.Final = true }),
		"open holds with no end of life":   change(Checkpoint, func(r *Record) { r.Checkpoint.LatestEnd = time.Time{}; r.Checkpoint.OpenSum = 0 }),
		"no open holds with a sum":         change(Checkpoint, func(r *Record) { r.Checkpoint.Open = 0 }),
		"a hand-off naming a hold twice":   change(Handoff, func(r *Record) { r.Holds[1].Auth = r.Holds[0].Auth }),
		"a held snapshot without its seq":  change(Handoff, func(r *Record) { r.Holds[0].SnapshotSeq = 0 }),
		"a manifest's seqs out of order":   change(Manifest, func(r *Record) { r.Manifest.Chunks, r.Manifest.Seqs = 2, []int64{7, 6} }),
		"a manifest's seqs miscounted":     change(Manifest, func(r *Record) { r.Manifest.Chunks = 2 }),
		"a manifest with a short digest":   change(Manifest, func(r *Record) { r.Manifest.HoldsDigest = []byte{1} }),
		"a checkpoint carrying a hand-off": change(Checkpoint, func(r *Record) { r.Holds = valid[Handoff].Holds }),
		"a settle carrying a reap's basis": change(Settle, func(r *Record) { r.Basis = []byte("x") }),
	} {
		if _, err := Encode(r); err == nil {
			t.Errorf("%s is encoded", name)
		}
	}
	for name, raw := range map[string]string{
		"an unknown field": `{"v":1,"lease":"l","epoch":1,"seq":1,"kind":"refund","a":"gwa-1","bonus":1}`,
		"two values":       `{"v":1,"lease":"l","kind":"tick"}{"v":1,"lease":"l","kind":"tick"}`,
		"not a record":     `[1,2]`,
	} {
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
	a, _ := HoldsDigest(holds)
	b, _ := HoldsDigest([]HeldHold{holds[1], holds[0]})
	c, _ := HoldsDigest(holds[:1])
	if !bytes.Equal(a, b) || bytes.Equal(a, c) || len(a) != 32 {
		t.Fatalf("digests %x, %x, %x", a, b, c)
	}
}

func clone(s *Snapshot) *Snapshot {
	if s == nil {
		return nil
	}
	c := *s
	return &c
}

func cloneCkpt(c *CheckpointOf) *CheckpointOf {
	if c == nil {
		return nil
	}
	out := *c
	return &out
}

func cloneManifest(m *ManifestOf) *ManifestOf {
	if m == nil {
		return nil
	}
	out := *m
	out.Seqs = append([]int64(nil), m.Seqs...)
	return &out
}
