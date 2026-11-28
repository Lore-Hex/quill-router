package store

import (
	"context"
	"errors"
	"fmt"
	"io"
	"slices"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"cloud.google.com/go/spanner"
	"cloud.google.com/go/spanner/apiv1/spannerpb"
	"google.golang.org/api/option"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/structpb"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// The order a grant takes its locks in, and that it gives them up when its
// context ends (2026-11-26: a grant held shared locks on every credit row of
// a busy workspace while it waited for a lease row an auditor's commit held,
// and everything that needed one of those credit rows waited behind it). The
// emulator runs one read-write transaction at a time, so no test shows a
// lock wait; these pin what decides one: which rows a grant has asked for
// when it asks for the next, and that a grant cut off sends its rollback and
// never its commit.

// shape names a statement or a read by what it locks: its verb and its
// table, as "select tr_lease" or "read tr_fastpath_member".
func shape(m any) string {
	switch r := m.(type) {
	case *spannerpb.ReadRequest:
		return "read " + r.GetTable()
	case *spannerpb.ExecuteSqlRequest:
		words := strings.Fields(r.GetSql())
		for i, w := range words {
			switch strings.ToUpper(w) {
			case "FROM", "UPDATE", "INTO":
				if i+1 < len(words) {
					table, _, _ := strings.Cut(words[i+1], "@")
					return strings.ToLower(words[0]) + " " + table
				}
			}
		}
		return strings.ToLower(strings.Join(words, " "))
	case *spannerpb.CommitRequest:
		return "commit"
	case *spannerpb.RollbackRequest:
		return "rollback"
	}
	return ""
}

// grantTap sees the calls a client makes. It records a grant's, each by its
// shape, with the SQL of its statements; and it holds the call whose shape
// is hold, the nth of that shape, until the call's context ends, answering
// as a call that ran out of time does, so a test cuts a grant off at a
// statement of its choosing.
type grantTap struct {
	mu     sync.Mutex
	calls  []string
	sql    []string
	hold   string
	holdAt int
	seen   map[string]int
}

func (g *grantTap) see(ctx context.Context, m any) error {
	what := shape(m)
	switch r := m.(type) {
	case *spannerpb.ReadRequest:
		if r.GetRequestOptions().GetRequestTag() != tag("grant") {
			return nil
		}
	case *spannerpb.ExecuteSqlRequest:
		if r.GetRequestOptions().GetRequestTag() != tag("grant") {
			return nil
		}
	case *spannerpb.CommitRequest:
		if r.GetRequestOptions().GetTransactionTag() != tag("grant") {
			return nil
		}
	case *spannerpb.RollbackRequest:
	default:
		return nil
	}
	g.mu.Lock()
	g.calls = append(g.calls, what)
	if r, ok := m.(*spannerpb.ExecuteSqlRequest); ok {
		g.sql = append(g.sql, strings.Join(strings.Fields(r.GetSql()), " "))
	}
	if g.seen == nil {
		g.seen = map[string]int{}
	}
	n := g.seen[what]
	g.seen[what]++
	held := g.hold == what && n == g.holdAt
	g.mu.Unlock()
	if !held {
		return nil
	}
	<-ctx.Done()
	return status.FromContextError(ctx.Err()).Err()
}

// holdNth has the tap hold the nth call of a shape, from 0; "" holds none.
func (g *grantTap) holdNth(what string, n int) {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.hold, g.holdAt, g.seen = what, n, nil
}

func (g *grantTap) taken() []string {
	g.mu.Lock()
	defer g.mu.Unlock()
	out := g.calls
	g.calls = nil
	return out
}

func (g *grantTap) statements() []string {
	g.mu.Lock()
	defer g.mu.Unlock()
	return slices.Clone(g.sql)
}

// scriptedRows is what a scripted Spanner answers a grant's reads with: the
// workspace's credit rows, each its headroom, and whether they are marked in
// debt; and its leases, each the remaining allocation of an open one.
type scriptedRows struct {
	headroom []int64
	marked   bool
	leases   []int64
	expiry   time.Time
}

// scripted is a Spanner with no server behind it: a client connection whose
// interceptors answer each call themselves, a grant's reads from rows and
// its writes as one row written. It takes no locks and keeps no rows; it is
// for the order of a grant's calls and for cutting one off, which the tap
// sees, and the emulator's tests hold what a grant writes.
type scripted struct {
	tap  *grantTap
	rows scriptedRows
	mu   sync.Mutex
	next int
}

func text(s string) *structpb.Value { return structpb.NewStringValue(s) }
func whole(n int64) *structpb.Value { return structpb.NewStringValue(strconv.FormatInt(n, 10)) }
func truth(b bool) *structpb.Value  { return structpb.NewBoolValue(b) }
func field(code spannerpb.TypeCode) *spannerpb.StructType_Field {
	return &spannerpb.StructType_Field{Type: &spannerpb.Type{Code: code}}
}

// answer is a call's result: the columns' types, the rows' values in order,
// and for a statement that writes, how many rows it wrote.
func (s *scripted) answer(m any) (fields []*spannerpb.StructType_Field, values []*structpb.Value, written int64) {
	const (
		str, num, flag = spannerpb.TypeCode_STRING, spannerpb.TypeCode_INT64, spannerpb.TypeCode_BOOL
	)
	switch shape(m) {
	case "read tr_lease":
		// No lease has the request's ID: a first ask.
		for range m.(*spannerpb.ReadRequest).GetColumns() {
			fields = append(fields, field(str))
		}
	case "read tr_fastpath_member":
		fields = []*spannerpb.StructType_Field{field(str), field(num)}
		values = []*structpb.Value{text(Serving), whole(1)}
	case "read tr_fastpath_workspace":
		fields = []*spannerpb.StructType_Field{field(flag)}
		values = []*structpb.Value{truth(true)}
	case "select tr_lease":
		fields = []*spannerpb.StructType_Field{field(str), field(str), field(num), field(flag)}
		for i, remaining := range s.rows.leases {
			values = append(values, text(fmt.Sprintf("lease-%d", i)), text("open"), whole(remaining), truth(false))
		}
	case "select tr_credit_balance":
		causes := &spannerpb.StructType_Field{Type: &spannerpb.Type{Code: spannerpb.TypeCode_ARRAY,
			ArrayElementType: &spannerpb.Type{Code: str}}}
		fields = []*spannerpb.StructType_Field{field(num), field(num), field(flag), field(num), field(flag), causes}
		for shard, h := range s.rows.headroom {
			values = append(values, whole(int64(shard)), whole(h), truth(s.rows.marked), whole(3), truth(false),
				structpb.NewListValue(&structpb.ListValue{}))
		}
	case "update tr_credit_balance":
		written = 1
	case "insert tr_lease":
		fields = []*spannerpb.StructType_Field{field(spannerpb.TypeCode_TIMESTAMP)}
		values = []*structpb.Value{text(s.rows.expiry.UTC().Format(time.RFC3339Nano))}
		written = 1
	}
	return fields, values, written
}

// metadata is a result's: its columns, and the transaction the call began,
// if it began one.
func (s *scripted) metadata(fields []*spannerpb.StructType_Field, txn *spannerpb.TransactionSelector) *spannerpb.ResultSetMetadata {
	out := &spannerpb.ResultSetMetadata{RowType: &spannerpb.StructType{Fields: fields}}
	if txn.GetBegin() != nil {
		s.mu.Lock()
		s.next++
		out.Transaction = &spannerpb.Transaction{Id: []byte(fmt.Sprintf("txn-%d", s.next))}
		s.mu.Unlock()
	}
	return out
}

func (s *scripted) unary(ctx context.Context, method string, req, reply any, _ *grpc.ClientConn, _ grpc.UnaryInvoker,
	_ ...grpc.CallOption) error {
	if err := s.tap.see(ctx, req); err != nil {
		return err
	}
	if err := ctx.Err(); err != nil {
		return status.FromContextError(err).Err()
	}
	switch r := req.(type) {
	case *spannerpb.CreateSessionRequest:
		proto.Merge(reply.(proto.Message), &spannerpb.Session{Name: r.GetDatabase() + "/sessions/scripted",
			Multiplexed: r.GetSession().GetMultiplexed()})
	case *spannerpb.ExecuteSqlRequest:
		fields, _, written := s.answer(r)
		proto.Merge(reply.(proto.Message), &spannerpb.ResultSet{Metadata: s.metadata(fields, r.GetTransaction()),
			Stats: &spannerpb.ResultSetStats{RowCount: &spannerpb.ResultSetStats_RowCountExact{RowCountExact: written}}})
	case *spannerpb.CommitRequest:
		proto.Merge(reply.(proto.Message), &spannerpb.CommitResponse{CommitTimestamp: timestamppb.Now()})
	case *spannerpb.BeginTransactionRequest:
		proto.Merge(reply.(proto.Message), s.metadata(nil, &spannerpb.TransactionSelector{
			Selector: &spannerpb.TransactionSelector_Begin{Begin: r.GetOptions()}}).GetTransaction())
	}
	return nil
}

func (s *scripted) stream(ctx context.Context, _ *grpc.StreamDesc, _ *grpc.ClientConn, _ string, _ grpc.Streamer,
	_ ...grpc.CallOption) (grpc.ClientStream, error) {
	return &scriptedStream{ctx: ctx, s: s}, nil
}

// scriptedStream is one streamed call: its request, then one answer.
type scriptedStream struct {
	ctx  context.Context
	s    *scripted
	req  any
	done bool
}

func (st *scriptedStream) Header() (metadata.MD, error) { return metadata.MD{}, nil }
func (st *scriptedStream) Trailer() metadata.MD         { return metadata.MD{} }
func (st *scriptedStream) CloseSend() error             { return nil }
func (st *scriptedStream) Context() context.Context     { return st.ctx }

func (st *scriptedStream) SendMsg(m any) error {
	st.req = m
	return st.s.tap.see(st.ctx, m)
}

func (st *scriptedStream) RecvMsg(m any) error {
	if err := st.ctx.Err(); err != nil {
		return status.FromContextError(err).Err()
	}
	if st.done {
		return io.EOF
	}
	st.done = true
	var txn *spannerpb.TransactionSelector
	switch r := st.req.(type) {
	case *spannerpb.ReadRequest:
		txn = r.GetTransaction()
	case *spannerpb.ExecuteSqlRequest:
		txn = r.GetTransaction()
	}
	fields, values, written := st.s.answer(st.req)
	out := &spannerpb.PartialResultSet{Metadata: st.s.metadata(fields, txn), Values: values}
	if written > 0 {
		out.Stats = &spannerpb.ResultSetStats{RowCount: &spannerpb.ResultSetStats_RowCountExact{RowCountExact: written}}
	}
	proto.Merge(m.(proto.Message), out)
	return nil
}

// scriptedStore is a store of a scripted Spanner that answers a grant's
// reads with rows, and the tap that sees its calls.
func scriptedStore(t *testing.T, rows scriptedRows) (*Store, *grantTap) {
	t.Helper()
	s := &scripted{tap: &grantTap{}, rows: rows}
	conn, err := grpc.NewClient("passthrough:///scripted", grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithChainUnaryInterceptor(s.unary), grpc.WithChainStreamInterceptor(s.stream))
	if err != nil {
		t.Fatal(err)
	}
	client, err := spanner.NewClientWithConfig(context.Background(), "projects/p/instances/i/databases/d",
		spanner.ClientConfig{DisableNativeMetrics: true}, option.WithGRPCConn(conn))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(client.Close)
	st, err := New(client, testConfig())
	if err != nil {
		t.Fatal(err)
	}
	return st, s.tap
}

// The calls of a grant that is granted from two donors, and of one the
// credit rows refuse, in the order it makes them: the lease the request's ID
// names, the owner's membership, the switch, the workspace's leases, and
// only then its credit rows.
var (
	grantReads   = []string{"read tr_lease", "read tr_fastpath_member", "read tr_fastpath_workspace", "select tr_lease", "select tr_credit_balance"}
	grantedCalls = append(slices.Clone(grantReads), "update tr_credit_balance", "update tr_credit_balance", "insert tr_lease", "commit")
)

func scriptedRequest() GrantRequest {
	return GrantRequest{Workspace: "w", LeaseID: "l", Region: "us-central1", Owner: Owner{Node: "node-a", Epoch: 1}, Amount: 50}
}

// TestGrantReadsItsLeasesBeforeItsCreditRows: a grant asks for the
// workspace's lease rows before any of its credit rows, as every other
// writer of the two tables does (a commit, a close, a shortfall, an append
// and a reap each write their lease's row and then its donors' credit
// rows), so a grant and a commit never each hold what the other asks for
// next, and a grant that waits for a lease row holds no credit row while it
// does. What it decides is as before: the donors in shard order, each up to
// its headroom, and the credit rows' refusal for a workspace in debt, which
// is now found after the leases are read and still writes nothing.
func TestGrantReadsItsLeasesBeforeItsCreditRows(t *testing.T) {
	expiry := time.Date(2026, 11, 26, 9, 0, 0, 0, time.UTC)
	s, tap := scriptedStore(t, scriptedRows{headroom: []int64{20, 100}, leases: []int64{7}, expiry: expiry})
	got, err := s.Grant(context.Background(), scriptedRequest())
	if err != nil || got.Refused != "" || !got.Expiry.Equal(expiry) ||
		!slices.Equal(got.Donors, []Donor{{Shard: 0, Allocation: 20}, {Shard: 1, Allocation: 30}}) {
		t.Fatalf("the grant: %+v %v", got, err)
	}
	if calls := tap.taken(); !slices.Equal(calls, grantedCalls) {
		t.Errorf("a grant's calls:\n got  %q\n want %q", calls, grantedCalls)
	}

	s, tap = scriptedStore(t, scriptedRows{headroom: []int64{20, 100}, marked: true})
	got, err = s.Grant(context.Background(), scriptedRequest())
	if err != nil || got.Refused != RefusedInDebt || len(got.Donors) != 0 {
		t.Fatalf("a grant for a workspace in debt: %+v %v", got, err)
	}
	if calls, want := tap.taken(), append(slices.Clone(grantReads), "commit"); !slices.Equal(calls, want) {
		t.Errorf("a refused grant's calls:\n got  %q\n want %q", calls, want)
	}
}

// TestGrantTakesNoRangeExclusively: none of a grant's statements carries the
// exclusive-scan hint. A grant reads every credit row of its workspace, and
// most asks of a workspace short of headroom are refused and write nothing;
// hinted, each would hold all of those rows exclusively until its
// transaction ended, against every reader that shares them today. A grant's
// time on them is bounded by the owner's deadline instead
// (owner.GrantDeadline). A statement that gains the hint is decided here.
func TestGrantTakesNoRangeExclusively(t *testing.T) {
	s, tap := scriptedStore(t, scriptedRows{headroom: []int64{20, 100}, expiry: time.Now()})
	if got, err := s.Grant(context.Background(), scriptedRequest()); err != nil || got.Refused != "" {
		t.Fatalf("the grant: %+v %v", got, err)
	}
	statements := tap.statements()
	if len(statements) != 5 {
		t.Fatalf("a grant from two donors ran %d statements, want 5: %q", len(statements), statements)
	}
	for _, sql := range statements {
		if strings.Contains(sql, "LOCK_SCANNED_RANGES") || strings.Contains(sql, "@{") {
			t.Errorf("a grant's statement is hinted: %s", sql)
		}
	}
	for i, want := range []string{
		"FROM tr_lease WHERE workspace_id = @w",
		"FROM tr_credit_balance WHERE workspace_id = @w ORDER BY shard",
	} {
		if !strings.HasPrefix(statements[i], "SELECT ") || !strings.HasSuffix(statements[i], want) {
			t.Errorf("statement %d reads other than %q: %s", i, want, statements[i])
		}
	}
}

// TestAGrantCutOffHoldsNoCreditRowBehindALeaseRow: a grant whose context
// ends while it waits for the workspace's lease rows has asked for no credit
// row, and it rolls back and commits nothing; cut off at any later
// statement, it has, and it rolls back and commits nothing all the same. A
// grant cut off at its first read has no transaction to roll back.
func TestAGrantCutOffHoldsNoCreditRowBehindALeaseRow(t *testing.T) {
	for at, want := range map[string][]string{
		"read tr_lease":            {"read tr_lease"},
		"read tr_fastpath_member":  {"read tr_lease", "read tr_fastpath_member", "rollback"},
		"select tr_lease":          {"read tr_lease", "read tr_fastpath_member", "read tr_fastpath_workspace", "select tr_lease", "rollback"},
		"select tr_credit_balance": append(slices.Clone(grantReads), "rollback"),
		"update tr_credit_balance": append(slices.Clone(grantReads), "update tr_credit_balance", "update tr_credit_balance", "rollback"),
		"insert tr_lease":          append(slices.Clone(grantReads), "update tr_credit_balance", "update tr_credit_balance", "insert tr_lease", "rollback"),
	} {
		s, tap := scriptedStore(t, scriptedRows{headroom: []int64{20, 100}, expiry: time.Now()})
		nth := 0
		if at == "update tr_credit_balance" {
			// The second donor's: the first's reservation is made, and
			// rolled back with the rest.
			nth = 1
		}
		tap.holdNth(at, nth)
		ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
		began := time.Now()
		got, err := s.Grant(ctx, scriptedRequest())
		cancel()
		if !errors.Is(err, context.DeadlineExceeded) && spanner.ErrCode(err) != codes.DeadlineExceeded {
			t.Errorf("cut off at %q: %+v %v, want its deadline's error", at, got, err)
		}
		if took := time.Since(began); took > 5*time.Second {
			t.Errorf("cut off at %q, the grant took %v to return", at, took)
		}
		if got.Refused != "" || len(got.Donors) != 0 || !got.Expiry.IsZero() || !got.CommitTS.IsZero() {
			t.Errorf("cut off at %q, the grant answered %+v", at, got)
		}
		if calls := tap.taken(); !slices.Equal(calls, want) {
			t.Errorf("cut off at %q:\n got  %q\n want %q", at, calls, want)
		}
	}
}

// tappedStore is a store of the shared database through a client of its own
// whose calls the tap sees, so a grant on the emulator can be cut off at one
// of its statements. The client closes when the test ends.
func tappedStore(t *testing.T, tap *grantTap) *Store {
	t.Helper()
	if emulator == nil {
		t.Skip(skipped)
	}
	unary := func(ctx context.Context, method string, req, reply any, cc *grpc.ClientConn, invoker grpc.UnaryInvoker,
		opts ...grpc.CallOption) error {
		if err := tap.see(ctx, req); err != nil {
			return err
		}
		return invoker(ctx, method, req, reply, cc, opts...)
	}
	stream := func(ctx context.Context, desc *grpc.StreamDesc, cc *grpc.ClientConn, method string,
		streamer grpc.Streamer, opts ...grpc.CallOption) (grpc.ClientStream, error) {
		s, err := streamer(ctx, desc, cc, method, opts...)
		if err != nil {
			return s, err
		}
		return tapped{ClientStream: s, ctx: ctx, tap: tap}, nil
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

// tapped shows a stream's requests to the tap.
type tapped struct {
	grpc.ClientStream
	ctx context.Context
	tap *grantTap
}

func (s tapped) SendMsg(m any) error {
	if err := s.tap.see(s.ctx, m); err != nil {
		return err
	}
	return s.ClientStream.SendMsg(m)
}

// TestAGrantCutOffAtItsDeadlineLeavesNothing: on the emulator, a grant whose
// deadline comes after it has reserved on its donor and before its lease is
// written leaves no reservation and no lease; the same request, asked again
// as the owner asks a lost one, is granted once, and asked once more finds
// its lease.
func TestAGrantCutOffAtItsDeadlineLeavesNothing(t *testing.T) {
	for _, at := range []string{"select tr_lease", "update tr_credit_balance", "insert tr_lease"} {
		tap := &grantTap{hold: at}
		s := tappedStore(t, tap)
		ws := seedWorkspace(t, 100)
		req := grantOf(ws, 30)
		ctx, cancel := context.WithTimeout(context.Background(), 500*time.Millisecond)
		got, err := s.Grant(ctx, req)
		cancel()
		if err == nil || got.Refused != "" || len(got.Donors) != 0 {
			t.Fatalf("cut off at %q: %+v %v, want an error and no answer", at, got, err)
		}
		if calls := tap.taken(); !slices.Contains(calls, "rollback") || slices.Contains(calls, "commit") {
			t.Errorf("cut off at %q, the grant's calls: %q", at, calls)
		}
		if r := reserved(readRows(t, ws)); !slices.Equal(r, []int64{0}) || leaseCount(t, ws) != 0 {
			t.Fatalf("cut off at %q: reserved %v, leases %d", at, r, leaseCount(t, ws))
		}
		identityHolds(t, s, ws)
		tap.holdNth("", 0)
		first, err := s.Grant(context.Background(), req)
		if err != nil || first.Refused != "" || !slices.Equal(first.Donors, []Donor{{Shard: 0, Allocation: 30}}) {
			t.Fatalf("asked again after %q: %+v %v", at, first, err)
		}
		if calls := tap.taken(); !slices.Equal(calls, append(slices.Clone(grantReads), "update tr_credit_balance", "insert tr_lease", "commit")) {
			t.Errorf("the grant's calls on the emulator: %q", calls)
		}
		again, err := s.Grant(context.Background(), req)
		if err != nil || again.Refused != "" || !again.Expiry.Equal(first.Expiry) || !slices.Equal(again.Donors, first.Donors) {
			t.Fatalf("asked once more after %q: %+v %v, first %+v", at, again, err, first)
		}
		if r := reserved(readRows(t, ws)); !slices.Equal(r, []int64{30}) || leaseCount(t, ws) != 1 {
			t.Fatalf("after %q and its retries: reserved %v, leases %d", at, r, leaseCount(t, ws))
		}
		identityHolds(t, s, ws)
	}
}
