package store

import (
	"context"
	"errors"
	"sync"
	"sync/atomic"
	"time"

	"cloud.google.com/go/spanner"
	"google.golang.org/grpc/codes"

	"github.com/Lore-Hex/quill-router/fastpath/internal/gwauth"
)

// The edge's leased authorization: the two rows Python's leased authorize
// writes (storage_gcp.py, _authorize_gateway_leased), a tr_reservation row
// and its tr_gateway_authorization row, inserted in one read-write
// transaction, and what the transaction's error proves about whether they
// landed (docs/design/fast-admission-e2-decide.md, §5 and §8.4).
//
// Nothing here may refund after a commit that might have applied: a refund
// after an applied commit lets an enclave retry replay, through Python, a row
// whose hold was given back. So an error is Failed only when it proves
// nothing applied, and every other error is read back once; without our row
// it is Unknown, which the caller answers with Python's 503, the hold kept.

// InsertOutcome is what an insert's transaction, and its one read-back,
// prove about its rows.
type InsertOutcome int

const (
	// Inserted: the transaction committed.
	Inserted InsertOutcome = iota
	// Landed: the commit's outcome was AlreadyExists or not known, and the
	// row under our authorization id, read back, is ours column for column:
	// a commit the client library re-ran after losing the first answer, or
	// one whose answer never came. It answers as after a read-back.
	Landed
	// ScopeTaken: the commit reported AlreadyExists, and our authorization
	// id has no row of ours: another authorization of the idempotency scope
	// landed first (tr_reservation_by_idemp). Nothing of ours applied, or
	// ever can; the caller refunds and passes, and Python replays or answers
	// 409.
	ScopeTaken
	// Unknown: the commit may have applied, and the read-back did not find
	// our row (or could not read it). The caller answers Python's 503 and
	// never refunds.
	Unknown
	// Failed: proven not applied: the callback never ran, or it ran once
	// and the code is one only a commit that did not apply is answered with
	// (ClassifyInsert). The caller refunds and passes.
	Failed
)

func (o InsertOutcome) String() string {
	switch o {
	case Inserted:
		return "inserted"
	case Landed:
		return "landed"
	case ScopeTaken:
		return "scope_taken"
	case Unknown:
		return "unknown"
	case Failed:
		return "failed"
	}
	return "InsertOutcome(?)"
}

const (
	// LeasedEdgeTag is the insert's transaction tag: Python tags its leased
	// insert tr_authorize_leased (storage_gcp.py), and the edge's is told
	// apart from it in Spanner's statistics by the suffix. It is the one
	// transaction of the store not tagged by tag(), so that it sorts beside
	// Python's.
	LeasedEdgeTag = "tr_authorize_leased_edge"
	// InsertDeadline bounds the insert's transaction, the client's own
	// retries of Aborted within it (decision 12).
	InsertDeadline = 1500 * time.Millisecond
	// ReadBackDeadline bounds the one read of our authorization by id that
	// follows an AlreadyExists or an outcome not known, on a context of its
	// own: the insert's may be spent. An insert takes at most InsertDeadline
	// and then ReadBackDeadline.
	ReadBackDeadline = 500 * time.Millisecond
)

// reservationColumns are the columns Python's reservation insert names
// (storage_gcp_counter_dml.py, RESERVATION_COLUMNS), in its order; settled
// takes its default, false, and actual_micro, settled_usage_type and
// terminal_at are NULL.
var reservationColumns = []string{
	"reservation_id", "workspace_id", "key_hash", "ws_shard", "credit_shard", "key_shard",
	"credit_reserved_micro", "key_reserved_micro", "hold_usage_type",
	"authorization_id", "idempotency_scope", "idempotency_fingerprint", "expires_at",
	"created_at",
}

// authorizationColumns are the columns Python's authorization insert names
// (storage_gcp_request_records.py, _INSERT_GATEWAY_AUTHORIZATION_SQL), in
// its order: settled false and terminal_at NULL as constants, then the
// payload and the 13 typed columns (AUTHORIZATION_TYPED_COLUMNS), of which
// the edge's rows set three. The retired spend-lease columns are not named,
// so they are NULL, as Python leaves them.
var authorizationColumns = []string{
	"authorization_id", "workspace_id", "key_hash", "reservation_id", "model_id",
	"provider", "usage_type", "estimated_microdollars", "settled", "created_at",
	"terminal_at", "payload", "idempotency_fingerprint", "finalization_outcome",
	"finalized_cost_microdollars", "started_at", "heartbeat_seq", "heartbeat_at",
	"heartbeat_hash", "selected_endpoint_id", "delivered_usage", "pricing_snapshot",
	"stage_d_boot_kid", "invocation_nonce", "gateway_request_id",
}

// leasedMutations are the two inserts, the reservation first, as Python's
// batch orders them. The shards and the reserved amounts are §5's
// constants.
func leasedMutations(a gwauth.AuthorizationRow, r gwauth.ReservationRow) []*spanner.Mutation {
	nonce := spanner.NullString{}
	if a.InvocationNonce != nil {
		nonce = spanner.NullString{StringVal: *a.InvocationNonce, Valid: true}
	}
	return []*spanner.Mutation{
		spanner.Insert("tr_reservation", reservationColumns, []any{
			r.ReservationID, r.WorkspaceID, r.KeyHash, int64(0), int64(0), int64(0),
			int64(0), int64(0), r.HoldUsageType,
			r.AuthorizationID, r.IdempotencyScope, r.IdempotencyFingerprint, r.ExpiresAt,
			r.CreatedAt,
		}),
		spanner.Insert("tr_gateway_authorization", authorizationColumns, []any{
			a.AuthorizationID, a.WorkspaceID, a.KeyHash, a.ReservationID, a.ModelID,
			a.Provider, a.UsageType, a.EstimatedMicrodollars, false, a.CreatedAt,
			spanner.NullTime{}, a.Payload, a.IdempotencyFingerprint, spanner.NullString{},
			spanner.NullInt64{}, spanner.NullTime{}, spanner.NullInt64{}, spanner.NullTime{},
			spanner.NullString{}, spanner.NullString{}, spanner.NullString{}, spanner.NullString{},
			a.StageDBootKid, nonce, spanner.NullString{},
		}),
	}
}

// checkLeasedRows refuses rows that are not one authorization's two rows,
// before anything is sent.
func checkLeasedRows(a gwauth.AuthorizationRow, r gwauth.ReservationRow) error {
	switch {
	case a.AuthorizationID == "" || r.ReservationID == "":
		return errors.New("store: an authorization id and a reservation id are required")
	case a.AuthorizationID != r.AuthorizationID || a.ReservationID != r.ReservationID:
		return errors.New("store: the reservation is not the authorization's")
	case a.WorkspaceID != r.WorkspaceID || a.KeyHash != r.KeyHash:
		return errors.New("store: the reservation is not the authorization's workspace and key")
	case r.IdempotencyScope == "":
		// Never NULL in the cohort: a key is always presented or minted, and
		// a NULL scope is outside the unique index that arbitrates retries.
		return errors.New("store: no idempotency scope")
	}
	return nil
}

// leasedInserts is the store's account of its leased inserts'
// transactions, which can outlive the insert that began them.
//
// The Spanner client (v1.88) waits for its multiplexed session without the
// context: a take before the client has a session waits on an unbuffered
// request to the session worker, up to the worker's own 30 s a creation,
// whatever its context says. Two things follow. The session worker exits
// for good if it serves a request whose context has ended (session.go,
// createMultiplexedSession), so the transaction's context here never ends
// while its take may wait: only a transaction whose callback has begun has
// its context cancelled at the deadline. And closing the client while such
// a take is blocked panics in the client (a send on the closed request
// channel), so the process waits for every transaction begun here
// (WaitLeasedInserts) before it closes the client. A process that warms
// the client's session first (WarmSession) never has a take wait at all.
type leasedInserts struct {
	running sync.WaitGroup
	// refusedLate counts the transactions whose session came only after
	// their insert was answered Failed, and whose callback then refused.
	refusedLate atomic.Int64
}

// WaitLeasedInserts waits for every transaction a leased insert began to
// end, those still waiting for the client's session included. The process
// calls it, once nothing can begin another insert, before it closes the
// client: closing it under a waiting transaction panics.
func (s *Store) WaitLeasedInserts() { s.leased.running.Wait() }

// WarmSession makes the client's multiplexed session before anything else
// uses the client, so that no take ever waits on the session worker: once
// the client has its session it keeps it (sessionReady is the one
// assignment), and a take returns it at once. It is one strong single-use
// read of nothing, on a context of its own that outlasts the worker's two
// 30 s creations a take can wait behind (the client's own at its opening,
// then the take's), so the take is never served on an ended context. An
// error is a client with no session: the process does not start.
func (s *Store) WarmSession() error {
	ctx, cancel := context.WithTimeout(context.Background(), warmSessionWait)
	defer cancel()
	ro := s.client.Single()
	defer ro.Close()
	return ro.QueryWithOptions(ctx, spanner.Statement{SQL: "SELECT 1"},
		spanner.QueryOptions{RequestTag: tag("warm-session")}).Do(func(*spanner.Row) error { return nil })
}

// warmSessionWait bounds WarmSession past the two session creations its
// take can wait behind.
const warmSessionWait = 2 * time.Minute

// leasedBegun runs once a callback has marked its transaction begun; a
// test stretches the moment to the deadline.
var leasedBegun = func() {}

// errAbandoned is the callback's refusal once its insert has ended without
// it: the transaction it would have committed was answered Failed.
var errAbandoned = errors.New("store: the insert ended before its transaction began")

// The states of an insert's transaction, which the callback and the
// deadline settle between them once: the first callback run marks it
// begun, and a deadline that finds it pending marks it abandoned, after
// which the callback refuses before it buffers anything, so the client
// neither begins nor commits a transaction for it.
const (
	txPending int32 = iota
	txBegun
	txAbandoned
)

// InsertLeasedAuthorization inserts an authorization's two rows in one
// read-write transaction tagged LeasedEdgeTag, and reports what its error,
// and one read-back, prove (§8.4). The callback only buffers the two
// inserts. Never Apply and never ApplyAtLeastOnce, which may replay a commit
// that applied.
//
// The transaction runs detached from ctx (context.WithoutCancel), so an
// enclave that hangs up mid-commit cannot turn a commit into Canceled, and
// ends at InsertDeadline whatever the client is doing. An AlreadyExists, and
// an error after which the commit may have applied, are read back by our
// authorization id with ReadBackDeadline on a context of their own; the
// caller reads nothing back itself.
func (s *Store) InsertLeasedAuthorization(ctx context.Context, a gwauth.AuthorizationRow, r gwauth.ReservationRow) InsertOutcome {
	if checkLeasedRows(a, r) != nil {
		return Failed
	}
	deadline := time.Now().Add(InsertDeadline)
	mutations := leasedMutations(a, r)
	// The transaction's context ends when its goroutine is done with it, or
	// at the deadline once the callback has begun; never while its session
	// take may wait (leasedInserts).
	txCtx, cancelTx := context.WithCancel(context.WithoutCancel(ctx))
	var state atomic.Int32
	// runs counts the callback's runs. Until it has run no commit can have
	// been sent (no session, or no transaction began). The client re-runs
	// it after an attempt that ended Aborted, ResourceExhausted with a
	// retry delay, or Internal of a broken stream (retry.go,
	// runWithRetryOnAbortedOrFailedInlineBegin), each of which can be a lost
	// answer to a commit that applied; so after a second run, no error
	// proves nothing applied. A run is counted before the callback marks
	// the transaction begun, so a deadline that finds it begun finds a run.
	var runs atomic.Int32
	type result struct {
		err  error
		runs int32
	}
	done := make(chan result, 1)
	s.leased.running.Add(1)
	go func() {
		defer s.leased.running.Done()
		defer cancelTx()
		var err error
		for attempt := 0; attempt < 2; attempt++ {
			_, err = s.client.ReadWriteTransactionWithOptions(txCtx,
				func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
					runs.Add(1)
					if !state.CompareAndSwap(txPending, txBegun) && state.Load() == txAbandoned {
						s.leased.refusedLate.Add(1)
						return errAbandoned
					}
					leasedBegun()
					return txn.BufferWrite(mutations)
				}, spanner.TransactionOptions{TransactionTag: LeasedEdgeTag})
			// Retried once, only while nothing was sent and the deadline has
			// not come: never a take for an insert already answered.
			if err == nil || runs.Load() > 0 || state.Load() == txAbandoned || !time.Now().Before(deadline) {
				break
			}
		}
		done <- result{err, runs.Load()}
	}()
	timer := time.NewTimer(time.Until(deadline))
	defer timer.Stop()
	var res result
	select {
	case res = <-done:
	case <-timer.C:
		if state.CompareAndSwap(txPending, txAbandoned) {
			// The callback never began: nothing was sent, and the callback
			// refuses before it buffers anything if the session comes later.
			// The take, if it waits, waits on a live context (leasedInserts).
			return Failed
		}
		// Begun: a commit may be in flight. Its context ends now (no grace
		// past the deadline, decision 12).
		cancelTx()
		select {
		case res = <-done:
		default:
			res = result{context.DeadlineExceeded, runs.Load()}
		}
	}
	outcome, readBack := ClassifyInsert(res.err, res.runs)
	if !readBack && outcome != Unknown {
		return outcome
	}
	// AlreadyExists: ours, landed by a commit the client re-ran after losing
	// the first answer, or the scope's, landed first by another
	// authorization; Python re-reads the scope the same way. Unknown: ours if
	// it landed. One read, either way.
	row, err := s.readOurRow(a)
	switch {
	case err != nil:
		return Unknown
	case row == rowOurs:
		return Landed
	case readBack && (row == rowAbsent || row == rowTheirs):
		// The row under our id is not ours, or there is none: with it there,
		// or with the scope's, our insert can never apply.
		return ScopeTaken
	}
	return Unknown
}

// ClassifyInsert is §8.4's table: the outcome the transaction's error
// proves, runs being how many times its callback ran. An AlreadyExists is
// no outcome yet: alreadyExists says so, for the caller to read its own id
// back.
//
// Failed after the callback ran needs a code that the request alone
// determines, and one run: InvalidArgument, OutOfRange and Unimplemented
// would answer the first commit as they answer any re-send of it, and
// Aborted is Spanner's own "not committed". NotFound, FailedPrecondition,
// PermissionDenied and Unauthenticated are Unknown once a commit may have
// been sent: the client re-sends a commit that met Unavailable or
// ResourceExhausted, of the same transaction, and the answer it returns is
// the re-send's. Spanner answers a re-sent commit with the first one's
// outcome while it remembers the transaction, but no guarantee covers one
// it has forgotten (NotFound, FailedPrecondition) or a re-send refused its
// credentials, after a first commit that applied.
func ClassifyInsert(err error, runs int32) (outcome InsertOutcome, alreadyExists bool) {
	if err == nil {
		return Inserted, false
	}
	if runs == 0 {
		return Failed, false
	}
	// The context's own end is Unknown whatever the client wrapped it in: a
	// commit in flight when the deadline passed may still apply.
	if errors.Is(err, context.DeadlineExceeded) || errors.Is(err, context.Canceled) {
		return Unknown, false
	}
	// spanner.ErrCode unwraps the client's error types to the gRPC status;
	// an error with none is codes.Unknown, and so Unknown.
	switch spanner.ErrCode(err) {
	case codes.AlreadyExists:
		return Unknown, true
	case codes.Aborted, codes.InvalidArgument, codes.OutOfRange, codes.Unimplemented:
		if runs == 1 {
			// Spanner applied nothing.
			return Failed, false
		}
	}
	// A second run, or DeadlineExceeded, Canceled, Unavailable, Internal,
	// Unknown, ResourceExhausted, DataLoss, NotFound, FailedPrecondition,
	// PermissionDenied, Unauthenticated, OK on an error, and any code not
	// named: the commit may have applied.
	return Unknown, false
}

// rowUnderOurID is what the read-back found under our authorization id.
type rowUnderOurID int

const (
	rowAbsent rowUnderOurID = iota
	// rowOurs: every column of the row (but the retired spend-lease ones,
	// NULL on every row and read by neither path) is what we wrote.
	rowOurs
	// rowTheirs: the row names another reservation.
	rowTheirs
	// rowDoubt: the row names our reservation but not our other columns,
	// which no commit of ours writes: not provably ours, nor provably not.
	rowDoubt
)

// readOurRow reads the row under our authorization id, in one strong read
// by primary key, on a context of its own with ReadBackDeadline (the
// insert's may be spent), and compares it with ours column for column, a
// NULL told from an empty string: a 200 is answered only for a row whose
// content is ours.
func (s *Store) readOurRow(a gwauth.AuthorizationRow) (rowUnderOurID, error) {
	readCtx, cancel := context.WithTimeout(context.Background(), ReadBackDeadline)
	defer cancel()
	row, err := s.client.Single().ReadRowWithOptions(readCtx, "tr_gateway_authorization",
		spanner.Key{a.AuthorizationID}, storedAuthorizationColumns, &spanner.ReadOptions{RequestTag: LeasedEdgeTag})
	if errors.Is(err, spanner.ErrRowNotFound) {
		return rowAbsent, nil
	}
	if err != nil {
		return rowAbsent, err
	}
	var got StoredAuthorization
	if err := row.Columns(got.fields()...); err != nil {
		return rowAbsent, err
	}
	if got.ReservationID != (spanner.NullString{StringVal: a.ReservationID, Valid: true}) {
		return rowTheirs, nil
	}
	if !isOurs(a, got) {
		return rowDoubt, nil
	}
	return rowOurs, nil
}

// isOurs: the stored row is a's as leasedMutations writes it.
func isOurs(a gwauth.AuthorizationRow, got StoredAuthorization) bool {
	some := func(v string) spanner.NullString { return spanner.NullString{StringVal: v, Valid: true} }
	nonce := spanner.NullString{}
	if a.InvocationNonce != nil {
		nonce = some(*a.InvocationNonce)
	}
	return got.AuthorizationID == a.AuthorizationID && got.WorkspaceID == a.WorkspaceID && got.KeyHash == a.KeyHash &&
		got.ModelID == a.ModelID && got.Provider == a.Provider && got.UsageType == a.UsageType &&
		got.EstimatedMicrodollars == a.EstimatedMicrodollars && !got.Settled && got.CreatedAt.Equal(a.CreatedAt) &&
		!got.TerminalAt.Valid && got.Payload == some(a.Payload) &&
		got.IdempotencyFingerprint == some(a.IdempotencyFingerprint) && got.StageDBootKid == some(a.StageDBootKid) &&
		got.InvocationNonce == nonce &&
		// The typed columns the edge's insert leaves NULL.
		!got.FinalizationOutcome.Valid && !got.FinalizedCostMicrodollars.Valid && !got.StartedAt.Valid &&
		!got.HeartbeatSeq.Valid && !got.HeartbeatAt.Valid && !got.HeartbeatHash.Valid &&
		!got.SelectedEndpointID.Valid && !got.DeliveredUsage.Valid && !got.PricingSnapshot.Valid &&
		!got.GatewayRequestID.Valid
}

// StoredAuthorization is every column of an authorization's row but the
// retired spend-lease ones (NULL on every row written since the pilot's
// removal, and read by neither path), as stored.
type StoredAuthorization struct {
	AuthorizationID       string
	WorkspaceID           string
	KeyHash               string
	ReservationID         spanner.NullString
	ModelID               string
	Provider              string
	UsageType             string
	EstimatedMicrodollars int64
	Settled               bool
	CreatedAt             time.Time
	TerminalAt            spanner.NullTime
	Payload               spanner.NullString
	// The 13 typed columns, in Python's order.
	IdempotencyFingerprint    spanner.NullString
	FinalizationOutcome       spanner.NullString
	FinalizedCostMicrodollars spanner.NullInt64
	StartedAt                 spanner.NullTime
	HeartbeatSeq              spanner.NullInt64
	HeartbeatAt               spanner.NullTime
	HeartbeatHash             spanner.NullString
	SelectedEndpointID        spanner.NullString
	DeliveredUsage            spanner.NullString
	PricingSnapshot           spanner.NullString
	StageDBootKid             spanner.NullString
	InvocationNonce           spanner.NullString
	GatewayRequestID          spanner.NullString
}

var storedAuthorizationColumns = []string{
	"authorization_id", "workspace_id", "key_hash", "reservation_id", "model_id",
	"provider", "usage_type", "estimated_microdollars", "settled", "created_at",
	"terminal_at", "payload", "idempotency_fingerprint", "finalization_outcome",
	"finalized_cost_microdollars", "started_at", "heartbeat_seq", "heartbeat_at",
	"heartbeat_hash", "selected_endpoint_id", "delivered_usage", "pricing_snapshot",
	"stage_d_boot_kid", "invocation_nonce", "gateway_request_id",
}

func (a *StoredAuthorization) fields() []any {
	return []any{
		&a.AuthorizationID, &a.WorkspaceID, &a.KeyHash, &a.ReservationID, &a.ModelID,
		&a.Provider, &a.UsageType, &a.EstimatedMicrodollars, &a.Settled, &a.CreatedAt,
		&a.TerminalAt, &a.Payload, &a.IdempotencyFingerprint, &a.FinalizationOutcome,
		&a.FinalizedCostMicrodollars, &a.StartedAt, &a.HeartbeatSeq, &a.HeartbeatAt,
		&a.HeartbeatHash, &a.SelectedEndpointID, &a.DeliveredUsage, &a.PricingSnapshot,
		&a.StageDBootKid, &a.InvocationNonce, &a.GatewayRequestID,
	}
}

// StoredReservation is every column of a reservation's row, as stored.
type StoredReservation struct {
	ReservationID          string
	WorkspaceID            spanner.NullString
	KeyHash                spanner.NullString
	WsShard                spanner.NullInt64
	CreditShard            int64
	KeyShard               spanner.NullInt64
	CreditReservedMicro    spanner.NullInt64
	KeyReservedMicro       spanner.NullInt64
	ActualMicro            spanner.NullInt64
	HoldUsageType          spanner.NullString
	SettledUsageType       spanner.NullString
	AuthorizationID        spanner.NullString
	Settled                bool
	IdempotencyScope       spanner.NullString
	IdempotencyFingerprint spanner.NullString
	CreatedAt              spanner.NullTime
	ExpiresAt              spanner.NullTime
	TerminalAt             spanner.NullTime
}

var storedReservationColumns = []string{
	"reservation_id", "workspace_id", "key_hash", "ws_shard", "credit_shard", "key_shard",
	"credit_reserved_micro", "key_reserved_micro", "actual_micro", "hold_usage_type",
	"settled_usage_type", "authorization_id", "settled", "idempotency_scope",
	"idempotency_fingerprint", "created_at", "expires_at", "terminal_at",
}

func (r *StoredReservation) fields() []any {
	return []any{
		&r.ReservationID, &r.WorkspaceID, &r.KeyHash, &r.WsShard, &r.CreditShard, &r.KeyShard,
		&r.CreditReservedMicro, &r.KeyReservedMicro, &r.ActualMicro, &r.HoldUsageType,
		&r.SettledUsageType, &r.AuthorizationID, &r.Settled, &r.IdempotencyScope,
		&r.IdempotencyFingerprint, &r.CreatedAt, &r.ExpiresAt, &r.TerminalAt,
	}
}

// AuthorizationForCompare is an authorization's row and the reservation
// row it names, read at one timestamp, for the shadow to compare with the
// rows the edge would have written (G10).
type AuthorizationForCompare struct {
	Authorization StoredAuthorization
	// Reservation is nil when the authorization names no reservation, or
	// names one that has no row.
	Reservation *StoredReservation
}

// ReadAuthorizationForCompare reads the authorization's row and its
// reservation's, each by primary key, in one strong read-only transaction.
// Found is false when the authorization has no row. Every time is in UTC.
func (s *Store) ReadAuthorizationForCompare(ctx context.Context, authorizationID string) (AuthorizationForCompare, bool, error) {
	if authorizationID == "" {
		return AuthorizationForCompare{}, false, errors.New("store: no authorization id")
	}
	ro := s.client.ReadOnlyTransaction()
	defer ro.Close()
	var out AuthorizationForCompare
	row, err := ro.ReadRowWithOptions(ctx, "tr_gateway_authorization", spanner.Key{authorizationID},
		storedAuthorizationColumns, &spanner.ReadOptions{RequestTag: tag("read-authorization-for-compare")})
	if errors.Is(err, spanner.ErrRowNotFound) {
		return AuthorizationForCompare{}, false, nil
	}
	if err == nil {
		err = row.Columns(out.Authorization.fields()...)
	}
	if err != nil {
		return AuthorizationForCompare{}, false, err
	}
	a := &out.Authorization
	a.CreatedAt = a.CreatedAt.UTC()
	for _, t := range []*spanner.NullTime{&a.TerminalAt, &a.StartedAt, &a.HeartbeatAt} {
		t.Time = t.Time.UTC()
	}
	if !a.ReservationID.Valid {
		return out, true, nil
	}
	row, err = ro.ReadRowWithOptions(ctx, "tr_reservation", spanner.Key{a.ReservationID.StringVal},
		storedReservationColumns, &spanner.ReadOptions{RequestTag: tag("read-authorization-for-compare")})
	if errors.Is(err, spanner.ErrRowNotFound) {
		return out, true, nil
	}
	var r StoredReservation
	if err == nil {
		err = row.Columns(r.fields()...)
	}
	if err != nil {
		return AuthorizationForCompare{}, false, err
	}
	for _, t := range []*spanner.NullTime{&r.CreatedAt, &r.ExpiresAt, &r.TerminalAt} {
		t.Time = t.Time.UTC()
	}
	out.Reservation = &r
	return out, true, nil
}
