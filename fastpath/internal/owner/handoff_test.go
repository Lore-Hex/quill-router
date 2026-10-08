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

	// A first heartbeat issued, its acknowledgement not come by the
	// allowance and the grace: the stream is not released.
	h, _ := releaseFixture(t)
	issued := func() string {
		a, err := h.lease.Admit(Admission{Estimate: 40, Stream: true, Boot: boot, OpenHeartbeat: true})
		if err != nil {
			t.Fatal(err)
		}
		return a.Auth
	}()
	h.log.hold()
	answered := make(chan error, 1)
	go func() {
		_, err := h.lease.Heartbeat(ctx, issued, HeartbeatOf{GatewaySeq: 1, Hash: sum("h1"), Usage: 1, Running: 1,
			Basis: []byte("terms")})
		answered <- err
	}()
	waitFor(t, "the heartbeat's record", func() bool { return len(h.log.records(t, "lease-1")) == 1 })
	h.clock.advance(70 * time.Second)
	reapPass(t, h)
	if ts := terminals(t, h); len(ts) != 0 {
		t.Fatalf("a release of a stream whose heartbeat was issued: %+v", ts)
	}
	h.log.letGo()
	<-answered

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
// over the lease issues no record, a decision, a heartbeat's or a
// checkpoint's, though its acknowledgement has not come;
// a hand-off whose manifest is not acknowledged by its deadline lets the
// lease go unmarked, and the auditor drains it once it expires.
func TestAHandOffDecidesNothingAfterItsManifest(t *testing.T) {
	f, sp := releaseFixture(t)
	a := f.admit(t, 40, false)
	s := f.admit(t, 30, true)
	first, err := f.lease.Heartbeat(context.Background(), s, HeartbeatOf{GatewaySeq: 1, Hash: sum("h1"), Usage: 1,
		Running: 1, Basis: []byte("terms")})
	if err != nil {
		t.Fatal(err)
	}
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
	issued := len(f.log.records(t, "lease-1"))
	if _, err := f.lease.Refund(context.Background(), a); !errors.Is(err, ErrPastCutoff) {
		t.Fatalf("a refund after the manifest: %v", err)
	}
	if _, err := f.lease.Heartbeat(context.Background(), s, HeartbeatOf{GatewaySeq: 2, Hash: sum("h2"), Usage: 2,
		Running: 2, Echoed: first}); !errors.Is(err, ErrRetry) {
		t.Fatalf("a heartbeat after the manifest: %v", err)
	}
	if final := f.lease.checkpoint(); final != nil {
		t.Fatal("a final checkpoint after the manifest")
	}
	if n := len(f.log.records(t, "lease-1")); n != issued {
		t.Fatalf("%d records issued after the manifest", n-issued)
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

// TestAHandOffTakesNoNewLease: from a forced exit's start the owner takes
// no lease, so none admits during it or is held after.
func TestAHandOffTakesNoNewLease(t *testing.T) {
	f, _ := releaseFixture(t)
	f.admit(t, 40, false)
	f.log.hold()
	done := make(chan error, 1)
	go func() { done <- f.owner.Handoff(context.Background()) }()
	waitFor(t, "the manifest", func() bool { _, m := handoffRecords(t, f); return m != nil })
	if _, err := f.owner.Take("lease-2", "ws-1", 100, start.Add(10*time.Minute)); err == nil {
		t.Fatal("a lease taken during a hand-off")
	}
	f.log.letGo()
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	if _, ok := f.owner.Lease("lease-2"); ok {
		t.Fatal("a lease held after a hand-off")
	}
}

// TestADrainWaitsForItsManifest: a lease is marked draining only once its
// manifest is acknowledged: not when it handed none over, past its cutoff,
// nor at a second hand-off's call, which waits for the first.
func TestADrainWaitsForItsManifest(t *testing.T) {
	f, sp := releaseFixture(t)
	f.admit(t, 40, false)
	f.clock.advance(time.Hour) // past the lease's cutoff
	if err := f.owner.Handoff(context.Background()); err != nil {
		t.Fatal(err)
	}
	if _, m := handoffRecords(t, f); m != nil {
		t.Fatalf("a manifest past the cutoff: %+v", m)
	}
	if _, _, _, drained := sp.state(); len(drained) != 0 {
		t.Fatalf("a draining write with no manifest: %v", drained)
	}
	if _, ok := f.owner.Lease("lease-1"); ok {
		t.Fatal("the lease held after its hand-off")
	}

	g, gp := releaseFixture(t)
	g.admit(t, 40, false)
	g.log.hold()
	first, second := make(chan error, 1), make(chan error, 1)
	go func() { first <- g.owner.Handoff(context.Background()) }()
	waitFor(t, "the manifest", func() bool { _, m := handoffRecords(t, g); return m != nil })
	go func() { second <- g.owner.Handoff(context.Background()) }()
	time.Sleep(50 * time.Millisecond)
	if _, _, _, drained := gp.state(); len(drained) != 0 {
		t.Fatalf("a draining write before the manifest's acknowledgement: %v", drained)
	}
	select {
	case err := <-second:
		t.Fatalf("a second hand-off returned before the first: %v", err)
	default:
	}
	g.log.letGo()
	if err := <-first; err != nil {
		t.Fatal(err)
	}
	if err := <-second; err != nil {
		t.Fatal(err)
	}
	if _, _, _, drained := gp.state(); !slices.Equal(drained, []string{"lease-1"}) {
		t.Fatalf("the draining writes: %v", drained)
	}
}

// TestStopEndsAHandOff: Stop ends a hand-off's draining write under way,
// and returns once it has.
func TestStopEndsAHandOff(t *testing.T) {
	f, sp := releaseFixture(t)
	f.admit(t, 40, false)
	sp.mu.Lock()
	sp.drainGate, sp.afterCancel, sp.drainReturned = make(chan struct{}), 150*time.Millisecond, make(chan struct{})
	sp.mu.Unlock()
	done := make(chan error, 1)
	go func() { done <- f.owner.Handoff(context.Background()) }()
	waitFor(t, "the manifest", func() bool { _, m := handoffRecords(t, f); return m != nil })
	time.Sleep(50 * time.Millisecond) // the draining write under way
	stopped := make(chan struct{})
	go func() {
		f.owner.Stop()
		close(stopped)
	}()
	select {
	case <-stopped:
	case <-time.After(10 * time.Second):
		t.Fatal("Stop did not return")
	}
	select {
	case <-sp.drainReturned:
	default:
		t.Fatal("Stop returned while a draining write ran")
	}
	select {
	case err := <-done:
		if err == nil {
			t.Fatal("a hand-off Stop ended reported none")
		}
	case <-time.After(10 * time.Second):
		t.Fatal("the hand-off did not end")
	}
}

// TestAChunkIsSizedAtItsOwnSequence: each hand-off record is sized at the
// sequence number it takes: two holds that fit one record at the first
// chunk's number, but not at the next, go in two.
func TestAChunkIsSizedAtItsOwnSequence(t *testing.T) {
	f, _ := releaseFixture(t)
	f.lease.mu.Lock()
	f.lease.allocation = 1 << 40
	f.lease.mu.Unlock()
	auths := []string{f.admit(t, 1, false), f.admit(t, 1, false), f.admit(t, 1, false)}
	slices.Sort(auths)
	f.lease.mu.Lock()
	f.lease.nextSeq = 9 // the first chunk takes 9, the second 10
	held := func(a string, bootLen int, estimate int64) record.HeldHold {
		h := f.lease.holds[a]
		h.boot, h.estimate = bytes.Repeat([]byte{'b'}, bootLen), estimate
		return record.HeldHold{Auth: a, Estimate: estimate, Deadline: h.endOfLife.UTC(), Boot: h.boot}
	}
	size := func(seq int64, holds ...record.HeldHold) int {
		data, err := record.Encode(record.Record{Version: record.Version, Lease: f.lease.id, Epoch: f.lease.o.cfg.Epoch,
			Seq: seq, Kind: record.Handoff, Holds: holds})
		if err != nil {
			t.Fatal(err)
		}
		return len(data)
	}
	held(auths[0], 700_000, 1) // a chunk alone
	b1 := held(auths[1], 300_000, 1)
	// The second and third together are exactly a record's size at 9: the
	// boot moves it four bytes at a time, the estimate's digits one.
	n, e := 300_000, int64(1)
	for {
		d := maxRecord - size(9, b1, held(auths[2], n, e))
		if d == 0 {
			break
		}
		switch {
		case d < 0:
			n -= 3 * ((-d + 3) / 4)
		case d >= 4:
			n += 3 * (d / 4)
		default:
			e *= 10
		}
	}
	b2 := held(auths[2], n, e)
	f.lease.mu.Unlock()
	if size(10, b1, b2) <= maxRecord {
		t.Fatal("the second and third fit one record at 10")
	}
	if err := f.owner.Handoff(context.Background()); err != nil {
		t.Fatal(err)
	}
	chunks, manifest := handoffRecords(t, f)
	if manifest == nil || len(chunks) != 3 {
		t.Fatalf("%d chunks, manifest %+v", len(chunks), manifest)
	}
	for i, c := range chunks {
		data, err := record.Encode(c)
		if err != nil || len(data) > maxRecord || len(c.Holds) != 1 || c.Holds[0].Auth != auths[i] || c.Seq != int64(9+i) {
			t.Fatalf("chunk %d at %d: %d bytes, %d holds, %v", i, c.Seq, len(data), len(c.Holds), err)
		}
	}
}
