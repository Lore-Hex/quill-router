package owner

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"maps"
	"math"
	"math/rand/v2"
	"runtime"
	"slices"
	"strings"
	"sync"
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
	waitFor(t, "the heartbeat's record", func() bool {
		for _, r := range h.log.records(t, "lease-1") {
			if r.Kind == record.Heartbeat && r.Auth == issued {
				return true
			}
		}
		return false
	})
	select {
	case err := <-answered:
		t.Fatalf("the heartbeat answered with its acknowledgement held: %v", err)
	default:
	}
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

// TestAnExitingOwnerAsksForNoLease: once a forced exit starts, a shard
// whose leases take no request asks Spanner for none.
func TestAnExitingOwnerAsksForNoLease(t *testing.T) {
	f, sp := shardFixture(t)
	if _, err := f.owner.Admit(key, Admission{Estimate: 5, Boot: boot}); !errors.Is(err, ErrNoRoom) {
		t.Fatalf("the first request: %v", err)
	}
	ls := f.waitLeases(t, 1)
	f.log.hold()
	done := make(chan error, 1)
	go func() { done <- f.owner.Handoff(context.Background()) }()
	waitFor(t, "the manifest", func() bool {
		for _, r := range f.log.records(t, ls[0].id) {
			if r.Kind == record.Manifest {
				return true
			}
		}
		return false
	})
	for range 3 {
		f.clock.advance(time.Minute) // past the cooldown
		if _, err := f.owner.Admit(key, Admission{Estimate: 5, Boot: boot}); err == nil {
			t.Fatal("a request admitted during a hand-off")
		}
	}
	time.Sleep(50 * time.Millisecond)
	if grants := sp.granted(); len(grants) != 1 {
		t.Fatalf("%d grants asked for during a hand-off", len(grants)-1)
	}
	f.log.letGo()
	if err := <-done; err != nil {
		t.Fatal(err)
	}
}

// heavyFixture is a lease with n streams, each heartbeated once with a
// basis of size bytes.
func heavyFixture(t *testing.T, n, size int) *fixture {
	t.Helper()
	f, _ := releaseFixture(t)
	f.lease.mu.Lock()
	f.lease.allocation = 1 << 40
	f.lease.mu.Unlock()
	basis := bytes.Repeat([]byte{'b'}, size)
	for range n {
		a := f.admit(t, 1, true)
		if _, err := f.lease.Heartbeat(context.Background(), a, HeartbeatOf{GatewaySeq: 1, Hash: sum(a), Usage: 1,
			Running: 1, Basis: basis}); err != nil {
			t.Fatal(err)
		}
	}
	return f
}

// TestAHandOffKeepsToItsDeadline: one whose time is up hands nothing over,
// no manifest even with no holds, and lets its leases go at once, however
// much its holds would take to encode; one whose time runs out as it hands
// its chunks over hands no more over; and many holds are sized in time in
// step with them.
func TestAHandOffKeepsToItsDeadline(t *testing.T) {
	ended, cancel := context.WithCancel(context.Background())
	cancel()
	for name, f := range map[string]*fixture{
		"8,000 holds":                       heavyFixture(t, 0, 0),
		"256 holds of a 128 KiB basis each": heavyFixture(t, 256, 128<<10),
		"no hold":                           heavyFixture(t, 0, 0),
	} {
		if name == "8,000 holds" {
			for range 8000 {
				f.admit(t, 1, false)
			}
		}
		began := time.Now()
		if err := f.owner.Handoff(ended); !errors.Is(err, context.Canceled) {
			t.Fatalf("%s: a hand-off whose time was up: %v", name, err)
		}
		took := time.Since(began)
		if chunks, m := handoffRecords(t, f); len(chunks) != 0 || m != nil || took > 200*time.Millisecond {
			t.Fatalf("%s: %d chunks, manifest %v, after %v", name, len(chunks), m != nil, took)
		}
		if _, ok := f.owner.Lease("lease-1"); ok {
			t.Fatalf("%s: the lease held after its hand-off", name)
		}
	}

	// Time that runs out as the first chunk is handed over hands no more
	// over.
	f := heavyFixture(t, 12, 200_000)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	f.log.mu.Lock()
	f.log.onPublish = func(data []byte) {
		if bytes.Contains(data, []byte(`"kind":"handoff"`)) {
			cancel()
		}
	}
	f.log.mu.Unlock()
	if err := f.owner.Handoff(ctx); !errors.Is(err, context.Canceled) {
		t.Fatalf("a hand-off whose time ran out: %v", err)
	}
	if chunks, m := handoffRecords(t, f); len(chunks) != 1 || m != nil {
		t.Fatalf("%d chunks, manifest %v", len(chunks), m != nil)
	}

	g, _ := releaseFixture(t)
	g.lease.mu.Lock()
	g.lease.allocation = 1 << 40
	g.lease.mu.Unlock()
	for range 8000 {
		g.admit(t, 1, false)
	}
	began := time.Now()
	if err := g.owner.Handoff(context.Background()); err != nil {
		t.Fatal(err)
	}
	if chunks, m := handoffRecords(t, g); len(chunks) == 0 || m == nil || time.Since(began) > 2*time.Second {
		t.Fatalf("8,000 holds handed off in %d chunks, manifest %v, in %v", len(chunks), m != nil, time.Since(began))
	}
}

// TestEveryHeartbeatedHoldCanBeHandedOff: the owner takes no heartbeat
// whose hold, its boot binding at its longest and its later snapshots'
// numbers at theirs, would not fit a hand-off record alone; so a forced
// exit hands every hold over, each in a chunk the log takes (§4.2). A hold
// the log could not take all the same is reported, not passed over.
func TestEveryHeartbeatedHoldCanBeHandedOff(t *testing.T) {
	f, _ := releaseFixture(t)
	f.lease.mu.Lock()
	f.lease.allocation = 1 << 62
	f.lease.mu.Unlock()
	ctx := context.Background()
	longBoot := bytes.Repeat([]byte{0xff}, maxBoot)
	admit := func() string {
		t.Helper()
		a, err := f.lease.Admit(Admission{Estimate: 1 << 55, Stream: true, Boot: longBoot})
		if err != nil {
			t.Fatal(err)
		}
		return a.Auth
	}
	// The largest basis a first heartbeat may carry, found by halving: a
	// heartbeat refused changes nothing, and one taken leaves its hold for
	// the hand-off, so the next try takes a new hold.
	auth := admit()
	var taken []string
	lo, hi := 1, maxRecord
	for hi-lo > 1 {
		mid := (lo + hi) / 2
		_, err := f.lease.Heartbeat(ctx, auth, HeartbeatOf{GatewaySeq: 1, Hash: sum(auth), Usage: 1, Running: 1,
			Basis: bytes.Repeat([]byte{'b'}, mid)})
		switch {
		case err == nil:
			lo = mid
			taken = append(taken, auth)
			auth = admit()
		case errors.Is(err, ErrRejected):
			hi = mid
		default:
			t.Fatal(err)
		}
	}
	largest := 0
	for _, r := range f.log.records(t, "lease-1") {
		if r.Kind == record.Heartbeat {
			data, err := record.Encode(r)
			if err != nil {
				t.Fatal(err)
			}
			largest = max(largest, len(data))
		}
	}
	if largest > maxHeartbeat || largest < maxHeartbeat-8 {
		t.Fatalf("the largest heartbeat record taken is %d bytes, and the bound %d", largest, maxHeartbeat)
	}
	// Each hold's later snapshot carries every number at its longest.
	for _, a := range taken {
		h := f.lease.holds[a]
		if _, err := f.lease.Heartbeat(ctx, a, HeartbeatOf{GatewaySeq: math.MaxInt64, Hash: sum(a + "!"),
			Usage: math.MaxInt64, Running: 1 << 55, Echoed: h.deadline}); err != nil {
			t.Fatal(err)
		}
	}
	if err := f.owner.Handoff(ctx); err != nil {
		t.Fatal(err)
	}
	chunks, manifest := handoffRecords(t, f)
	var listed []string
	for _, c := range chunks {
		data, err := record.Encode(c)
		if err != nil || len(data) > maxRecord {
			t.Fatalf("a chunk of %d bytes: %v", len(data), err)
		}
		for _, h := range c.Holds {
			listed = append(listed, h.Auth)
		}
	}
	if manifest == nil || len(listed) != len(taken)+1 {
		t.Fatalf("%d holds handed over in %d chunks, manifest %v; %d heartbeated", len(listed), len(chunks),
			manifest != nil, len(taken))
	}

	// A hold the log could not take all the same is reported, and the
	// hand-off hands nothing over.
	g, _ := releaseFixture(t)
	a := g.admit(t, 40, true)
	if _, err := g.lease.Heartbeat(ctx, a, HeartbeatOf{GatewaySeq: 1, Hash: sum(a), Usage: 1, Running: 1,
		Basis: []byte("terms")}); err != nil {
		t.Fatal(err)
	}
	g.lease.mu.Lock()
	g.lease.holds[a].basis = bytes.Repeat([]byte{'b'}, maxRecord)
	g.lease.mu.Unlock()
	if err := g.owner.Handoff(ctx); !errors.Is(err, ErrTooLarge) || !strings.Contains(err.Error(), "lease-1") {
		t.Fatalf("a hold past the record size: %v", err)
	}
	if chunks, m := handoffRecords(t, g); len(chunks) != 0 || m != nil {
		t.Fatalf("%d chunks, manifest %v", len(chunks), m != nil)
	}
	if _, ok := g.owner.Lease("lease-1"); ok {
		t.Fatal("the lease held after its hand-off")
	}
}

// TestAHandOffLetsGoAtItsDeadline: a hand-off whose time is up lets its
// lease go at once, though the draining write it began is still unwinding
// its cancelled call; Stop waits for that write.
func TestAHandOffLetsGoAtItsDeadline(t *testing.T) {
	f, sp := releaseFixture(t)
	f.admit(t, 40, false)
	sp.mu.Lock()
	sp.drainGate, sp.afterCancel, sp.drainReturned = make(chan struct{}), 500*time.Millisecond, make(chan struct{})
	sp.mu.Unlock()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- f.owner.Handoff(ctx) }()
	waitFor(t, "the manifest", func() bool { _, m := handoffRecords(t, f); return m != nil })
	time.Sleep(50 * time.Millisecond) // the draining write under way
	cancel()
	select {
	case err := <-done:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("a hand-off whose time was up: %v", err)
		}
	case <-time.After(400 * time.Millisecond):
		t.Fatal("the hand-off waited for its cancelled draining write")
	}
	if _, ok := f.owner.Lease("lease-1"); ok {
		t.Fatal("the lease held past the hand-off's deadline")
	}
	select {
	case <-sp.drainReturned:
		t.Fatal("the draining write unwound before the hand-off ended")
	default:
	}
	f.owner.Stop()
	select {
	case <-sp.drainReturned:
	default:
		t.Fatal("Stop returned while a draining write ran")
	}
}

// TestASortStopsWhenAsked: the hand-off's gathering of its holds'
// authorizations asks whether to stop before every sortRun of them, and its
// sort before each run it sorts and before every sortRun strings a merge
// writes, the last merge, of all of them, too; each stops at once when told,
// and told nothing, it sorts.
func TestASortStopsWhenAsked(t *testing.T) {
	rng := rand.New(rand.NewPCG(1, 2))
	for _, n := range []int{0, 1, 2, sortRun, sortRun + 1, 10*sortRun + 7} {
		l := &Lease{holds: map[string]*hold{}}
		for len(l.holds) < n {
			auth := fmt.Sprintf("auth-%08x", rng.Uint32())
			l.holds[auth] = &hold{auth: auth}
		}
		want := slices.Sorted(maps.Keys(l.holds))
		gathering := (n + sortRun - 1) / sortRun
		checks := gathering + (n+sortRun-1)/sortRun
		for width := sortRun; width < n; width *= 2 {
			for lo := 0; lo < n; lo += 2 * width {
				checks += (min(lo+2*width, n) - lo + sortRun - 1) / sortRun
			}
		}
		calls := 0
		got, ok := l.sortedAuths(func() bool { calls++; return false })
		if !ok || !slices.Equal(got, want) || calls != checks {
			t.Fatalf("%d holds: sorted %v, %d checks, want %d", n, ok && slices.Equal(got, want), calls, checks)
		}
		for k := 1; k <= checks; k++ {
			calls := 0
			if _, ok := l.sortedAuths(func() bool { calls++; return calls == k }); ok || calls != k {
				t.Fatalf("%d holds told to stop at check %d of %d (%d gathering): done %v after %d checks", n, k,
					checks, gathering, ok, calls)
			}
		}
	}
}

// TestAHandOffInterruptsTheReleasePass: a reaper's pass releasing many holds
// does so a batch at a time, so a hand-off whose time is up takes the
// lease's lock between batches and lets the lease go at once; the pass then
// releases no more.
func TestAHandOffInterruptsTheReleasePass(t *testing.T) {
	f, _ := releaseFixture(t)
	f.lease.mu.Lock()
	f.lease.allocation = 1 << 40
	f.lease.mu.Unlock()
	const holds = 50_000
	for range holds {
		if _, err := f.lease.Admit(Admission{Estimate: 1, Stream: true, Boot: boot, OpenHeartbeat: true}); err != nil {
			t.Fatal(err)
		}
	}
	f.clock.advance(70 * time.Second)
	releasing := make(chan struct{})
	var once sync.Once
	f.log.mu.Lock()
	f.log.onPublish = func(data []byte) {
		if bytes.Contains(data, []byte(`"kind":"release"`)) {
			once.Do(func() { close(releasing) })
		}
	}
	f.log.mu.Unlock()
	passed := make(chan error, 1)
	go func() { passed <- f.owner.Reap(context.Background()) }()
	<-releasing
	ctx, cancel := context.WithTimeout(context.Background(), time.Millisecond)
	defer cancel()
	began := time.Now()
	if err := f.owner.Handoff(ctx); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("a hand-off whose time ran out: %v", err)
	}
	if took := time.Since(began); took > 100*time.Millisecond {
		t.Fatalf("the hand-off let its lease go %v after it began", took)
	}
	if err := <-passed; err != nil {
		t.Fatal(err)
	}
	released := 0
	for _, r := range f.log.records(t, "lease-1") {
		if r.Kind == record.Release {
			released++
		}
	}
	if released == 0 || released >= holds {
		t.Fatalf("%d of %d holds released", released, holds)
	}
}

// TestAHandOffInterruptsAScan: the reaper's scans of a lease's holds, for
// those due to be reaped and those due to be released, let the lease's lock
// go between batches, so a hand-off whose time is up takes it and lets the
// lease go at once, though 100,000 holds are due to be reaped, or 300,000
// released (whose scan, lighter, takes about 100 ms unbatched under the
// race detector); and the scan then stops.
func TestAHandOffInterruptsAScan(t *testing.T) {
	for _, c := range []struct {
		name  string
		holds int
		make  func(holds int) *fixture
		scan  func(f *fixture) (left int)
	}{
		{"the reap scan", 100_000, func(holds int) *fixture {
			f := heavyFixture(t, holds, 16)
			f.clock.advance(5 * time.Minute) // past each heartbeat's deadline and the grace
			return f
		}, func(f *fixture) int { return len(f.lease.due(f.clock.Now(), time.Minute)) }},
		{"the release scan", 300_000, func(holds int) *fixture {
			f, _ := releaseFixture(t)
			f.lease.mu.Lock()
			f.lease.allocation = 1 << 40
			f.lease.mu.Unlock()
			for range holds {
				if _, err := f.lease.Admit(Admission{Estimate: 1, Stream: true, Boot: boot, OpenHeartbeat: true}); err != nil {
					t.Fatal(err)
				}
			}
			f.clock.advance(70 * time.Second) // past the allowance and the grace
			return f
		}, func(f *fixture) int {
			f.lease.releaseDue(f.clock.Now(), 10*time.Second, time.Minute)
			f.lease.mu.Lock()
			defer f.lease.mu.Unlock()
			return len(f.lease.holds)
		}},
	} {
		f := c.make(c.holds)
		scanned := make(chan int, 1)
		go func() { scanned <- c.scan(f) }()
		// The hand-off begins once the scan holds the lease's lock.
		for f.lease.mu.TryLock() {
			f.lease.mu.Unlock()
			runtime.Gosched()
		}
		ctx, cancel := context.WithTimeout(context.Background(), time.Millisecond)
		began := time.Now()
		err := f.owner.Handoff(ctx)
		took := time.Since(began)
		cancel()
		if !errors.Is(err, context.DeadlineExceeded) || took > 50*time.Millisecond {
			t.Fatalf("%s: the hand-off let its lease go %v after it began: %v", c.name, took, err)
		}
		switch left := <-scanned; {
		case c.name == "the reap scan" && left >= c.holds:
			t.Fatalf("%s: the scan found all %d due once the lease was let go", c.name, left)
		case c.name == "the release scan" && left != c.holds:
			t.Fatalf("%s: %d of %d holds left once the lease was let go", c.name, left, c.holds)
		}
	}
}

// TestAHandOffLeavesNoLeaseDeciding: a lease being let go while a decision
// under it is under way stays among the owner's leases until it is let go,
// so a hand-off begun meanwhile returns only once no lease of the owner's
// decides, and no record follows its return.
func TestAHandOffLeavesNoLeaseDeciding(t *testing.T) {
	f, _ := releaseFixture(t)
	a := f.admit(t, 10, false)
	gate, waiting := make(chan struct{}), make(chan struct{})
	open := sync.OnceFunc(func() { close(gate) })
	t.Cleanup(open)
	f.clock.mu.Lock()
	f.clock.gate, f.clock.waiting = gate, waiting
	f.clock.mu.Unlock()
	rctx, rcancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer rcancel()
	refunded := make(chan struct{})
	go func() {
		defer close(refunded)
		_, _ = f.lease.Refund(rctx, a)
	}()
	<-waiting // the refund decides under the lease's lock, at its cutoff's reading
	let := make(chan struct{})
	go func() {
		defer close(let)
		f.owner.Let("lease-1")
	}()
	// The Let is under way: it waits for the lease's lock.
	time.Sleep(50 * time.Millisecond)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	handedOff := make(chan error, 1)
	go func() { handedOff <- f.owner.Handoff(ctx) }()
	select {
	case err := <-handedOff:
		t.Fatalf("the hand-off returned, %v, while a decision under a lease being let go was under way", err)
	case <-time.After(100 * time.Millisecond):
	}
	open()
	for _, done := range []chan struct{}{refunded, let} {
		select {
		case <-done:
		case <-time.After(10 * time.Second):
			t.Fatal("the refund or the Let did not return")
		}
	}
	if err := <-handedOff; err != nil {
		t.Fatal(err)
	}
	issued := len(f.log.records(t, "lease-1"))
	time.Sleep(50 * time.Millisecond)
	if n := len(f.log.records(t, "lease-1")); n != issued {
		t.Fatalf("%d records after the hand-off returned, %d before", n, issued)
	}
}

// TestACheckpointsLatestEndIsItsOpenHoldsLatest: a checkpoint's latest end
// of life is the latest of the holds open at it, as holds are admitted and
// decided, the latest first too, and as the owner's clock steps back.
func TestACheckpointsLatestEndIsItsOpenHoldsLatest(t *testing.T) {
	f, _ := releaseFixture(t)
	ctx := context.Background()
	life := time.Hour // releaseFixture's HoldLife
	latest := func(want time.Time) {
		t.Helper()
		f.lease.checkpoint()
		r := f.lastRecord(t)
		if r.Kind != record.Checkpoint || !r.Checkpoint.LatestEnd.Equal(want) {
			t.Fatalf("the checkpoint's latest end %v, want %v (%+v)", r.Checkpoint.LatestEnd, want, r)
		}
	}
	at := f.clock.Now()
	first := f.admit(t, 10, false)
	f.clock.advance(time.Second)
	second := f.admit(t, 10, false)
	f.clock.advance(time.Second)
	third := f.admit(t, 10, false)
	latest(at.Add(2*time.Second + life))
	if _, err := f.lease.Settle(ctx, third, 5, sum("third")); err != nil {
		t.Fatal(err)
	}
	latest(at.Add(time.Second + life))
	// The clock steps back ten seconds: the next hold ends earliest.
	f.clock.advance(-10 * time.Second)
	fourth := f.admit(t, 10, false)
	latest(at.Add(time.Second + life))
	for _, auth := range []string{second, first} {
		if _, err := f.lease.Settle(ctx, auth, 5, sum(auth)); err != nil {
			t.Fatal(err)
		}
	}
	latest(at.Add(-8*time.Second + life))
	if _, err := f.lease.Settle(ctx, fourth, 5, sum("fourth")); err != nil {
		t.Fatal(err)
	}
	latest(time.Time{})
}

// TestACheckpointTakesItsLatestEndAtOnce: a checkpoint keeps the lease's
// lock no longer for many open holds, 300,000 here, so a hand-off whose
// time is up is not kept waiting for it.
func TestACheckpointTakesItsLatestEndAtOnce(t *testing.T) {
	f, _ := releaseFixture(t)
	f.lease.mu.Lock()
	f.lease.allocation = 1 << 40
	f.lease.mu.Unlock()
	for range 300_000 {
		f.admit(t, 1, false)
	}
	fastest := time.Hour
	for range 5 {
		began := time.Now()
		f.lease.checkpoint()
		fastest = min(fastest, time.Since(began))
	}
	if fastest > 5*time.Millisecond {
		t.Fatalf("a checkpoint over 300,000 open holds took %v", fastest)
	}
}

// TestAHandOffLetsGoDuringAFinalDrain: a lease whose final checkpoint's
// draining write is under way is let go at the hand-off's deadline, not once
// that write has unwound; Stop waits for it.
func TestAHandOffLetsGoDuringAFinalDrain(t *testing.T) {
	f, sp := releaseFixture(t)
	begun := make(chan struct{}, 1)
	sp.mu.Lock()
	sp.drainGate, sp.afterCancel, sp.drainReturned = make(chan struct{}), 500*time.Millisecond, make(chan struct{})
	sp.drainBegun = begun
	sp.mu.Unlock()
	f.lease.Close()
	renew(t, f)
	<-begun // the final checkpoint acknowledged, its draining write held
	// The hand-off's manifest is never acknowledged, so its own draining
	// write never begins: the one under way is the final checkpoint's.
	f.log.mu.Lock()
	f.log.holding = true
	f.log.mu.Unlock()
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	began := time.Now()
	_ = f.owner.Handoff(ctx)
	if took := time.Since(began); took > 400*time.Millisecond {
		t.Fatalf("the hand-off waited %v for a draining write", took)
	}
	if _, ok := f.owner.Lease("lease-1"); ok {
		t.Fatal("the lease held past the hand-off's deadline")
	}
	f.owner.Stop()
	select {
	case <-sp.drainReturned:
	default:
		t.Fatal("Stop returned while a draining write ran")
	}
}

// TestAReleaseSkipsAHoldThatHeartbeated: the release pass rechecks each hold
// as its batch comes, so a hold whose first heartbeat was issued between the
// pass's scan and its batch is not released.
func TestAReleaseSkipsAHoldThatHeartbeated(t *testing.T) {
	f, _ := releaseFixture(t)
	f.lease.mu.Lock()
	f.lease.allocation = 1 << 40
	f.lease.mu.Unlock()
	var last string
	for range 20 * releaseBatch {
		a, err := f.lease.Admit(Admission{Estimate: 1, Stream: true, Boot: boot, OpenHeartbeat: true})
		if err != nil {
			t.Fatal(err)
		}
		last = max(last, a.Auth)
	}
	f.clock.advance(70 * time.Second)
	beat := make(chan error, 1)
	var once sync.Once
	f.log.mu.Lock()
	f.log.onPublish = func(data []byte) {
		if bytes.Contains(data, []byte(`"kind":"release"`)) {
			// The first batch is being decided: the last hold, in the last
			// batch, heartbeats as soon as the lease's lock is free.
			once.Do(func() {
				go func() {
					_, err := f.lease.Heartbeat(context.Background(), last, HeartbeatOf{GatewaySeq: 1, Hash: sum(last),
						Usage: 1, Running: 1, Basis: []byte("terms")})
					beat <- err
				}()
			})
		}
	}
	f.log.mu.Unlock()
	if err := f.owner.Reap(context.Background()); err != nil {
		t.Fatal(err)
	}
	if err := <-beat; err != nil {
		t.Fatalf("the heartbeat: %v", err)
	}
	released := 0
	for _, r := range f.log.records(t, "lease-1") {
		if r.Kind == record.Release {
			released++
			if r.Auth == last {
				t.Fatal("a hold whose heartbeat was issued was released")
			}
		}
	}
	if released != 20*releaseBatch-1 {
		t.Fatalf("%d holds released", released)
	}
}

// TestAHandOffInterruptsARepublish: after a failed publish the flusher
// hands the lease's backlog to its key again a batch at a time, so a
// hand-off whose time is up takes the lease's lock between batches and lets
// the lease go at once, though the backlog is 50,000 records.
func TestAHandOffInterruptsARepublish(t *testing.T) {
	f, _ := releaseFixture(t)
	f.lease.mu.Lock()
	f.lease.allocation = 1 << 40
	f.lease.mu.Unlock()
	const holds = 50_000
	for range holds {
		if _, err := f.lease.Admit(Admission{Estimate: 1, Stream: true, Boot: boot, OpenHeartbeat: true}); err != nil {
			t.Fatal(err)
		}
	}
	f.clock.advance(70 * time.Second)
	// Every release is handed over behind the first's acknowledgement, held;
	// then that acknowledgement fails, and the flusher republishes them all.
	f.log.hold()
	reapPass(t, f)
	republishing := make(chan struct{})
	var once sync.Once
	f.log.mu.Lock()
	f.log.onRepublish = func() { once.Do(func() { close(republishing) }) }
	f.log.mu.Unlock()
	f.log.letGoFailing(errors.New("the publish failed"))
	<-republishing
	ctx, cancel := context.WithTimeout(context.Background(), time.Millisecond)
	defer cancel()
	began := time.Now()
	if err := f.owner.Handoff(ctx); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("a hand-off whose time ran out: %v", err)
	}
	if took := time.Since(began); took > 50*time.Millisecond {
		t.Fatalf("the hand-off let its lease go %v after it began", took)
	}
}
