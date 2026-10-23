package store

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"cloud.google.com/go/spanner"
	"google.golang.org/api/option"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/Lore-Hex/quill-router/fastpath/internal/gwauth"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store/storetest"
)

// The edge's leased insert (gatewayauth.go) against §8.4 of
// docs/design/fast-admission-e2-decide.md: the outcome every commit error
// proves, the rows byte for byte as Python writes them, the scope conflict
// against a row Python's own statements wrote, and the money-safety rule
// that nothing which may have applied is refunded.

// --- the table, without the emulator -------------------------------------

// insertTable is §8.4's table, code by code, for an error after the
// callback ran. AlreadyExists is not an outcome yet: it is read back.
var insertTable = map[codes.Code]InsertOutcome{
	codes.Canceled:           Unknown,
	codes.Unknown:            Unknown,
	codes.InvalidArgument:    Failed,
	codes.DeadlineExceeded:   Unknown,
	codes.NotFound:           Failed,
	codes.PermissionDenied:   Failed,
	codes.ResourceExhausted:  Unknown,
	codes.FailedPrecondition: Failed,
	codes.Aborted:            Failed,
	codes.OutOfRange:         Failed,
	codes.Unimplemented:      Failed,
	codes.Internal:           Unknown,
	codes.Unavailable:        Unknown,
	codes.DataLoss:           Unknown,
	codes.Unauthenticated:    Failed,
}

// TestClassifyInsertIsTheContractTable: every gRPC code, raw, as the
// client's own error type and wrapped again, maps as §8.4 says; the
// context's end and an error with no status are Unknown; any error before
// the callback ran is Failed; no error is Inserted.
func TestClassifyInsertIsTheContractTable(t *testing.T) {
	if got, ae := classifyInsert(nil, true); got != Inserted || ae {
		t.Errorf("no error: %v %v", got, ae)
	}
	for code := codes.Canceled; code <= codes.Unauthenticated; code++ {
		raw := status.Error(code, "injected")
		for name, err := range map[string]error{
			"raw":     raw,
			"spanner": spanner.ToSpannerError(raw),
			"wrapped": fmt.Errorf("commit: %w", spanner.ToSpannerError(raw)),
		} {
			got, alreadyExists := classifyInsert(err, true)
			if code == codes.AlreadyExists {
				if !alreadyExists {
					t.Errorf("%v %s: not read back", code, name)
				}
				continue
			}
			if alreadyExists || got != insertTable[code] {
				t.Errorf("%v %s: %v (read back %v), want %v", code, name, got, alreadyExists, insertTable[code])
			}
			if got, _ := classifyInsert(err, false); got != Failed {
				t.Errorf("%v %s before the callback ran: %v, want failed", code, name, got)
			}
		}
	}
	for name, err := range map[string]error{
		"deadline":         context.DeadlineExceeded,
		"canceled":         context.Canceled,
		"wrapped deadline": fmt.Errorf("x: %w", context.DeadlineExceeded),
		"client deadline":  spanner.ToSpannerError(context.DeadlineExceeded),
		"no status":        errors.New("connection reset"),
	} {
		if got, ae := classifyInsert(err, true); got != Unknown || ae {
			t.Errorf("%s: %v %v, want unknown", name, got, ae)
		}
	}
}

// --- rows ----------------------------------------------------------------

// leasedRows are an authorization's two rows with ids no other test uses.
func leasedRows(scope string) (gwauth.AuthorizationRow, gwauth.ReservationRow) {
	ws, kh := storetest.UniqueID("ws"), storetest.UniqueID("kh")
	aid, rid := storetest.UniqueID("gwa"), storetest.UniqueID("res")
	if scope == "" {
		scope = ws + "#" + kh + "#" + storetest.UniqueID("idem")
	}
	at := time.Date(2026, 10, 19, 2, 0, 0, 123456000, time.UTC)
	return gwauth.AuthorizationRow{AuthorizationID: aid, WorkspaceID: ws, KeyHash: kh, ReservationID: rid,
			ModelID: "z-ai/glm-5.3", Provider: "wafer", UsageType: "Credits", EstimatedMicrodollars: 2640, CreatedAt: at,
			Payload: `{"id":"` + aid + `"}`, IdempotencyFingerprint: "fp", StageDBootKid: "kid"},
		gwauth.ReservationRow{ReservationID: rid, WorkspaceID: ws, KeyHash: kh, HoldUsageType: "Credits",
			AuthorizationID: aid, IdempotencyScope: scope, IdempotencyFingerprint: "fp",
			ExpiresAt: at.Add(7200 * time.Second), CreatedAt: at}
}

// landed reports whether the authorization's row and its reservation's are
// in the shared database, reading both strongly through the shared client
// (a test's faulty client may not read at all), and fails the test if one
// is without the other: the two inserts are one transaction.
func landed(t *testing.T, a gwauth.AuthorizationRow) bool {
	t.Helper()
	ctx := context.Background()
	ro := shared.ReadOnlyTransaction()
	defer ro.Close()
	_, errA := ro.ReadRow(ctx, "tr_gateway_authorization", spanner.Key{a.AuthorizationID}, []string{"authorization_id"})
	_, errR := ro.ReadRow(ctx, "tr_reservation", spanner.Key{a.ReservationID}, []string{"reservation_id"})
	for _, err := range []error{errA, errR} {
		if err != nil && !errors.Is(err, spanner.ErrRowNotFound) {
			t.Fatal(err)
		}
	}
	if (errA == nil) != (errR == nil) {
		t.Fatalf("one row of the two landed: authorization %v, reservation %v", errA, errR)
	}
	return errA == nil
}

// moneySafe fails the test if an outcome that refunds (ScopeTaken, Failed)
// follows a commit that applied, or one that answers (Inserted, Landed)
// follows one that did not.
func moneySafe(t *testing.T, name string, out InsertOutcome, applied bool) {
	t.Helper()
	switch {
	case applied && (out == ScopeTaken || out == Failed):
		t.Errorf("%s: the rows landed and the outcome is %v, which refunds their hold", name, out)
	case !applied && (out == Inserted || out == Landed):
		t.Errorf("%s: nothing landed and the outcome is %v, which answers", name, out)
	}
}

// --- Python's statements -------------------------------------------------

// pythonReservationSQL and pythonAuthorizationSQL are Python's inserts,
// verbatim: reservation_insert_statement (storage_gcp_counter_dml.py, the
// column list RESERVATION_COLUMNS) and _INSERT_GATEWAY_AUTHORIZATION_SQL
// (storage_gcp_request_records.py).
const (
	pythonReservationSQL = "INSERT INTO tr_reservation (reservation_id, workspace_id, key_hash, ws_shard, " +
		"credit_shard, key_shard, credit_reserved_micro, key_reserved_micro, hold_usage_type, authorization_id, " +
		"idempotency_scope, idempotency_fingerprint, expires_at, created_at) VALUES (@reservation_id, " +
		"@workspace_id, @key_hash, @ws_shard, @credit_shard, @key_shard, @credit_reserved_micro, " +
		"@key_reserved_micro, @hold_usage_type, @authorization_id, @idempotency_scope, " +
		"@idempotency_fingerprint, @expires_at, @created_at)"
	pythonAuthorizationSQL = "INSERT INTO tr_gateway_authorization (" +
		"authorization_id, workspace_id, key_hash, reservation_id, model_id, " +
		"provider, usage_type, estimated_microdollars, settled, created_at, " +
		"terminal_at, payload, idempotency_fingerprint, finalization_outcome, " +
		"finalized_cost_microdollars, started_at, heartbeat_seq, heartbeat_at, " +
		"heartbeat_hash, selected_endpoint_id, delivered_usage, pricing_snapshot, " +
		"stage_d_boot_kid, invocation_nonce, gateway_request_id" +
		") VALUES (" +
		"@authorization_id, @workspace_id, @key_hash, @reservation_id, @model_id, " +
		"@provider, @usage_type, @estimated_microdollars, false, @created_at, " +
		"NULL, @payload, @idempotency_fingerprint, @finalization_outcome, " +
		"@finalized_cost_microdollars, @started_at, @heartbeat_seq, @heartbeat_at, " +
		"@heartbeat_hash, @selected_endpoint_id, @delivered_usage, @pricing_snapshot, " +
		"@stage_d_boot_kid, @invocation_nonce, @gateway_request_id" +
		")"
)

// pythonInsert writes the two rows as _authorize_gateway_leased does: its
// two statements in one batch, in one read-write transaction tagged as
// Python tags it.
func pythonInsert(ctx context.Context, c *spanner.Client, a gwauth.AuthorizationRow, r gwauth.ReservationRow) error {
	nonce := spanner.NullString{}
	if a.InvocationNonce != nil {
		nonce = spanner.NullString{StringVal: *a.InvocationNonce, Valid: true}
	}
	_, err := c.ReadWriteTransactionWithOptions(ctx, func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		_, err := txn.BatchUpdate(ctx, []spanner.Statement{
			{SQL: pythonReservationSQL, Params: map[string]any{
				"reservation_id": r.ReservationID, "workspace_id": r.WorkspaceID, "key_hash": r.KeyHash,
				"ws_shard": int64(0), "credit_shard": int64(0), "key_shard": int64(0),
				"credit_reserved_micro": int64(0), "key_reserved_micro": int64(0),
				"hold_usage_type": r.HoldUsageType, "authorization_id": r.AuthorizationID,
				"idempotency_scope": r.IdempotencyScope, "idempotency_fingerprint": r.IdempotencyFingerprint,
				"expires_at": r.ExpiresAt, "created_at": r.CreatedAt,
			}},
			{SQL: pythonAuthorizationSQL, Params: map[string]any{
				"authorization_id": a.AuthorizationID, "workspace_id": a.WorkspaceID, "key_hash": a.KeyHash,
				"reservation_id": a.ReservationID, "model_id": a.ModelID, "provider": a.Provider,
				"usage_type": a.UsageType, "estimated_microdollars": a.EstimatedMicrodollars,
				"created_at": a.CreatedAt, "payload": a.Payload,
				"idempotency_fingerprint":     spanner.NullString{StringVal: a.IdempotencyFingerprint, Valid: true},
				"finalization_outcome":        spanner.NullString{},
				"finalized_cost_microdollars": spanner.NullInt64{},
				"started_at":                  spanner.NullTime{},
				"heartbeat_seq":               spanner.NullInt64{},
				"heartbeat_at":                spanner.NullTime{},
				"heartbeat_hash":              spanner.NullString{},
				"selected_endpoint_id":        spanner.NullString{},
				"delivered_usage":             spanner.NullString{},
				"pricing_snapshot":            spanner.NullString{},
				"stage_d_boot_kid":            spanner.NullString{StringVal: a.StageDBootKid, Valid: true},
				"invocation_nonce":            nonce,
				"gateway_request_id":          spanner.NullString{},
			}},
		})
		return err
	}, spanner.TransactionOptions{TransactionTag: "tr_authorize_leased"})
	return err
}

// --- the golden rows -----------------------------------------------------

// goldenCase is what this test reads of a P1 case
// (fastpath/testdata/authorize_cases_v1.json, §10.1): the rows Python wrote
// for an answered request.
type goldenCase struct {
	Name   string `json:"name"`
	Insert string `json:"insert"`
	Expect struct {
		Outcome          string               `json:"outcome"`
		AuthorizationRow goldenAuthorization  `json:"authorization_row"`
		ReservationRow   goldenReservationRow `json:"reservation_row"`
	} `json:"expect"`
}

type goldenAuthorization struct {
	AuthorizationID           string  `json:"authorization_id"`
	WorkspaceID               string  `json:"workspace_id"`
	KeyHash                   string  `json:"key_hash"`
	ReservationID             string  `json:"reservation_id"`
	ModelID                   string  `json:"model_id"`
	Provider                  string  `json:"provider"`
	UsageType                 string  `json:"usage_type"`
	EstimatedMicrodollars     int64   `json:"estimated_microdollars"`
	Settled                   bool    `json:"settled"`
	CreatedAtUS               int64   `json:"created_at_us"`
	TerminalAtUS              *int64  `json:"terminal_at_us"`
	Payload                   string  `json:"payload"`
	IdempotencyFingerprint    string  `json:"idempotency_fingerprint"`
	FinalizationOutcome       *string `json:"finalization_outcome"`
	FinalizedCostMicrodollars *int64  `json:"finalized_cost_microdollars"`
	StartedAtUS               *int64  `json:"started_at_us"`
	HeartbeatSeq              *int64  `json:"heartbeat_seq"`
	HeartbeatAtUS             *int64  `json:"heartbeat_at_us"`
	HeartbeatHash             *string `json:"heartbeat_hash"`
	SelectedEndpointID        *string `json:"selected_endpoint_id"`
	DeliveredUsage            *string `json:"delivered_usage"`
	PricingSnapshot           *string `json:"pricing_snapshot"`
	StageDBootKid             string  `json:"stage_d_boot_kid"`
	InvocationNonce           *string `json:"invocation_nonce"`
	GatewayRequestID          *string `json:"gateway_request_id"`
}

type goldenReservationRow struct {
	ReservationID          string `json:"reservation_id"`
	WorkspaceID            string `json:"workspace_id"`
	KeyHash                string `json:"key_hash"`
	WsShard                int64  `json:"ws_shard"`
	CreditShard            int64  `json:"credit_shard"`
	KeyShard               int64  `json:"key_shard"`
	CreditReservedMicro    int64  `json:"credit_reserved_micro"`
	KeyReservedMicro       int64  `json:"key_reserved_micro"`
	HoldUsageType          string `json:"hold_usage_type"`
	AuthorizationID        string `json:"authorization_id"`
	IdempotencyScope       string `json:"idempotency_scope"`
	IdempotencyFingerprint string `json:"idempotency_fingerprint"`
	ExpiresAtUS            int64  `json:"expires_at_us"`
	CreatedAtUS            int64  `json:"created_at_us"`
}

// goldenAnswered is every P1 case Python answered, with the rows it wrote.
func goldenAnswered(t *testing.T) []goldenCase {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("..", "..", "testdata", "authorize_cases_v1.json"))
	if err != nil {
		t.Fatal(err)
	}
	var file struct {
		Cases []goldenCase `json:"cases"`
	}
	if err := json.Unmarshal(raw, &file); err != nil {
		t.Fatal(err)
	}
	var out []goldenCase
	for _, c := range file.Cases {
		if c.Expect.Outcome == "answer" {
			out = append(out, c)
		}
	}
	// 161 inserted and 2 read back when P1 merged; fewer would mean the
	// file's shape moved under this test.
	if len(out) < 100 {
		t.Fatalf("%d answered cases in the goldens", len(out))
	}
	return out
}

func fromUS(us int64) time.Time { return time.UnixMicro(us).UTC() }

// rows are the golden's two rows as the compose side hands them to the
// store.
func (g goldenCase) rows() (gwauth.AuthorizationRow, gwauth.ReservationRow) {
	a, r := g.Expect.AuthorizationRow, g.Expect.ReservationRow
	return gwauth.AuthorizationRow{AuthorizationID: a.AuthorizationID, WorkspaceID: a.WorkspaceID, KeyHash: a.KeyHash,
			ReservationID: a.ReservationID, ModelID: a.ModelID, Provider: a.Provider, UsageType: a.UsageType,
			EstimatedMicrodollars: a.EstimatedMicrodollars, CreatedAt: fromUS(a.CreatedAtUS), Payload: a.Payload,
			IdempotencyFingerprint: a.IdempotencyFingerprint, StageDBootKid: a.StageDBootKid,
			InvocationNonce: a.InvocationNonce},
		gwauth.ReservationRow{ReservationID: r.ReservationID, WorkspaceID: r.WorkspaceID, KeyHash: r.KeyHash,
			HoldUsageType: r.HoldUsageType, AuthorizationID: r.AuthorizationID, IdempotencyScope: r.IdempotencyScope,
			IdempotencyFingerprint: r.IdempotencyFingerprint, ExpiresAt: fromUS(r.ExpiresAtUS),
			CreatedAt: fromUS(r.CreatedAtUS)}
}

func nullString(p *string) spanner.NullString {
	if p == nil {
		return spanner.NullString{}
	}
	return spanner.NullString{StringVal: *p, Valid: true}
}

func nullInt(p *int64) spanner.NullInt64 {
	if p == nil {
		return spanner.NullInt64{}
	}
	return spanner.NullInt64{Int64: *p, Valid: true}
}

func nullTime(us *int64) spanner.NullTime {
	if us == nil {
		return spanner.NullTime{}
	}
	return spanner.NullTime{Time: fromUS(*us), Valid: true}
}

func someString(s string) spanner.NullString { return spanner.NullString{StringVal: s, Valid: true} }
func someInt(v int64) spanner.NullInt64      { return spanner.NullInt64{Int64: v, Valid: true} }
func someTime(us int64) spanner.NullTime     { return spanner.NullTime{Time: fromUS(us), Valid: true} }

// want is every column of the golden's two rows as ReadAuthorizationForCompare
// returns them.
func (g goldenCase) want() AuthorizationForCompare {
	a, r := g.Expect.AuthorizationRow, g.Expect.ReservationRow
	return AuthorizationForCompare{
		Authorization: StoredAuthorization{
			AuthorizationID: a.AuthorizationID, WorkspaceID: a.WorkspaceID, KeyHash: a.KeyHash,
			ReservationID: someString(a.ReservationID), ModelID: a.ModelID, Provider: a.Provider,
			UsageType: a.UsageType, EstimatedMicrodollars: a.EstimatedMicrodollars, Settled: a.Settled,
			CreatedAt: fromUS(a.CreatedAtUS), TerminalAt: nullTime(a.TerminalAtUS), Payload: someString(a.Payload),
			IdempotencyFingerprint: someString(a.IdempotencyFingerprint),
			FinalizationOutcome:    nullString(a.FinalizationOutcome), FinalizedCostMicrodollars: nullInt(a.FinalizedCostMicrodollars),
			StartedAt: nullTime(a.StartedAtUS), HeartbeatSeq: nullInt(a.HeartbeatSeq), HeartbeatAt: nullTime(a.HeartbeatAtUS),
			HeartbeatHash: nullString(a.HeartbeatHash), SelectedEndpointID: nullString(a.SelectedEndpointID),
			DeliveredUsage: nullString(a.DeliveredUsage), PricingSnapshot: nullString(a.PricingSnapshot),
			StageDBootKid: someString(a.StageDBootKid), InvocationNonce: nullString(a.InvocationNonce),
			GatewayRequestID: nullString(a.GatewayRequestID),
		},
		Reservation: &StoredReservation{
			ReservationID: r.ReservationID, WorkspaceID: someString(r.WorkspaceID), KeyHash: someString(r.KeyHash),
			WsShard: someInt(r.WsShard), CreditShard: r.CreditShard, KeyShard: someInt(r.KeyShard),
			CreditReservedMicro: someInt(r.CreditReservedMicro), KeyReservedMicro: someInt(r.KeyReservedMicro),
			HoldUsageType: someString(r.HoldUsageType), AuthorizationID: someString(r.AuthorizationID),
			IdempotencyScope: someString(r.IdempotencyScope), IdempotencyFingerprint: someString(r.IdempotencyFingerprint),
			CreatedAt: someTime(r.CreatedAtUS), ExpiresAt: someTime(r.ExpiresAtUS),
			// Python's insert leaves these to their defaults and NULL.
			Settled: false,
		},
	}
}

func compareStored(t *testing.T, name string, got, want AuthorizationForCompare) {
	t.Helper()
	if got.Authorization != want.Authorization {
		t.Errorf("%s: the authorization row\n got %+v\nwant %+v", name, got.Authorization, want.Authorization)
	}
	if got.Authorization.Payload.StringVal != want.Authorization.Payload.StringVal {
		t.Errorf("%s: the payload's bytes differ", name)
	}
	if got.Reservation == nil || want.Reservation == nil {
		t.Fatalf("%s: a reservation is missing: got %v, want %v", name, got.Reservation, want.Reservation)
	}
	if *got.Reservation != *want.Reservation {
		t.Errorf("%s: the reservation row\n got %+v\nwant %+v", name, *got.Reservation, *want.Reservation)
	}
}

// typedColumnsWritten checks, one by one, what the whole-row comparison
// also holds: settled is false (the edge names the column, NOT NULL DEFAULT
// false, as Python's statement does), and each typed column the edge writes
// is there with its value, the nonce NULL exactly when none was sent. Settle
// cannot tell a NULL typed column from its payload's copy (Python falls back
// to the payload), so only a read-back sees a column the insert left out.
func typedColumnsWritten(t *testing.T, name string, got StoredAuthorization, a gwauth.AuthorizationRow) {
	t.Helper()
	if got.Settled {
		t.Errorf("%s: settled is true", name)
	}
	if got.IdempotencyFingerprint != someString(a.IdempotencyFingerprint) || a.IdempotencyFingerprint == "" {
		t.Errorf("%s: idempotency_fingerprint %+v, want %q", name, got.IdempotencyFingerprint, a.IdempotencyFingerprint)
	}
	if got.StageDBootKid != someString(a.StageDBootKid) || a.StageDBootKid == "" {
		t.Errorf("%s: stage_d_boot_kid %+v, want %q", name, got.StageDBootKid, a.StageDBootKid)
	}
	if got.InvocationNonce != nullString(a.InvocationNonce) {
		t.Errorf("%s: invocation_nonce %+v, want %+v", name, got.InvocationNonce, nullString(a.InvocationNonce))
	}
}

// TestLeasedInsertWritesP1sRowsByteForByte: each row Python wrote for an
// answered P1 case, given to the store as compose will give it, is read
// back with every column as the golden has it, the payload's bytes
// identical; and the same rows written by Python's own statements, into a
// database of their own, read back the same, column for column.
func TestLeasedInsertWritesP1sRowsByteForByte(t *testing.T) {
	if emulator == nil {
		t.Skip(skipped)
	}
	ctx := context.Background()
	goClient, err := emulator.Database(ctx, "gwauth-go", nil)
	if err != nil {
		t.Fatal(err)
	}
	defer goClient.Close()
	pyClient, err := emulator.Database(ctx, "gwauth-python", nil)
	if err != nil {
		t.Fatal(err)
	}
	defer pyClient.Close()
	goStore, err := New(goClient, testConfig())
	if err != nil {
		t.Fatal(err)
	}
	pyStore, err := New(pyClient, testConfig())
	if err != nil {
		t.Fatal(err)
	}
	nonces := 0
	for _, g := range goldenAnswered(t) {
		a, r := g.rows()
		if a.InvocationNonce != nil {
			nonces++
		}
		if out := goStore.InsertLeasedAuthorization(ctx, a, r); out != Inserted {
			t.Fatalf("%s: %v", g.Name, out)
		}
		got, found, err := goStore.ReadAuthorizationForCompare(ctx, a.AuthorizationID)
		if err != nil || !found {
			t.Fatalf("%s: read back: %v %v", g.Name, found, err)
		}
		compareStored(t, g.Name+" (the edge's insert)", got, g.want())
		typedColumnsWritten(t, g.Name, got.Authorization, a)
		if err := pythonInsert(ctx, pyClient, a, r); err != nil {
			t.Fatalf("%s: Python's statements: %v", g.Name, err)
		}
		py, found, err := pyStore.ReadAuthorizationForCompare(ctx, a.AuthorizationID)
		if err != nil || !found {
			t.Fatalf("%s: read back Python's: %v %v", g.Name, found, err)
		}
		compareStored(t, g.Name+" (Python's statements)", py, got)
	}
	if nonces == 0 {
		t.Error("no golden case sends an invocation nonce: the typed column's non-NULL value went untested")
	}
	if _, found, err := goStore.ReadAuthorizationForCompare(ctx, "gwa-none"); found || err != nil {
		t.Errorf("an id with no row: %v %v", found, err)
	}
}

// --- AlreadyExists ---------------------------------------------------------

// TestScopeTakenByPythonsRow: Python's own statements land an authorization
// of a scope; the edge's insert of another authorization of the scope is
// ScopeTaken, lands nothing, and leaves Python's rows as they were. Of the
// same rows with a scope of their own, the insert is Inserted.
func TestScopeTakenByPythonsRow(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	pa, pr := leasedRows("")
	if err := pythonInsert(ctx, shared, pa, pr); err != nil {
		t.Fatal(err)
	}
	before, _, err := s.ReadAuthorizationForCompare(ctx, pa.AuthorizationID)
	if err != nil {
		t.Fatal(err)
	}
	ga, gr := leasedRows(pr.IdempotencyScope)
	if out := s.InsertLeasedAuthorization(ctx, ga, gr); out != ScopeTaken {
		t.Fatalf("another authorization of Python's scope: %v, want scope_taken", out)
	}
	moneySafe(t, "scope taken", ScopeTaken, landed(t, ga))
	if landed(t, ga) {
		t.Error("the scope's second authorization landed")
	}
	after, _, err := s.ReadAuthorizationForCompare(ctx, pa.AuthorizationID)
	if err != nil {
		t.Fatal(err)
	}
	compareStored(t, "Python's rows", after, before)
	// The positive control: the same rows, a scope of their own.
	gr.IdempotencyScope = storetest.UniqueID("scope")
	if out := s.InsertLeasedAuthorization(ctx, ga, gr); out != Inserted || !landed(t, ga) {
		t.Fatalf("the rows with a scope of their own: %v", out)
	}
}

// TestOurOwnRowIsLanded: inserting rows that are already there is
// AlreadyExists, and our authorization id has its row: Landed, not
// ScopeTaken, which would refund a hold the row uses.
func TestOurOwnRowIsLanded(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	a, r := leasedRows("")
	if out := s.InsertLeasedAuthorization(ctx, a, r); out != Inserted {
		t.Fatal(out)
	}
	if out := s.InsertLeasedAuthorization(ctx, a, r); out != Landed {
		t.Fatalf("the same rows again: %v, want landed", out)
	}
}

// TestRowsThatAreNotOneAuthorizationsSendNothing: mismatched rows are
// Failed before any transaction.
func TestRowsThatAreNotOneAuthorizationsSendNothing(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	for name, change := range map[string]func(*gwauth.AuthorizationRow, *gwauth.ReservationRow){
		"authorization id": func(a *gwauth.AuthorizationRow, r *gwauth.ReservationRow) { r.AuthorizationID = "gwa-other" },
		"reservation id":   func(a *gwauth.AuthorizationRow, r *gwauth.ReservationRow) { a.ReservationID = "res-other" },
		"workspace":        func(a *gwauth.AuthorizationRow, r *gwauth.ReservationRow) { r.WorkspaceID = "ws-other" },
		"key":              func(a *gwauth.AuthorizationRow, r *gwauth.ReservationRow) { r.KeyHash = "kh-other" },
		"scope":            func(a *gwauth.AuthorizationRow, r *gwauth.ReservationRow) { r.IdempotencyScope = "" },
	} {
		a, r := leasedRows("")
		change(&a, &r)
		if out := s.InsertLeasedAuthorization(ctx, a, r); out != Failed || landed(t, a) {
			t.Errorf("%s: %v", name, out)
		}
	}
}

// --- fault injection ---------------------------------------------------------

const (
	commitMethod        = "/google.spanner.v1.Spanner/Commit"
	createSessionMethod = "/google.spanner.v1.Spanner/CreateSession"
	streamingReadMethod = "/google.spanner.v1.Spanner/StreamingRead"
)

// fault decides, for each call a client makes, what happens: it may
// forward the call (invoke), answer instead of it, or both. A streaming
// call's invoke opens the stream.
type fault func(ctx context.Context, method string, call int, invoke func() error) error

// faultStore is a store of the shared database through a client of its own
// whose calls go through f; call counts each method's calls, from 1. The
// client closes when the test ends.
func faultStore(t *testing.T, f fault) (*Store, *spanner.Client) {
	t.Helper()
	s, c := openFaultStore(t, f)
	t.Cleanup(c.Close)
	return s, c
}

// openFaultStore is faultStore's store, its client left open: closing a
// client while a transaction still waits for its session panics in the
// client library (a send on the closed session-request channel), and an
// insert that ended at its deadline leaves its transaction waiting, up to
// the client's own 30 s.
func openFaultStore(t *testing.T, f fault) (*Store, *spanner.Client) {
	t.Helper()
	if emulator == nil {
		t.Skip(skipped)
	}
	var mu sync.Mutex
	calls := map[string]int{}
	interceptor := func(ctx context.Context, method string, req, reply any, cc *grpc.ClientConn,
		invoker grpc.UnaryInvoker, opts ...grpc.CallOption) error {
		mu.Lock()
		calls[method]++
		n := calls[method]
		mu.Unlock()
		return f(ctx, method, n, func() error { return invoker(ctx, method, req, reply, cc, opts...) })
	}
	streamInterceptor := func(ctx context.Context, desc *grpc.StreamDesc, cc *grpc.ClientConn, method string,
		streamer grpc.Streamer, opts ...grpc.CallOption) (grpc.ClientStream, error) {
		mu.Lock()
		calls[method]++
		n := calls[method]
		mu.Unlock()
		var stream grpc.ClientStream
		err := f(ctx, method, n, func() error {
			var err error
			stream, err = streamer(ctx, desc, cc, method, opts...)
			return err
		})
		return stream, err
	}
	c, err := emulator.Client(context.Background(), "spike",
		option.WithGRPCDialOption(grpc.WithUnaryInterceptor(interceptor)),
		option.WithGRPCDialOption(grpc.WithStreamInterceptor(streamInterceptor)))
	if err != nil {
		t.Fatal(err)
	}
	s, err := New(c, testConfig())
	if err != nil {
		t.Fatal(err)
	}
	return s, c
}

// dropFirstCommitAnswer applies the first commit and answers it with code
// instead of its response: the answer lost on the way back.
func dropFirstCommitAnswer(code codes.Code, message string) fault {
	return func(ctx context.Context, method string, call int, invoke func() error) error {
		if method != commitMethod || call != 1 {
			return invoke()
		}
		if err := invoke(); err != nil {
			return err
		}
		return status.Error(code, message)
	}
}

// TestADroppedCommitAnswerIsNeverRefunded: the first commit applies and
// its answer is lost, replaced by each error a lost answer can arrive as.
// Whatever the client then does, retrying the commit, re-running the
// transaction into AlreadyExists, or giving up, the outcome is never one
// that refunds. A lost answer the client re-runs the transaction after
// (Aborted, and Internal of a reset stream) reports the second attempt's
// AlreadyExists, which the own-id check makes Landed.
func TestADroppedCommitAnswerIsNeverRefunded(t *testing.T) {
	for _, tc := range []struct {
		code    codes.Code
		message string
		want    InsertOutcome
	}{
		// The client re-runs the transaction: AlreadyExists of our own rows.
		{codes.Aborted, "lost", Landed},
		{codes.Internal, "stream terminated by RST_STREAM with error code: INTERNAL_ERROR", Landed},
		// The client retries the commit itself, of the same transaction,
		// which Spanner answers as it did the first.
		{codes.Unavailable, "lost", Inserted},
		{codes.ResourceExhausted, "lost", Inserted},
		// The client gives up: the outcome is unknown, and the row is there
		// for the caller's read-back.
		{codes.DeadlineExceeded, "lost", Unknown},
		{codes.Canceled, "lost", Unknown},
		{codes.Internal, "lost", Unknown},
		{codes.Unknown, "lost", Unknown},
		{codes.DataLoss, "lost", Unknown},
	} {
		name := fmt.Sprintf("%v %q", tc.code, tc.message)
		s, _ := faultStore(t, dropFirstCommitAnswer(tc.code, tc.message))
		a, r := leasedRows("")
		out := s.InsertLeasedAuthorization(context.Background(), a, r)
		applied := landed(t, a)
		if !applied {
			t.Errorf("%s: the first commit did not apply", name)
		}
		moneySafe(t, name, out, applied)
		if out != tc.want {
			t.Errorf("%s: %v, want %v", name, out, tc.want)
		}
		if out == Unknown {
			if mine, err := s.AuthorizationExists(context.Background(), a.AuthorizationID); err != nil || !mine {
				t.Errorf("%s: the caller's read-back: %v %v", name, mine, err)
			}
		}
	}
}

// TestTheRetriedCommitReportsAlreadyExists is the positive control of the
// test above: through the client alone, without the store, a commit whose
// first answer is lost to a reset stream reports AlreadyExists though its
// rows are its own. Classified by its code alone it would be ScopeTaken, and
// refunded.
func TestTheRetriedCommitReportsAlreadyExists(t *testing.T) {
	_, c := faultStore(t, dropFirstCommitAnswer(codes.Internal, "stream terminated by RST_STREAM"))
	a, r := leasedRows("")
	_, err := c.ReadWriteTransactionWithOptions(context.Background(),
		func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
			return txn.BufferWrite(leasedMutations(a, r))
		}, spanner.TransactionOptions{TransactionTag: LeasedEdgeTag})
	if spanner.ErrCode(err) != codes.AlreadyExists || !landed(t, a) {
		t.Fatalf("the client's own answer: %v, landed %v", err, landed(t, a))
	}
	if out, alreadyExists := classifyInsert(err, true); !alreadyExists {
		t.Fatalf("the error is not read back: %v", out)
	}
}

// TestEachRefusedCommitsOutcome: the commit is answered with each code and
// never applied. The codes §8.4 says prove nothing applied are Failed; the
// rest are Unknown; AlreadyExists with no row of ours is ScopeTaken. The
// client retries Unavailable and Aborted itself until the deadline, which
// then ends them as Unknown: within a deadline, an Aborted never reaches the
// store, so its Failed row of the table is the classifier's alone
// (TestClassifyInsertIsTheContractTable).
func TestEachRefusedCommitsOutcome(t *testing.T) {
	cases := map[codes.Code]InsertOutcome{
		codes.InvalidArgument:    Failed,
		codes.FailedPrecondition: Failed,
		codes.NotFound:           Failed,
		codes.PermissionDenied:   Failed,
		codes.Unauthenticated:    Failed,
		codes.OutOfRange:         Failed,
		codes.Unimplemented:      Failed,
		codes.AlreadyExists:      ScopeTaken,
		codes.DeadlineExceeded:   Unknown,
		codes.Canceled:           Unknown,
		codes.Internal:           Unknown,
		codes.Unknown:            Unknown,
		codes.ResourceExhausted:  Unknown,
		codes.DataLoss:           Unknown,
		codes.Unavailable:        Unknown, // retried by the client to the deadline
		codes.Aborted:            Unknown, // retried by the client to the deadline
	}
	for code, want := range cases {
		s, _ := faultStore(t, func(ctx context.Context, method string, call int, invoke func() error) error {
			if method == commitMethod {
				return status.Error(code, "refused")
			}
			return invoke()
		})
		a, r := leasedRows("")
		start := time.Now()
		out := s.InsertLeasedAuthorization(context.Background(), a, r)
		took := time.Since(start)
		applied := landed(t, a)
		if applied {
			t.Errorf("%v: a refused commit applied", code)
		}
		moneySafe(t, code.String(), out, applied)
		if out != want {
			t.Errorf("%v: %v, want %v", code, out, want)
		}
		if took > InsertDeadline+ReadBackDeadline+500*time.Millisecond {
			t.Errorf("%v: took %v, past the deadlines", code, took)
		}
	}
}

// TestNoSessionIsFailedAndRetriedOnce: before the callback runs nothing can
// have been sent, so any error is Failed, after one retry within the
// deadline; and a client that waits for a session past the deadline does
// not hold the insert past it.
func TestNoSessionIsFailedAndRetriedOnce(t *testing.T) {
	sessionFails := func(code codes.Code, times int) fault {
		return func(ctx context.Context, method string, call int, invoke func() error) error {
			if method == createSessionMethod && call <= times {
				return status.Error(code, "no session")
			}
			return invoke()
		}
	}
	for _, tc := range []struct {
		name  string
		f     fault
		want  InsertOutcome
		limit time.Duration
	}{
		// Refused when the client opens (it makes its session then, and
		// again at its first transaction) and at the first attempt: the
		// retry takes a session and inserts.
		{"refused once", sessionFails(codes.PermissionDenied, 2), Inserted, InsertDeadline},
		// Refused always: Failed at once, after the retry.
		{"refused always", sessionFails(codes.PermissionDenied, 1<<30), Failed, InsertDeadline},
		// Unavailable always: the client retries the session for 30 s
		// without the deadline; the insert ends at its deadline, Failed.
		{"unavailable always", sessionFails(codes.Unavailable, 1<<30), Failed, InsertDeadline + 500*time.Millisecond},
	} {
		s, _ := openFaultStore(t, tc.f)
		a, r := leasedRows("")
		start := time.Now()
		out := s.InsertLeasedAuthorization(context.Background(), a, r)
		took := time.Since(start)
		applied := landed(t, a)
		moneySafe(t, tc.name, out, applied)
		if out != tc.want || took > tc.limit {
			t.Errorf("%s: %v in %v, want %v within %v", tc.name, out, took, tc.want, tc.limit)
		}
	}
}

// --- the detached context ----------------------------------------------------

// TestTheInsertOutlivesTheRequest: a request context cancelled before the
// insert, past its deadline, or cancelled while the commit is in flight
// (the enclave hanging up) inserts all the same; the read-back reads on such
// a context too. The positive control: the same client, on the cancelled
// context directly, is Canceled.
func TestTheInsertOutlivesTheRequest(t *testing.T) {
	cancelled, cancel := context.WithCancel(context.Background())
	cancel()
	expired, cancelExpired := context.WithDeadline(context.Background(), time.Now().Add(-time.Second))
	defer cancelExpired()
	var hangUp atomic.Pointer[context.CancelFunc]
	s, c := faultStore(t, func(ctx context.Context, method string, call int, invoke func() error) error {
		if method == commitMethod {
			if f := hangUp.Swap(nil); f != nil {
				(*f)()
			}
		}
		return invoke()
	})
	for name, ctx := range map[string]context.Context{"cancelled": cancelled, "expired": expired} {
		a, r := leasedRows("")
		if out := s.InsertLeasedAuthorization(ctx, a, r); out != Inserted || !landed(t, a) {
			t.Errorf("%s: %v", name, out)
		}
		if mine, err := s.AuthorizationExists(ctx, a.AuthorizationID); err != nil || !mine {
			t.Errorf("%s: the read-back: %v %v", name, mine, err)
		}
	}
	request, hang := context.WithCancel(context.Background())
	hangUp.Store(&hang)
	a, r := leasedRows("")
	if out := s.InsertLeasedAuthorization(request, a, r); out != Inserted || !landed(t, a) {
		t.Errorf("hung up mid-commit: %v", out)
	}
	if request.Err() == nil {
		t.Error("the request's context was never cancelled: the test hung nothing up")
	}
	// The positive control.
	a, r = leasedRows("")
	_, err := c.ReadWriteTransactionWithOptions(cancelled, func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		return txn.BufferWrite(leasedMutations(a, r))
	}, spanner.TransactionOptions{})
	if spanner.ErrCode(err) != codes.Canceled || landed(t, a) {
		t.Errorf("the client on the cancelled context: %v", err)
	}
}

// TestTheDeadlinesAreTheInsertsOwn: a commit that never answers is Unknown
// at the insert's deadline, not before and not long after; a read that never
// answers ends at the read-back's; and the read-back after an AlreadyExists
// that arrives as the insert's deadline passes has a deadline of its own, so
// it still finds our row.
func TestTheDeadlinesAreTheInsertsOwn(t *testing.T) {
	ctx := context.Background()
	var hold atomic.Bool
	s, _ := faultStore(t, func(ctx context.Context, method string, call int, invoke func() error) error {
		if hold.Load() && (method == commitMethod || method == streamingReadMethod) {
			<-ctx.Done()
			return status.FromContextError(ctx.Err()).Err()
		}
		return invoke()
	})
	hold.Store(true)
	a, r := leasedRows("")
	start := time.Now()
	out := s.InsertLeasedAuthorization(ctx, a, r)
	took := time.Since(start)
	if out != Unknown || took < InsertDeadline || took > InsertDeadline+500*time.Millisecond {
		t.Errorf("a commit that never answers: %v in %v", out, took)
	}
	start = time.Now()
	_, err := s.AuthorizationExists(ctx, a.AuthorizationID)
	took = time.Since(start)
	if err == nil || took < ReadBackDeadline || took > ReadBackDeadline+500*time.Millisecond {
		t.Errorf("a read that never answers: %v in %v", err, took)
	}
	hold.Store(false)
	if landed(t, a) {
		t.Error("the held commit applied")
	}

	// AlreadyExists at the insert's deadline, of rows that are ours.
	a, r = leasedRows("")
	if out := s.InsertLeasedAuthorization(ctx, a, r); out != Inserted {
		t.Fatal(out)
	}
	late, _ := faultStore(t, func(ctx context.Context, method string, call int, invoke func() error) error {
		if method == commitMethod {
			<-ctx.Done()
			return status.Error(codes.AlreadyExists, "row already exists")
		}
		return invoke()
	})
	if out := late.InsertLeasedAuthorization(ctx, a, r); out != Landed {
		t.Errorf("AlreadyExists as the deadline passes: %v, want landed", out)
	}
}
