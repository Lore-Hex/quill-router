package frontdoor

import (
	"bytes"
	"context"
	"crypto/sha256"
	"errors"
	"fmt"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	"cloud.google.com/go/spanner"

	"github.com/Lore-Hex/quill-router/fastpath/internal/record"
	"github.com/Lore-Hex/quill-router/fastpath/internal/ring"
	"github.com/Lore-Hex/quill-router/fastpath/internal/settlelog"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

var key = bytes.Repeat([]byte{7}, MinKeySize)

var start = time.Date(2026, 10, 8, 12, 0, 0, 0, time.UTC)

var errInjected = errors.New("a failure the test injected")

// events is what the fakes did, in order, so a test can hold the front
// door to an order: "publish <auth>", "owner <address> <what>", "append
// <record ID> <cause>", "disposition <auth>".
type events struct {
	mu   sync.Mutex
	list []string
}

func (e *events) add(format string, args ...any) {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.list = append(e.list, fmt.Sprintf(format, args...))
}

func (e *events) all() []string {
	e.mu.Lock()
	defer e.mu.Unlock()
	return append([]string(nil), e.list...)
}

// fakeOwners answers as each address's owner is set to: unreachable, or
// with the answer set for it (an authorize's by shard).
type fakeOwners struct {
	ev          *events
	unreachable map[string]bool
	// pinging, when set, hears of each ping, which then waits for pingGate.
	pinging    chan string
	pingGate   chan struct{}
	admitted   map[int64]OwnerAdmitted
	heartbeat  HeartbeatAnswer
	terminal   OwnerTerminalAnswer
	heartbeats []OwnerHeartbeat
	terminals  []OwnerTerminal
	authorizes []OwnerAuthorize
}

func (f *fakeOwners) reach(ctx context.Context, address string) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	if f.unreachable[address] {
		return ErrUnreachable
	}
	return nil
}

func (f *fakeOwners) Authorize(ctx context.Context, address string, req OwnerAuthorize) (OwnerAdmitted, error) {
	f.ev.add("owner %s authorize %d", address, req.Shard)
	f.authorizes = append(f.authorizes, req)
	if err := f.reach(ctx, address); err != nil {
		return OwnerAdmitted{}, err
	}
	return f.admitted[req.Shard], nil
}

func (f *fakeOwners) Heartbeat(ctx context.Context, address string, req OwnerHeartbeat) (HeartbeatAnswer, error) {
	f.ev.add("owner %s heartbeat", address)
	f.heartbeats = append(f.heartbeats, req)
	if err := f.reach(ctx, address); err != nil {
		return HeartbeatAnswer{}, err
	}
	return f.heartbeat, nil
}

func (f *fakeOwners) Ping(ctx context.Context, address string) error {
	f.ev.add("owner %s ping", address)
	if f.pinging != nil {
		f.pinging <- address
		<-f.pingGate
	}
	return f.reach(ctx, address)
}

func (f *fakeOwners) Terminal(ctx context.Context, address string, req OwnerTerminal) (OwnerTerminalAnswer, error) {
	f.ev.add("owner %s %s", address, req.Kind)
	f.terminals = append(f.terminals, req)
	if err := f.reach(ctx, address); err != nil {
		return OwnerTerminalAnswer{}, err
	}
	return f.terminal, nil
}

type fakeStore struct {
	ev          *events
	appended    []store.DrainTerminal
	refuse      bool
	failAppend  bool
	disposition store.Disposition
	failDisp    bool
	failRevoke  int
	// With a gate, a revocation tells revoking and waits for its context
	// to end, then a while more, and tells revoked. onRevoke runs in each.
	gate              chan struct{}
	revoking, revoked chan string
	onRevoke          func()
}

func (f *fakeStore) Append(_ context.Context, t store.DrainTerminal) (store.AppendResult, error) {
	f.ev.add("append %s %s", t.RecordID, t.Cause)
	if f.failAppend {
		return store.AppendResult{}, errInjected
	}
	f.appended = append(f.appended, t)
	if f.refuse {
		return store.AppendResult{Refused: store.RefusedClosed}, nil
	}
	return store.AppendResult{CommitTS: start}, nil
}

func (f *fakeStore) Revoke(ctx context.Context, ref store.LeaseRef) (bool, time.Time, error) {
	if f.gate != nil {
		f.revoking <- ref.LeaseID
		<-ctx.Done()
		time.Sleep(100 * time.Millisecond)
		defer func() { f.revoked <- ref.LeaseID }()
	}
	if f.onRevoke != nil {
		f.onRevoke()
	}
	f.ev.add("revoke %s", ref.LeaseID)
	if f.failRevoke > 0 {
		f.failRevoke--
		return false, time.Time{}, errInjected
	}
	return true, start, nil
}

func (f *fakeStore) Disposition(_ context.Context, authorization string) (store.Disposition, error) {
	f.ev.add("disposition %s", authorization)
	if f.failDisp {
		return store.Disposition{}, errInjected
	}
	return f.disposition, nil
}

type answered struct{ err error }

func (a answered) Wait(context.Context) (string, error) { return "id", a.err }

// fakeRecords is the record topic: it keeps what it is handed, the slice
// itself, as a publisher does until its publish ends; and fails each, or
// holds its acknowledgement until the wait for it ends.
type fakeRecords struct {
	ev        *events
	fail      bool
	holding   bool
	published [][]byte
}

type held struct{}

func (held) Wait(ctx context.Context) (string, error) {
	<-ctx.Done()
	return "", ctx.Err()
}

func (f *fakeRecords) Publish(authorization, kind string, data []byte) Waiter {
	f.ev.add("publish %s %s", authorization, kind)
	if f.fail {
		return answered{errInjected}
	}
	f.published = append(f.published, data)
	if f.holding {
		return held{}
	}
	return answered{}
}

type fakeMembers struct{ view ring.View }

func (f fakeMembers) View() (ring.View, time.Time) { return f.view, f.view.ReadAt }

// owners is a view of live serving owners at the addresses.
func owners(addresses ...string) ring.View {
	v := ring.View{ReadAt: start}
	for _, a := range addresses {
		v.Members = append(v.Members, store.Member{Address: a, Epoch: 1, Roles: []string{ring.OwnerRole},
			State: store.Serving, Live: true})
	}
	return v
}

type doorFixture struct {
	ev      *events
	owners  *fakeOwners
	store   *fakeStore
	records *fakeRecords
	view    ring.View
	shards  int64
	door    *FrontDoor
}

func newDoor(t *testing.T, shards int64) *doorFixture {
	t.Helper()
	ev := &events{}
	f := &doorFixture{ev: ev, owners: &fakeOwners{ev: ev, unreachable: map[string]bool{}, admitted: map[int64]OwnerAdmitted{}},
		store: &fakeStore{ev: ev}, records: &fakeRecords{ev: ev}, view: owners("node-a", "node-b", "node-c"), shards: shards}
	door, err := New(Config{Enabled: everyWorkspace, Owners: f.owners, Store: f.store, Records: f.records, Members: fakeMembers{f.view}, Key: key,
		Shards: func(string) int64 { return f.shards }, OwnerWait: time.Second, PublishWait: time.Second})
	if err != nil {
		t.Fatal(err)
	}
	f.door = door
	return f
}

// envelope is a sealed envelope for a hold at node-b.
func envelope(t *testing.T) (Envelope, string) {
	t.Helper()
	e := Envelope{Auth: "gwa-1", Workspace: "ws-1", Lease: "lease-1", Owner: "node-b", Estimate: 40, Stream: true,
		EndOfLife: start.Add(time.Hour)}
	sealed, err := Seal(key, e)
	if err != nil {
		t.Fatal(err)
	}
	return e, sealed
}

func (f *doorFixture) ownerOf(shard int64) string {
	m, ok := f.view.Owner(ring.ShardKey("ws-1", shard))
	if !ok {
		panic("no owner")
	}
	return m.Address
}

func checkEvents(t *testing.T, ev *events, want ...string) {
	t.Helper()
	if got := ev.all(); !reflect.DeepEqual(got, want) {
		t.Fatalf("events:\n%q\nwant\n%q", got, want)
	}
}

// TestAnAuthorizeGoesToItsShardsOwner: a request's own hash picks its
// workspace's shard, and the shard's owner by rendezvous hashing gets it,
// with all the authorize says of the hold: its estimate, whether it streams,
// its boot binding, and whether that boot declares the heartbeat at stream
// open.
func TestAnAuthorizeGoesToItsShardsOwner(t *testing.T) {
	for _, k := range []int64{1, 4} {
		for _, open := range []bool{false, true} {
			f := newDoor(t, k)
			shard := pick("request-1", k)
			f.owners.admitted[shard] = OwnerAdmitted{Status: Admitted, Envelope: "sealed", EndOfLife: start.Add(time.Hour)}
			got := f.door.Authorize(context.Background(), AuthorizeOf{Workspace: "ws-1", Request: "request-1",
				Estimate: 40, Stream: open, Boot: []byte("boot"), OpenHeartbeat: open})
			if want := (Authorized{Status: Admitted, Envelope: "sealed", EndOfLife: start.Add(time.Hour)}); got != want {
				t.Fatalf("K=%d: %+v, want %+v", k, got, want)
			}
			checkEvents(t, f.ev, fmt.Sprintf("owner %s authorize %d", f.ownerOf(shard), shard))
			want := []OwnerAuthorize{{Workspace: "ws-1", Shard: shard, Estimate: 40, Stream: open, Boot: []byte("boot"),
				OpenHeartbeat: open}}
			if !reflect.DeepEqual(f.owners.authorizes, want) {
				t.Fatalf("K=%d: the owner was sent %+v, want %+v", k, f.owners.authorizes, want)
			}
		}
	}
	seen := map[int64]bool{}
	for i := 0; i < 64; i++ {
		seen[pick(fmt.Sprint("request-", i), 4)] = true
	}
	if len(seen) != 4 {
		t.Fatalf("64 requests picked shards %v of 4", seen)
	}
}

// TestAShardWithoutRoomTriesOneOtherShard: a sharded workspace's request
// that its shard's owner cannot take, having no lease with room or not
// answering, goes to one other shard's owner, and then is Busy (§4.4).
func TestAShardWithoutRoomTriesOneOtherShard(t *testing.T) {
	ctx := context.Background()
	a := AuthorizeOf{Workspace: "ws-1", Request: "request-1", Estimate: 40, Boot: []byte("boot")}
	shard := pick(a.Request, 4)
	other := (shard + 1) % 4

	f := newDoor(t, 4)
	f.owners.admitted[shard] = OwnerAdmitted{Status: Busy}
	f.owners.admitted[other] = OwnerAdmitted{Status: Admitted, Envelope: "sealed"}
	if got := f.door.Authorize(ctx, a); got.Status != Admitted || got.Envelope != "sealed" {
		t.Fatalf("the other shard's owner admitted it: %+v", got)
	}
	checkEvents(t, f.ev, fmt.Sprintf("owner %s authorize %d", f.ownerOf(shard), shard),
		fmt.Sprintf("owner %s authorize %d", f.ownerOf(other), other))

	f = newDoor(t, 4)
	f.owners.admitted[shard] = OwnerAdmitted{Status: Busy}
	f.owners.admitted[other] = OwnerAdmitted{Status: Busy}
	if got := f.door.Authorize(ctx, a); got.Status != Busy {
		t.Fatalf("two shards without room: %+v", got)
	}
	if n := len(f.ev.all()); n != 2 {
		t.Fatalf("%d owners asked, want 2: %q", n, f.ev.all())
	}

	f = newDoor(t, 4)
	f.owners.unreachable[f.ownerOf(shard)] = true
	f.owners.admitted[other] = OwnerAdmitted{Status: Admitted, Envelope: "sealed"}
	if f.ownerOf(shard) == f.ownerOf(other) {
		t.Fatal("the test needs the two shards at different owners")
	}
	if got := f.door.Authorize(ctx, a); got.Status != Admitted {
		t.Fatalf("an owner not reached, then the other shard's: %+v", got)
	}

	f = newDoor(t, 1)
	f.owners.admitted[0] = OwnerAdmitted{Status: Busy}
	if got := f.door.Authorize(ctx, a); got.Status != Busy {
		t.Fatalf("an unsharded workspace: %+v", got)
	}

	// A declared stream's request goes to the other shard's owner with its
	// declaration, after a busy owner and after one not reached.
	declared := a
	declared.Stream, declared.OpenHeartbeat = true, true
	for _, reached := range []bool{true, false} {
		d := newDoor(t, 4)
		if reached {
			d.owners.admitted[shard] = OwnerAdmitted{Status: Busy}
		} else {
			d.owners.unreachable[d.ownerOf(shard)] = true
		}
		d.owners.admitted[other] = OwnerAdmitted{Status: Admitted, Envelope: "sealed"}
		if got := d.door.Authorize(ctx, declared); got.Status != Admitted {
			t.Fatalf("a declared request at the other shard, the first reached %v: %+v", reached, got)
		}
		want := []OwnerAuthorize{
			{Workspace: "ws-1", Shard: shard, Estimate: 40, Stream: true, Boot: []byte("boot"), OpenHeartbeat: true},
			{Workspace: "ws-1", Shard: other, Estimate: 40, Stream: true, Boot: []byte("boot"), OpenHeartbeat: true},
		}
		if !reflect.DeepEqual(d.owners.authorizes, want) {
			t.Fatalf("the first reached %v: the owners were sent %+v, want %+v", reached, d.owners.authorizes, want)
		}
	}
	if n := len(f.ev.all()); n != 1 {
		t.Fatalf("an unsharded workspace asked %d owners: %q", n, f.ev.all())
	}

	f = newDoor(t, 4)
	f.owners.admitted[shard] = OwnerAdmitted{Status: Invalid}
	if got := f.door.Authorize(ctx, a); got.Status != Invalid {
		t.Fatalf("an authorize its owner cannot take: %+v", got)
	}
	if n := len(f.ev.all()); n != 1 {
		t.Fatalf("an authorize its owner cannot take went to %d owners", n)
	}

	f = newDoor(t, 4)
	f.view = ring.View{ReadAt: start}
	door, err := New(Config{Enabled: everyWorkspace, Owners: f.owners, Store: f.store, Records: f.records, Members: fakeMembers{f.view}, Key: key,
		Shards: func(string) int64 { return 4 }, OwnerWait: time.Second, PublishWait: time.Second})
	if err != nil {
		t.Fatal(err)
	}
	if got := door.Authorize(ctx, a); got.Status != Busy || len(f.ev.all()) != 0 {
		t.Fatalf("no owners: %+v, %q", got, f.ev.all())
	}
}

// TestAHeartbeatGoesToItsEnvelopesOwner: a heartbeat goes to the owner and
// lease its envelope names; one whose owner does not answer gets Retry; one
// whose envelope's seal does not hold goes nowhere.
func TestAHeartbeatGoesToItsEnvelopesOwner(t *testing.T) {
	ctx := context.Background()
	e, sealed := envelope(t)
	hb := HeartbeatOf{Envelope: sealed, GatewaySeq: 3, Hash: []byte("hash"), Usage: 7, Running: 12,
		Echoed: start.Add(time.Minute), Basis: []byte("basis")}

	f := newDoor(t, 1)
	f.owners.heartbeat = HeartbeatAnswer{Status: Accepted, Deadline: start.Add(2 * time.Minute)}
	if got := f.door.Heartbeat(ctx, hb); got != f.owners.heartbeat {
		t.Fatalf("the owner's answer: %+v", got)
	}
	want := OwnerHeartbeat{Lease: e.Lease, Auth: e.Auth, GatewaySeq: 3, Hash: []byte("hash"), Usage: 7, Running: 12,
		Echoed: start.Add(time.Minute), Basis: []byte("basis")}
	if len(f.owners.heartbeats) != 1 || !reflect.DeepEqual(f.owners.heartbeats[0], want) {
		t.Fatalf("forwarded %+v, want %+v", f.owners.heartbeats, want)
	}
	checkEvents(t, f.ev, "owner node-b heartbeat")

	f = newDoor(t, 1)
	f.owners.unreachable["node-b"] = true
	if got := f.door.Heartbeat(ctx, hb); got.Status != Retry {
		t.Fatalf("an owner not reached: %+v", got)
	}

	f = newDoor(t, 1)
	bad := hb
	bad.Envelope = sealed[:len(sealed)-2] + "AA"
	if got := f.door.Heartbeat(ctx, bad); got.Status != Invalid || len(f.ev.all()) != 0 {
		t.Fatalf("an envelope whose seal does not hold: %+v, %q", got, f.ev.all())
	}
}

// settle is the tests' settle of 55 under the envelope.
func settle(sealed string) SettleOf {
	return SettleOf{Envelope: sealed, Charge: 55, Full: []byte(`{"full":"record"}`), Money: []byte(`{"cost":55}`)}
}

func digestOf(b []byte) []byte {
	d := sha256.Sum256(b)
	return d[:]
}

// TestASettlesFullRecordIsPublishedFirst: a settle's full record goes to
// the record topic, keyed by its authorization, and only once that is
// acknowledged does the settle go on, with the record's digest; the owner's
// winner is the answer. A publish that fails sends nothing on.
func TestASettlesFullRecordIsPublishedFirst(t *testing.T) {
	ctx := context.Background()
	e, sealed := envelope(t)
	s := settle(sealed)

	f := newDoor(t, 1)
	f.owners.terminal = OwnerTerminalAnswer{Status: Won, Kind: record.Settle, Charge: 55}
	if got := f.door.Settle(ctx, s); got != (TerminalAnswer{Status: Won, Kind: record.Settle, Charge: 55}) {
		t.Fatalf("the owner's winner: %+v", got)
	}
	checkEvents(t, f.ev, "publish gwa-1 "+settlelog.FullRecord, "owner node-b settle")
	want := OwnerTerminal{Lease: e.Lease, Auth: e.Auth, Kind: record.Settle, Charge: 55, Digest: digestOf(s.Full)}
	if !reflect.DeepEqual(f.owners.terminals, []OwnerTerminal{want}) {
		t.Fatalf("forwarded %+v, want %+v", f.owners.terminals, want)
	}
	if !reflect.DeepEqual(f.records.published, [][]byte{s.Full}) {
		t.Fatalf("published %q", f.records.published)
	}

	f = newDoor(t, 1)
	f.records.fail = true
	if got := f.door.Settle(ctx, s); got.Status != Failed {
		t.Fatalf("a full record not published: %+v", got)
	}
	checkEvents(t, f.ev, "publish gwa-1 "+settlelog.FullRecord)

	// A publish that outlives its request publishes what the request
	// stated, though the caller then reuses its buffer.
	f = newDoor(t, 1)
	f.records.holding = true
	reused := settle(sealed)
	wctx, cancel := context.WithTimeout(ctx, 20*time.Millisecond)
	got := f.door.Settle(wctx, reused)
	cancel()
	copy(reused.Full, "xxxxxxxxxxxxxxxxx")
	if got.Status != Failed || len(f.records.published) != 1 || string(f.records.published[0]) != `{"full":"record"}` {
		t.Fatalf("a publish past its wait: %+v, published %q", got, f.records.published)
	}

	for _, bad := range []SettleOf{{Envelope: "v1.x.y", Charge: 1, Full: s.Full, Money: s.Money},
		{Envelope: sealed, Charge: -1, Full: s.Full, Money: s.Money}, {Envelope: sealed, Charge: 1, Money: s.Money},
		{Envelope: sealed, Charge: 1, Full: s.Full}} {
		f = newDoor(t, 1)
		if got := f.door.Settle(ctx, bad); got.Status != Invalid || len(f.ev.all()) != 0 {
			t.Fatalf("%+v: %+v, %q", bad, got, f.ev.all())
		}
	}
}

// TestATerminalTheOwnerDoesNotTakeGoesToTheDrainLog: a terminal whose owner
// cannot be reached, or answers that it may never publish it, is appended
// to its lease's drain log at once, with the estimate its envelope carries
// and the cause, and answered Recorded (§4.3, §4.5). The owner's other
// answers are the answer, and append nothing.
func TestATerminalTheOwnerDoesNotTakeGoesToTheDrainLog(t *testing.T) {
	ctx := context.Background()
	e, sealed := envelope(t)
	s := settle(sealed)
	id := rowID(OwnerTerminal{Kind: record.Settle, Charge: 55, Digest: digestOf(s.Full)}, s.Money)
	if !strings.HasPrefix(id, "settle-") || len(id) != len("settle-")+32 {
		t.Fatalf("a settle's row ID %q", id)
	}
	row := func(cause string) store.DrainTerminal {
		return store.DrainTerminal{Ref: store.LeaseRef{Workspace: e.Workspace, LeaseID: e.Lease},
			AuthorizationID: e.Auth, RecordID: id, Kind: "settle", Charge: 55, Estimate: 40,
			Digest: digestOf(s.Full), Money: s.Money, Cause: cause}
	}

	f := newDoor(t, 1)
	f.owners.unreachable["node-b"] = true
	if got := f.door.Settle(ctx, s); got.Status != Recorded {
		t.Fatalf("an owner not reached: %+v", got)
	}
	checkEvents(t, f.ev, "publish gwa-1 "+settlelog.FullRecord, "owner node-b settle", "append "+id+" unreachable")
	if !reflect.DeepEqual(f.store.appended, []store.DrainTerminal{row("unreachable")}) {
		t.Fatalf("appended %+v, want %+v", f.store.appended, row("unreachable"))
	}

	f = newDoor(t, 1)
	f.owners.terminal = OwnerTerminalAnswer{Status: PastCutoff}
	if got := f.door.Settle(ctx, s); got.Status != Recorded {
		t.Fatalf("an owner past its cutoff: %+v", got)
	}
	if !reflect.DeepEqual(f.store.appended, []store.DrainTerminal{row("past_cutoff")}) {
		t.Fatalf("appended %+v, want %+v", f.store.appended, row("past_cutoff"))
	}

	for _, answer := range []Status{Recorded, Failed, Invalid} {
		f = newDoor(t, 1)
		f.owners.terminal = OwnerTerminalAnswer{Status: answer}
		if got := f.door.Settle(ctx, s); got.Status != answer || len(f.store.appended) != 0 {
			t.Fatalf("the owner's %s: %+v, appended %+v", answer, got, f.store.appended)
		}
	}

	// A retry of the settle names the same row, through any front door;
	// a settle that differs in anything it states names its own.
	f = newDoor(t, 1)
	f.owners.unreachable["node-b"] = true
	f.door.Settle(ctx, s)
	f.door.Settle(ctx, s)
	other := s
	other.Money = []byte(`{"cost":56}`)
	f.door.Settle(ctx, other)
	other = s
	other.Charge = 56
	f.door.Settle(ctx, other)
	other = s
	other.Full = []byte(`{"full":"compacted"}`)
	f.door.Settle(ctx, other)
	ids := map[string]int{}
	for _, row := range f.store.appended {
		ids[row.RecordID]++
	}
	if len(f.store.appended) != 5 || len(ids) != 4 || ids[id] != 2 {
		t.Fatalf("a retried settle and three others: %+v", ids)
	}
}

// TestARefundGoesToTheDrainLogWithoutAFullRecord: a refund publishes no full
// record, and its row is named for its kind.
func TestARefundGoesToTheDrainLogWithoutAFullRecord(t *testing.T) {
	ctx := context.Background()
	e, sealed := envelope(t)
	f := newDoor(t, 1)
	f.owners.unreachable["node-b"] = true
	if got := f.door.Refund(ctx, RefundOf{Envelope: sealed, Money: []byte(`{"cost":0}`)}); got.Status != Recorded {
		t.Fatalf("a refund whose owner is not reached: %+v", got)
	}
	id := rowID(OwnerTerminal{Kind: record.Refund}, []byte(`{"cost":0}`))
	if !strings.HasPrefix(id, "refund-") || id == rowID(OwnerTerminal{Kind: record.Refund}, []byte(`{"cost":1}`)) {
		t.Fatalf("a refund's row ID %q", id)
	}
	checkEvents(t, f.ev, "owner node-b refund", "append "+id+" unreachable")
	want := store.DrainTerminal{Ref: store.LeaseRef{Workspace: e.Workspace, LeaseID: e.Lease}, AuthorizationID: e.Auth,
		RecordID: id, Kind: "refund", Estimate: 40, Money: []byte(`{"cost":0}`), Cause: "unreachable"}
	if !reflect.DeepEqual(f.store.appended, []store.DrainTerminal{want}) {
		t.Fatalf("appended %+v, want %+v", f.store.appended, want)
	}
	if got := f.door.Refund(ctx, RefundOf{Envelope: sealed}); got.Status != Invalid {
		t.Fatalf("a refund without money fields: %+v", got)
	}
}

// TestAClosedLeasesTerminalIsAnsweredFromItsDisposition: the drain log
// refuses a closed lease's terminal, and the authorization's disposition
// answers, as today's already_settled does (§4.5). An append or a
// disposition that fails is an error the gateway retries.
func TestAClosedLeasesTerminalIsAnsweredFromItsDisposition(t *testing.T) {
	ctx := context.Background()
	_, sealed := envelope(t)
	f := newDoor(t, 1)
	f.owners.unreachable["node-b"] = true
	f.store.refuse = true
	f.store.disposition = store.Disposition{Outcome: "reaped_snapshot", Cost: spanner.NullInt64{Int64: 31, Valid: true},
		From: "winner"}
	want := TerminalAnswer{Status: Settled, Outcome: "reaped_snapshot", Cost: 31, CostKnown: true}
	if got := f.door.Settle(ctx, settle(sealed)); got != want {
		t.Fatalf("a closed lease: %+v, want %+v", got, want)
	}
	if last := f.ev.all()[len(f.ev.all())-1]; last != "disposition gwa-1" {
		t.Fatalf("last %q", last)
	}
	f.store.disposition = store.Disposition{Outcome: "pending", From: "nowhere"}
	if got := f.door.Settle(ctx, settle(sealed)); got != (TerminalAnswer{Status: Settled, Outcome: "pending"}) {
		t.Fatalf("a closed lease with no winner known: %+v", got)
	}
	f.store.failDisp = true
	if got := f.door.Settle(ctx, settle(sealed)); got.Status != Failed {
		t.Fatalf("a disposition that failed: %+v", got)
	}
	f.store.failAppend = true
	if got := f.door.Settle(ctx, settle(sealed)); got.Status != Failed {
		t.Fatalf("an append that failed: %+v", got)
	}
}

// TestAGatewayThatStoppedWaitingAppendsNothing: a terminal whose request
// ended before its owner answered is not appended; the gateway retries it.
func TestAGatewayThatStoppedWaitingAppendsNothing(t *testing.T) {
	_, sealed := envelope(t)
	f := newDoor(t, 1)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if got := f.door.Refund(ctx, RefundOf{Envelope: sealed, Money: []byte(`{}`)}); got.Status != Failed {
		t.Fatalf("an ended request: %+v", got)
	}
	if len(f.store.appended) != 0 {
		t.Fatalf("appended %+v", f.store.appended)
	}
}

// TestAWorkspaceNotEnabledIsOff: an authorize for a workspace the switch has
// not enabled is Off and reaches no owner, while one for an enabled
// workspace is taken as before.
func TestAWorkspaceNotEnabledIsOff(t *testing.T) {
	ctx := context.Background()
	f := newDoor(t, 1)
	f.door.cfg.Enabled = func(ws string) bool { return ws == "ws-1" }
	f.owners.admitted[pick("request-1", 1)] = OwnerAdmitted{Status: Admitted, Envelope: "sealed",
		EndOfLife: start.Add(time.Hour)}
	off := f.door.Authorize(ctx, AuthorizeOf{Workspace: "ws-2", Request: "request-1", Estimate: 40, Boot: []byte("boot")})
	if off != (Authorized{Status: Off}) {
		t.Fatalf("a workspace not enabled: %+v", off)
	}
	checkEvents(t, f.ev)
	on := f.door.Authorize(ctx, AuthorizeOf{Workspace: "ws-1", Request: "request-1", Estimate: 40, Boot: []byte("boot")})
	if on.Status != Admitted {
		t.Fatalf("an enabled workspace: %+v", on)
	}
}

// rotated is the key a rotation moves to from key.
var rotated = bytes.Repeat([]byte{9}, MinKeySize)

// third is a key a rotation after the next moves to.
var third = bytes.Repeat([]byte{11}, MinKeySize)

// TestOpenWithTakesEitherKeyOfARotation: an envelope sealed with any key
// given opens with them, the last of three too, and one sealed with a key
// not given does not.
func TestOpenWithTakesEitherKeyOfARotation(t *testing.T) {
	e, _ := envelope(t)
	for _, sealer := range [][]byte{key, rotated, third} {
		sealed, err := Seal(sealer, e)
		if err != nil {
			t.Fatal(err)
		}
		if got, err := OpenWith([][]byte{key, rotated, third}, sealed); err != nil || got != e {
			t.Fatalf("sealed with %x: %+v %v", sealer[0], got, err)
		}
	}
	other, err := Seal(bytes.Repeat([]byte{8}, MinKeySize), e)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := OpenWith([][]byte{key, rotated}, other); err != ErrSeal {
		t.Fatalf("a key not given: %v", err)
	}
}

// TestARotationsPhasesOverlap: in a rotation's first phase a node seals with
// the old key and accepts the new, in its second it seals with the new and
// accepts the old, and in a deploy's overlap a node of either phase takes
// the other's envelopes; a node that accepts no other key refuses one sealed
// with it. And a closed lease's terminal, sealed before the rotation and
// retried after it, is answered from its disposition, not refused.
func TestARotationsPhasesOverlap(t *testing.T) {
	ctx := context.Background()
	e, _ := envelope(t)
	sealedWith := func(k []byte) string {
		t.Helper()
		s, err := Seal(k, e)
		if err != nil {
			t.Fatal(err)
		}
		return s
	}
	door := func(seal []byte, accept ...[]byte) *doorFixture {
		t.Helper()
		f := newDoor(t, 1)
		f.door.cfg.Key, f.door.cfg.Accept = seal, accept
		f.owners.unreachable["node-b"] = true
		return f
	}
	first, second, both := door(key, rotated), door(rotated, key), door(third, key, rotated)
	for name, c := range map[string]struct {
		f      *doorFixture
		sealer []byte
	}{"the first phase takes the second's": {first, rotated}, "the second phase takes the first's": {second, key},
		"a node accepting two keys takes the second's": {both, rotated}} {
		// The envelope's owner is unreachable: a settle and a refund go to
		// the drain log, and a heartbeat is answered Retry, none refused.
		sealed := sealedWith(c.sealer)
		if got := c.f.door.Settle(ctx, settle(sealed)); got.Status != Recorded {
			t.Errorf("%s, a settle: %+v", name, got)
		}
		if got := c.f.door.Refund(ctx, RefundOf{Envelope: sealed, Money: []byte(`{"cost":0}`)}); got.Status != Recorded {
			t.Errorf("%s, a refund: %+v", name, got)
		}
		if got := c.f.door.Heartbeat(ctx, HeartbeatOf{Envelope: sealed, GatewaySeq: 1}); got.Status != Retry {
			t.Errorf("%s, a heartbeat: %+v", name, got)
		}
	}
	unrotated := door(rotated)
	sealed := sealedWith(key)
	if got := unrotated.door.Settle(ctx, settle(sealed)); got.Status != Invalid {
		t.Errorf("a node that accepts no other key took the old one's settle: %+v", got)
	}
	if got := unrotated.door.Refund(ctx, RefundOf{Envelope: sealed, Money: []byte(`{"cost":0}`)}); got.Status != Invalid {
		t.Errorf("a node that accepts no other key took the old one's refund: %+v", got)
	}
	if got := unrotated.door.Heartbeat(ctx, HeartbeatOf{Envelope: sealed, GatewaySeq: 1}); got.Status != Invalid {
		t.Errorf("a node that accepts no other key took the old one's heartbeat: %+v", got)
	}
	late := door(rotated, key)
	late.store.refuse = true
	late.store.disposition = store.Disposition{Outcome: "settled", Cost: spanner.NullInt64{Int64: 55, Valid: true},
		From: "winner"}
	want := TerminalAnswer{Status: Settled, Outcome: "settled", Cost: 55, CostKnown: true}
	if got := late.door.Settle(ctx, settle(sealedWith(key))); got != want {
		t.Errorf("a closed lease's late retry, sealed before the rotation: %+v, want %+v", got, want)
	}
}

// TestAnAcceptedKeyIsAWholeKey: a key accepted beside the fleet's is at
// least as long as the fleet's must be.
func TestAnAcceptedKeyIsAWholeKey(t *testing.T) {
	f := newDoor(t, 1)
	cfg := f.door.cfg
	cfg.Accept = [][]byte{rotated, rotated[:MinKeySize-1]}
	if _, err := New(cfg); err == nil {
		t.Fatal("a short accepted key is taken")
	}
	cfg.Accept = [][]byte{rotated}
	if _, err := New(cfg); err != nil {
		t.Fatalf("a whole accepted key: %v", err)
	}
}
