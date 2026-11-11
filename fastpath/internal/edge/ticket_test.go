package edge

import (
	"context"
	"crypto/ed25519"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/frontdoor"
	"github.com/Lore-Hex/quill-router/fastpath/internal/gwauth"
	"github.com/Lore-Hex/quill-router/fastpath/internal/routingsnapshot"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

type ticketTransport func(*http.Request) (*http.Response, error)

func (f ticketTransport) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }
func ticketResponse(status int, body string) *http.Response {
	return &http.Response{StatusCode: status, Header: make(http.Header), Body: io.NopCloser(strings.NewReader(body))}
}

// ticketEdge holds the same golden world as newDecideEdge, with all HTTP
// calls served by a transport so no listener is needed.
func ticketEdge(t *testing.T, f authorizeFile, c authorizeCase, transport http.RoundTripper) *decideEdge {
	t.Helper()
	h := &decideEdge{holder: &LateAdmitter{}, logs: &logLines{}, policy: policyOf(t, f, c),
		rows: &fakeInserter{insert: func(context.Context) store.InsertOutcome { return store.Inserted }},
		admitter: &fakeAdmitter{authorize: func(context.Context, frontdoor.AuthorizeOf) frontdoor.Authorized {
			return frontdoor.Authorized{Status: frontdoor.Admitted, Envelope: c.Admitted.Envelope, EndOfLife: endOfLifeOf(t, c)}
		}}}
	h.cache = &recordingCache{KeyCache: warmCache(t, &fakeWorld{c: c}, lookupHashOf(c.body()), c.at())}
	upstream, _ := url.Parse("https://python.invalid")
	snapshotURL, _ := url.Parse("https://python.invalid/internal/fastpath/routing-snapshot")
	policyURL, _ := url.Parse("https://python.invalid/internal/fastpath/edge-policy")
	keys := []SnapshotKey{{Kid: "routing-snapshot-1", PublicKey: make(ed25519.PublicKey, ed25519.PublicKeySize)}}
	e, err := New(Config{Token: f.InternalToken, Upstream: upstream, Boots: bootsOf(t, f, c), Clock: func() time.Time { return c.at() },
		Transport: ticketTransport(func(r *http.Request) (*http.Response, error) {
			if r.URL.Path != ticketPath {
				return ticketResponse(418, `{"python":true}`), nil
			}
			return transport.RoundTrip(r)
		}), Logf: h.logs.logf, Admitter: h.holder,
		Snapshot: SnapshotConfig{URL: snapshotURL, Keys: keys, Plane: "gcp/us-central1"},
		Policy:   PolicyConfig{URL: policyURL, Keys: keys, Plane: "gcp/us-central1"},
		Decide:   &DecideConfig{Cache: h.cache, Decides: &fakeDecides{}, Rows: h.rows, Mint: pinnedMint(c)}})
	if err != nil {
		t.Fatal(err)
	}
	snap := snapshotOver(t, f, h.policy)
	e.poller.current.Store(hold(snap, routingsnapshot.Claims{CompiledAt: snap.CompiledAt, NotAfter: snap.NotAfter}))
	e.policy.current.Store(&heldPolicy{policy: h.policy, claims: routingsnapshot.PolicyClaims{IssuedAt: h.policy.IssuedAt, NotAfter: h.policy.NotAfter}})
	view := decideAll(h.policy, c.Facts.Key.WorkspaceID, c.at())
	e.decider.decides.current.Store(&view)
	h.holder.Bind(h.admitter)
	h.edge = e
	t.Cleanup(e.WaitDecide)
	return h
}

func enableTickets(t *testing.T, f authorizeFile, c authorizeCase) authorizeCase {
	return withAsyncSettle(t, f, c, func(a *routingsnapshot.AsyncSettle) {
		yes := true
		a.AdmissionEnabled = true
		a.AdmissionExcludedWorkspaces = nil
		a.EdgeTickets = &yes
	})
}

func ticketAnswerOf(t *testing.T, raw string) string {
	t.Helper()
	var body struct {
		Data map[string]json.RawMessage `json:"data"`
	}
	if err := json.Unmarshal([]byte(raw), &body); err != nil {
		t.Fatal(err)
	}
	out := map[string]json.RawMessage{}
	for _, k := range []string{"async_eligible", "settlement_ticket", "settlement_status_url"} {
		out[k] = body.Data[k]
	}
	b, err := json.Marshal(out)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

func goldenTicketTransport(t *testing.T, c authorizeCase) http.RoundTripper {
	return ticketTransport(func(r *http.Request) (*http.Response, error) {
		if r.URL.Path != ticketPath {
			return ticketResponse(418, `{"python":true}`), nil
		}
		return ticketResponse(200, ticketAnswerOf(t, c.Expect.Response)), nil
	})
}

func TestEdgeTicketRequestAndConcurrentCommit(t *testing.T) {
	f := asyncAuthorizeGoldens(t)
	for _, name := range []string{"answer:async.B:invocation_nonce", "answer:async.B:stream:route_type:responses"} {
		base := asyncCaseNamed(t, name)
		c := enableTickets(t, f, base)
		expected := string(ticketed(t, base.Expect.Response))
		if strings.Contains(name, ":stream:") {
			expected = strings.Replace(expected, `"async_eligible":false`, `"async_eligible":true`, 1)
		}
		var data struct {
			Data map[string]any `json:"data"`
		}
		if err := json.Unmarshal([]byte(expected), &data); err != nil {
			t.Fatal(err)
		}
		var body map[string]any
		if err := json.Unmarshal(c.body(), &body); err != nil {
			t.Fatal(err)
		}
		ids := []any{}
		for _, candidate := range data.Data["billing_snapshot"].(map[string]any)["candidates"].([]any) {
			ids = append(ids, candidate.(map[string]any)["endpoint_id"])
		}
		nonce, _ := body["invocation_nonce"].(string)
		want := map[string]any{"v": float64(1), "authorization_id": data.Data["authorization_id"],
			"workspace_id": data.Data["workspace_id"], "key_hash": data.Data["api_key_hash"],
			"reservation_id": data.Data["credit_reservation_id"], "invocation_nonce": nonce,
			"route_type": body["route_type"], "streamed": body["stream"], "region": data.Data["region"],
			"candidate_endpoint_ids": ids, "priced_at": float64(c.at().Unix()), "billing_snapshot_hash": data.Data["billing_snapshot_hash"]}
		for _, outcome := range []store.InsertOutcome{store.Inserted, store.Landed} {
			t.Run(name+"/"+outcome.String(), func(t *testing.T) {
				arrived, release, inserted := make(chan struct{}), make(chan struct{}), make(chan struct{})
				tr := ticketTransport(func(r *http.Request) (*http.Response, error) {
					if r.Method != "POST" || r.URL.Path != ticketPath || r.URL.Scheme != "https" || r.URL.Host != "python.invalid" ||
						r.Header.Get(TokenHeader) != f.InternalToken || r.Header.Get("Content-Type") != "application/json" {
						t.Error("ticket request metadata differs")
					}
					var got map[string]any
					if err := json.NewDecoder(r.Body).Decode(&got); err != nil {
						t.Error(err)
					}
					if !reflect.DeepEqual(got, want) {
						t.Errorf("ticket request members differ: got %v want %v", got, want)
					}
					close(arrived)
					return ticketResponse(200, ticketAnswerOf(t, expected)), nil
				})
				h := ticketEdge(t, f, c, tr)
				h.rows.insert = func(context.Context) store.InsertOutcome { close(inserted); <-release; return outcome }
				done := make(chan *httptest.ResponseRecorder, 1)
				go func() { done <- h.send(t, c) }()
				select {
				case <-inserted:
				case <-time.After(time.Second):
					t.Fatal("insert not entered")
				}
				select {
				case <-arrived:
				case <-time.After(time.Second):
					close(release)
					t.Fatal("ticket did not start while commit was blocked")
				}
				select {
				case <-done:
					t.Fatal("answered before commit")
				default:
				}
				close(release)
				rec := <-done
				if rec.Code != 200 || withGoldenTiming(rec.Body.Bytes()) != expected {
					t.Fatalf("ticketed response differs: %d %s", rec.Code, rec.Body.String())
				}
				counts := h.edge.DecideCounts().Tickets
				if counts["asked"] != 1 || counts["spliced"] != 1 || counts["discarded"] != 0 {
					t.Fatalf("ticket counts %v", counts)
				}
				if !rowsAreGoldens(t, base, h.rows.inserted()[0]) {
					t.Fatal("ticket changed stored rows")
				}
			})
		}
	}
}

func TestEdgeTicketFallbacks(t *testing.T) {
	f := asyncAuthorizeGoldens(t)
	base := asyncCaseNamed(t, "answer:async.B:invocation_nonce")
	c := enableTickets(t, f, base)
	good := ticketAnswerOf(t, string(ticketed(t, base.Expect.Response)))
	changed := func(ticket string) string {
		var fields map[string]any
		if err := json.Unmarshal([]byte(good), &fields); err != nil {
			t.Fatal(err)
		}
		fields["settlement_ticket"] = ticket
		raw, err := json.Marshal(fields)
		if err != nil {
			t.Fatal(err)
		}
		return string(raw)
	}
	for _, v := range []struct {
		name, body, stage string
		status            int
		err               error
	}{
		{name: "declined", body: `{"async_eligible":false}`, status: 200},
		{name: "transport", err: errors.New("unavailable"), stage: "transport"},
		{name: "status", body: good, status: 503, stage: "status"},
		{name: "oversized", body: strings.Repeat(" ", maxTicketBody) + good, status: 200, stage: "size"},
		{name: "duplicate", body: strings.Replace(good, `"async_eligible":false`, `"async_eligible":false,"async_eligible":false`, 1), status: 200, stage: "parse"},
		{name: "extra", body: strings.Replace(good, "{", `{"extra":0,`, 1), status: 200, stage: "parse"},
		{name: "non_boolean", body: strings.Replace(good, `"async_eligible":false`, `"async_eligible":0`, 1), status: 200, stage: "parse"},
		{name: "empty", body: changed(""), status: 200, stage: "parse"},
		{name: "segments", body: changed("YQ.Yg"), status: 200, stage: "parse"},
		{name: "url", body: strings.Replace(good, ".settle", ".other", 1), status: 200, stage: "parse"},
		{name: "true_only", body: `{"async_eligible":true}`, status: 200, stage: "parse"},
		{name: "trailing", body: good + "{}", status: 200, stage: "parse"},
		{name: "array", body: "[" + good + "]", status: 200, stage: "parse"},
	} {
		t.Run(v.name, func(t *testing.T) {
			h := ticketEdge(t, f, c, ticketTransport(func(*http.Request) (*http.Response, error) {
				if v.err != nil {
					return nil, v.err
				}
				return ticketResponse(v.status, v.body), nil
			}))
			rec := h.send(t, c)
			if rec.Code != 200 || withGoldenTiming(rec.Body.Bytes()) != base.Expect.Response {
				t.Fatal("fallback changed shape B bytes")
			}
			n := h.edge.DecideCounts().Tickets
			if n["asked"] != 1 || n["spliced"] != 0 || n["late"] != 0 || n["discarded"] != 0 {
				t.Fatalf("counts %v", n)
			}
			if v.stage == "" {
				if n["declined"] != 1 || n["failed"] != 0 {
					t.Fatal(n)
				}
			} else if n["failed"] != 1 || n["failed."+v.stage] != 1 || n["declined"] != 0 {
				t.Fatal(n)
			}
		})
	}
}

func TestEdgeTicketLateCancels(t *testing.T) {
	f := asyncAuthorizeGoldens(t)
	base := asyncCaseNamed(t, "answer:async.B:invocation_nonce")
	c := enableTickets(t, f, base)
	stopped := make(chan struct{})
	h := ticketEdge(t, f, c, ticketTransport(func(r *http.Request) (*http.Response, error) {
		<-r.Context().Done()
		close(stopped)
		return nil, r.Context().Err()
	}))
	h.edge.cfg.AsyncTicketTimeout = time.Second
	h.edge.decider.ticketGrace = 5 * time.Millisecond
	rec := h.send(t, c)
	if rec.Code != 200 || withGoldenTiming(rec.Body.Bytes()) != base.Expect.Response {
		t.Fatal("late fallback changed shape B")
	}
	select {
	case <-stopped:
	case <-time.After(200 * time.Millisecond):
		t.Fatal("ticket context not cancelled on return")
	}
	if n := h.edge.DecideCounts().Tickets; n["late"] != 1 || n["spliced"] != 0 || n["discarded"] != 0 || n["failed"] != 0 || n["declined"] != 0 {
		t.Fatal(n)
	}
}

func TestEdgeTicketDiscardedWithoutDurability(t *testing.T) {
	f := asyncAuthorizeGoldens(t)
	base := asyncCaseNamed(t, "answer:async.B:invocation_nonce")
	c := enableTickets(t, f, base)
	good := ticketAnswerOf(t, string(ticketed(t, base.Expect.Response)))
	for _, v := range []struct {
		name    string
		outcome store.InsertOutcome
		refuse  bool
		status  int
		pass    string
	}{
		{"unknown", store.Unknown, false, 503, ""}, {"scope", store.ScopeTaken, false, 418, PassInsertScopeTaken},
		{"failed", store.Failed, false, 418, PassInsertFailed}, {"admit", store.Inserted, true, 418, PassOwnerBusy},
	} {
		t.Run(v.name, func(t *testing.T) {
			arrived := make(chan struct{})
			h := ticketEdge(t, f, c, ticketTransport(func(r *http.Request) (*http.Response, error) {
				if r.URL.Path != ticketPath {
					return ticketResponse(418, `{"python":true}`), nil
				}
				close(arrived)
				return ticketResponse(200, good), nil
			}))
			h.rows.insert = func(context.Context) store.InsertOutcome { <-arrived; return v.outcome }
			if v.refuse {
				h.admitter.authorize = func(context.Context, frontdoor.AuthorizeOf) frontdoor.Authorized {
					<-arrived
					return frontdoor.Authorized{Status: frontdoor.Busy}
				}
			}
			rec := h.send(t, c)
			if rec.Code != v.status || strings.Contains(rec.Body.String(), "settlement_ticket") {
				t.Fatalf("response %d %s", rec.Code, rec.Body.String())
			}
			if v.status == 503 && withGoldenTiming(rec.Body.Bytes()) != string(gwauth.Unavailable(gwauth.Timing{})) {
				t.Fatal("unknown response changed")
			}
			n := h.edge.DecideCounts()
			if n.Tickets["discarded"] != 1 || n.Tickets["spliced"] != 0 || n.Tickets["asked"] != 1 || n.Tickets["failed"] != 0 || n.Tickets["declined"] != 0 {
				t.Fatal(n.Tickets)
			}
			if v.pass != "" && n.Passed[v.pass] != 1 {
				t.Fatal(n.Passed)
			}
		})
	}
}

func TestEdgeTicketSelection(t *testing.T) {
	f := asyncAuthorizeGoldens(t)
	base := asyncCaseNamed(t, "answer:async.B:invocation_nonce")
	enabled := enableTickets(t, f, base)
	left := withAsyncSettle(t, f, enabled, func(a *routingsnapshot.AsyncSettle) {
		ids := []string{"a_" + base.Facts.Key.WorkspaceID}
		a.EdgeTicketWorkspaces = &ids
	})
	excluded := withAsyncSettle(t, f, enabled, func(a *routingsnapshot.AsyncSettle) {
		ids := []string{base.Facts.Key.WorkspaceID}
		a.AdmissionExcludedWorkspaces = &ids
	})
	shapeA := enableTickets(t, f, asyncCaseNamed(t, "answer:async.A:ticket_authority_false"))
	for _, v := range []struct {
		name string
		c    authorizeCase
		pass string
	}{
		{"list", left, PassAsyncAdmission}, {"excluded", excluded, ""}, {"shape_A", shapeA, ""}, {"absent", base, ""},
	} {
		t.Run(v.name, func(t *testing.T) {
			var asked atomic.Int64
			h := ticketEdge(t, f, v.c, ticketTransport(func(r *http.Request) (*http.Response, error) {
				if r.URL.Path == ticketPath {
					asked.Add(1)
				}
				return ticketResponse(418, `{"python":true}`), nil
			}))
			rec := h.send(t, v.c)
			if asked.Load() != 0 || len(h.edge.DecideCounts().Tickets) != 0 {
				t.Fatal("unexpected ticket call")
			}
			if v.pass != "" {
				if rec.Code != 418 || h.edge.DecideCounts().Passed[v.pass] != 1 {
					t.Fatal("covered workspace did not pass")
				}
				in, _ := caseInputs(t, f, enabled)
				d := Decide(in).ForShadow()
				d.Input.Async = asyncComposeInput(d.Input.Async.Request, *h.policy.AsyncSettle, d.Workspace)
				if _, err := gwauth.Stage(d.Input); err == nil {
					t.Fatal("compose did not retain admission refusal")
				}
			} else if rec.Code != 200 || withGoldenTiming(rec.Body.Bytes()) != v.c.Expect.Response {
				t.Fatal("non-ticket response differs")
			}
		})
	}
}

func TestEdgeTicketCountersAndTimers(t *testing.T) {
	f := asyncAuthorizeGoldens(t)
	base := asyncCaseNamed(t, "answer:async.B:invocation_nonce")
	c := enableTickets(t, f, base)
	arrived, committed := make(chan struct{}), make(chan struct{})
	h := ticketEdge(t, f, c, ticketTransport(func(*http.Request) (*http.Response, error) {
		close(arrived)
		<-committed
		time.Sleep(12 * time.Millisecond)
		return ticketResponse(200, ticketAnswerOf(t, string(ticketed(t, base.Expect.Response)))), nil
	}))
	h.rows.insert = func(context.Context) store.InsertOutcome {
		<-arrived
		time.Sleep(4 * time.Millisecond)
		close(committed)
		return store.Inserted
	}
	if rec := h.send(t, c); rec.Code != 200 {
		t.Fatal(rec.Code)
	}
	h.edge.decider.flush()
	var line CountsLine
	for _, l := range strings.Split(h.logs.all(), "\n") {
		parsed, ok, err := ParseCountsLine(l)
		if err != nil {
			t.Fatal(err)
		}
		if ok && parsed.Of == CountsDecide {
			line = parsed
		}
	}
	if line.ByName["tickets"]["asked"] != 1 || line.ByName["tickets"]["spliced"] != 1 {
		t.Fatal(line)
	}
	for _, kind := range []string{"ticket_call", "ticket_wait", "insert_call"} {
		sum, max := line.Counts[kind+"_ms_sum"], line.Counts[kind+"_ms_max"]
		if sum < 1 || sum != max {
			t.Fatalf("%s timer: sum %d max %d", kind, sum, max)
		}
	}
}

func TestEdgeTicketClientBoundsAndOrigin(t *testing.T) {
	upstream, _ := url.Parse("https://python.invalid")
	for _, raw := range []string{"http://python.invalid" + ticketPath, "https://other.invalid" + ticketPath,
		"https://user@python.invalid" + ticketPath, "https://python.invalid/other",
		"https://python.invalid" + ticketPath + "?x=1"} {
		u, _ := url.Parse(raw)
		if _, err := newTicketClient(u, upstream, "token", nil); err == nil {
			t.Errorf("accepted route %s", raw)
		}
	}
	u, _ := url.Parse("https://python.invalid" + ticketPath)
	var calls atomic.Int64
	client, err := newTicketClient(u, upstream, "token", ticketTransport(func(*http.Request) (*http.Response, error) {
		calls.Add(1)
		resp := ticketResponse(302, "")
		resp.Header.Set("Location", "https://other.invalid")
		return resp, nil
	}))
	if err != nil {
		t.Fatal(err)
	}
	if _, stage := client.fetch(context.Background(), ticketRequest{}); stage != "status" || calls.Load() != 1 {
		t.Fatal("redirect was followed")
	}
	// Each malformed ticket otherwise has a valid object, boolean and URL.
	for _, value := range []string{"", "a.b", "a.b.c.d", "a.b.c=", "a.b. ", "YQ.YQ.Y", strings.Repeat("a", 4093) + ".b.c"} {
		b, _ := json.Marshal(map[string]any{"async_eligible": true, "settlement_ticket": value, "settlement_status_url": "/v1/settlements/id.settle"})
		if _, stage := parseTicket(b, "/v1/settlements/id.settle"); stage != "parse" {
			t.Errorf("accepted malformed compact ticket of length %d", len(value))
		}
	}
	f := asyncAuthorizeGoldens(t)
	c := enableTickets(t, f, asyncCaseNamed(t, "answer:async.B:invocation_nonce"))
	in, _ := caseInputs(t, f, c)
	d := Decide(in).ForShadow()
	// The normal Stage id check is stricter, but the request builder also
	// holds the route's safe-id rule if handed a decision of another origin.
	stagedInput := d.Staged.Input()
	stagedInput.AuthorizationID = "gwa/other"
	d.Input = stagedInput
	if _, ok := ticketRequestOf(d); ok {
		t.Fatal("unsafe authorization id accepted")
	}
}

func TestEdgeTicketWaitRespectsDecideBudget(t *testing.T) {
	dp := newDecider(DecideConfig{}, func(string, ...any) {}, time.Now, time.Minute)
	dp.ticketGrace = time.Second
	call := &ticketCall{result: make(chan ticketResult, 1)}
	started := time.Now()
	if got := dp.awaitTicket(call, started, started.Add(5*time.Millisecond)); got != nil {
		t.Fatal("unexpected ticket")
	}
	if time.Since(started) > 200*time.Millisecond {
		t.Fatal("wait exceeded decide budget")
	}
	if dp.total.tickets["late"] != 1 {
		t.Fatal("wait not counted late")
	}
}

// A result that is already waiting when the commit returns is still refused
// when the call finished after the wait's deadline: the grace after the
// commit, or the decide budget when that comes first.
func TestEdgeTicketFinishedAfterTheDeadlineIsLate(t *testing.T) {
	ticket := &gwauth.Ticket{Eligible: true, SettlementTicket: "a.b.c", StatusURL: "/v1/settlements/gwa.settle"}
	committed := time.Now().Add(-time.Second)
	for _, c := range []struct {
		name            string
		finished, limit time.Duration // after the commit
		late            bool
	}{
		{"inside the grace", 9 * time.Millisecond, time.Minute, false},
		{"at the grace", 10 * time.Millisecond, time.Minute, false},
		{"after the grace", 11 * time.Millisecond, time.Minute, true},
		{"inside the grace, after the budget", 6 * time.Millisecond, 5 * time.Millisecond, true},
	} {
		dp := newDecider(DecideConfig{}, func(string, ...any) {}, time.Now, time.Minute)
		dp.ticketGrace = 10 * time.Millisecond
		call := &ticketCall{result: make(chan ticketResult, 1)}
		call.result <- ticketResult{ticket: ticket, finished: committed.Add(c.finished)}
		got := dp.awaitTicket(call, committed, committed.Add(c.limit))
		if c.late != (got == nil) || (!c.late && got != ticket) {
			t.Fatalf("%s: ticket %v", c.name, got)
		}
		want := int64(0)
		if c.late {
			want = 1
		}
		if n := dp.total.tickets; n["late"] != want || n["failed"] != 0 || n["declined"] != 0 {
			t.Fatalf("%s: counts %v", c.name, n)
		}
	}
}

func TestEdgeTicketDetachedTimeout(t *testing.T) {
	f := asyncAuthorizeGoldens(t)
	base := asyncCaseNamed(t, "answer:async.B:invocation_nonce")
	c := enableTickets(t, f, base)
	arrived := make(chan context.Context, 1)
	stopped := make(chan struct{})
	h := ticketEdge(t, f, c, ticketTransport(func(r *http.Request) (*http.Response, error) {
		arrived <- r.Context()
		<-r.Context().Done()
		if r.Context().Err() != context.DeadlineExceeded {
			t.Error("ticket inherited client cancellation")
		}
		close(stopped)
		return nil, r.Context().Err()
	}))
	h.edge.cfg.AsyncTicketTimeout = 15 * time.Millisecond
	requestCtx, cancel := context.WithCancel(context.Background())
	defer cancel()
	h.rows.insert = func(context.Context) store.InsertOutcome {
		ctx := <-arrived
		deadline, ok := ctx.Deadline()
		if !ok || time.Until(deadline) > 15*time.Millisecond {
			t.Error("ticket timeout not configured")
		}
		cancel()
		if ctx.Err() != nil {
			t.Error("ticket context was not detached")
		}
		<-stopped
		return store.Inserted
	}
	rec := httptest.NewRecorder()
	h.edge.ServeHTTP(rec, request(c, requestCtx))
	if rec.Code != 200 || withGoldenTiming(rec.Body.Bytes()) != base.Expect.Response {
		t.Fatal("timed out ticket changed shape B")
	}
	n := h.edge.DecideCounts().Tickets
	if n["failed.transport"] != 1 || n["spliced"] != 0 || n["late"] != 0 {
		t.Fatal(n)
	}
}
