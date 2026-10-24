package service

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"cloud.google.com/go/spanner"
	"cloud.google.com/go/spanner/apiv1/spannerpb"
	"google.golang.org/api/option"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/Lore-Hex/quill-router/fastpath/internal/edge"
	"github.com/Lore-Hex/quill-router/fastpath/internal/frontdoor"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store/storetest"
)

// The decide path end to end (docs/design/fast-admission-e2-decide.md §1,
// G11): a process with -edge-decide over the emulator, its routing snapshot
// and edge policy signed and served fresh by a fake Python, a workspace in
// the pilot with its key, credit row and decide row, and a boot of the
// test's own signing the requests. The edge decides from its cache of the
// entity table, admits through the node's own front door and owner, inserts
// the rows in Spanner and answers; Python sees only what passes.

// goldenFile is what these tests take from P1's goldens (§10.1): the
// policy, the internal token and one answered case, as templates.
type goldenFile struct {
	Policy        map[string]json.RawMessage `json:"policy"`
	InternalToken string                     `json:"internal_token"`
	Cases         []struct {
		Name    string `json:"name"`
		Request struct {
			Body string `json:"body"`
		} `json:"request"`
		Boot struct {
			ImageDigest string `json:"image_digest"`
		} `json:"boot"`
		Key struct {
			KeyBody string `json:"key_body"`
		} `json:"key"`
		Workspace struct {
			Body string `json:"body"`
		} `json:"workspace"`
	} `json:"cases"`
}

func readGolden(t *testing.T, name string) []byte {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("..", "..", "testdata", name))
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

// decideWorld is one test's cohort: a workspace, its key, and a boot whose
// private key the test signs with.
type decideWorld struct {
	workspace, lookupHash, keyHash, kid string
	boot                                ed25519.PrivateKey
	body                                []byte
	token                               string
	policy                              map[string]json.RawMessage
}

// seedDecideWorld stores a cohort's world as production stores it: the
// key's lookup pointer, the key and the workspace from the golden case's
// bodies under the test's own ids, the boot, a credit row at the trust tier
// leases need, the switch on and the decide row on for gcp/us-central1.
func seedDecideWorld(t *testing.T) decideWorld {
	t.Helper()
	var f goldenFile
	if err := json.Unmarshal(readGolden(t, "authorize_cases_v1.json"), &f); err != nil {
		t.Fatal(err)
	}
	var c = f.Cases[0]
	for _, x := range f.Cases {
		if x.Name == "answer:idempotency:header" {
			c = x
		}
	}
	if c.Name != "answer:idempotency:header" {
		t.Fatal("no answer:idempotency:header case")
	}
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	w := decideWorld{workspace: storetest.UniqueID("ws"), keyHash: storetest.UniqueID("key"),
		kid: storetest.UniqueID("boot"), boot: private, token: f.InternalToken}
	sum := sha256.Sum256([]byte(w.keyHash))
	w.lookupHash = hex.EncodeToString(sum[:])
	set := func(body string, fields map[string]any) string {
		var m map[string]any
		if err := json.Unmarshal([]byte(body), &m); err != nil {
			t.Fatal(err)
		}
		for k, v := range fields {
			m[k] = v
		}
		out, _ := json.Marshal(m)
		return string(out)
	}
	keyBody := set(c.Key.KeyBody, map[string]any{"hash": w.keyHash, "lookup_hash": w.lookupHash,
		"workspace_id": w.workspace})
	wsBody := set(c.Workspace.Body, map[string]any{"id": w.workspace})
	bootBody := fmt.Sprintf(`{"kid":%q,"jwk":{"kty":"OKP","crv":"Ed25519","x":%q},"approved":true,"verified":true,`+
		`"image_digest":%q,"declares_usage_heartbeat":true,"attestation_kind":"gcp-cs-jwt",`+
		`"registered_at":"2026-10-19T00:00:00Z"}`, w.kid, base64.RawURLEncoding.EncodeToString(public), c.Boot.ImageDigest)
	entity := func(kind, id, body string) *spanner.Mutation {
		return spanner.Insert("tr_entities", []string{"kind", "id", "body", "updated_at"},
			[]any{kind, id, body, spanner.CommitTimestamp})
	}
	ctx := context.Background()
	if _, err := shared.Apply(ctx, []*spanner.Mutation{
		entity("api_key_lookup", w.lookupHash, `{"key_id":"`+w.keyHash+`"}`),
		entity("api_key", w.keyHash, keyBody),
		entity("workspace", w.workspace, wsBody),
		entity("spend_lease_boot", w.kid, bootBody),
		storetest.Enabled(w.workspace),
		spanner.InsertMap("tr_credit_balance", map[string]any{"workspace_id": w.workspace, "shard": int64(0),
			"total_credits": int64(1_000_000_000_000), "trust_tier": int64(3)}),
	}); err != nil {
		t.Fatal(err)
	}
	s, err := store.New(shared, config(listen(t)).Store)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := s.SetDecide(ctx, w.workspace, "gcp", "us-central1", true); err != nil {
		t.Fatal(err)
	}
	var body map[string]any
	if err := json.Unmarshal([]byte(c.Request.Body), &body); err != nil {
		t.Fatal(err)
	}
	w.body = []byte(strings.Replace(c.Request.Body, body["api_key_lookup_hash"].(string), w.lookupHash, 1))
	w.policy = map[string]json.RawMessage{}
	for k, v := range f.Policy {
		w.policy[k] = v
	}
	pilot, _ := json.Marshal([]string{w.workspace})
	w.policy["fastpath_pilot_workspaces"], w.policy["stage_d"] = pilot,
		json.RawMessage(strings.Replace(string(f.Policy["stage_d"]), `"ws_e2_cohort"`, strconv.Quote(w.workspace), 1))
	return w
}

// request is the world's authorize as the enclave sends it, signed by its
// boot, with the idempotency key in its header.
func (w decideWorld) request(t *testing.T, edgeAddr, idempotencyKey string) *http.Request {
	t.Helper()
	const path = "/internal/gateway/authorize"
	req, err := http.NewRequest(http.MethodPost, "http://"+edgeAddr+path, bytes.NewReader(w.body))
	if err != nil {
		t.Fatal(err)
	}
	sig := ed25519.Sign(w.boot, edge.Digest(http.MethodPost, path, w.body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept-Encoding", "identity")
	req.Header.Set(edge.TokenHeader, w.token)
	req.Header.Set(edge.BootHeader, "kid="+w.kid+",sig="+base64.RawURLEncoding.EncodeToString(sig))
	req.Header.Set("Idempotency-Key", idempotencyKey)
	return req
}

// signingPython is the fake Python: the golden routing snapshot moved to
// now and the world's policy, each signed fresh by the test's key on every
// poll, and a 200 replay to each authorize that reaches it.
type signingPython struct {
	server     *httptest.Server
	private    ed25519.PrivateKey
	public     ed25519.PublicKey
	snapshot   []byte
	claims     map[string]any
	policy     map[string]json.RawMessage
	authorizes atomic.Int64
}

const snapshotKid = "routing-snapshot-1"

func newSigningPython(t *testing.T, w decideWorld) *signingPython {
	t.Helper()
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	py := &signingPython{private: private, public: public, policy: w.policy}
	raw := readGolden(t, "routing_snapshot_v1.body.json")
	var fields struct {
		CompiledAt     int64             `json:"compiled_at"`
		SourceRevision string            `json:"source_revision"`
		Plane          map[string]string `json:"plane"`
	}
	if err := json.Unmarshal(raw, &fields); err != nil {
		t.Fatal(err)
	}
	// Every instant of the snapshot moved by one shift, so its window and
	// its prices' epochs hold now.
	shift := time.Now().Unix() - 60 - fields.CompiledAt
	py.snapshot = regexp.MustCompile(`"(compiled_at|not_after|starts_at)":(\d+)`).ReplaceAllFunc(raw, func(m []byte) []byte {
		i := bytes.LastIndexByte(m, ':')
		n, _ := strconv.ParseInt(string(m[i+1:]), 10, 64)
		return append(m[:i+1:i+1], strconv.FormatInt(n+shift, 10)...)
	})
	// The claims' instants, read back from the moved body (the routing
	// snapshot's package is the edge's and gwauth's alone to import).
	var moved struct {
		CompiledAt int64 `json:"compiled_at"`
		NotAfter   int64 `json:"not_after"`
	}
	if err := json.Unmarshal(py.snapshot, &moved); err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(py.snapshot)
	// A manifest's claims, as canonical JSON: sorted keys, compact.
	py.claims = map[string]any{"aud": edge.SnapshotAudience, "compiled_at": moved.CompiledAt, "iss": edge.SnapshotIssuer,
		"not_after": moved.NotAfter, "plane": fields.Plane, "sha256": hex.EncodeToString(sum[:]),
		"size": len(py.snapshot), "source_revision": fields.SourceRevision, "v": 1}
	py.server = httptest.NewServer(http.HandlerFunc(py.serve))
	t.Cleanup(py.server.Close)
	return py
}

func (py *signingPython) jws(typ string, claims any) string {
	header := fmt.Sprintf(`{"alg":"EdDSA","kid":%q,"typ":%q}`, snapshotKid, typ)
	payload, err := json.Marshal(claims)
	if err != nil {
		panic(err)
	}
	signing := base64.RawURLEncoding.EncodeToString([]byte(header)) + "." + base64.RawURLEncoding.EncodeToString(payload)
	return signing + "." + base64.RawURLEncoding.EncodeToString(ed25519.Sign(py.private, []byte(signing)))
}

func (py *signingPython) serve(w http.ResponseWriter, r *http.Request) {
	_, _ = io.Copy(io.Discard, r.Body)
	w.Header().Set("Content-Type", "application/json")
	switch {
	case r.Method == http.MethodGet && r.URL.Path == "/internal/fastpath/routing-snapshot":
		out, _ := json.Marshal(map[string]any{"v": 1, "kind": "tr-routing-snapshot-publication",
			"manifest": py.jws("tr-routing-snapshot-v1", py.claims), "body": base64.StdEncoding.EncodeToString(py.snapshot),
			"refresh_at": time.Now().Unix() + 60})
		_, _ = w.Write(out)
	case r.Method == http.MethodGet && r.URL.Path == "/internal/fastpath/edge-policy":
		// Issued on this machine's clock, which the edge's runs ahead of
		// (edgeLead).
		now := time.Now().Unix()
		policy := map[string]json.RawMessage{}
		for k, v := range py.policy {
			policy[k] = v
		}
		for k, v := range map[string]int64{"issued_at": now, "refresh_at": now + 15, "not_after": now + 60} {
			policy[k] = json.RawMessage(strconv.FormatInt(v, 10))
		}
		body, _ := json.Marshal(policy)
		sum := sha256.Sum256(body)
		claims := map[string]any{"aud": edge.SnapshotAudience, "iss": edge.SnapshotIssuer, "issued_at": now,
			"not_after": now + 60, "plane": map[string]string{"cloud": "gcp", "region": "us-central1"},
			"sha256": hex.EncodeToString(sum[:]), "size": len(body), "v": 1}
		out := fmt.Sprintf(`{"body":%q,"kind":"tr-edge-policy-publication","manifest":%q,"v":1}`,
			base64.StdEncoding.EncodeToString(body), py.jws("tr-edge-policy-v1", claims))
		_, _ = w.Write([]byte(out))
	case r.Method == http.MethodPost:
		py.authorizes.Add(1)
		_, _ = w.Write([]byte(`{"data":{"idempotent_replay":true}}`))
	default:
		w.WriteHeader(http.StatusNotFound)
	}
}

// decideConfig is a process at ln with the decide path on, its edge at
// edgeLn before py.
func decideConfig(t *testing.T, py *signingPython, w decideWorld) (Config, chan *edge.Edge, string) {
	t.Helper()
	ln, edgeLn := listen(t), listen(t)
	cfg := config(ln)
	cfg.Auditor = false
	// The owner's first lease covers every hold a test makes, so no admit
	// meets a lease being topped up (owner.busy) after the warm-up.
	cfg.Owner.TopUps.Min = cfg.Owner.TopUps.Max
	cfg.Edge = EdgeConfig{Address: edgeLn.Addr().String(), Listener: edgeLn, Upstream: py.server.URL, Token: w.token,
		SnapshotURL: py.server.URL + "/internal/fastpath/routing-snapshot", SnapshotKid: snapshotKid,
		SnapshotPublicKey: base64.RawURLEncoding.EncodeToString(py.public), SnapshotPlane: "gcp/us-central1",
		PolicyURL: py.server.URL + "/internal/fastpath/edge-policy", Decide: true}
	made := make(chan *edge.Edge, 1)
	cfg.edgeMade = func(e *edge.Edge) { made <- e }
	cfg.edgeClock = func() time.Time { return time.Now().Add(edgeLead) }
	return cfg, made, edgeLn.Addr().String()
}

// edgeLead is how far the tests' edge clock runs ahead of this machine's,
// and so of the emulator's. A reservation's created_at is a
// commit-timestamp column (allow_commit_timestamp), and Spanner refuses a
// value in it that is past the commit's own timestamp: FailedPrecondition
// "Cannot write timestamps in the future", which the insert reads as
// Unknown (§8.4), and so a 503 that keeps its hold. The edge writes the
// commit's timestamp there, never its reading, so an edge whose clock runs
// ahead answers every request itself; one writing its reading would fail
// the warm-up.
const edgeLead = time.Second

// warmUp sends the world's authorize, under a fresh idempotency key each
// time, until the edge answers one itself; a request passes while the edge
// fills its caches and the owner takes its lease. An edge 503 (an insert
// whose outcome was not known keeps its hold) fails the test at once: the
// counts that follow are exact.
func warmUp(t *testing.T, w decideWorld, e *edge.Edge, edgeAddr string) {
	t.Helper()
	eventually(t, 60*time.Second, "the edge's own answer", func() (bool, error) {
		a := send(t, w.request(t, edgeAddr, storetest.UniqueID("warm")))
		if a.byEdge && a.status != http.StatusOK {
			return false, fmt.Errorf("the edge answered %d during the warm-up: %s", a.status, a.body)
		}
		return a.byEdge, nil
	})
	if c := e.DecideCounts(); c.Unavailable != 0 || c.Answered != 1 {
		t.Fatalf("after the warm-up: %+v", c)
	}
}

// answer is one response as the enclave read it.
type answer struct {
	status int
	header http.Header
	body   []byte
	byEdge bool // the edge's own 200 or 503, not Python's
}

func send(t *testing.T, req *http.Request) answer {
	t.Helper()
	resp, err := (&http.Client{Timeout: 20 * time.Second}).Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	return answer{status: resp.StatusCode, header: resp.Header, body: body,
		byEdge: !bytes.Contains(body, []byte(`"idempotent_replay":true`))}
}

// edgeIDs are the ids of an edge's 200.
func edgeIDs(t *testing.T, a answer) (authorization, reservation string) {
	t.Helper()
	var got struct {
		Data struct {
			AuthorizationID string `json:"authorization_id"`
			Reservation     string `json:"credit_reservation_id"`
			Replay          bool   `json:"idempotent_replay"`
		} `json:"data"`
	}
	if err := json.Unmarshal(a.body, &got); err != nil || got.Data.AuthorizationID == "" || got.Data.Replay {
		t.Fatalf("not the edge's 200: %d %s", a.status, a.body)
	}
	return got.Data.AuthorizationID, got.Data.Reservation
}

// scopeReservations counts the reservations of the world's key under an
// idempotency key's scope.
func scopeReservations(t *testing.T, w decideWorld, idempotencyKey string) int {
	t.Helper()
	sum := sha256.Sum256([]byte(idempotencyKey))
	scope := w.workspace + "#" + w.keyHash + "#" + hex.EncodeToString(sum[:])
	n := 0
	err := shared.Single().Query(context.Background(), spanner.Statement{
		SQL: `SELECT reservation_id FROM tr_reservation WHERE idempotency_scope = @scope`, Params: map[string]any{"scope": scope},
	}).Do(func(*spanner.Row) error { n++; return nil })
	if err != nil {
		t.Fatal(err)
	}
	return n
}

// TestTheEdgeDecidesEndToEnd: once the edge holds the snapshot, the policy,
// the decide copy and the key, an authorize in the cohort is answered by
// the edge: a 200 of the cohort's shape, Python never reached, the hold the
// node's owner made sealed in the row, and both rows in Spanner under the
// request's idempotency scope. The same key again is the scope taken: the
// hold refunded and the request passed to Python, the scope still one
// reservation. A new key sent twice at once is answered once by the edge
// and passed once, one reservation for it. The process then stops cleanly.
func TestTheEdgeDecidesEndToEnd(t *testing.T) {
	if emulator == nil {
		t.Skip(skipped)
	}
	w := seedDecideWorld(t)
	py := newSigningPython(t, w)
	cfg, made, edgeAddr := decideConfig(t, py, w)
	stop := start(t, cfg, Clients{Spanner: shared, PubSub: pubSub(t)})
	var e *edge.Edge
	select {
	case e = <-made:
	case <-time.After(20 * time.Second):
		t.Fatal("the edge was not made")
	}
	if !e.DecideOn() {
		t.Fatal("-edge-decide made no decide path")
	}
	warmUp(t, w, e, edgeAddr)
	busy := e.DecideCounts().Passed["owner.busy"]
	first := send(t, w.request(t, edgeAddr, "client-key-1"))
	if !first.byEdge || first.status != http.StatusOK {
		t.Fatalf("the first: %d %s, counts %+v", first.status, first.body, e.DecideCounts())
	}
	if first.header.Get("Content-Type") != "application/json" || first.header.Get("Content-Length") != strconv.Itoa(len(first.body)) {
		t.Errorf("the 200's headers: %v", first.header)
	}
	authorization, reservation := edgeIDs(t, first)
	s, err := store.New(shared, cfg.Store)
	if err != nil {
		t.Fatal(err)
	}
	rows, found, err := s.ReadAuthorizationForCompare(context.Background(), authorization)
	if err != nil || !found || rows.Reservation == nil {
		t.Fatalf("the edge's rows: %+v %v %v", rows, found, err)
	}
	var payload struct {
		Envelope string `json:"fastpath_envelope"`
		Key      string `json:"idempotency_key"`
	}
	if err := json.Unmarshal([]byte(rows.Authorization.Payload.StringVal), &payload); err != nil {
		t.Fatal(err)
	}
	env, err := frontdoor.Open(key, payload.Envelope)
	if err != nil || env.Auth == "" || payload.Key != "client-key-1" ||
		rows.Reservation.ReservationID != reservation || rows.Authorization.WorkspaceID != w.workspace ||
		rows.Reservation.IdempotencyFingerprint.StringVal != rows.Authorization.IdempotencyFingerprint.StringVal {
		t.Fatalf("the rows do not hold the owner's hold: %+v %v", payload, err)
	}
	if n := scopeReservations(t, w, "client-key-1"); n != 1 {
		t.Fatalf("%d reservations of the scope", n)
	}

	// The same key again: the scope is taken; refunded and passed.
	before := py.authorizes.Load()
	again := send(t, w.request(t, edgeAddr, "client-key-1"))
	if again.byEdge || py.authorizes.Load() != before+1 || scopeReservations(t, w, "client-key-1") != 1 {
		t.Fatalf("the same key again: %d %s, Python reached %d", again.status, again.body, py.authorizes.Load()-before)
	}
	eventually(t, 10*time.Second, "the refund", func() (bool, error) {
		c := e.DecideCounts()
		return c.Passed["insert.scope_taken"] == 1 && c.Refunded == 1, nil
	})

	// A new key twice at once: one answer by the edge, one pass.
	var wg sync.WaitGroup
	twice := make([]answer, 2)
	for i := range twice {
		wg.Add(1)
		go func() {
			defer wg.Done()
			twice[i] = send(t, w.request(t, edgeAddr, "client-key-2"))
		}()
	}
	wg.Wait()
	edgeAnswers := 0
	for _, a := range twice {
		if a.byEdge {
			edgeAnswers++
			if a.status != http.StatusOK {
				t.Errorf("the edge answered %d %s", a.status, a.body)
			}
		}
	}
	if edgeAnswers != 1 || scopeReservations(t, w, "client-key-2") != 1 {
		t.Fatalf("one key twice at once: %d answered by the edge, %d reservations", edgeAnswers,
			scopeReservations(t, w, "client-key-2"))
	}
	eventually(t, 10*time.Second, "the second refund", func() (bool, error) {
		c := e.DecideCounts()
		return c.Passed["insert.scope_taken"] == 2 && c.Refunded == 2 && c.RefundFailed == 0, nil
	})
	if c := e.DecideCounts(); c.Unavailable != 0 || c.Passed["owner.busy"] != busy || c.Answered != 3 {
		t.Fatalf("after the warm-up, every request but the scope's passes was the edge's 200: %+v", c)
	}
	stop()
}

// TestTheDecidePathNeedsTheSnapshotAndThePolicy: -edge-decide decides from
// both, so it is refused without either; off is the default.
func TestTheDecidePathNeedsTheSnapshotAndThePolicy(t *testing.T) {
	cfg := Defaults()
	if cfg.Edge.Decide {
		t.Fatal("the defaults run the decide path")
	}
	cfg.Admission, cfg.Address, cfg.Region, cfg.Key = true, "127.0.0.1:1", "us-central1", key
	cfg.SettleTopic, cfg.RecordTopic = "projects/spike/topics/settle-log", "projects/spike/topics/records"
	public := base64.RawURLEncoding.EncodeToString(make([]byte, ed25519.PublicKeySize))
	whole := EdgeConfig{Address: "127.0.0.1:2", Upstream: "http://python.internal", Token: "t",
		SnapshotURL: "http://python.internal/internal/fastpath/routing-snapshot",
		PolicyURL:   "http://python.internal/internal/fastpath/edge-policy", SnapshotPublicKey: public,
		SnapshotKid: "routing-snapshot-1", SnapshotPlane: "gcp/us-central1", Decide: true}
	cfg.Edge = whole
	if err := cfg.valid(); err != nil {
		t.Fatalf("a whole decide path: %v", err)
	}
	for name, change := range map[string]func(*EdgeConfig){
		"no snapshot": func(e *EdgeConfig) { e.SnapshotURL = "" },
		"no policy":   func(e *EdgeConfig) { e.PolicyURL = "" },
	} {
		cfg.Edge = whole
		change(&cfg.Edge)
		if err := cfg.valid(); err == nil {
			t.Errorf("%s: valid", name)
		}
	}
}

// TestAProcessDrainsItsInsertsBeforeItReturns (§8.4): an insert whose
// commit hangs past its deadline is answered Python's 503 at once, and its
// transaction lives on; a process told to stop then returns only once that
// transaction has ended, so the Spanner client its caller closes next is
// never closed under it (a session take still waiting would panic).
func TestAProcessDrainsItsInsertsBeforeItReturns(t *testing.T) {
	if emulator == nil {
		t.Skip(skipped)
	}
	const hang = 6 * time.Second
	var hanging atomic.Bool
	var hungEnded atomic.Int64
	interceptor := func(ctx context.Context, method string, req, reply any, cc *grpc.ClientConn,
		invoker grpc.UnaryInvoker, opts ...grpc.CallOption) error {
		if c, ok := req.(*spannerpb.CommitRequest); ok && hanging.Load() &&
			c.GetRequestOptions().GetTransactionTag() == store.LeasedEdgeTag {
			time.Sleep(hang)
			hungEnded.Store(time.Now().UnixNano())
			return status.Error(codes.Unavailable, "hung")
		}
		return invoker(ctx, method, req, reply, cc, opts...)
	}
	client, err := emulator.Client(context.Background(), "spike",
		option.WithGRPCDialOption(grpc.WithUnaryInterceptor(interceptor)))
	if err != nil {
		t.Fatal(err)
	}
	w := seedDecideWorld(t)
	py := newSigningPython(t, w)
	cfg, made, edgeAddr := decideConfig(t, py, w)
	cfg.HandOff = 0
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- Run(ctx, cfg, Clients{Spanner: client, PubSub: pubSub(t)}) }()
	var e *edge.Edge
	select {
	case e = <-made:
	case <-time.After(20 * time.Second):
		t.Fatal("the edge was not made")
	}
	// Warm: the edge answers one itself before the commit hangs.
	warmUp(t, w, e, edgeAddr)
	hanging.Store(true)
	sent := time.Now()
	a := send(t, w.request(t, edgeAddr, "client-key-hang"))
	if c := e.DecideCounts(); c.Unavailable != 1 {
		t.Errorf("counts after the hung commit: %+v", c)
	}
	if !a.byEdge || a.status != http.StatusServiceUnavailable || a.header.Get("Retry-After") != "1" ||
		!bytes.HasPrefix(a.body, []byte(`{"error":{"code":503,"message":"Authorization unavailable; retry",`)) ||
		time.Since(sent) > 3*time.Second {
		t.Fatalf("the hung commit's answer after %v: %d %v %s", time.Since(sent), a.status, a.header, a.body)
	}
	cancel()
	var runErr error
	select {
	case runErr = <-done:
	case <-time.After(60 * time.Second):
		t.Fatal("the process did not stop")
	}
	returned := time.Now()
	if runErr != nil {
		t.Fatalf("the process ended with %v", runErr)
	}
	if ended := hungEnded.Load(); ended == 0 || returned.UnixNano() < ended {
		t.Fatalf("Run returned before the hung commit ended (ended %d, returned %d)", ended, returned.UnixNano())
	}
	client.Close()
	if n := scopeReservations(t, w, "client-key-hang"); n != 0 {
		t.Fatalf("%d reservations of a commit that never reached Spanner", n)
	}
}
