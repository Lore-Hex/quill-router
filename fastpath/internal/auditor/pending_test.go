package auditor

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"reflect"
	"slices"
	"sync"
	"testing"
	"time"

	"cloud.google.com/go/spanner"

	"github.com/Lore-Hex/quill-router/fastpath/internal/settlelog"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store/storetest"
)

// fakeTopic is the record topic: it keeps what is published, in order, and
// fails the publishes of the authorizations in failAuth. events, when set,
// hears of each.
type fakeTopic struct {
	mu       sync.Mutex
	failAuth map[string]bool
	sent     []string
	data     [][]byte
	events   *[]string
}

type published struct{ err error }

func (p published) Wait(context.Context) (string, error) { return "m", p.err }

func (f *fakeTopic) Publish(authorization, kind string, data []byte) Waiter {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.events != nil {
		*f.events = append(*f.events, "publish "+authorization)
	}
	if f.failAuth[authorization] {
		return published{errors.New("the record topic failed")}
	}
	f.sent = append(f.sent, authorization+"/"+kind)
	f.data = append(f.data, slices.Clone(data))
	return published{}
}

// fakeStaged is a record topic message.
type fakeStaged struct {
	auth, kind string
	data       []byte
	acked      int
	nacked     int
}

func (m *fakeStaged) Authorization() string { return m.auth }
func (m *fakeStaged) Kind() string          { return m.kind }
func (m *fakeStaged) Data() []byte          { return m.data }
func (m *fakeStaged) ID() string            { return "msg-" + m.auth }
func (m *fakeStaged) Published() time.Time  { return start }
func (m *fakeStaged) Ack()                  { m.acked++ }
func (m *fakeStaged) Nack()                 { m.nacked++ }

// leaseRecords are the records the pending work wrote for an
// authorization, by kind.
func leaseRecords(t *testing.T, db *spanner.Client, auth string) map[string]store.LeaseRecord {
	t.Helper()
	out := map[string]store.LeaseRecord{}
	err := db.Single().Query(context.Background(), spanner.Statement{
		SQL: `SELECT kind, workspace_id, lease_id, outcome, cost, winner_digest, boot_binding, body
		        FROM tr_lease_record WHERE authorization_id = @a`,
		Params: map[string]any{"a": auth},
	}).Do(func(row *spanner.Row) error {
		r := store.LeaseRecord{AuthorizationID: auth}
		if err := row.Columns(&r.Kind, &r.Ref.Workspace, &r.Ref.LeaseID, &r.Outcome, &r.Cost, &r.WinnerDigest,
			&r.BootBinding, &r.Body); err != nil {
			return err
		}
		out[r.Kind] = r
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	return out
}

// TestAPacksWorkIsDoneOnceItsFullRecordIsStaged: a commit's pack, a settle
// and a refund, has its outcomes published by the first sweep, which then
// waits for the settle's staged record; once it is staged, the next sweep
// writes the settle's generation and activity records and the refund's
// disposition record, marks the pack done, and drops the staged record.
func TestAPacksWorkIsDoneOnceItsFullRecordIsStaged(t *testing.T) {
	if emulator == nil {
		t.Skip(skipped)
	}
	ctx := context.Background()
	// The sweep reads every lease's packs: the test's database is its own.
	db, err := emulator.Database(ctx, storetest.UniqueID("pending")[:20], nil)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	f := newRuntimeFixtureOn(t, db)
	a, err := store.NewAuthorizationID(f.ref.LeaseID)
	if err != nil {
		t.Fatal(err)
	}
	b, err := store.NewAuthorizationID(f.ref.LeaseID)
	if err != nil {
		t.Fatal(err)
	}
	rt := f.runtime()
	// The settle's full record, which states the request's boot binding.
	body := encoded(map[string]any{"a": a, "boot": boot})
	settled2 := settle(2, a, 40, 0)
	settled2.Digest = sum(string(body))
	handleAll(rt, on(t, f.ref, hb(1, a, 1, 10)), on(t, f.ref, settled2), on(t, f.ref, refund(3, b)))
	round(rt)
	topic := &fakeTopic{}
	var alerts []string
	p, err := NewPending(PendingConfig{Store: f.s, Records: topic, Every: time.Hour, Limit: 1, Wait: time.Second,
		Alert: func(auth, what string) { alerts = append(alerts, what) }})
	if err != nil {
		t.Fatal(err)
	}
	pack := func() store.Pack {
		packs, _, err := f.s.LoadWinners(ctx, f.ref)
		if err != nil || len(packs) < 1 {
			t.Fatalf("the lease's packs: %+v %v", packs, err)
		}
		return packs[0]
	}
	p.sweep(ctx)
	if pk := pack(); pk.WorkDoneAt.Valid || len(leaseRecords(t, db, a)) != 0 || len(leaseRecords(t, db, b)) != 0 ||
		len(topic.sent) != 0 {
		t.Fatalf("work done with the settle's full record not staged: %+v, published %v", pk, topic.sent)
	}

	stager, err := NewStager(f.s, func(auth, what string) { alerts = append(alerts, what) })
	if err != nil {
		t.Fatal(err)
	}
	full := &fakeStaged{auth: a, kind: settlelog.FullRecord, data: body}
	stager.Handle(ctx, full)
	if full.acked != 1 {
		t.Fatalf("the full record staged, acknowledged %d", full.acked)
	}
	p.sweep(ctx)
	if pk := pack(); !pk.WorkDoneAt.Valid {
		t.Fatalf("the pack after its full record was staged: %+v", pk)
	}
	if !slices.Equal(topic.sent, []string{a + "/outcome", b + "/outcome"}) {
		t.Fatalf("the outcomes published: %v", topic.sent)
	}
	var settled, refunded OutcomeRecord
	if json.Unmarshal(topic.data[0], &settled) != nil || json.Unmarshal(topic.data[1], &refunded) != nil {
		t.Fatalf("the outcomes: %q", topic.data)
	}
	if want := (OutcomeRecord{V: 1, Auth: a, Outcome: "settled", Cost: 40, Boot: boot, Digest: sum(string(body))}); !reflect.DeepEqual(settled, want) {
		t.Fatalf("the settle's outcome %+v, want %+v", settled, want)
	}
	if want := (OutcomeRecord{V: 1, Auth: b, Outcome: "refunded", Boot: boot}); !reflect.DeepEqual(refunded, want) {
		t.Fatalf("the refund's outcome %+v, want %+v", refunded, want)
	}
	cost := spanner.NullInt64{Int64: 40, Valid: true}
	for _, kind := range []string{"generation", "activity"} {
		want := store.LeaseRecord{AuthorizationID: a, Kind: kind, Ref: f.ref, Outcome: "settled", Cost: cost,
			WinnerDigest: sum(string(body)), BootBinding: boot, Body: body}
		if got := leaseRecords(t, db, a)[kind]; !reflect.DeepEqual(got, want) {
			t.Fatalf("the settle's %s record %+v, want %+v", kind, got, want)
		}
	}
	disposition, err := json.Marshal(DispositionRecord{V: 1, Auth: b, Outcome: "refunded", Boot: boot})
	if err != nil {
		t.Fatal(err)
	}
	want := store.LeaseRecord{AuthorizationID: b, Kind: "disposition", Ref: f.ref, Outcome: "refunded",
		Cost: spanner.NullInt64{Valid: true}, BootBinding: boot, Body: disposition}
	if got := leaseRecords(t, db, b); len(got) != 1 || !reflect.DeepEqual(got["disposition"], want) {
		t.Fatalf("the refund's records %+v, want %+v", got, want)
	}
	if _, ok, err := f.s.ReadStaged(ctx, a, sum(string(body))); ok || err != nil {
		t.Fatalf("the staged record after its pack was done: %v %v", ok, err)
	}
	if len(alerts) != 0 {
		t.Fatalf("told %v", alerts)
	}
	n := len(topic.sent)
	p.sweep(ctx)
	if len(topic.sent) != n {
		t.Fatalf("a sweep with every pack done published %v", topic.sent[n:])
	}

	// Two more commits, two packs: a sweep a pack a page does both.
	c, err := store.NewAuthorizationID(f.ref.LeaseID)
	if err != nil {
		t.Fatal(err)
	}
	d, err := store.NewAuthorizationID(f.ref.LeaseID)
	if err != nil {
		t.Fatal(err)
	}
	handleAll(rt, on(t, f.ref, refund(4, c)))
	round(rt)
	handleAll(rt, on(t, f.ref, refund(5, d)))
	round(rt)
	p.sweep(ctx)
	packs, _, err := f.s.LoadWinners(ctx, f.ref)
	if err != nil || len(packs) != 3 {
		t.Fatalf("the lease's packs: %d %v", len(packs), err)
	}
	for _, pk := range packs {
		if !pk.WorkDoneAt.Valid {
			t.Fatalf("pack %d after the sweep: not done", pk.CommitVersion)
		}
	}
	if len(leaseRecords(t, db, c)) != 1 || len(leaseRecords(t, db, d)) != 1 {
		t.Fatalf("the later refunds' records: %v and %v", leaseRecords(t, db, c), leaseRecords(t, db, d))
	}

	// The settle's full record delivered again, its acknowledgement lost: it
	// is staged again, and stays while its lease is open; once the lease has
	// retired, the sweep drops it.
	stager.Handle(ctx, full)
	staged := func() bool {
		t.Helper()
		_, ok, err := f.s.ReadStaged(ctx, a, sum(string(body)))
		if err != nil {
			t.Fatal(err)
		}
		return ok
	}
	if p.sweep(ctx); !staged() {
		t.Fatal("a record staged again went while its lease was open")
	}
	if _, err := db.ReadWriteTransaction(ctx, func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		_, err := txn.Update(ctx, spanner.Statement{
			SQL: `UPDATE tr_lease SET state = 'closed', closed_at = CURRENT_TIMESTAMP(), close_kind = 'auditor',
			             drained_by = 'owner', fence_time = TIMESTAMP_ADD(expiry, INTERVAL 1 SECOND),
			             retire_at = CURRENT_TIMESTAMP()
			       WHERE workspace_id = @w AND lease_id = @l`,
			Params: map[string]any{"w": f.ref.Workspace, "l": f.ref.LeaseID}})
		return err
	}); err != nil {
		t.Fatal(err)
	}
	if p.sweep(ctx); staged() {
		t.Fatal("a record staged again stayed once its lease retired")
	}
}

// fakePending is a store whose writes the test sees in order, and can
// fail. Its staged records' bodies are body, or one that states a boot
// binding.
type fakePending struct {
	PendingStore
	events                        *[]string
	staged                        bool
	body                          []byte
	failWrite, failMark, failDrop bool
	written                       [][]store.LeaseRecord
	retired                       [][]store.StagedKey
}

func (f *fakePending) ReadStaged(_ context.Context, auth string, digest []byte) (store.StagedRecord, bool, error) {
	*f.events = append(*f.events, "read "+auth)
	body := f.body
	if body == nil {
		body = []byte(`{"boot":"Ym9vdC0x"}`)
	}
	return store.StagedRecord{AuthorizationID: auth, Digest: digest, Body: body}, f.staged, nil
}

func (f *fakePending) WriteRecords(_ context.Context, records []store.LeaseRecord) error {
	*f.events = append(*f.events, "write")
	if f.failWrite {
		return errors.New("the write failed")
	}
	f.written = append(f.written, records)
	return nil
}

func (f *fakePending) MarkPackDone(context.Context, store.LeaseRef, int64) (bool, error) {
	*f.events = append(*f.events, "mark")
	if f.failMark {
		return false, errors.New("the mark failed")
	}
	return true, nil
}

func (f *fakePending) DropStaged(_ context.Context, keys ...store.StagedKey) error {
	e := "drop"
	for _, k := range keys {
		e += " " + k.AuthorizationID
	}
	*f.events = append(*f.events, e)
	if f.failDrop {
		return errors.New("the drop failed")
	}
	return nil
}

// RetiredStaged reads the pages of retired, one a call.
func (f *fakePending) RetiredStaged(context.Context, int) ([]store.StagedKey, error) {
	*f.events = append(*f.events, "retired")
	if len(f.retired) == 0 {
		return nil, nil
	}
	page := f.retired[0]
	f.retired = f.retired[1:]
	return page, nil
}

// TestAPacksWorkIsDoneInItsOrder: a settle's and a reap's staged full
// records are read first, for their boot binding; every outcome is then
// published, and only once each is acknowledged are the records written, in
// batches; the pack is marked done after, and its winners' staged records
// dropped after that, in batches too. A step that fails ends the work
// there, and the pack waits for the next sweep, but for the drop, which
// leaves the pack done. A winner whose work cannot be read, or whose boot
// binding no record states, is told.
func TestAPacksWorkIsDoneInItsOrder(t *testing.T) {
	ctx := context.Background()
	pack := store.Pack{CommitVersion: 4, Winners: []store.Winner{
		{AuthorizationID: "a", Kind: "settle", Charge: 40, Work: encoded(Work{V: 1, Digest: sum("a")})},
		{AuthorizationID: "b", Kind: "reap", Charge: 9, Work: encoded(Work{V: 1, Digest: sum("b"), SnapshotSeq: 3})},
		{AuthorizationID: "c", Kind: "release", Work: encoded(Work{V: 1, Boot: boot})}}}
	run := func(change func(*fakePending, *fakeTopic)) (bool, []string, []string, *fakePending, *fakeTopic) {
		var events, alerts []string
		s := &fakePending{events: &events, staged: true}
		topic := &fakeTopic{events: &events, failAuth: map[string]bool{}}
		change(s, topic)
		p, err := NewPending(PendingConfig{Store: s, Records: topic, Every: time.Hour, Limit: 1, Wait: time.Second,
			Alert: func(auth, what string) { alerts = append(alerts, auth+": "+what) }})
		if err != nil {
			t.Fatal(err)
		}
		done, _ := p.Do(ctx, store.LeaseRef{Workspace: "ws", LeaseID: "l"}, pack)
		return done, events, alerts, s, topic
	}
	done, events, alerts, s, topic := run(func(*fakePending, *fakeTopic) {})
	want := []string{"read a", "read b", "publish a", "publish b", "publish c", "write", "mark", "drop a b"}
	if !done || !slices.Equal(events, want) || len(alerts) != 0 {
		t.Fatalf("done %v, %v, told %v; want %v", done, events, alerts, want)
	}
	for i, wantBoot := range [][]byte{[]byte("boot-1"), []byte("boot-1"), boot} {
		var out OutcomeRecord
		if err := json.Unmarshal(topic.data[i], &out); err != nil || !bytes.Equal(out.Boot, wantBoot) {
			t.Fatalf("outcome %d: %+v %v, want the boot binding %q", i, out, err, wantBoot)
		}
	}
	for _, r := range s.written[0] {
		if len(r.BootBinding) == 0 {
			t.Fatalf("a record with no boot binding: %+v", r)
		}
	}
	upToOutcomes := []string{"read a", "read b", "publish a", "publish b", "publish c"}
	for name, c := range map[string]struct {
		change func(*fakePending, *fakeTopic)
		want   []string
		wrote  int
	}{
		"a full record not staged": {func(s *fakePending, _ *fakeTopic) { s.staged = false }, []string{"read a"}, 0},
		"a's outcome not acknowledged": {func(_ *fakePending, tp *fakeTopic) { tp.failAuth["a"] = true },
			upToOutcomes, 0},
		"b's outcome not acknowledged": {func(_ *fakePending, tp *fakeTopic) { tp.failAuth["b"] = true },
			upToOutcomes, 0},
		"c's outcome not acknowledged": {func(_ *fakePending, tp *fakeTopic) { tp.failAuth["c"] = true },
			upToOutcomes, 0},
		"a write that failed": {func(s *fakePending, _ *fakeTopic) { s.failWrite = true },
			append(slices.Clone(upToOutcomes), "write"), 0},
		"a mark that failed": {func(s *fakePending, _ *fakeTopic) { s.failMark = true },
			append(slices.Clone(upToOutcomes), "write", "mark"), 1},
	} {
		done, events, _, s, _ := run(c.change)
		if done || !slices.Equal(events, c.want) || len(s.written) != c.wrote {
			t.Fatalf("%s: done %v, %v, %d batches written; want %v and %d", name, done, events, len(s.written), c.want,
				c.wrote)
		}
	}
	if done, events, _, _, _ := run(func(s *fakePending, _ *fakeTopic) { s.failDrop = true }); !done ||
		!slices.Equal(events, want) {
		t.Fatalf("a drop that failed: done %v, %v; want %v", done, events, want)
	}
	if done, events, alerts, _, _ := run(func(s *fakePending, _ *fakeTopic) { s.body = []byte("not json") }); done ||
		len(alerts) != 1 || slices.Contains(events, "publish a") {
		t.Fatalf("a full record that states no boot binding: done %v, %v, told %v", done, events, alerts)
	}

	// The records go in batches each within its bounds.
	saved := recordBatch
	recordBatch.bytes, recordBatch.rows = 40, 3
	done, _, _, s, _ = run(func(s *fakePending, _ *fakeTopic) {
		s.body = []byte(`{"boot":"Ym9vdC0x","pad":"0123456789"}`)
	})
	recordBatch = saved
	var sizes []int
	n := 0
	for _, batch := range s.written {
		size := 0
		for _, r := range batch {
			size += len(r.Body)
		}
		if len(batch) > 3 || (len(batch) > 1 && size > 40) {
			t.Fatalf("a batch of %d records, %d bytes", len(batch), size)
		}
		sizes = append(sizes, len(batch))
		n += len(batch)
	}
	if !done || n != 5 || len(s.written) < 2 {
		t.Fatalf("the batches: %v, done %v", sizes, done)
	}
	// Small records the byte bound would take all at once go a row bound's
	// worth at a time; the staged records too.
	recordBatch.rows, stagedBatch = 2, 1
	done, events, _, s, _ = run(func(*fakePending, *fakeTopic) {})
	recordBatch, stagedBatch = saved, store.MaxDropStaged
	sizes = nil
	for _, batch := range s.written {
		sizes = append(sizes, len(batch))
	}
	if !done || !slices.Equal(sizes, []int{2, 2, 1}) || !slices.Equal(events[len(events)-2:], []string{"drop a", "drop b"}) {
		t.Fatalf("batches of two rows: %v, done %v, then %v", sizes, done, events)
	}

	pack.Winners = append(pack.Winners, store.Winner{AuthorizationID: "d", Kind: "settle",
		Work: encoded(map[string]any{"v": 2, "digest": sum("d")})})
	if done, events, alerts, _, _ := run(func(*fakePending, *fakeTopic) {}); done || len(events) != 0 || len(alerts) != 1 {
		t.Fatalf("a winner whose work cannot be read: done %v, %v, told %v", done, events, alerts)
	}
	pack.Winners[3].Work = encoded(Work{V: 1})
	if done, _, alerts, _, _ := run(func(*fakePending, *fakeTopic) {}); done || len(alerts) != 1 {
		t.Fatalf("a settle with no digest: done %v, told %v", done, alerts)
	}
	pack.WorkDoneAt = spanner.NullTime{Time: start, Valid: true}
	if done, events, _, _, _ := run(func(*fakePending, *fakeTopic) {}); !done || len(events) != 0 {
		t.Fatalf("a pack done already: %v %v", done, events)
	}
}

// TestTheSweepDropsWhatRetiredLeasesStaged: after the packs' work, the
// sweep drops the staged records of leases that have retired, a page at a
// time, until a page is short or a drop fails.
func TestTheSweepDropsWhatRetiredLeasesStaged(t *testing.T) {
	ctx := context.Background()
	saved := stagedBatch
	stagedBatch = 2
	defer func() { stagedBatch = saved }()
	keys := func(auths ...string) []store.StagedKey {
		var out []store.StagedKey
		for _, a := range auths {
			out = append(out, store.StagedKey{AuthorizationID: a, Digest: sum(a)})
		}
		return out
	}
	for name, c := range map[string]struct {
		retired  [][]store.StagedKey
		failDrop bool
		want     []string
	}{
		"none":               {nil, false, []string{"retired", "drop"}},
		"pages":              {[][]store.StagedKey{keys("a", "b"), keys("c")}, false, []string{"retired", "drop a b", "retired", "drop c"}},
		"full pages":         {[][]store.StagedKey{keys("a", "b"), keys("c", "d")}, false, []string{"retired", "drop a b", "retired", "drop c d", "retired", "drop"}},
		"a drop that failed": {[][]store.StagedKey{keys("a", "b"), keys("c")}, true, []string{"retired", "drop a b"}},
	} {
		var events []string
		s := &fakePending{events: &events, retired: c.retired, failDrop: c.failDrop}
		p, err := NewPending(PendingConfig{Store: s, Records: &fakeTopic{}, Every: time.Hour, Limit: 1, Wait: time.Second,
			Alert: func(string, string) {}})
		if err != nil {
			t.Fatal(err)
		}
		p.dropRetired(ctx)
		if !slices.Equal(events, c.want) {
			t.Fatalf("%s: %v, want %v", name, events, c.want)
		}
	}
}

// TestTheStagerStagesFullRecords: a full record is staged under its
// authorization and digest with its lease, and acknowledged; an outcome is
// acknowledged and not staged; a message no retry stages is told and
// acknowledged; a read or a write that fails asks for it again.
func TestTheStagerStagesFullRecords(t *testing.T) {
	f := newRuntimeFixture(t)
	ctx := context.Background()
	a, err := store.NewAuthorizationID(f.ref.LeaseID)
	if err != nil {
		t.Fatal(err)
	}
	var alerts []string
	stager, err := NewStager(f.store, func(auth, what string) { alerts = append(alerts, what) })
	if err != nil {
		t.Fatal(err)
	}
	full := &fakeStaged{auth: a, kind: settlelog.FullRecord, data: []byte("the record")}
	stager.Handle(ctx, full)
	got, ok, err := f.s.ReadStaged(ctx, a, sum("the record"))
	if err != nil || !ok || full.acked != 1 || full.nacked != 0 {
		t.Fatalf("the full record: staged %v %v, acknowledged %d, again %d", ok, err, full.acked, full.nacked)
	}
	if want := (store.StagedRecord{AuthorizationID: a, Digest: sum("the record"), Ref: f.ref, Body: []byte("the record"),
		MessageID: "msg-" + a, PublishTime: start}); !reflect.DeepEqual(got, want) {
		t.Fatalf("staged %+v, want %+v", got, want)
	}
	outcome := &fakeStaged{auth: a, kind: settlelog.Outcome, data: []byte("an outcome")}
	stager.Handle(ctx, outcome)
	if _, ok, _ := f.s.ReadStaged(ctx, a, sum("an outcome")); ok || outcome.acked != 1 {
		t.Fatalf("an outcome: staged %v, acknowledged %d", ok, outcome.acked)
	}
	gone, err := store.NewAuthorizationID(store.NewLeaseID())
	if err != nil {
		t.Fatal(err)
	}
	for _, m := range []*fakeStaged{{auth: a, kind: "settle", data: []byte("x")},
		{auth: "gwa-not-a-lease", kind: settlelog.FullRecord, data: []byte("x")},
		{auth: gone, kind: settlelog.FullRecord, data: []byte("x")},
		{auth: a, kind: settlelog.FullRecord}} {
		alerts = nil
		stager.Handle(ctx, m)
		if m.acked != 1 || len(alerts) != 1 {
			t.Fatalf("%+v: acknowledged %d, told %v", m, m.acked, alerts)
		}
	}
	f.store.failFind = 1
	again := &fakeStaged{auth: a, kind: settlelog.FullRecord, data: []byte("again")}
	stager.Handle(ctx, again)
	if again.acked != 0 || again.nacked != 1 {
		t.Fatalf("a read that failed: acknowledged %d, again %d", again.acked, again.nacked)
	}
	f.store.failStage = 1
	stager.Handle(ctx, again)
	if again.acked != 0 || again.nacked != 2 {
		t.Fatalf("a write that failed: acknowledged %d, again %d", again.acked, again.nacked)
	}
	stager.Handle(ctx, again)
	if again.acked != 1 {
		t.Fatalf("staged at last: acknowledged %d", again.acked)
	}
}
