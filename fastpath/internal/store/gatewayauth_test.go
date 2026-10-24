package store

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
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
// callback ran once. AlreadyExists is not an outcome yet: it is read back.
// Only codes the request alone determines are Failed; after a second run
// nothing is.
var insertTable = map[codes.Code]InsertOutcome{
	codes.Canceled:           Unknown,
	codes.Unknown:            Unknown,
	codes.InvalidArgument:    Failed,
	codes.DeadlineExceeded:   Unknown,
	codes.NotFound:           Unknown, // a re-sent commit of a forgotten transaction
	codes.PermissionDenied:   Unknown, // a re-sent commit refused its credentials
	codes.ResourceExhausted:  Unknown,
	codes.FailedPrecondition: Unknown, // a re-sent commit of a forgotten transaction
	codes.Aborted:            Failed,
	codes.OutOfRange:         Failed,
	codes.Unimplemented:      Failed,
	codes.Internal:           Unknown,
	codes.Unavailable:        Unknown,
	codes.DataLoss:           Unknown,
	codes.Unauthenticated:    Unknown, // a re-sent commit refused its credentials
}

// TestClassifyInsertIsTheContractTable: every gRPC code, raw, as the
// client's own error type and wrapped again, maps as §8.4 says after one
// run of the callback; after two, nothing is Failed; the context's end and
// an error with no status are Unknown; any error before the callback ran is
// Failed; no error is Inserted.
func TestClassifyInsertIsTheContractTable(t *testing.T) {
	if got, ae := classifyInsert(nil, 1); got != Inserted || ae {
		t.Errorf("no error: %v %v", got, ae)
	}
	for code := codes.Canceled; code <= codes.Unauthenticated; code++ {
		raw := status.Error(code, "injected")
		for name, err := range map[string]error{
			"raw":     raw,
			"spanner": spanner.ToSpannerError(raw),
			"wrapped": fmt.Errorf("commit: %w", spanner.ToSpannerError(raw)),
		} {
			for _, runs := range []int32{1, 2, 3} {
				got, alreadyExists := classifyInsert(err, runs)
				if code == codes.AlreadyExists {
					if !alreadyExists {
						t.Errorf("%v %s, %d runs: not read back", code, name, runs)
					}
					continue
				}
				want := insertTable[code]
				if runs > 1 {
					want = Unknown
				}
				if alreadyExists || got != want {
					t.Errorf("%v %s, %d runs: %v (read back %v), want %v", code, name, runs, got, alreadyExists, want)
				}
			}
			if got, _ := classifyInsert(err, 0); got != Failed {
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
		if got, ae := classifyInsert(err, 1); got != Unknown || ae {
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
	commitMethod           = "/google.spanner.v1.Spanner/Commit"
	createSessionMethod    = "/google.spanner.v1.Spanner/CreateSession"
	beginTransactionMethod = "/google.spanner.v1.Spanner/BeginTransaction"
	streamingReadMethod    = "/google.spanner.v1.Spanner/StreamingRead"
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
// AlreadyExists, which the own-id check makes Landed; one the client gives
// up on is read back, and found, so Landed too.
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
		// The client gives up: the outcome is not known, and the store's
		// read-back finds the row, ours.
		{codes.DeadlineExceeded, "lost", Landed},
		{codes.Canceled, "lost", Landed},
		{codes.Internal, "lost", Landed},
		{codes.Unknown, "lost", Landed},
		{codes.DataLoss, "lost", Landed},
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
	if out, alreadyExists := classifyInsert(err, 2); !alreadyExists {
		t.Fatalf("the error is not read back: %v", out)
	}
}

// TestEachRefusedCommitsOutcome: the commit is answered with each code and
// never applied. The codes §8.4 says prove nothing applied are Failed; the
// rest are Unknown, NotFound, FailedPrecondition and the credential codes
// among them (a commit had been sent); AlreadyExists with no row of ours is
// ScopeTaken. The
// client retries Unavailable and Aborted itself until the deadline, which
// then ends them as Unknown: within a deadline, an Aborted never reaches the
// store, so its Failed row of the table is the classifier's alone
// (TestClassifyInsertIsTheContractTable).
func TestEachRefusedCommitsOutcome(t *testing.T) {
	cases := map[codes.Code]InsertOutcome{
		codes.InvalidArgument:    Failed,
		codes.FailedPrecondition: Unknown,
		codes.NotFound:           Unknown,
		codes.PermissionDenied:   Unknown,
		codes.Unauthenticated:    Unknown,
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
// (the enclave hanging up) inserts all the same, and an AlreadyExists on
// such a context is read back all the same. The positive control: the same
// client, on the cancelled context directly, is Canceled.
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
		if out := s.InsertLeasedAuthorization(ctx, a, r); out != Landed {
			t.Errorf("%s: the read-back of our own rows: %v", name, out)
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

// TestTheDeadlinesAreTheInsertsOwn: a commit that never answers ends at
// the insert's deadline, not before and not long after, and so does one
// whose call ignores the deadline (no grace past it, decision 12); its
// read-back, which never answers either, ends at the read-back's: Unknown
// after the two deadlines and no later. The read-back after an
// AlreadyExists that arrives just before the insert's deadline has a
// deadline of its own, so it still finds our row though it takes longer
// than the insert has left.
func TestTheDeadlinesAreTheInsertsOwn(t *testing.T) {
	ctx := context.Background()
	var hold, ignore atomic.Bool
	s, _ := faultStore(t, func(ctx context.Context, method string, call int, invoke func() error) error {
		if ignore.Load() && method == commitMethod {
			time.Sleep(InsertDeadline + time.Second)
			return status.Error(codes.Unavailable, "late")
		}
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
	if out != Unknown || took < InsertDeadline+ReadBackDeadline || took > InsertDeadline+ReadBackDeadline+250*time.Millisecond {
		t.Errorf("a commit and a read-back that never answer: %v in %v", out, took)
	}
	hold.Store(false)
	if landed(t, a) {
		t.Error("the held commit applied")
	}

	ignore.Store(true)
	a, r = leasedRows("")
	start = time.Now()
	out = s.InsertLeasedAuthorization(ctx, a, r)
	took = time.Since(start)
	if out != Unknown || took > InsertDeadline+ReadBackDeadline+250*time.Millisecond {
		t.Errorf("a commit that ignores the deadline: %v in %v", out, took)
	}
	ignore.Store(false)

	// AlreadyExists just before the insert's deadline, of rows that are
	// ours, and a read-back slower than what the insert has left.
	a, r = leasedRows("")
	if out := s.InsertLeasedAuthorization(ctx, a, r); out != Inserted {
		t.Fatal(out)
	}
	late, _ := faultStore(t, func(ctx context.Context, method string, call int, invoke func() error) error {
		switch method {
		case commitMethod:
			if d, ok := ctx.Deadline(); ok {
				time.Sleep(time.Until(d) - 150*time.Millisecond)
			}
			return status.Error(codes.AlreadyExists, "row already exists")
		case streamingReadMethod:
			time.Sleep(300 * time.Millisecond)
		}
		return invoke()
	})
	if out := late.InsertLeasedAuthorization(ctx, a, r); out != Landed {
		t.Errorf("AlreadyExists just before the deadline: %v, want landed", out)
	}
}

// TestALostAnswerThenARefusalIsNeverFailed: the first commit applies and
// its answer is lost as an error after which the client re-runs the
// transaction (Aborted, or Internal of a reset stream); the re-run's
// BeginTransaction or Commit is then refused with each code that, after one
// run, would be Failed or proves nothing about the first commit. The rows
// landed, so the outcome must not refund: the error proves nothing, and the
// read-back finds the row, Landed. (The review of #2019 reproduced Failed
// here.)
func TestALostAnswerThenARefusalIsNeverFailed(t *testing.T) {
	for _, lost := range []struct {
		code    codes.Code
		message string
	}{{codes.Aborted, "lost"}, {codes.Internal, "stream terminated by RST_STREAM"}} {
		for _, second := range []string{beginTransactionMethod, commitMethod} {
			for _, code := range []codes.Code{codes.Unauthenticated, codes.PermissionDenied,
				codes.FailedPrecondition, codes.NotFound, codes.InvalidArgument, codes.OutOfRange,
				codes.Unimplemented} {
				name := fmt.Sprintf("lost as %v, then %s %v", lost.code, second, code)
				var committed atomic.Bool
				s, _ := faultStore(t, func(ctx context.Context, method string, call int, invoke func() error) error {
					if method == commitMethod && !committed.Load() {
						committed.Store(true)
						if err := invoke(); err != nil {
							return err
						}
						return status.Error(lost.code, lost.message)
					}
					if method == second && committed.Load() {
						return status.Error(code, "refused")
					}
					return invoke()
				})
				a, r := leasedRows("")
				out := s.InsertLeasedAuthorization(context.Background(), a, r)
				applied := landed(t, a)
				if !applied {
					t.Errorf("%s: the first commit did not apply", name)
				}
				moneySafe(t, name, out, applied)
				if out != Landed {
					t.Errorf("%s: %v, want landed", name, out)
				}
			}
		}
	}
}

// TestAReSentCommitsRefusalIsReadBack: the first commit applies and its
// answer is lost as Unavailable, so the client re-sends the commit of the
// same transaction, and the re-send is refused: as a transaction Spanner
// has forgotten (NotFound, FailedPrecondition) or as credentials refused.
// No guarantee says the first did not apply, and it did: not Failed but
// read back, and found, Landed.
func TestAReSentCommitsRefusalIsReadBack(t *testing.T) {
	for _, code := range []codes.Code{codes.NotFound, codes.FailedPrecondition, codes.PermissionDenied,
		codes.Unauthenticated} {
		s, _ := faultStore(t, func(ctx context.Context, method string, call int, invoke func() error) error {
			if method != commitMethod {
				return invoke()
			}
			if call == 1 {
				if err := invoke(); err != nil {
					return err
				}
				return status.Error(codes.Unavailable, "lost")
			}
			return status.Error(code, "transaction not found")
		})
		a, r := leasedRows("")
		out := s.InsertLeasedAuthorization(context.Background(), a, r)
		applied := landed(t, a)
		moneySafe(t, code.String(), out, applied)
		if !applied || out != Landed {
			t.Errorf("%v: %v, applied %v; want landed, applied", code, out, applied)
		}
	}
}

// TestARowUnderOurIDNamingAnotherReservationIsNotOurs: a row already under
// our authorization id that names another reservation is not ours: the
// insert's AlreadyExists is ScopeTaken, not Landed, and nothing of ours
// lands, nor can it.
func TestARowUnderOurIDNamingAnotherReservationIsNotOurs(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	theirs, theirReservation := leasedRows("")
	a, r := leasedRows("")
	theirs.AuthorizationID, theirReservation.AuthorizationID = a.AuthorizationID, a.AuthorizationID
	if err := pythonInsert(ctx, shared, theirs, theirReservation); err != nil {
		t.Fatal(err)
	}
	if out := s.InsertLeasedAuthorization(ctx, a, r); out != ScopeTaken {
		t.Fatalf("%v, want scope_taken", out)
	}
	if _, err := shared.Single().ReadRow(ctx, "tr_reservation", spanner.Key{r.ReservationID},
		[]string{"reservation_id"}); !errors.Is(err, spanner.ErrRowNotFound) {
		t.Errorf("our reservation: %v", err)
	}
	got, _, err := s.ReadAuthorizationForCompare(ctx, a.AuthorizationID)
	if err != nil || got.Authorization.ReservationID.StringVal != theirReservation.ReservationID {
		t.Errorf("the row under our id: %+v %v", got.Authorization, err)
	}
}

// --- the client's session ------------------------------------------------

// lateSession is a fault that refuses every session until after a first
// insert's deadline (the client retries it, without the deadline), and
// records every BeginTransaction and Commit, and when a session is made.
type lateSession struct {
	opened time.Time
	mu     sync.Mutex
	made   bool
	sent   []string
}

func (l *lateSession) fault(ctx context.Context, method string, call int, invoke func() error) error {
	switch method {
	case createSessionMethod:
		if time.Since(l.opened) < InsertDeadline+time.Second {
			return status.Error(codes.Unavailable, "no session yet")
		}
		err := invoke()
		l.mu.Lock()
		l.made = l.made || err == nil
		l.mu.Unlock()
		return err
	case beginTransactionMethod, commitMethod:
		l.mu.Lock()
		l.sent = append(l.sent, method)
		l.mu.Unlock()
	}
	return invoke()
}

func (l *lateSession) seen() (made bool, sent []string) {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.made, append([]string(nil), l.sent...)
}

// sessionWorkers counts the Spanner clients' session workers running in
// the process (sessionManager.createMultiplexedSession, one a client).
func sessionWorkers() int {
	buf := make([]byte, 1<<24)
	n := runtime.Stack(buf, true)
	return strings.Count(string(buf[:n]), ").createMultiplexedSession(")
}

// waitInserts is WaitLeasedInserts, bounded so a test cannot hang on it.
func waitInserts(t *testing.T, s *Store) {
	t.Helper()
	done := make(chan struct{})
	go func() {
		s.WaitLeasedInserts()
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(70 * time.Second):
		t.Fatal("the leased inserts' transactions did not end")
	}
}

// TestASessionAfterTheDeadlineSendsNothing: the client cannot make a
// session until after the insert's deadline, so the insert is Failed at the
// deadline with its transaction still waiting. When the session comes the
// transaction takes it on a context that has not ended, so the client's
// session worker, which exits for good when it serves an ended one, still
// runs; its callback then refuses, so nothing is sent at all, on any
// context: no BeginTransaction, no Commit, no row. The positive control:
// the waiting transaction got its session and its callback ran and refused
// (refusedLate), so the test saw something late.
func TestASessionAfterTheDeadlineSendsNothing(t *testing.T) {
	late := &lateSession{opened: time.Now()}
	s, _ := openFaultStore(t, late.fault)
	workers := sessionWorkers()
	a, r := leasedRows("")
	if out := s.InsertLeasedAuthorization(context.Background(), a, r); out != Failed {
		t.Fatalf("%v, want failed", out)
	}
	waitInserts(t, s)
	made, sent := late.seen()
	if !made || s.leased.refusedLate.Load() != 1 {
		t.Fatalf("session made %v, late refusals %d: the waiting transaction never took its session",
			made, s.leased.refusedLate.Load())
	}
	if len(sent) != 0 {
		t.Errorf("sent after Failed: %v", sent)
	}
	if landed(t, a) {
		t.Error("the rows of a Failed insert landed")
	}
	if got := sessionWorkers(); got != workers {
		t.Errorf("%d session workers, %d before: a session take was served on an ended context", got, workers)
	}
	// The client is whole: the next insert takes the session and inserts.
	a, r = leasedRows("")
	if out := s.InsertLeasedAuthorization(context.Background(), a, r); out != Inserted || !landed(t, a) {
		t.Errorf("after the late session: %v", out)
	}
}

// TestOneInsertAtATimeWaitsForASession: until the client has a session,
// one insert's transaction may wait for it; another insert waits only
// within its own deadline, for that session, and begins no transaction:
// Failed with nothing sent. So the transactions left waiting are one, not
// one an insert (each could hold the client's close for its own 30 s).
func TestOneInsertAtATimeWaitsForASession(t *testing.T) {
	late := &lateSession{opened: time.Now()}
	s, _ := openFaultStore(t, late.fault)
	var wg sync.WaitGroup
	outs := make([]InsertOutcome, 3)
	took := make([]time.Duration, 3)
	for i := range outs {
		wg.Add(1)
		go func() {
			defer wg.Done()
			a, r := leasedRows("")
			start := time.Now()
			outs[i] = s.InsertLeasedAuthorization(context.Background(), a, r)
			took[i] = time.Since(start)
		}()
	}
	wg.Wait()
	for i, out := range outs {
		if out != Failed || took[i] < InsertDeadline-50*time.Millisecond || took[i] > InsertDeadline+250*time.Millisecond {
			t.Errorf("insert %d: %v in %v, want failed at the deadline", i, out, took[i])
		}
	}
	waitInserts(t, s)
	if n := s.leased.refusedLate.Load(); n != 1 {
		t.Errorf("%d transactions waited for the session and refused, want 1", n)
	}
	if _, sent := late.seen(); len(sent) != 0 {
		t.Errorf("sent after Failed: %v", sent)
	}
	// With the session made, inserts no longer wait on one another.
	for i := range outs {
		wg.Add(1)
		go func() {
			defer wg.Done()
			a, r := leasedRows("")
			outs[i] = s.InsertLeasedAuthorization(context.Background(), a, r)
		}()
	}
	wg.Wait()
	for i, out := range outs {
		if out != Inserted {
			t.Errorf("insert %d with the session: %v", i, out)
		}
	}
}

// closeEarly runs TestTheClientClosesAfterItsWaitingInsert's positive
// control in a process of its own: the client closed while an insert's
// transaction waits for its session panics, which ends the process.
const closeEarly = "FASTPATH_STORE_CLOSE_EARLY"

// TestTheClientClosesAfterItsWaitingInsert (§8.4): closing the client while
// an insert's transaction still waits for its session panics in the client
// library (a send on its closed session-request channel), so the process
// closes it only after WaitLeasedInserts. Here the client closes after the
// wait and nothing panics; the positive control, in a process of its own,
// closes it without waiting and the process dies of that panic.
func TestTheClientClosesAfterItsWaitingInsert(t *testing.T) {
	if emulator == nil {
		t.Skip(skipped)
	}
	late := &lateSession{opened: time.Now()}
	s, c := openFaultStore(t, late.fault)
	a, r := leasedRows("")
	if out := s.InsertLeasedAuthorization(context.Background(), a, r); out != Failed {
		t.Fatalf("%v, want failed", out)
	}
	if os.Getenv(closeEarly) == "1" {
		c.Close()
		time.Sleep(5 * time.Second)
		return
	}
	waitInserts(t, s)
	c.Close()

	cmd := exec.Command(os.Args[0], "-test.run=^TestTheClientClosesAfterItsWaitingInsert$", "-test.count=1")
	cmd.Env = append(os.Environ(), closeEarly+"=1")
	out, err := cmd.CombinedOutput()
	if err == nil || !strings.Contains(string(out), "send on closed channel") {
		t.Fatalf("closing the client under a waiting insert: %v\n%s", err, out)
	}
}

// --- the money-safety invariants under concurrency (§8.4) -----------------

// scopeRows counts the reservations of a scope, and of those the ones whose
// authorization row is there too.
func scopeRows(t *testing.T, scope string) (reservations, authorizations int) {
	t.Helper()
	err := shared.Single().Query(context.Background(), spanner.Statement{
		SQL: `SELECT r.authorization_id, a.authorization_id IS NOT NULL FROM tr_reservation r
		      LEFT JOIN tr_gateway_authorization a ON a.authorization_id = r.authorization_id
		      WHERE r.idempotency_scope = @scope`,
		Params: map[string]any{"scope": scope},
	}).Do(func(row *spanner.Row) error {
		var id spanner.NullString
		var paired bool
		if err := row.Columns(&id, &paired); err != nil {
			return err
		}
		reservations++
		if paired {
			authorizations++
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	return reservations, authorizations
}

// TestOneIdempotencyKeyTwiceAtOnce: authorizations of one scope (the same
// idempotency key twice, with the same body or not) inserted at once: one
// lands, Inserted; every other is ScopeTaken, refunds, and lands nothing.
// No two holds answer for one scope, and none answers without its rows.
func TestOneIdempotencyKeyTwiceAtOnce(t *testing.T) {
	s := spikeStore(t)
	for round := 0; round < 5; round++ {
		scope := storetest.UniqueID("ws") + "#kh#" + storetest.UniqueID("idem")
		const n = 6
		outs := make([]InsertOutcome, n)
		rows := make([]gwauth.AuthorizationRow, n)
		var wg sync.WaitGroup
		start := make(chan struct{})
		for i := range outs {
			a, r := leasedRows(scope)
			rows[i] = a
			wg.Add(1)
			go func() {
				defer wg.Done()
				<-start
				outs[i] = s.InsertLeasedAuthorization(context.Background(), a, r)
			}()
		}
		close(start)
		wg.Wait()
		inserted := 0
		for i, out := range outs {
			applied := landed(t, rows[i])
			moneySafe(t, fmt.Sprintf("round %d insert %d", round, i), out, applied)
			switch out {
			case Inserted:
				inserted++
			case ScopeTaken:
			default:
				t.Errorf("round %d insert %d: %v, want inserted or scope_taken", round, i, out)
			}
		}
		if res, auths := scopeRows(t, scope); inserted != 1 || res != 1 || auths != 1 {
			t.Errorf("round %d: %d inserted, %d reservations and %d authorizations of the scope; want one each",
				round, inserted, res, auths)
		}
	}
}

// TestARetryAfterUnknownNeverHoldsTwice: an insert whose commit applied but
// whose answer and read-back were both lost is Unknown (the 503, the hold
// kept); the enclave's retry of the same idempotency key, under new ids,
// is ScopeTaken (refund, and Python replays the first). An insert whose
// commit never applied is Unknown too when its read-back is lost, and then
// the retry inserts. Either way the scope holds one reservation, and no
// refund follows a commit that applied.
func TestARetryAfterUnknownNeverHoldsTwice(t *testing.T) {
	for _, applies := range []bool{true, false} {
		var commits atomic.Int32
		s, _ := faultStore(t, func(ctx context.Context, method string, call int, invoke func() error) error {
			switch method {
			case commitMethod:
				if commits.Add(1) == 1 {
					if applies {
						if err := invoke(); err != nil {
							return err
						}
					}
					return status.Error(codes.DeadlineExceeded, "lost")
				}
			case streamingReadMethod:
				return status.Error(codes.Unavailable, "read-back lost")
			}
			return invoke()
		})
		scope := storetest.UniqueID("ws") + "#kh#" + storetest.UniqueID("idem")
		first, firstReservation := leasedRows(scope)
		out := s.InsertLeasedAuthorization(context.Background(), first, firstReservation)
		applied := landed(t, first)
		moneySafe(t, "the first", out, applied)
		if out != Unknown || applied != applies {
			t.Fatalf("applies %v: the first insert %v, landed %v; want unknown", applies, out, applied)
		}
		// The retry, through a store whose read-back works.
		retry, retryReservation := leasedRows(scope)
		want := ScopeTaken
		if !applies {
			want = Inserted
		}
		out = spikeStore(t).InsertLeasedAuthorization(context.Background(), retry, retryReservation)
		moneySafe(t, "the retry", out, landed(t, retry))
		if out != want {
			t.Errorf("applies %v: the retry %v, want %v", applies, out, want)
		}
		if res, auths := scopeRows(t, scope); res != 1 || auths != 1 {
			t.Errorf("applies %v: %d reservations, %d authorizations of the scope; want one", applies, res, auths)
		}
	}
}

// TestPythonAndTheEdgeRaceForAScope: Python's leased insert and the edge's
// of one scope at once: exactly one lands; the edge's is Inserted exactly
// when Python's failed, and otherwise ScopeTaken with nothing of its own.
func TestPythonAndTheEdgeRaceForAScope(t *testing.T) {
	s := spikeStore(t)
	for round := 0; round < 8; round++ {
		scope := storetest.UniqueID("ws") + "#kh#" + storetest.UniqueID("idem")
		pa, pr := leasedRows(scope)
		ga, gr := leasedRows(scope)
		var pyErr error
		var out InsertOutcome
		var wg sync.WaitGroup
		wg.Add(2)
		go func() { defer wg.Done(); pyErr = pythonInsert(context.Background(), shared, pa, pr) }()
		go func() { defer wg.Done(); out = s.InsertLeasedAuthorization(context.Background(), ga, gr) }()
		wg.Wait()
		ours := landed(t, ga)
		moneySafe(t, fmt.Sprintf("round %d", round), out, ours)
		if (out == Inserted) != (pyErr != nil) || (out != Inserted && out != ScopeTaken) || landed(t, pa) == ours {
			t.Errorf("round %d: edge %v (landed %v), Python %v", round, out, ours, pyErr)
		}
		if res, auths := scopeRows(t, scope); res != 1 || auths != 1 {
			t.Errorf("round %d: %d reservations, %d authorizations of the scope", round, res, auths)
		}
	}
}
