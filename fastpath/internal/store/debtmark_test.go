package store

import (
	"context"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"cloud.google.com/go/spanner"
	"cloud.google.com/go/spanner/apiv1/spannerpb"
	"google.golang.org/api/option"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/Lore-Hex/quill-router/fastpath/internal/store/storetest"
)

// creditRowsOf writes a workspace's credit rows, one per mark given (a
// spanner.NullBool{} writes in_debt NULL, as a row older than the column
// holds it), and returns the commit's timestamp.
func creditRowsOf(t *testing.T, workspace string, marks ...any) time.Time {
	t.Helper()
	var mutations []*spanner.Mutation
	for shard, mark := range marks {
		mutations = append(mutations, spanner.InsertMap("tr_credit_balance", map[string]any{
			"workspace_id": workspace, "shard": int64(shard), "total_credits": int64(1000), "in_debt": mark}))
	}
	at, err := shared.Apply(context.Background(), mutations)
	if err != nil {
		t.Fatal(err)
	}
	return at
}

// markAt sets the debt mark of one credit row of a workspace, or of every
// row (shard below 0), with DML, as the store's own writes do, and returns
// the commit's timestamp.
func markAt(t *testing.T, workspace string, shard int64, marked bool) time.Time {
	t.Helper()
	at, err := shared.ReadWriteTransaction(context.Background(), func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		_, err := txn.Update(ctx, spanner.Statement{
			SQL: `UPDATE tr_credit_balance SET in_debt = @marked
			       WHERE workspace_id = @w AND (@shard < 0 OR shard = @shard)`,
			Params: map[string]any{"marked": marked, "w": workspace, "shard": shard},
		})
		return err
	})
	if err != nil {
		t.Fatal(err)
	}
	return at
}

// TestReadDebtMarkAtReadsTheMarkAsOfItsTimestamp: the read is of the
// workspace's credit rows as they stood at exactly its timestamp, and marked
// is any row's mark, the edge's own workspace.in_debt and Python's. Over one
// workspace's history (three rows unmarked, one of them NULL; every row
// marked; the mark cleared; one row alone marked) it reads what the decide
// shadow's triage reads at its two instants:
//
//   - unmarked at both: at the rows' first commit and just before the mark;
//   - marked at the later only: just before the mark's commit and at it;
//   - marked at both: at the mark's commit and just before it is cleared;
//   - marked at the earlier only: just before the clearing and at it;
//   - one row of several marked: a marked workspace.
//
// Before the rows were written it reads no rows, which is not an unmarked
// workspace's three. Another workspace's mark is not read, one whose id this
// one's begins included. No workspace or no timestamp reads nothing.
func TestReadDebtMarkAtReadsTheMarkAsOfItsTimestamp(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	ws := storetest.UniqueID("ws")
	written := creditRowsOf(t, ws, false, spanner.NullBool{}, false)
	// Neighbours marked throughout: an id this one begins, and one that
	// begins with this one.
	creditRowsOf(t, ws[:len(ws)-1], true)
	creditRowsOf(t, ws+"0", true)
	marked := markAt(t, ws, -1, true)
	cleared := markAt(t, ws, -1, false)
	one := markAt(t, ws, 1, true)
	before := func(at time.Time) time.Time { return at.Add(-time.Microsecond) }
	for _, x := range []struct {
		name string
		at   time.Time
		want DebtMark
	}{
		{"before the rows were written", before(written), DebtMark{}},
		{"at the rows' commit", written, DebtMark{Rows: 3}},
		{"just before the mark", before(marked), DebtMark{Rows: 3}},
		{"at the mark's commit", marked, DebtMark{Rows: 3, Marked: true}},
		{"just before the mark is cleared", before(cleared), DebtMark{Rows: 3, Marked: true}},
		{"at the clearing's commit", cleared, DebtMark{Rows: 3}},
		{"just before one row is marked", before(one), DebtMark{Rows: 3}},
		{"with one row of three marked", one, DebtMark{Rows: 3, Marked: true}},
	} {
		got, err := s.ReadDebtMarkAt(ctx, ws, x.at)
		if err != nil || got != x.want {
			t.Errorf("%s: %+v, %v; want %+v", x.name, got, err, x.want)
		}
	}
	// The same history through the triage's pairs of instants.
	for _, x := range []struct {
		name           string
		earlier, later time.Time
		want           [2]bool
	}{
		{"unmarked at both", written, before(marked), [2]bool{false, false}},
		{"marked at the later only", before(marked), marked, [2]bool{false, true}},
		{"marked at both", marked, before(cleared), [2]bool{true, true}},
		{"marked at the earlier only", before(cleared), cleared, [2]bool{true, false}},
		{"one row of three marked at the later", cleared, one, [2]bool{false, true}},
	} {
		earlier, err1 := s.ReadDebtMarkAt(ctx, ws, x.earlier)
		later, err2 := s.ReadDebtMarkAt(ctx, ws, x.later)
		if err1 != nil || err2 != nil || [2]bool{earlier.Marked, later.Marked} != x.want || earlier.Rows != 3 ||
			later.Rows != 3 {
			t.Errorf("%s: %+v and %+v (%v, %v); want marks %v", x.name, earlier, later, err1, err2, x.want)
		}
	}
	if got, err := s.ReadDebtMarkAt(ctx, storetest.UniqueID("ws"), one); err != nil || got != (DebtMark{}) {
		t.Errorf("a workspace with no rows: %+v, %v", got, err)
	}
	if _, err := s.ReadDebtMarkAt(ctx, "", one); err == nil {
		t.Error("a read of no workspace")
	}
	if _, err := s.ReadDebtMarkAt(ctx, ws, time.Time{}); err == nil {
		t.Error("a read with no timestamp")
	}
}

// TestTheDebtMarkIsTheGatesOwnFact: over the same rows, the triage's read
// says marked exactly when the edge's own credit fact does (ReadCreditState,
// which the cache holds and workspace.in_debt gates on, by its InDebt or its
// refusal): no row marked, a mark never written (NULL), every row marked,
// and one row of several marked, the first or the last. Were the two to
// read the mark apart, the triage would clear what the gate should stop, or
// page what the gate rightly let by.
func TestTheDebtMarkIsTheGatesOwnFact(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	for _, x := range []struct {
		name  string
		marks []any
		want  bool
	}{
		{"no row marked", []any{false, false}, false},
		{"a mark never written", []any{spanner.NullBool{}}, false},
		{"every row marked", []any{true, true, true}, true},
		{"the first row of three marked", []any{true, false, false}, true},
		{"the last row of three marked", []any{false, spanner.NullBool{}, true}, true},
	} {
		ws := storetest.UniqueID("ws")
		written := creditRowsOf(t, ws, x.marks...)
		fact, err := s.ReadCreditState(ctx, ws)
		if err != nil {
			t.Fatalf("%s: the gate's fact: %v", x.name, err)
		}
		mark, err := s.ReadDebtMarkAt(ctx, ws, written)
		if err != nil {
			t.Fatalf("%s: the triage's read: %v", x.name, err)
		}
		if fact.InDebt != x.want || (fact.Refused == RefusedInDebt) != x.want || mark.Marked != x.want ||
			mark.Rows != len(x.marks) || fact.Shards != len(x.marks) {
			t.Errorf("%s: the gate's fact %+v, the triage's read %+v; want marked %v", x.name, fact, mark, x.want)
		}
	}
}

// debtReads is a store of the shared database whose debt mark reads are
// kept as they are sent, each one's request, and whose reads fail with fail
// when it is set, before anything is sent; begun counts every read begun,
// failed or not.
type debtReads struct {
	mu    sync.Mutex
	sent  []*spannerpb.ReadRequest
	fail  error
	begun int
}

func (d *debtReads) store(t *testing.T) *Store {
	t.Helper()
	if emulator == nil {
		t.Skip(skipped)
	}
	stream := func(ctx context.Context, desc *grpc.StreamDesc, cc *grpc.ClientConn, method string,
		streamer grpc.Streamer, opts ...grpc.CallOption) (grpc.ClientStream, error) {
		reading := strings.HasSuffix(method, "/StreamingRead")
		d.mu.Lock()
		fail := d.fail
		if reading {
			d.begun++
		}
		d.mu.Unlock()
		if fail != nil && reading {
			return nil, fail
		}
		s, err := streamer(ctx, desc, cc, method, opts...)
		return debtStream{ClientStream: s, reads: d}, err
	}
	c, err := emulator.Client(context.Background(), "spike",
		option.WithGRPCDialOption(grpc.WithChainStreamInterceptor(stream)))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(c.Close)
	s, err := New(c, testConfig())
	if err != nil {
		t.Fatal(err)
	}
	return s
}

func (d *debtReads) taken() []*spannerpb.ReadRequest {
	d.mu.Lock()
	defer d.mu.Unlock()
	out := d.sent
	d.sent = nil
	return out
}

type debtStream struct {
	grpc.ClientStream
	reads *debtReads
}

func (s debtStream) SendMsg(m any) error {
	if r, ok := m.(*spannerpb.ReadRequest); ok && r.GetRequestOptions().GetRequestTag() == tag("read-debt-mark-at") {
		s.reads.mu.Lock()
		s.reads.sent = append(s.reads.sent, r)
		s.reads.mu.Unlock()
	}
	return s.ClientStream.SendMsg(m)
}

// TestReadDebtMarkAtIsOneBoundedLowPriorityReadByKey: what Spanner is sent
// for one read. One request and no more: a read of tr_credit_balance by the
// workspace's primary-key prefix alone (the closed range of keys beginning
// with the workspace id, so no other workspace's rows and no scan), through
// no index, of in_debt alone, single-use and read-only at exactly the
// timestamp given (no strong read, no staleness bound that Spanner may
// choose within), at low priority, under its own request tag.
func TestReadDebtMarkAtIsOneBoundedLowPriorityReadByKey(t *testing.T) {
	reads := &debtReads{}
	s := reads.store(t)
	ctx := context.Background()
	ws := storetest.UniqueID("ws")
	written := creditRowsOf(t, ws, false, true)
	if got, err := s.ReadDebtMarkAt(ctx, ws, written); err != nil || got != (DebtMark{Rows: 2, Marked: true}) {
		t.Fatalf("the read: %+v, %v", got, err)
	}
	sent := reads.taken()
	if len(sent) != 1 {
		t.Fatalf("%d requests for one read", len(sent))
	}
	r := sent[0]
	if r.GetTable() != "tr_credit_balance" || r.GetIndex() != "" || !slices.Equal(r.GetColumns(), []string{"in_debt"}) {
		t.Errorf("the read's table, index and columns: %q %q %v", r.GetTable(), r.GetIndex(), r.GetColumns())
	}
	keys := r.GetKeySet()
	if keys.GetAll() || len(keys.GetKeys()) != 0 || len(keys.GetRanges()) != 1 {
		t.Fatalf("the read's keys are not one range: %v", keys)
	}
	start, end := keys.GetRanges()[0].GetStartClosed().GetValues(), keys.GetRanges()[0].GetEndClosed().GetValues()
	if len(start) != 1 || start[0].GetStringValue() != ws || len(end) != 1 || end[0].GetStringValue() != ws {
		t.Errorf("the read's range is not the workspace's prefix, closed at both ends: %v", keys.GetRanges()[0])
	}
	bound := r.GetTransaction().GetSingleUse().GetReadOnly()
	if bound == nil || bound.GetReadTimestamp() == nil || !bound.GetReadTimestamp().AsTime().Equal(written) ||
		bound.GetStrong() {
		t.Errorf("the read's bound is not exactly its timestamp, single-use and read-only: %v", r.GetTransaction())
	}
	if o := r.GetRequestOptions(); o.GetPriority() != spannerpb.RequestOptions_PRIORITY_LOW ||
		o.GetRequestTag() != "fastpath-read-debt-mark-at" || o.GetTransactionTag() != "" {
		t.Errorf("the read's options: %v", o)
	}
}

// TestAFailedDebtMarkReadIsAnErrorAndNotAnUnmarkedWorkspace: for a workspace
// whose rows are marked, every read that cannot be served returns an error
// and no mark, never rows read as unmarked: one whose deadline has passed;
// one at a timestamp past the database's version retention, which Spanner
// refuses (FailedPrecondition) and the store sends once, not again; one a
// test's Spanner refuses outright; and one at a timestamp ahead of Spanner's
// clock, which Spanner holds until that instant, so it fails at its deadline
// when the deadline comes first. A timestamp ahead by less than the deadline
// is served once Spanner's clock reaches it, with what stood then.
func TestAFailedDebtMarkReadIsAnErrorAndNotAnUnmarkedWorkspace(t *testing.T) {
	reads := &debtReads{}
	s := reads.store(t)
	ws := storetest.UniqueID("ws")
	written := creditRowsOf(t, ws, true, true)
	want := DebtMark{Rows: 2, Marked: true}
	ended, cancel := context.WithDeadline(context.Background(), time.Now().Add(-time.Second))
	defer cancel()
	if got, err := s.ReadDebtMarkAt(ended, ws, written); err == nil || got != (DebtMark{}) {
		t.Errorf("a read past its deadline: %+v, %v", got, err)
	}
	begun := func() int {
		reads.mu.Lock()
		defer reads.mu.Unlock()
		return reads.begun
	}
	before := begun()
	got, err := s.ReadDebtMarkAt(context.Background(), ws, written.Add(-2*time.Hour))
	if status.Code(err) != codes.FailedPrecondition || got != (DebtMark{}) {
		t.Errorf("a read past the version retention: %+v, %v", got, err)
	}
	if n := begun() - before; n != 1 {
		t.Errorf("a read Spanner refused was begun %d times", n)
	}
	reads.mu.Lock()
	reads.fail = status.Error(codes.PermissionDenied, "synthetic refusal")
	reads.mu.Unlock()
	before = begun()
	if got, err := s.ReadDebtMarkAt(context.Background(), ws, written); status.Code(err) != codes.PermissionDenied ||
		got != (DebtMark{}) {
		t.Errorf("a read Spanner refuses outright: %+v, %v", got, err)
	}
	if n := begun() - before; n != 1 {
		t.Errorf("a read refused outright was begun %d times", n)
	}
	reads.mu.Lock()
	reads.fail = nil
	reads.mu.Unlock()
	short, cancelShort := context.WithTimeout(context.Background(), 200*time.Millisecond)
	defer cancelShort()
	if got, err := s.ReadDebtMarkAt(short, ws, time.Now().Add(time.Hour)); status.Code(err) != codes.DeadlineExceeded ||
		got != (DebtMark{}) {
		t.Errorf("a read an hour ahead of Spanner's clock, within 200 ms: %+v, %v", got, err)
	}
	// Ahead by Spanner's own clock, whatever this machine's says: 400 ms
	// past a commit made after began, so Spanner's clock reaches it no
	// sooner than 400 ms after began.
	long, cancelLong := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancelLong()
	began := time.Now()
	ahead := markAt(t, ws, -1, true).Add(400 * time.Millisecond)
	if got, err := s.ReadDebtMarkAt(long, ws, ahead); err != nil || got != want {
		t.Errorf("a read 400 ms ahead of Spanner's clock, within 30 s: %+v, %v", got, err)
	} else if time.Since(began) < 400*time.Millisecond {
		t.Error("a read ahead of Spanner's clock was served before its timestamp")
	}
	if got, err := s.ReadDebtMarkAt(context.Background(), ws, written); err != nil || got != want {
		t.Errorf("the read once Spanner serves it: %+v, %v", got, err)
	}
}
