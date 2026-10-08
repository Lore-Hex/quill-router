package owner

import (
	"bytes"
	"context"
	"errors"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/record"
)

// releaseFixture is an owner with a store and the record topic, a grace of
// a minute and a first-heartbeat allowance of ten seconds.
func releaseFixture(t *testing.T) (*fixture, *fakeSpanner) {
	t.Helper()
	sp := newFakeSpanner()
	sp.expiry = start.Add(10 * time.Minute)
	f := newFixture(t, 1000, nil, func(c *Config) {
		c.Spanner, c.Node, c.RenewEvery, c.Window, c.KeyStatus = sp, "owner-1", time.Hour, 30*time.Second, 7
		c.Grace, c.Records, c.FirstHeartbeat = time.Minute, &fakeRecords{}, 10*time.Second
	})
	renew(t, f)
	return f, sp
}

func reapPass(t *testing.T, f *fixture) {
	t.Helper()
	if err := f.owner.Reap(context.Background()); err != nil {
		t.Fatal(err)
	}
}

// TestAStreamWithNoHeartbeatIsReleased: a stream's hold whose boot declares
// the heartbeat at stream open, and for which none was issued by its
// admission plus the allowance plus the grace, is released uncharged, its
// record naming its boot binding; not a microsecond sooner, and no hold
// that heartbeated, does not stream, or whose boot declares nothing
// (§4.5, TerminalOrder's OwnerRelease).
func TestAStreamWithNoHeartbeatIsReleased(t *testing.T) {
	f, _ := releaseFixture(t)
	ctx := context.Background()
	admit := func(e int64, stream, open bool) string {
		t.Helper()
		a, err := f.lease.Admit(Admission{Estimate: e, Stream: stream, Boot: boot, OpenHeartbeat: open})
		if err != nil {
			t.Fatal(err)
		}
		return a.Auth
	}
	silent := admit(40, true, true)
	beat := admit(30, true, true)
	plain := admit(20, false, true)
	undeclared := admit(10, true, false)
	if _, err := f.lease.Heartbeat(ctx, beat, HeartbeatOf{GatewaySeq: 1, Hash: sum("h1"), Usage: 1, Running: 1,
		Basis: []byte("terms")}); err != nil {
		t.Fatal(err)
	}
	f.clock.advance(70*time.Second - time.Microsecond)
	reapPass(t, f)
	if ts := terminals(t, f); len(ts) != 0 {
		t.Fatalf("a release before its allowance and grace passed: %+v", ts)
	}
	before := f.lease.Books()
	f.clock.advance(time.Microsecond)
	reapPass(t, f)
	ts := terminals(t, f)
	if len(ts) != 1 || ts[0].Kind != record.Release || ts[0].Auth != silent || ts[0].Charge != 0 ||
		!bytes.Equal(ts[0].Boot, boot) {
		t.Fatalf("the releases: %+v; of %s, and not %s, %s or %s", ts, silent, beat, plain, undeclared)
	}
	if b := f.lease.Books(); b.Held != before.Held-40 || b.Consumed != before.Consumed || b.Open != before.Open-1 {
		t.Fatalf("the books after the release: %+v, before %+v", b, before)
	}

	g, _ := releaseFixture(t)
	g.owner.cfg.FirstHeartbeat = 0
	if _, err := g.lease.Admit(Admission{Estimate: 40, Stream: true, Boot: boot, OpenHeartbeat: true}); err != nil {
		t.Fatal(err)
	}
	g.clock.advance(70 * time.Second) // the grace and more, within the lease's cutoff
	reapPass(t, g)
	if ts := terminals(t, g); len(ts) != 0 {
		t.Fatalf("a release with no allowance: %+v", ts)
	}
}

// handoffRecords are the lease's hand-off records and its manifest.
func handoffRecords(t *testing.T, f *fixture) (chunks []record.Record, manifest *record.Record) {
	t.Helper()
	for _, r := range f.log.records(t, "lease-1") {
		switch r.Kind {
		case record.Handoff:
			chunks = append(chunks, r)
		case record.Manifest:
			manifest = &r
		}
	}
	return chunks, manifest
}

// TestAForcedExitHandsOffItsOpenHolds: a forced exit stops admitting, lists
// its open holds in a hand-off, a heartbeated stream's with its last
// snapshot, the record that carried it and its basis, another's with its
// end of life, then the manifest naming the chunk and their holds' digest;
// then it marks the lease draining and lets it go (§4.2).
func TestAForcedExitHandsOffItsOpenHolds(t *testing.T) {
	f, sp := releaseFixture(t)
	ctx := context.Background()
	a := f.admit(t, 40, true)
	granted, err := f.lease.Heartbeat(ctx, a, HeartbeatOf{GatewaySeq: 2, Hash: sum("h2"), Usage: 5, Running: 7,
		Basis: []byte("terms")})
	if err != nil {
		t.Fatal(err)
	}
	var hbSeq int64
	for _, r := range f.log.records(t, "lease-1") {
		if r.Kind == record.Heartbeat {
			hbSeq = r.Seq
		}
	}
	b := f.admit(t, 20, false)
	if err := f.owner.Handoff(ctx); err != nil {
		t.Fatal(err)
	}
	chunks, manifest := handoffRecords(t, f)
	if len(chunks) != 1 || manifest == nil {
		t.Fatalf("the hand-off: %d chunks, manifest %+v", len(chunks), manifest)
	}
	want := []record.HeldHold{
		{Auth: a, Estimate: 40, Deadline: granted.UTC(), Boot: boot,
			Snapshot: &record.Snapshot{GatewaySeq: 2, Hash: sum("h2"), Usage: []byte(`{"tokens":5}`), Running: 7,
				Deadline: granted.UTC()}, SnapshotSeq: hbSeq, Basis: []byte("terms")},
		{Auth: b, Estimate: 20, Deadline: start.Add(time.Hour), Boot: boot},
	}
	slices.SortFunc(want, func(x, y record.HeldHold) int { return strings.Compare(x.Auth, y.Auth) })
	got := chunks[0].Holds
	if len(got) != 2 {
		t.Fatalf("the chunk's holds: %+v", got)
	}
	for i := range want {
		g, w := got[i], want[i]
		if g.Auth != w.Auth || g.Estimate != w.Estimate || !g.Deadline.Equal(w.Deadline) || !bytes.Equal(g.Boot, w.Boot) ||
			g.SnapshotSeq != w.SnapshotSeq || !bytes.Equal(g.Basis, w.Basis) || (g.Snapshot == nil) != (w.Snapshot == nil) ||
			(g.Snapshot != nil && (g.Snapshot.GatewaySeq != w.Snapshot.GatewaySeq || !bytes.Equal(g.Snapshot.Hash, w.Snapshot.Hash) ||
				string(g.Snapshot.Usage) != string(w.Snapshot.Usage) || g.Snapshot.Running != w.Snapshot.Running ||
				!g.Snapshot.Deadline.Equal(w.Snapshot.Deadline))) {
			t.Fatalf("held hold %d: %+v (snapshot %+v), want %+v (snapshot %+v)", i, g, g.Snapshot, w, w.Snapshot)
		}
	}
	digest, err := record.HoldsDigest(got)
	if err != nil {
		t.Fatal(err)
	}
	if m := manifest.Manifest; m.Chunks != 1 || !slices.Equal(m.Seqs, []int64{chunks[0].Seq}) ||
		!bytes.Equal(m.HoldsDigest, digest) || manifest.Seq <= chunks[0].Seq {
		t.Fatalf("the manifest %+v at %d, the chunk at %d", m, manifest.Seq, chunks[0].Seq)
	}
	if _, _, _, drained := sp.state(); !slices.Equal(drained, []string{"lease-1"}) {
		t.Fatalf("the draining writes: %v", drained)
	}
	if _, ok := f.owner.Lease("lease-1"); ok {
		t.Fatal("the lease is held after its hand-off")
	}
}

// TestAHandOffDecidesNothingAfterItsManifest: once the manifest is handed
// over the lease decides nothing, though its acknowledgement has not come;
// a hand-off whose manifest is not acknowledged by its deadline lets the
// lease go unmarked, and the auditor drains it once it expires.
func TestAHandOffDecidesNothingAfterItsManifest(t *testing.T) {
	f, sp := releaseFixture(t)
	a := f.admit(t, 40, false)
	f.log.hold()
	ctx, cancel := context.WithTimeout(context.Background(), 300*time.Millisecond)
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- f.owner.Handoff(ctx) }()
	for {
		if _, m := handoffRecords(t, f); m != nil {
			break
		}
		time.Sleep(time.Millisecond)
	}
	if _, err := f.lease.Refund(context.Background(), a); !errors.Is(err, ErrPastCutoff) {
		t.Fatalf("a refund after the manifest: %v", err)
	}
	if ts := terminals(t, f); len(ts) != 0 {
		t.Fatalf("a terminal decided after the manifest: %+v", ts)
	}
	if _, err := f.lease.Admit(Admission{Estimate: 1, Boot: boot}); err == nil {
		t.Fatal("an admission during a hand-off")
	}
	if err := <-done; !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("a hand-off past its deadline: %v", err)
	}
	f.log.letGo()
	if _, _, _, drained := sp.state(); len(drained) != 0 {
		t.Fatalf("a draining write past the hand-off's deadline: %v", drained)
	}
	if _, ok := f.owner.Lease("lease-1"); ok {
		t.Fatal("the lease is held after its hand-off's deadline")
	}
}

// TestAHandOffIsChunkedUnderTheRecordSize: holds too many for one record
// go in chunks each the settle log takes, and the manifest names them all.
func TestAHandOffIsChunkedUnderTheRecordSize(t *testing.T) {
	f, _ := releaseFixture(t)
	f.lease.mu.Lock()
	f.lease.allocation = 1 << 40
	f.lease.mu.Unlock()
	ctx := context.Background()
	var auths []string
	for i := range 12 {
		a := f.admit(t, 1, true)
		basis := bytes.Repeat([]byte{byte('a' + i)}, 200_000)
		if _, err := f.lease.Heartbeat(ctx, a, HeartbeatOf{GatewaySeq: 1, Hash: sum(a), Usage: 1, Running: 1,
			Basis: basis}); err != nil {
			t.Fatal(err)
		}
		auths = append(auths, a)
	}
	if err := f.owner.Handoff(ctx); err != nil {
		t.Fatal(err)
	}
	chunks, manifest := handoffRecords(t, f)
	var seqs []int64
	var listed []string
	for _, c := range chunks {
		data, err := record.Encode(c)
		if err != nil || len(data) > maxRecord {
			t.Fatalf("a chunk of %d bytes: %v", len(data), err)
		}
		seqs = append(seqs, c.Seq)
		for _, h := range c.Holds {
			listed = append(listed, h.Auth)
		}
	}
	slices.Sort(auths)
	if len(chunks) < 3 || manifest == nil || !slices.Equal(manifest.Manifest.Seqs, seqs) ||
		manifest.Manifest.Chunks != len(chunks) || !slices.Equal(listed, auths) {
		t.Fatalf("%d chunks listing %v, manifest %+v", len(chunks), listed, manifest)
	}
}
