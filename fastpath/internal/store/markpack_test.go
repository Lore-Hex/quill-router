package store

import (
	"context"
	"fmt"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"cloud.google.com/go/spanner"
	"cloud.google.com/go/spanner/apiv1/spannerpb"
	"google.golang.org/api/option"
	"google.golang.org/grpc"
)

const exclusiveHint = "@{LOCK_SCANNED_RANGES=EXCLUSIVE} "

// TestMarkStatementsLockWhatTheyDecideOn pins MarkPackDone's statements: the
// two a marker's transaction begins with take their ranges exclusively and
// read nothing else (the hint takes every range a statement reads), one a
// pack's row by its whole key, the other a lease's pending packs through
// their index; the retirement is unhinted and guarded.
func TestMarkStatementsLockWhatTheyDecideOn(t *testing.T) {
	for name, sql := range map[string]string{"markPackDone": markPackDone, "lockPendingPacks": lockPendingPacks} {
		if !strings.HasPrefix(sql, exclusiveHint) {
			t.Errorf("%s does not take its range exclusively: %s", name, sql)
		}
		if n := strings.Count(sql, "SELECT"); n > 1 || (n == 1 && !strings.HasPrefix(sql, exclusiveHint+"SELECT")) {
			t.Errorf("%s has a subquery, which the hint would take exclusively too: %s", name, sql)
		}
	}
	for _, want := range []string{"UPDATE tr_lease_winners ",
		"WHERE workspace_id = @w AND lease_id = @l AND commit_version = @v AND work_done_at IS NULL"} {
		if !strings.Contains(markPackDone, want) {
			t.Errorf("markPackDone lacks %q: %s", want, markPackDone)
		}
	}
	for _, want := range []string{"FROM tr_lease_winners@{FORCE_INDEX=tr_lease_winners_by_work}",
		"WHERE work_done_at IS NULL AND workspace_id = @w AND lease_id = @l"} {
		if !strings.Contains(lockPendingPacks, want) {
			t.Errorf("lockPendingPacks lacks %q: %s", want, lockPendingPacks)
		}
	}
	if strings.Contains(retireLease, "@{") || strings.Contains(retireLease, "SELECT") {
		t.Errorf("retireLease is hinted or reads another table: %s", retireLease)
	}
	for _, guard := range []string{"workspace_id = @w AND lease_id = @l", "state = 'closed'", "retire_at IS NULL"} {
		if !strings.Contains(retireLease, guard) {
			t.Errorf("retireLease lost its guard %q", guard)
		}
	}
}

// markCalls records, in order, the calls a client makes for MarkPackDone:
// each statement or read with how it names its transaction, and each commit
// and rollback. A rollback carries no tag, so a test records it only where
// nothing else rolls back.
type markCalls struct {
	mu    sync.Mutex
	calls []string
}

func (c *markCalls) note(m any) {
	var what string
	var txn *spannerpb.TransactionSelector
	switch r := m.(type) {
	case *spannerpb.ExecuteSqlRequest:
		if r.GetRequestOptions().GetRequestTag() != tag("mark-pack-done") {
			return
		}
		what, txn = strings.Join(strings.Fields(r.GetSql()), " "), r.GetTransaction()
	case *spannerpb.ReadRequest:
		if r.GetRequestOptions().GetRequestTag() != tag("mark-pack-done") {
			return
		}
		what, txn = "read "+r.GetTable()+" "+strings.Join(r.GetColumns(), ","), r.GetTransaction()
	case *spannerpb.CommitRequest:
		if r.GetRequestOptions().GetTransactionTag() != tag("mark-pack-done") {
			return
		}
		what = "commit"
	case *spannerpb.RollbackRequest:
		what = "rollback"
	default:
		return
	}
	switch {
	case txn.GetSingleUse() != nil:
		what = "single-use " + what
	case txn.GetBegin() != nil:
		what = "begin " + what
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	c.calls = append(c.calls, what)
}

// take is what was recorded since the last take.
func (c *markCalls) take() []string {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := c.calls
	c.calls = nil
	return out
}

// recordingStore is a store of the shared database through a client of its
// own whose MarkPackDone calls c records. The client closes when the test
// ends.
func recordingStore(t *testing.T, c *markCalls) *Store {
	t.Helper()
	if emulator == nil {
		t.Skip(skipped)
	}
	unary := func(ctx context.Context, method string, req, reply any, cc *grpc.ClientConn, invoker grpc.UnaryInvoker,
		opts ...grpc.CallOption) error {
		c.note(req)
		return invoker(ctx, method, req, reply, cc, opts...)
	}
	stream := func(ctx context.Context, desc *grpc.StreamDesc, cc *grpc.ClientConn, method string,
		streamer grpc.Streamer, opts ...grpc.CallOption) (grpc.ClientStream, error) {
		s, err := streamer(ctx, desc, cc, method, opts...)
		if err != nil {
			return s, err
		}
		return noted{ClientStream: s, c: c}, nil
	}
	client, err := emulator.Client(context.Background(), "spike",
		option.WithGRPCDialOption(grpc.WithChainUnaryInterceptor(unary)),
		option.WithGRPCDialOption(grpc.WithChainStreamInterceptor(stream)))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(client.Close)
	s, err := New(client, testConfig())
	if err != nil {
		t.Fatal(err)
	}
	return s
}

// noted records a stream's requests.
type noted struct {
	grpc.ClientStream
	c *markCalls
}

func (s noted) SendMsg(m any) error {
	s.c.note(m)
	return s.ClientStream.SendMsg(m)
}

// withPacks is a draining lease with n packs whose work is pending,
// versions 1 to n: drained's commit, one that decides a1 with the holds
// listed, and empty commits after it, so that a close is taken. It returns
// the lease, its version and the drain log's read time a close needs.
func withPacks(t *testing.T, s *Store, n int) (LeaseRef, int64, time.Time) {
	t.Helper()
	ref, version := drained(t, s, 100)
	listed := int64(2)
	got := commitOne(t, s, CommitRequest{Ref: ref, ReadVersion: version, AppliedSeq: 2, HoldsListedSeq: &listed,
		Winners: []Winner{{AuthorizationID: "a1", Kind: "refund", RecordID: "o3"}}})
	for got.NewVersion < int64(n) {
		got = commitOne(t, s, CommitRequest{Ref: ref, ReadVersion: got.NewVersion, AppliedSeq: 2, HoldsListedSeq: &listed})
	}
	if got.Refused != "" || got.NewVersion != int64(n) {
		t.Fatalf("commit: %+v", got)
	}
	_, read, err := s.ReadDrainSince(context.Background(), ref, time.Time{})
	if err != nil {
		t.Fatal(err)
	}
	if pending := pendingOf(t, ref); pending != n {
		t.Fatalf("%d packs pending, want %d", pending, n)
	}
	return ref, got.NewVersion, read
}

func closeOf(t *testing.T, s *Store, ref LeaseRef, version int64, read time.Time) {
	t.Helper()
	if c, err := s.CloseLease(context.Background(), ref, version, read, time.Now()); err != nil || c.Refused != "" {
		t.Errorf("close: %+v %v", c, err)
	}
}

// pendingOf is how many of the lease's packs have work pending.
func pendingOf(t *testing.T, ref LeaseRef) int {
	t.Helper()
	var n int64
	err := shared.Single().Query(context.Background(), spanner.Statement{
		SQL:    `SELECT COUNT(*) FROM tr_lease_winners WHERE workspace_id = @w AND lease_id = @l AND work_done_at IS NULL`,
		Params: ref.params(),
	}).Do(func(row *spanner.Row) error { return row.Column(0, &n) })
	if err != nil {
		t.Fatal(err)
	}
	return int(n)
}

// settled says whether the lease is closed and its row may go, and fails
// the test where its retire_at is before its close or a pack's mark: the
// row's time is set once both have happened (§4.8).
func settled(t *testing.T, ref LeaseRef) (closed, retiring bool) {
	t.Helper()
	var closedAt, retire, lastDone spanner.NullTime
	err := shared.Single().Query(context.Background(), spanner.Statement{
		SQL: `SELECT closed_at, retire_at,
		             (SELECT MAX(work_done_at) FROM tr_lease_winners WHERE workspace_id = @w AND lease_id = @l)
		        FROM tr_lease WHERE workspace_id = @w AND lease_id = @l`,
		Params: ref.params(),
	}).Do(func(row *spanner.Row) error { return row.Columns(&closedAt, &retire, &lastDone) })
	if err != nil {
		t.Fatal(err)
	}
	if retire.Valid && (!closedAt.Valid || retire.Time.Before(closedAt.Time) ||
		(lastDone.Valid && retire.Time.Before(lastDone.Time))) {
		t.Errorf("retire_at %v before the close %v or the last mark %v", retire, closedAt, lastDone)
	}
	return closedAt.Valid, retire.Valid
}

// TestEachMarkFirstLocksWhatItDecidesOn: the first read of every
// transaction MarkPackDone runs, the one Spanner ages it from, is the
// exclusive lock it decides under, so two markers that want one lock queue
// in the order they asked. An open lease's marker locks its pack's row and
// then reads the state a close writes; a closed lease's locks the lease's
// pending packs, then marks, then retires the lease after its last. Which
// is a strong read, outside any transaction.
func TestEachMarkFirstLocksWhatItDecidesOn(t *testing.T) {
	c := &markCalls{}
	s := recordingStore(t, c)
	ctx := context.Background()
	ref, version, read := withPacks(t, s, 3)
	sql := func(statement string) string { return strings.Join(strings.Fields(statement), " ") }
	state := "single-use read tr_lease state"
	open := []string{state, "begin " + sql(markPackDone), "read tr_lease state", "commit"}
	closed := []string{state, "begin " + sql(lockPendingPacks), sql(markPackDone), "commit"}
	last := []string{state, "begin " + sql(lockPendingPacks), sql(markPackDone), sql(retireLease), "commit"}
	again := []string{state, "begin " + sql(lockPendingPacks), "commit"}
	mark := func(v int64, want bool, calls []string) {
		t.Helper()
		if ok, err := s.MarkPackDone(ctx, ref, v); err != nil || ok != want {
			t.Fatalf("mark %d: %v %v", v, ok, err)
		}
		if got := c.take(); fmt.Sprint(got) != fmt.Sprint(calls) {
			t.Errorf("mark %d:\n got  %q\n want %q", v, got, calls)
		}
	}
	mark(1, true, open)
	closeOf(t, s, ref, version, read)
	mark(2, true, closed)
	mark(3, true, last)
	mark(3, false, again)
	if closed, retiring := settled(t, ref); !closed || !retiring {
		t.Fatalf("closed %v, retiring %v", closed, retiring)
	}
}

// TestALeaseClosedAfterTheMarkReadItIsDecidedClosed: a close that commits
// between MarkPackDone's read of the lease and its transaction is found in
// the transaction, which rolls back and marks the pack as a closed lease's,
// retiring the lease after its last pack and not before.
func TestALeaseClosedAfterTheMarkReadItIsDecidedClosed(t *testing.T) {
	for _, packs := range []int{2, 3} {
		c := &markCalls{}
		s := recordingStore(t, c)
		ctx := context.Background()
		ref, version, read := withPacks(t, s, packs)
		if ok, err := s.MarkPackDone(ctx, ref, 1); err != nil || !ok {
			t.Fatalf("mark 1: %v %v", ok, err)
		}
		s.markStateRead = func() {
			s.markStateRead = nil
			closeOf(t, s, ref, version, read)
		}
		c.take()
		if ok, err := s.MarkPackDone(ctx, ref, 2); err != nil || !ok {
			t.Fatalf("mark 2: %v %v", ok, err)
		}
		sql := func(statement string) string { return strings.Join(strings.Fields(statement), " ") }
		want := []string{"single-use read tr_lease state", "begin " + sql(markPackDone), "read tr_lease state", "rollback",
			"begin " + sql(lockPendingPacks), sql(markPackDone)}
		if packs == 2 {
			want = append(want, sql(retireLease))
		}
		want = append(want, "commit")
		if got := c.take(); fmt.Sprint(got) != fmt.Sprint(want) {
			t.Errorf("%d packs:\n got  %q\n want %q", packs, got, want)
		}
		if closed, retiring := settled(t, ref); !closed || retiring != (packs == 2) {
			t.Fatalf("%d packs: closed %v, retiring %v", packs, closed, retiring)
		}
		if packs == 3 {
			if ok, err := s.MarkPackDone(ctx, ref, 3); err != nil || !ok {
				t.Fatalf("mark 3: %v %v", ok, err)
			}
			if _, retiring := settled(t, ref); !retiring {
				t.Fatal("a closed lease with no work pending is kept")
			}
		}
	}
}

// TestMarkersAndACloseSettleInAnyOrder: two members mark each of a lease's
// packs at once, while the lease closes, or after it has closed. Each pack
// is marked exactly once, and the lease's row may go once it is closed and
// its last pack marked, whichever came last.
func TestMarkersAndACloseSettleInAnyOrder(t *testing.T) {
	s := spikeStore(t)
	const packs, members = 5, 2
	for round := range 6 {
		closeFirst := round%2 == 0
		ref, version, read := withPacks(t, s, packs)
		if closeFirst {
			closeOf(t, s, ref, version, read)
		}
		var wg sync.WaitGroup
		start := make(chan struct{})
		var marked [packs + 1]atomic.Int32
		for v := int64(1); v <= packs; v++ {
			for range members {
				wg.Go(func() {
					<-start
					ok, err := s.MarkPackDone(context.Background(), ref, v)
					if err != nil {
						t.Errorf("mark %d: %v", v, err)
					}
					if ok {
						marked[v].Add(1)
					}
				})
			}
		}
		if !closeFirst {
			wg.Go(func() {
				<-start
				closeOf(t, s, ref, version, read)
			})
		}
		close(start)
		wg.Wait()
		for v := 1; v <= packs; v++ {
			if n := marked[v].Load(); n != 1 {
				t.Errorf("round %d: pack %d marked %d times", round, v, n)
			}
		}
		if closed, retiring := settled(t, ref); pendingOf(t, ref) != 0 || !closed || !retiring {
			t.Fatalf("round %d: %d pending, closed %v, retiring %v", round, pendingOf(t, ref), closed, retiring)
		}
	}
}
