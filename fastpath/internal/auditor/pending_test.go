package auditor

import (
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
// fails that many publishes. events, when set, hears of each.
type fakeTopic struct {
	mu      sync.Mutex
	failing int
	sent    []string
	data    [][]byte
	events  *[]string
}

type published struct{ err error }

func (p published) Wait(context.Context) (string, error) { return "m", p.err }

func (f *fakeTopic) Publish(authorization, kind string, data []byte) Waiter {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.events != nil {
		*f.events = append(*f.events, "publish "+authorization)
	}
	if f.failing > 0 {
		f.failing--
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
	handleAll(rt, on(t, f.ref, hb(1, a, 1, 10)), on(t, f.ref, settle(2, a, 40, 0)), on(t, f.ref, refund(3, b)))
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
	if pk := pack(); pk.WorkDoneAt.Valid || len(leaseRecords(t, db, a)) != 0 || len(leaseRecords(t, db, b)) != 0 {
		t.Fatalf("work done with the settle's full record not staged: %+v", pk)
	}
	if !slices.Equal(topic.sent, []string{a + "/outcome", b + "/outcome"}) {
		t.Fatalf("the outcomes published: %v", topic.sent)
	}
	var settled, refunded OutcomeRecord
	if json.Unmarshal(topic.data[0], &settled) != nil || json.Unmarshal(topic.data[1], &refunded) != nil {
		t.Fatalf("the outcomes: %q", topic.data)
	}
	if want := (OutcomeRecord{V: 1, Auth: a, Outcome: "settled", Cost: 40, Digest: sum("full " + a)}); !reflect.DeepEqual(settled, want) {
		t.Fatalf("the settle's outcome %+v, want %+v", settled, want)
	}
	if want := (OutcomeRecord{V: 1, Auth: b, Outcome: "refunded", Boot: boot}); !reflect.DeepEqual(refunded, want) {
		t.Fatalf("the refund's outcome %+v, want %+v", refunded, want)
	}

	stager, err := NewStager(f.s, func(auth, what string) { alerts = append(alerts, what) })
	if err != nil {
		t.Fatal(err)
	}
	full := &fakeStaged{auth: a, kind: settlelog.FullRecord, data: []byte("full " + a)}
	stager.Handle(ctx, full)
	if full.acked != 1 {
		t.Fatalf("the full record staged, acknowledged %d", full.acked)
	}
	p.sweep(ctx)
	if pk := pack(); !pk.WorkDoneAt.Valid {
		t.Fatalf("the pack after its full record was staged: %+v", pk)
	}
	cost := spanner.NullInt64{Int64: 40, Valid: true}
	for _, kind := range []string{"generation", "activity"} {
		want := store.LeaseRecord{AuthorizationID: a, Kind: kind, Ref: f.ref, Outcome: "settled", Cost: cost,
			WinnerDigest: sum("full " + a), Body: []byte("full " + a)}
		if got := leaseRecords(t, db, a)[kind]; !reflect.DeepEqual(got, want) {
			t.Fatalf("the settle's %s record %+v, want %+v", kind, got, want)
		}
	}
	body, err := json.Marshal(DispositionRecord{V: 1, Auth: b, Outcome: "refunded", Boot: boot})
	if err != nil {
		t.Fatal(err)
	}
	want := store.LeaseRecord{AuthorizationID: b, Kind: "disposition", Ref: f.ref, Outcome: "refunded",
		Cost: spanner.NullInt64{Valid: true}, BootBinding: boot, Body: body}
	if got := leaseRecords(t, db, b); len(got) != 1 || !reflect.DeepEqual(got["disposition"], want) {
		t.Fatalf("the refund's records %+v, want %+v", got, want)
	}
	if _, ok, err := f.s.ReadStaged(ctx, a, sum("full "+a)); ok || err != nil {
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
}

// fakePending is a store whose writes the test sees in order, and can
// fail.
type fakePending struct {
	PendingStore
	events              *[]string
	staged              bool
	failWrite, failMark bool
	dropped             bool
}

func (f *fakePending) ReadStaged(_ context.Context, auth string, digest []byte) (store.StagedRecord, bool, error) {
	*f.events = append(*f.events, "read "+auth)
	return store.StagedRecord{AuthorizationID: auth, Digest: digest, Body: []byte("full")}, f.staged, nil
}

func (f *fakePending) WriteRecords(_ context.Context, records []store.LeaseRecord) error {
	*f.events = append(*f.events, "write")
	if f.failWrite {
		return errors.New("the write failed")
	}
	return nil
}

func (f *fakePending) MarkPackDone(context.Context, store.LeaseRef, int64) (bool, error) {
	*f.events = append(*f.events, "mark")
	if f.failMark {
		return false, errors.New("the mark failed")
	}
	return true, nil
}

func (f *fakePending) DropStaged(_ context.Context, auth string, _ ...[]byte) (bool, error) {
	*f.events = append(*f.events, "drop "+auth)
	return f.dropped, nil
}

// TestAPacksWorkIsDoneInItsOrder: every outcome is published before any
// record is written, and the pack is marked done only after; a step that
// fails ends the work there, and the pack waits for the next sweep. A
// winner whose work cannot be read is told.
func TestAPacksWorkIsDoneInItsOrder(t *testing.T) {
	ctx := context.Background()
	pack := store.Pack{CommitVersion: 4, Winners: []store.Winner{
		{AuthorizationID: "a", Kind: "settle", Charge: 40, Work: encoded(Work{V: 1, Digest: sum("a")})},
		{AuthorizationID: "b", Kind: "reap", Charge: 9, Work: encoded(Work{V: 1, Digest: sum("b"), SnapshotSeq: 3})},
		{AuthorizationID: "c", Kind: "release", Work: encoded(Work{V: 1, Boot: boot})}}}
	run := func(change func(*fakePending, *fakeTopic)) (bool, []string, []string) {
		var events, alerts []string
		s := &fakePending{events: &events, staged: true, dropped: true}
		topic := &fakeTopic{events: &events}
		change(s, topic)
		p, err := NewPending(PendingConfig{Store: s, Records: topic, Every: time.Hour, Limit: 1, Wait: time.Second,
			Alert: func(auth, what string) { alerts = append(alerts, auth+": "+what) }})
		if err != nil {
			t.Fatal(err)
		}
		done, _ := p.Do(ctx, store.LeaseRef{Workspace: "ws", LeaseID: "l"}, pack)
		return done, events, alerts
	}
	done, events, alerts := run(func(*fakePending, *fakeTopic) {})
	want := []string{"publish a", "publish b", "publish c", "read a", "read b", "write", "mark", "drop a", "drop b"}
	if !done || !slices.Equal(events, want) || len(alerts) != 0 {
		t.Fatalf("done %v, %v, told %v; want %v", done, events, alerts, want)
	}
	for name, c := range map[string]struct {
		change func(*fakePending, *fakeTopic)
		want   []string
	}{
		"an outcome not published": {func(_ *fakePending, tp *fakeTopic) { tp.failing = 1 },
			[]string{"publish a", "publish b", "publish c"}},
		"a full record not staged": {func(s *fakePending, _ *fakeTopic) { s.staged = false },
			[]string{"publish a", "publish b", "publish c", "read a"}},
		"a write that failed": {func(s *fakePending, _ *fakeTopic) { s.failWrite = true },
			[]string{"publish a", "publish b", "publish c", "read a", "read b", "write"}},
		"a mark that failed": {func(s *fakePending, _ *fakeTopic) { s.failMark = true },
			[]string{"publish a", "publish b", "publish c", "read a", "read b", "write", "mark"}},
	} {
		done, events, _ := run(c.change)
		if done || !slices.Equal(events, c.want) {
			t.Fatalf("%s: done %v, %v; want %v", name, done, events, c.want)
		}
	}
	if _, _, alerts := run(func(s *fakePending, _ *fakeTopic) { s.dropped = false }); len(alerts) != 2 {
		t.Fatalf("staged records that did not go: told %v", alerts)
	}
	pack.Winners = append(pack.Winners, store.Winner{AuthorizationID: "d", Kind: "settle",
		Work: encoded(map[string]any{"v": 2, "digest": sum("d")})})
	if done, events, alerts := run(func(*fakePending, *fakeTopic) {}); done || len(events) != 0 || len(alerts) != 1 {
		t.Fatalf("a winner whose work cannot be read: done %v, %v, told %v", done, events, alerts)
	}
	pack.Winners[3].Work = encoded(Work{V: 1})
	if done, _, alerts := run(func(*fakePending, *fakeTopic) {}); done || len(alerts) != 1 {
		t.Fatalf("a settle with no digest: done %v, told %v", done, alerts)
	}
	if done, events, _ := run(func(*fakePending, *fakeTopic) {}); done || len(events) != 0 {
		t.Fatalf("again: %v %v", done, events)
	}
	pack.WorkDoneAt = spanner.NullTime{Time: start, Valid: true}
	if done, events, _ := run(func(*fakePending, *fakeTopic) {}); !done || len(events) != 0 {
		t.Fatalf("a pack done already: %v %v", done, events)
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
