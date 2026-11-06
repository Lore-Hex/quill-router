package auditor

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"reflect"
	"slices"
	"sync"
	"sync/atomic"
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
// disposition record, marks the pack done, and drops the staged record. A
// record staged again stays while its lease is open, and goes at the first
// drop of retired leases' records once the lease has retired.
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
	clock := start
	p, err := NewPending(PendingConfig{Store: f.s, Records: topic, Every: time.Hour, Limit: 1, Wait: time.Second,
		Alert: func(auth, what string) { alerts = append(alerts, what) }, Clock: func() time.Time { return clock }})
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
	// A sweep that drops retired leases' records, DropEvery past the last.
	clock = clock.Add(DefaultDropEvery)
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
	// The next sweep, before DropEvery has passed, drops nothing; the one
	// at DropEvery drops it.
	clock = clock.Add(DefaultDropEvery - time.Second)
	if p.sweep(ctx); !staged() {
		t.Fatal("a record staged again went before DropEvery had passed since the last drop")
	}
	clock = clock.Add(time.Second)
	if p.sweep(ctx); staged() {
		t.Fatal("a record staged again stayed once its lease retired")
	}
}

// TestADrainLogRefundsWorkIsDoneFromItsHoldsBootBinding: a refund the drain
// log decides, for a hold the heartbeats alone made, has no full record to
// read a boot binding from and none is staged for it: its work carries the
// hold's, so the first sweep publishes its outcome, writes its disposition
// record and marks its pack done, with no alert. A settle the drain log
// decides still waits for its staged full record, and finishes from it.
func TestADrainLogRefundsWorkIsDoneFromItsHoldsBootBinding(t *testing.T) {
	if emulator == nil {
		t.Skip(skipped)
	}
	ctx := context.Background()
	db, err := emulator.Database(ctx, storetest.UniqueID("drainrefund")[:20], nil)
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
	first := hb(1, a, 1, 10)
	first.Boot = boot
	handleAll(rt, on(t, f.ref, first))
	round(rt)
	if ok, _, err := f.s.OwnerMarkDraining(ctx, grantee, f.ref); err != nil || !ok {
		t.Fatalf("the draining write: %v %v", ok, err)
	}
	row, _, err := f.s.ReadLease(ctx, f.ref)
	if err != nil {
		t.Fatal(err)
	}
	money := []byte(`{}`)
	appendRow := func(term store.DrainTerminal) {
		t.Helper()
		term.Ref, term.Money = f.ref, money
		if got, err := f.s.Append(ctx, term); err != nil || got.Refused != "" {
			t.Fatalf("the front door's append of %s: %+v %v", term.RecordID, got, err)
		}
	}
	// The front door's refund of a, its owner unreachable: no full record.
	appendRow(store.DrainTerminal{AuthorizationID: a, RecordID: "refund-a", Kind: "refund", Estimate: 100,
		Cause: "unreachable"})
	n := int64(0)
	tickAt := func(at time.Time) {
		n++
		handleAll(rt, on(t, f.ref, tick(n, at)))
		round(rt)
	}
	tickAt(row.FenceTime.Time.Add(2 * time.Second)) // the fence tick: S
	tickAt(row.FenceTime.Time.Add(3 * time.Second)) // a's refund applied: its winner, in a pack of its own
	// A settle of b, which the member never saw as a hold, in a later pack.
	body := encoded(map[string]any{"a": b, "boot": []byte("boot-2")})
	appendRow(store.DrainTerminal{AuthorizationID: b, RecordID: "settle-b", Kind: "settle", Charge: 5, Estimate: 100,
		Digest: sum(string(body)), Cause: "past_cutoff"})
	tickAt(row.FenceTime.Time.Add(4 * time.Second)) // b's settle applied
	done := func() map[string]bool {
		t.Helper()
		packs, _, err := f.s.LoadWinners(ctx, f.ref)
		if err != nil {
			t.Fatal(err)
		}
		out := map[string]bool{}
		for _, p := range packs {
			for _, w := range p.Winners {
				out[w.AuthorizationID] = p.WorkDoneAt.Valid
			}
		}
		return out
	}
	won := map[string]store.Winner{}
	packs, _, err := f.s.LoadWinners(ctx, f.ref)
	if err != nil {
		t.Fatal(err)
	}
	for _, p := range packs {
		for _, w := range p.Winners {
			won[w.AuthorizationID] = w
		}
	}
	wantA := store.Winner{AuthorizationID: a, Kind: "refund", FromDrain: true, RecordID: "refund-a",
		Work: encoded(Work{V: 1, Estimate: 100, Boot: boot, Money: money, Cause: "unreachable"})}
	wantB := store.Winner{AuthorizationID: b, Kind: "settle", Charge: 5, FromDrain: true, RecordID: "settle-b",
		Work: encoded(Work{V: 1, Estimate: 100, Digest: sum(string(body)), Money: money, Cause: "past_cutoff"})}
	if len(won) != 2 || !reflect.DeepEqual(won[a], wantA) || !reflect.DeepEqual(won[b], wantB) {
		t.Fatalf("the winners %+v, want %+v and %+v", won, wantA, wantB)
	}
	if rows, _, err := f.s.ReadHoldDrainRows(ctx, f.ref, a); err != nil || len(rows) != 1 {
		t.Fatalf("a's drain-log rows: %+v %v", rows, err)
	}

	topic := &fakeTopic{}
	var alerts []string
	p, err := NewPending(PendingConfig{Store: f.s, Records: topic, Every: time.Hour, Limit: 1, Wait: time.Second,
		Alert: func(auth, what string) { alerts = append(alerts, auth+": "+what) }})
	if err != nil {
		t.Fatal(err)
	}
	// The first sweep: the refund's pack is done, nothing staged; the
	// settle's waits for its full record.
	p.sweep(ctx)
	if got := done(); !got[a] || got[b] || len(alerts) != 0 {
		t.Fatalf("after the first sweep: done %v, told %v", got, alerts)
	}
	if !slices.Equal(topic.sent, []string{a + "/outcome"}) {
		t.Fatalf("the outcomes published: %v", topic.sent)
	}
	var refunded OutcomeRecord
	if err := json.Unmarshal(topic.data[0], &refunded); err != nil {
		t.Fatal(err)
	}
	if want := (OutcomeRecord{V: 1, Auth: a, Outcome: "refunded", Boot: boot}); !reflect.DeepEqual(refunded, want) {
		t.Fatalf("the refund's outcome %+v, want %+v", refunded, want)
	}
	disposition, err := json.Marshal(DispositionRecord{V: 1, Auth: a, Outcome: "refunded", Boot: boot})
	if err != nil {
		t.Fatal(err)
	}
	want := store.LeaseRecord{AuthorizationID: a, Kind: "disposition", Ref: f.ref, Outcome: "refunded",
		Cost: spanner.NullInt64{Valid: true}, BootBinding: boot, Body: disposition}
	if got := leaseRecords(t, db, a); len(got) != 1 || !reflect.DeepEqual(got["disposition"], want) {
		t.Fatalf("the refund's records %+v, want %+v", got, want)
	}
	if got := leaseRecords(t, db, b); len(got) != 0 {
		t.Fatalf("the settle's records before its full record was staged: %+v", got)
	}
	// Another sweep, the settle's record still not staged: nothing more.
	p.sweep(ctx)
	if got := done(); got[b] || len(alerts) != 0 || len(topic.sent) != 1 {
		t.Fatalf("after the second sweep: done %v, told %v, published %v", got, alerts, topic.sent)
	}
	// The settle's full record staged: the next sweep finishes it from it.
	stager, err := NewStager(f.s, func(auth, what string) { alerts = append(alerts, auth+": "+what) })
	if err != nil {
		t.Fatal(err)
	}
	stager.Handle(ctx, &fakeStaged{auth: b, kind: settlelog.FullRecord, data: body})
	p.sweep(ctx)
	if got := done(); !got[b] || len(alerts) != 0 {
		t.Fatalf("after the settle's full record was staged: done %v, told %v", got, alerts)
	}
	if !slices.Equal(topic.sent, []string{a + "/outcome", b + "/outcome"}) {
		t.Fatalf("the outcomes published: %v", topic.sent)
	}
	var settled OutcomeRecord
	if err := json.Unmarshal(topic.data[1], &settled); err != nil {
		t.Fatal(err)
	}
	if want := (OutcomeRecord{V: 1, Auth: b, Outcome: "settled", Cost: 5, Boot: []byte("boot-2"),
		Digest: sum(string(body))}); !reflect.DeepEqual(settled, want) {
		t.Fatalf("the settle's outcome %+v, want %+v", settled, want)
	}
	cost := spanner.NullInt64{Int64: 5, Valid: true}
	for _, kind := range []string{"generation", "activity"} {
		want := store.LeaseRecord{AuthorizationID: b, Kind: kind, Ref: f.ref, Outcome: "settled", Cost: cost,
			WinnerDigest: sum(string(body)), BootBinding: []byte("boot-2"), Body: body}
		if got := leaseRecords(t, db, b)[kind]; !reflect.DeepEqual(got, want) {
			t.Fatalf("the settle's %s record %+v, want %+v", kind, got, want)
		}
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
	failRead, perAuth             bool
	missing                       map[string]bool
	reads                         []int
	written                       [][]store.LeaseRecord
	retired                       [][]store.StagedKey
}

// ReadStagedBatch answers each key's record, unless staged is unset or its
// authorization is missing, in the order opposite to the keys', since the
// store promises none.
func (f *fakePending) ReadStagedBatch(_ context.Context, keys ...store.StagedKey) ([]store.StagedRecord, error) {
	e := "read"
	for _, k := range keys {
		e += " " + k.AuthorizationID
	}
	*f.events = append(*f.events, e)
	f.reads = append(f.reads, len(keys))
	if f.failRead {
		return nil, errors.New("the read failed")
	}
	body := f.body
	if body == nil {
		body = []byte(`{"boot":"Ym9vdC0x"}`)
	}
	var out []store.StagedRecord
	for _, k := range slices.Backward(keys) {
		if f.staged && !f.missing[k.AuthorizationID] {
			b := body
			if f.perAuth {
				b = encoded(map[string]any{"boot": boot, "a": k.AuthorizationID})
			}
			out = append(out, store.StagedRecord{AuthorizationID: k.AuthorizationID, Digest: k.Digest, Body: b})
		}
	}
	return out, nil
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
	want := []string{"read a b", "publish a", "publish b", "publish c", "write", "mark", "drop a b"}
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
	upToOutcomes := []string{"read a b", "publish a", "publish b", "publish c"}
	for name, c := range map[string]struct {
		change func(*fakePending, *fakeTopic)
		want   []string
		wrote  int
	}{
		"a full record not staged": {func(s *fakePending, _ *fakeTopic) { s.staged = false }, []string{"read a b"}, 0},
		"b's full record not staged": {func(s *fakePending, _ *fakeTopic) { s.missing = map[string]bool{"b": true} },
			[]string{"read a b"}, 0},
		"a read that failed": {func(s *fakePending, _ *fakeTopic) { s.failRead = true }, []string{"read a b"}, 0},
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
		!slices.Equal(alerts, []string{"a: " + AlertNoBootBinding}) || slices.Contains(events, "publish a") {
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
	if done, events, alerts, _, _ := run(func(*fakePending, *fakeTopic) {}); done || len(events) != 0 ||
		!slices.Equal(alerts, []string{"d: " + AlertUnreadableWork}) {
		t.Fatalf("a winner whose work cannot be read: done %v, %v, told %v", done, events, alerts)
	}
	pack.Winners[3].Work = encoded(Work{V: 1})
	if done, _, alerts, _, _ := run(func(*fakePending, *fakeTopic) {}); done || !slices.Equal(alerts, []string{"d: " + AlertUnreadableWork}) {
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

// droppings is a pending store with no pack pending, which counts the
// sweeps' reads of retired leases' staged records.
type droppings struct {
	PendingStore
	retired int
	fail    bool
}

func (d *droppings) PendingPacks(context.Context, store.PendingPack, int) ([]store.PendingPack, error) {
	return nil, nil
}

func (d *droppings) RetiredStaged(context.Context, int) ([]store.StagedKey, error) {
	d.retired++
	if d.fail {
		return nil, errors.New("the scan failed")
	}
	return nil, nil
}

func (d *droppings) DropStaged(context.Context, ...store.StagedKey) error { return nil }

// TestTheSweepDropsRetiredRecordsEveryDropEvery: the first sweep drops
// what retired leases staged, and a later one only once DropEvery, a
// minute by default, has passed by the sweep's clock since the last drop
// began, though that drop failed; a clock that went back drops at once.
func TestTheSweepDropsRetiredRecordsEveryDropEvery(t *testing.T) {
	ctx := context.Background()
	if DefaultDropEvery != time.Minute {
		t.Fatalf("the default drop interval %v, want a minute", DefaultDropEvery)
	}
	for _, every := range []time.Duration{0, 10 * time.Second} {
		want := every
		if want == 0 {
			want = DefaultDropEvery
		}
		clock := start
		d := &droppings{}
		p, err := NewPending(PendingConfig{Store: d, Records: &fakeTopic{}, Every: time.Second, Limit: 1,
			Wait: time.Second, Alert: func(string, string) {}, Clock: func() time.Time { return clock },
			DropEvery: every})
		if err != nil {
			t.Fatal(err)
		}
		for _, step := range []struct {
			at    time.Duration
			drops int
			fail  bool
		}{
			{0, 1, false},
			{time.Second, 1, false},
			{want / 2, 1, false},
			{want - time.Nanosecond, 1, false},
			{want, 2, true}, // the drop fails
			{want + time.Second, 2, false},
			{2*want - time.Nanosecond, 2, false},
			{2 * want, 3, false},
			{5 * want, 4, false},
			{5*want + time.Second, 4, false},
			{want, 5, false}, // the clock went back
			{want + time.Second, 5, false},
		} {
			clock = start.Add(step.at)
			d.fail = step.fail
			p.sweep(ctx)
			if d.retired != step.drops {
				t.Fatalf("every %v: at %v, %d drops, want %d", every, step.at, d.retired, step.drops)
			}
		}
	}
	if _, err := NewPending(PendingConfig{Store: &droppings{}, Records: &fakeTopic{}, Every: time.Second, Limit: 1,
		Wait: time.Second, Alert: func(string, string) {}, DropEvery: -time.Second}); err == nil {
		t.Fatal("a negative drop interval is taken")
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
	for _, c := range []struct {
		m    *fakeStaged
		want string
	}{{&fakeStaged{auth: a, kind: "settle", data: []byte("x")}, AlertUnknownRecordKind},
		{&fakeStaged{auth: "gwa-not-a-lease", kind: settlelog.FullRecord, data: []byte("x")}, AlertNoLeaseAuth},
		{&fakeStaged{auth: gone, kind: settlelog.FullRecord, data: []byte("x")}, AlertFullRecordNoLease},
		{&fakeStaged{auth: a, kind: settlelog.FullRecord}, AlertNoLeaseAuth}} {
		alerts = nil
		stager.Handle(ctx, c.m)
		if c.m.acked != 1 || !slices.Equal(alerts, []string{c.want}) {
			t.Fatalf("%+v: acknowledged %d, told %v, want %q", c.m, c.m.acked, alerts, c.want)
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
	// Once the lease has retired, a record is acknowledged, staged for no
	// one and told to no one: a redelivery, or a copy no winner names.
	if _, err := f.db.ReadWriteTransaction(ctx, func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		_, err := txn.Update(ctx, spanner.Statement{SQL: `UPDATE tr_lease SET retire_at = CURRENT_TIMESTAMP()
		       WHERE workspace_id = @w AND lease_id = @l`, Params: map[string]any{"w": f.ref.Workspace, "l": f.ref.LeaseID}})
		return err
	}); err != nil {
		t.Fatal(err)
	}
	alerts = nil
	late := &fakeStaged{auth: a, kind: settlelog.FullRecord, data: []byte("late")}
	stager.Handle(ctx, late)
	if _, ok, err := f.s.ReadStaged(ctx, a, sum("late")); ok || err != nil || late.acked != 1 || late.nacked != 0 ||
		len(alerts) != 0 {
		t.Fatalf("a record for a lease retired: staged %v %v, acknowledged %d, again %d, told %v", ok, err, late.acked,
			late.nacked, alerts)
	}
	// A lease's row that goes between the stager finding it and staging,
	// seven days after it retired, is told and acknowledged, as one never
	// found.
	f.store.staging, f.store.staged = make(chan struct{}, 1), make(chan struct{})
	vanished := &fakeStaged{auth: a, kind: settlelog.FullRecord, data: []byte("gone")}
	alerts = nil
	handled := make(chan struct{})
	go func() {
		defer close(handled)
		stager.Handle(ctx, vanished)
	}()
	<-f.store.staging
	if _, err := f.db.ReadWriteTransaction(ctx, func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		_, err := txn.Update(ctx, spanner.Statement{SQL: `DELETE FROM tr_lease WHERE workspace_id = @w AND lease_id = @l`,
			Params: map[string]any{"w": f.ref.Workspace, "l": f.ref.LeaseID}})
		return err
	}); err != nil {
		t.Fatal(err)
	}
	close(f.store.staged)
	<-handled
	if _, ok, err := f.s.ReadStaged(ctx, a, sum("gone")); ok || err != nil || vanished.acked != 1 || vanished.nacked != 0 ||
		!slices.Equal(alerts, []string{AlertFullRecordNoLease}) {
		t.Fatalf("a record whose lease went while it was staged: staged %v %v, acknowledged %d, again %d, told %v", ok,
			err, vanished.acked, vanished.nacked, alerts)
	}
}

// TestAPackPendingTooLongIsTold: a pack whose work is still not done
// longer than Overdue after the sweep first saw it is told of once, at the
// first sweep past that; once its work is done, it is forgotten.
func TestAPackPendingTooLongIsTold(t *testing.T) {
	if emulator == nil {
		t.Skip(skipped)
	}
	ctx := context.Background()
	db, err := emulator.Database(ctx, storetest.UniqueID("overdue")[:20], nil)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	f := newRuntimeFixtureOn(t, db)
	a, err := store.NewAuthorizationID(f.ref.LeaseID)
	if err != nil {
		t.Fatal(err)
	}
	rt := f.runtime()
	body := encoded(map[string]any{"a": a, "boot": boot})
	settled2 := settle(2, a, 40, 0)
	settled2.Digest = sum(string(body))
	handleAll(rt, on(t, f.ref, hb(1, a, 1, 10)), on(t, f.ref, settled2))
	round(rt)
	clock := start
	var alerts []string
	p, err := NewPending(PendingConfig{Store: f.s, Records: &fakeTopic{}, Every: time.Hour, Limit: 1, Wait: time.Second,
		Overdue: 10 * time.Minute, Clock: func() time.Time { return clock },
		Alert: func(subject, what string) { alerts = append(alerts, subject+": "+what) }})
	if err != nil {
		t.Fatal(err)
	}
	key := fmt.Sprintf("%s/%s@%d", f.ref.Workspace, f.ref.LeaseID, 1)
	p.sweep(ctx) // first seen
	clock = start.Add(10 * time.Minute)
	p.sweep(ctx) // exactly the allowance: not past it
	if len(alerts) != 0 {
		t.Fatalf("told within the allowance: %v", alerts)
	}
	clock = start.Add(10*time.Minute + time.Second)
	p.sweep(ctx)
	p.sweep(ctx)
	if want := []string{key + ": " + AlertPendingWorkOverdue}; !slices.Equal(alerts, want) {
		t.Fatalf("told %v, want %v once", alerts, want)
	}
	stager, err := NewStager(f.s, func(string, string) {})
	if err != nil {
		t.Fatal(err)
	}
	stager.Handle(ctx, &fakeStaged{auth: a, kind: settlelog.FullRecord, data: body})
	p.sweep(ctx) // the work done
	p.sweep(ctx) // the pack gone from the pending ones: forgotten
	if len(p.firstSaw) != 0 || len(p.told) != 0 || len(alerts) != 1 {
		t.Fatalf("after the work was done: first saw %v, told %v, alerts %v", p.firstSaw, p.told, alerts)
	}
}

// failingPacks is a pending store whose read of the packs fails while
// fail is set.
type failingPacks struct {
	PendingStore
	mu   sync.Mutex
	fail bool
}

func (f *failingPacks) PendingPacks(ctx context.Context, after store.PendingPack, limit int) ([]store.PendingPack, error) {
	f.mu.Lock()
	fail := f.fail
	f.mu.Unlock()
	if fail {
		return nil, errors.New("a failure the test injected")
	}
	return f.PendingStore.PendingPacks(ctx, after, limit)
}

// pendingFixture is a lease with one or more packs whose work is not done,
// each a settle whose full record is not staged, on a database of its own.
func pendingFixture(t *testing.T, settles int) *runtimeFixture {
	t.Helper()
	if emulator == nil {
		t.Skip(skipped)
	}
	ctx := context.Background()
	db, err := emulator.Database(ctx, storetest.UniqueID("pending")[:20], nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(db.Close)
	f := newRuntimeFixtureOn(t, db)
	rt := f.runtime()
	for i := range settles {
		a, err := store.NewAuthorizationID(f.ref.LeaseID)
		if err != nil {
			t.Fatal(err)
		}
		seq := int64(2*i + 1)
		settled := settle(seq+1, a, 40, 0)
		settled.Digest = sum(a)
		handleAll(rt, on(t, f.ref, hb(seq, a, 1, 10)), on(t, f.ref, settled))
		round(rt) // a commit, so a pack, per settle
	}
	return f
}

// TestAPackPendingTooLongIsToldThoughASweepFailed: a sweep whose read of
// the packs fails forgets nothing: a pack first seen before it is still
// told of at the allowance from then, and one told of is not told of
// again after such a sweep.
func TestAPackPendingTooLongIsToldThoughASweepFailed(t *testing.T) {
	f := pendingFixture(t, 1)
	ctx := context.Background()
	clock := start
	st := &failingPacks{PendingStore: f.s}
	var alerts []string
	p, err := NewPending(PendingConfig{Store: st, Records: &fakeTopic{}, Every: time.Hour, Limit: 1, Wait: time.Second,
		Overdue: 10 * time.Minute, Clock: func() time.Time { return clock },
		Alert: func(subject, what string) { alerts = append(alerts, what) }})
	if err != nil {
		t.Fatal(err)
	}
	p.sweep(ctx) // first seen
	st.fail = true
	clock = start.Add(9 * time.Minute)
	p.sweep(ctx) // fails: forgets nothing
	st.fail = false
	clock = start.Add(11 * time.Minute)
	p.sweep(ctx)
	if !slices.Equal(alerts, []string{AlertPendingWorkOverdue}) {
		t.Fatalf("told %v after a sweep that failed", alerts)
	}
	st.fail = true
	clock = start.Add(12 * time.Minute)
	p.sweep(ctx)
	st.fail = false
	clock = start.Add(13 * time.Minute)
	p.sweep(ctx)
	if len(alerts) != 1 {
		t.Fatalf("told %v: again after a sweep that failed", alerts)
	}
}

// TestAPackIsFirstSeenWhenItsPageIsRead: a pack on a later page of a sweep
// is first seen when that page is read, not when the sweep began, so a
// sweep whose earlier pages took long does not make it overdue at once.
func TestAPackIsFirstSeenWhenItsPageIsRead(t *testing.T) {
	f := pendingFixture(t, 2)
	ctx := context.Background()
	// The clock as the sweeps read it: the first sweep's first page at
	// start, its second eleven minutes later (and its empty last page, and
	// its look at when it last dropped retired leases' records); the
	// second sweep a second after that.
	reads := []time.Duration{0, 11 * time.Minute, 11 * time.Minute, 11 * time.Minute}
	calls := 0
	clock := func() time.Time {
		calls++
		if calls <= len(reads) {
			return start.Add(reads[calls-1])
		}
		return start.Add(11*time.Minute + time.Second)
	}
	var alerts []string
	p, err := NewPending(PendingConfig{Store: f.s, Records: &fakeTopic{}, Every: time.Hour, Limit: 1, Wait: time.Second,
		Overdue: 10 * time.Minute, Clock: clock, Alert: func(subject, what string) { alerts = append(alerts, subject) }})
	if err != nil {
		t.Fatal(err)
	}
	p.sweep(ctx)
	if calls != 4 || len(alerts) != 0 {
		t.Fatalf("the first sweep read the clock %d times and told %v", calls, alerts)
	}
	p.sweep(ctx)
	if want := []string{fmt.Sprintf("%s/%s@%d", f.ref.Workspace, f.ref.LeaseID, 1)}; !slices.Equal(alerts, want) {
		t.Fatalf("told %v, want the first pack alone, %v", alerts, want)
	}
}

// TestAReapsWorkReadsNoBootBindingFromABasis: a pack of one reap winner
// whose staged full record states no boot binding is told, and nothing is
// published, written, marked done or dropped, whatever the record's basis,
// the gateway's, contains.
func TestAReapsWorkReadsNoBootBindingFromABasis(t *testing.T) {
	ctx := context.Background()
	pack := store.Pack{CommitVersion: 4, Winners: []store.Winner{
		{AuthorizationID: "b", Kind: "reap", Charge: 9, Work: encoded(Work{V: 1, Digest: sum("b"), SnapshotSeq: 3})}}}
	var events, alerts []string
	s := &fakePending{events: &events, staged: true,
		body: []byte(`{"v":1,"a":"b","basis":"eyJib290IjoiWW05dmRDMHkifQ=="}`)}
	topic := &fakeTopic{events: &events, failAuth: map[string]bool{}}
	p, err := NewPending(PendingConfig{Store: s, Records: topic, Every: time.Hour, Limit: 1, Wait: time.Second,
		Alert: func(auth, what string) { alerts = append(alerts, auth+": "+what) }})
	if err != nil {
		t.Fatal(err)
	}
	done, _ := p.Do(ctx, store.LeaseRef{Workspace: "ws", LeaseID: "l"}, pack)
	if done || !slices.Equal(events, []string{"read b"}) || !slices.Equal(alerts, []string{"b: " + AlertNoBootBinding}) ||
		len(s.written) != 0 || len(topic.data) != 0 {
		t.Fatalf("a reap whose record states no boot binding: done %v, %v, told %v, wrote %d, published %d", done, events,
			alerts, len(s.written), len(topic.data))
	}
}

// widePack is a pack of n settles, w0 to w(n-1), and two releases.
func widePack(n int) store.Pack {
	pack := store.Pack{CommitVersion: 7}
	for i := range n {
		a := fmt.Sprintf("w%d", i)
		pack.Winners = append(pack.Winners, store.Winner{AuthorizationID: a, Kind: "settle", Charge: 4,
			Work: encoded(Work{V: 1, Digest: sum(a)})})
	}
	for _, a := range []string{"r0", "r1"} {
		pack.Winners = append(pack.Winners, store.Winner{AuthorizationID: a, Kind: "release",
			Work: encoded(Work{V: 1, Boot: boot})})
	}
	return pack
}

// TestAPacksStagedRecordsAreReadInFewCalls: a pack's staged full records
// are read readBatch at a time, not one a winner, and each winner's records
// carry its own staged record, in whatever order the store answers; once a
// read finds one not staged, no more are read and nothing is published.
func TestAPacksStagedRecordsAreReadInFewCalls(t *testing.T) {
	ctx := context.Background()
	const n = 200
	run := func(change func(*fakePending)) (bool, *fakePending, *fakeTopic) {
		var events []string
		s := &fakePending{events: &events, staged: true, perAuth: true}
		change(s)
		topic := &fakeTopic{failAuth: map[string]bool{}}
		p, err := NewPending(PendingConfig{Store: s, Records: topic, Every: time.Hour, Limit: 1, Wait: time.Second,
			Alert: func(auth, what string) { t.Errorf("told %s: %s", auth, what) }})
		if err != nil {
			t.Fatal(err)
		}
		done, _ := p.Do(ctx, store.LeaseRef{Workspace: "ws", LeaseID: "l"}, widePack(n))
		return done, s, topic
	}
	done, s, topic := run(func(*fakePending) {})
	if !done || !slices.Equal(s.reads, []int{n}) || len(topic.sent) != n+2 {
		t.Fatalf("done %v, reads of %v keys, %d published; want one read of %d keys", done, s.reads, len(topic.sent), n)
	}
	records := 0
	for _, batch := range s.written {
		for _, r := range batch {
			records++
			if r.Kind == "disposition" {
				continue
			}
			var full struct {
				A string `json:"a"`
			}
			if err := json.Unmarshal(r.Body, &full); err != nil || full.A != r.AuthorizationID ||
				!bytes.Equal(r.WinnerDigest, sum(r.AuthorizationID)) {
				t.Fatalf("%s's %s record carries %q's staged record (%v)", r.AuthorizationID, r.Kind, full.A, err)
			}
		}
	}
	if records != 2*n+2 {
		t.Fatalf("%d records written, want %d", records, 2*n+2)
	}

	saved := readBatch
	defer func() { readBatch = saved }()
	readBatch = 64
	if done, s, _ := run(func(*fakePending) {}); !done || !slices.Equal(s.reads, []int{64, 64, 64, 8}) {
		t.Fatalf("readBatch 64: done %v, reads of %v keys", done, s.reads)
	}
	// w70 is the second read's: no third, and nothing published or written.
	if done, s, topic := run(func(s *fakePending) { s.missing = map[string]bool{"w70": true} }); done ||
		!slices.Equal(s.reads, []int{64, 64}) || len(topic.sent) != 0 || len(s.written) != 0 {
		t.Fatalf("w70 not staged: done %v, reads of %v keys, %d published, %d written", done, s.reads,
			len(topic.sent), len(s.written))
	}
}

// sweepStore is a pending store of leases' packs, each of winners settles
// (one when 0), whose every call takes latency, and whose marks the test
// watches: how many are under way at once, of all leases and of each, and
// in what order they come. The first marks wait until want are under way
// or two seconds have passed, so a sweep that works fewer leases at once
// than it should is seen to, and every mark is held a little after.
type sweepStore struct {
	PendingStore
	rows       []store.PendingPack
	want       int
	winners    int
	latency    time.Duration
	failPage   int // the PendingPacks call, counted from 1, that fails; 0 is none
	unreadable bool

	mu      sync.Mutex
	pages   int
	reads   int
	loads   map[store.LeaseRef]int
	under   int
	peak    int
	underOf map[store.LeaseRef]int
	overlap bool
	marked  []store.PendingPack
	gate    chan struct{}
	opened  sync.Once
	timer   *time.Timer
}

func newSweepStore(leases, packs, want int) *sweepStore {
	s := &sweepStore{want: want, loads: map[store.LeaseRef]int{}, underOf: map[store.LeaseRef]int{},
		gate: make(chan struct{})}
	for l := range leases {
		ref := store.LeaseRef{Workspace: "ws", LeaseID: fmt.Sprintf("lease-%02d", l)}
		for v := 1; v <= packs; v++ {
			s.rows = append(s.rows, store.PendingPack{Ref: ref, CommitVersion: int64(v)})
		}
	}
	return s
}

// keyAfter is whether a comes after b in the packs' key order.
func keyAfter(a, b store.PendingPack) bool {
	if a.Ref.Workspace != b.Ref.Workspace {
		return a.Ref.Workspace > b.Ref.Workspace
	}
	if a.Ref.LeaseID != b.Ref.LeaseID {
		return a.Ref.LeaseID > b.Ref.LeaseID
	}
	return a.CommitVersion > b.CommitVersion
}

func (s *sweepStore) PendingPacks(_ context.Context, after store.PendingPack, limit int) ([]store.PendingPack, error) {
	time.Sleep(s.latency)
	s.mu.Lock()
	defer s.mu.Unlock()
	s.pages++
	if s.pages == s.failPage {
		return nil, errors.New("a failure the test injected")
	}
	var page []store.PendingPack
	for _, r := range s.rows {
		if keyAfter(r, after) && len(page) < limit {
			page = append(page, r)
		}
	}
	return page, nil
}

func (s *sweepStore) LoadWinners(_ context.Context, ref store.LeaseRef) ([]store.Pack, time.Time, error) {
	time.Sleep(s.latency)
	s.mu.Lock()
	s.loads[ref]++
	s.mu.Unlock()
	var packs []store.Pack
	for _, r := range s.rows {
		if r.Ref != ref {
			continue
		}
		pack := store.Pack{CommitVersion: r.CommitVersion}
		for i := range max(s.winners, 1) {
			a := fmt.Sprintf("%s/%d/%d", ref.LeaseID, r.CommitVersion, i)
			work := encoded(Work{V: 1, Digest: sum(a)})
			if s.unreadable {
				work = []byte("not json")
			}
			pack.Winners = append(pack.Winners, store.Winner{AuthorizationID: a, Kind: "settle", Charge: 4, Work: work})
		}
		packs = append(packs, pack)
	}
	return packs, time.Time{}, nil
}

func (s *sweepStore) ReadStagedBatch(_ context.Context, keys ...store.StagedKey) ([]store.StagedRecord, error) {
	time.Sleep(s.latency)
	s.mu.Lock()
	s.reads++
	s.mu.Unlock()
	out := make([]store.StagedRecord, 0, len(keys))
	for _, k := range keys {
		out = append(out, store.StagedRecord{AuthorizationID: k.AuthorizationID, Digest: k.Digest,
			Body: []byte(`{"boot":"Ym9vdC0x"}`)})
	}
	return out, nil
}

func (s *sweepStore) WriteRecords(context.Context, []store.LeaseRecord) error {
	time.Sleep(s.latency)
	return nil
}

func (s *sweepStore) DropStaged(context.Context, ...store.StagedKey) error {
	time.Sleep(s.latency)
	return nil
}

func (s *sweepStore) open() { s.opened.Do(func() { close(s.gate) }) }

func (s *sweepStore) MarkPackDone(_ context.Context, ref store.LeaseRef, v int64) (bool, error) {
	time.Sleep(s.latency)
	s.mu.Lock()
	s.under++
	s.peak = max(s.peak, s.under)
	s.underOf[ref]++
	s.overlap = s.overlap || s.underOf[ref] > 1
	s.marked = append(s.marked, store.PendingPack{Ref: ref, CommitVersion: v})
	if s.timer == nil {
		s.timer = time.AfterFunc(2*time.Second, s.open)
	}
	if s.under >= s.want {
		s.open()
	}
	s.mu.Unlock()
	<-s.gate
	// Held a little past the gate, so work started past the bound would
	// be seen under way with it.
	time.Sleep(5 * time.Millisecond)
	s.mu.Lock()
	s.under--
	s.underOf[ref]--
	s.mu.Unlock()
	return true, nil
}

// sweepWith is a sweep of s's packs, Limit 4 a page, with workers workers.
func sweepWith(t testing.TB, s *sweepStore, workers int, alert func(string, string)) {
	t.Helper()
	if alert == nil {
		alert = func(auth, what string) { t.Errorf("told %s: %s", auth, what) }
	}
	p, err := NewPending(PendingConfig{Store: s, Records: &fakeTopic{}, Every: time.Hour, Limit: 4, Wait: time.Second,
		Workers: workers, Alert: alert})
	if err != nil {
		t.Fatal(err)
	}
	p.sweepPacks(context.Background())
}

// TestASweepWorksLeasesAtOnceEachInItsOrder: a sweep works Workers leases'
// packs at once, never more, and never two packs of one lease at once; it
// works each lease's packs once each, in commit order, reading them once
// though they run on from one page to the next; and it returns once every
// pack's work is done. One worker works them in the order the sweep read
// them, as the sweep did before it had workers. A lease the sweep held for
// the next page, whose read then failed, is still worked.
func TestASweepWorksLeasesAtOnceEachInItsOrder(t *testing.T) {
	const leases, packs, workers = 6, 3, 3
	s := newSweepStore(leases, packs, workers)
	sweepWith(t, s, workers, nil)
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.peak != workers || s.overlap {
		t.Fatalf("%d packs worked at once at most, want %d; two of one lease at once: %v", s.peak, workers, s.overlap)
	}
	if len(s.marked) != leases*packs {
		t.Fatalf("the sweep returned with %d packs done of %d", len(s.marked), leases*packs)
	}
	byLease := map[store.LeaseRef][]int64{}
	for _, m := range s.marked {
		byLease[m.Ref] = append(byLease[m.Ref], m.CommitVersion)
	}
	for ref, versions := range byLease {
		if !slices.Equal(versions, []int64{1, 2, 3}) || s.loads[ref] != 1 {
			t.Fatalf("%s: packs worked %v, its packs read %d times; want [1 2 3], once", ref.LeaseID, versions,
				s.loads[ref])
		}
	}
	if len(byLease) != leases {
		t.Fatalf("%d leases worked, want %d", len(byLease), leases)
	}

	one := newSweepStore(leases, packs, 1)
	sweepWith(t, one, 1, nil)
	if !slices.Equal(one.marked, one.rows) || one.peak != 1 {
		t.Fatalf("one worker: %v at most %d at once, want %v", one.marked, one.peak, one.rows)
	}

	failing := newSweepStore(leases, packs, 1)
	failing.failPage = 2
	sweepWith(t, failing, workers, nil)
	slices.SortFunc(failing.marked, func(a, b store.PendingPack) int {
		if keyAfter(a, b) {
			return 1
		}
		return -1
	})
	if !slices.Equal(failing.marked, failing.rows[:4]) {
		t.Fatalf("the second page's read failed: %v worked, want the first page's, %v", failing.marked, failing.rows[:4])
	}
}

// TestTheDefaultIsTwoWorkers: a sweep with Workers 0 works two leases'
// packs at once, never more, though more leases wait: pack work spreads out
// rather than bursting into Spanner beside the live settles.
func TestTheDefaultIsTwoWorkers(t *testing.T) {
	if DefaultPendingWorkers != 2 {
		t.Fatalf("the default workers %d, want 2", DefaultPendingWorkers)
	}
	const leases = 6
	// The gate opens once every lease is under way, or two seconds after
	// the first mark: so more workers than two would all be seen at once.
	s := newSweepStore(leases, 1, leases)
	sweepWith(t, s, 0, nil)
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.peak != 2 || len(s.marked) != leases {
		t.Fatalf("the default: %d packs worked at once at most, %d done; want 2, %d", s.peak, len(s.marked), leases)
	}
}

// TestWorkersTellOneAtATime: workers that each tell of a winner whose work
// cannot be read are heard one at a time, as one sweep was.
func TestWorkersTellOneAtATime(t *testing.T) {
	s := newSweepStore(4, 1, 1)
	s.unreadable = true
	var in, told atomic.Int32
	var overlap atomic.Bool
	sweepWith(t, s, 4, func(_, what string) {
		if what != AlertUnreadableWork {
			t.Errorf("told %s", what)
		}
		if in.Add(1) > 1 {
			overlap.Store(true)
		}
		time.Sleep(20 * time.Millisecond)
		in.Add(-1)
		told.Add(1)
	})
	if overlap.Load() || told.Load() != 4 {
		t.Fatalf("told %d times, two at once: %v", told.Load(), overlap.Load())
	}
}

// BenchmarkASweep sweeps 8 leases' packs, two each of 100 settles, through
// a store whose every call takes a millisecond: as the sweep did before,
// a staged record read a winner and one pack at a time; with the reads
// batched; and with the default workers too. store-reads/pack is the
// staged records' reads a pack.
func BenchmarkASweep(b *testing.B) {
	for _, c := range []struct {
		name           string
		batch, workers int
	}{{"a-read-a-winner/one-worker", 1, 1}, {"batched/one-worker", store.MaxReadStaged, 1},
		{"batched/default-workers", store.MaxReadStaged, 0}} {
		b.Run(c.name, func(b *testing.B) {
			saved := readBatch
			readBatch = c.batch
			defer func() { readBatch = saved }()
			reads, packs := 0, 0
			for b.Loop() {
				s := newSweepStore(8, 2, 1)
				s.winners, s.latency = 100, time.Millisecond
				sweepWith(b, s, c.workers, nil)
				reads, packs = reads+s.reads, packs+len(s.marked)
			}
			b.ReportMetric(float64(reads)/float64(packs), "store-reads/pack")
		})
	}
}
