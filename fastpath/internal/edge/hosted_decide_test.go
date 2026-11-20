package edge

import (
	"context"
	"encoding/json"
	"errors"
	"go/ast"
	"go/parser"
	gotoken "go/token"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/gwstate"
	"github.com/Lore-Hex/quill-router/fastpath/internal/routingsnapshot"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

func hostedGoldens(t *testing.T) authorizeFile {
	t.Helper()
	raw, err := readTestdata("authorize_hosted_decide_v1.json")
	if err != nil {
		t.Fatal(err)
	}
	var f authorizeFile
	if err := json.Unmarshal(raw, &f); err != nil {
		t.Fatal(err)
	}
	return f
}

func hostedCase(t *testing.T, f authorizeFile, name string) authorizeCase {
	t.Helper()
	for _, c := range f.Cases {
		if c.Name == name {
			return c
		}
	}
	t.Fatal("missing hosted case")
	return authorizeCase{}
}

// This test needs no sockets: every answered golden traverses the real
// cohort, evaluator, compose and row builder, including ticket composition.
func TestHostedDecidePipeline(t *testing.T) {
	f := hostedGoldens(t)
	for _, c := range f.Cases {
		t.Run(c.Name, func(t *testing.T) {
			in, _ := caseInputs(t, f, c)
			out := Decide(in)
			d := out.ForShadow()
			if c.Expect.Outcome == "pass" {
				want := c.Expect.Reason
				if postAdmission(want) {
					want = ""
				}
				if out.Pass != want {
					t.Fatalf("pass=%s want=%s", out.Pass, want)
				}
				return
			}
			if out.Pass != "" || d == nil {
				t.Fatalf("passed %s", out.Pass)
			}
			composed, err := d.Finish(c.Admitted.Envelope, endOfLifeOf(t, c))
			if err != nil {
				t.Fatal(err)
			}
			if c.Expect.AsyncShape == "C" {
				request, ok := ticketRequestOf(d)
				if !ok || request.RouteType != "decide" || request.Streamed {
					t.Fatal("ticket request facts differ")
				}
				ticket, stage := parseTicket([]byte(ticketAnswerOf(t, c.Expect.Response)), ticketStatusURL(d.Input.AuthorizationID))
				if ticket == nil || stage != "" {
					t.Fatal("golden ticket not parsed")
				}
				composed, err = composed.WithTicket(*ticket)
				if err != nil {
					t.Fatal(err)
				}
			}
			if string(composed.Render(c.Expect.timing(), false)) != c.Expect.Response {
				t.Fatal("answer differs from Python")
			}
			if d.Estimate != c.Expect.Estimate || d.Reservation != c.Expect.Reservation {
				t.Fatal("hold differs")
			}
			rowsAreGoldens(t, c, composed)
		})
	}
}

func TestHostedDecideDarkShadow(t *testing.T) {
	f := hostedGoldens(t)
	raw, err := readTestdata("edge_policy_hosted_decide_v1.json")
	if err != nil {
		t.Fatal(err)
	}
	var vectors struct {
		Cases []struct {
			Name, Body, Mode string
			Accept           bool
			PythonDecide     bool `json:"python_decide"`
		}
	}
	if json.Unmarshal(raw, &vectors) != nil {
		t.Fatal("policy vectors unreadable")
	}
	for _, v := range vectors.Cases {
		if !v.Accept {
			continue
		}
		published, err := routingsnapshot.ParseEdgePolicy([]byte(v.Body))
		if err != nil {
			t.Fatal("published policy unreadable")
		}
		names := []string{"answer:hosted:none", "answer:hosted:A:route_off"}
		if v.PythonDecide {
			names = []string{"answer:hosted:none", "answer:hosted:B", "answer:hosted:C:eligible"}
		}
		for _, name := range names {
			t.Run(v.Name+"/"+name, func(t *testing.T) {
				c := hostedCase(t, f, name)
				in, _ := caseInputs(t, f, c)
				// Hold the case's cohort and ticket admission settings fixed, using
				// both optional members exactly as the Python publisher emitted them.
				p, async := *in.Policy, *in.Policy.AsyncSettle
				p.FastpathEdgeHostedDecide = published.FastpathEdgeHostedDecide
				async.DecideRoute = published.AsyncSettle.DecideRoute
				p.AsyncSettle = &async
				in.Policy = &p
				out := Decide(in)
				unknown := name != "answer:hosted:none" && !p.HostedDecide() && !p.AsyncSettle.SupportsDecide()
				wantPass := ""
				if unknown {
					wantPass = PassHostedDecideAsyncUnknown
				} else if v.Mode != "on" {
					wantPass = PassHostedDecideOff
				}
				if out.Pass != wantPass || (out.ForShadow() == nil) != unknown {
					t.Fatalf("pass=%s decision=%v", out.Pass, out.ForShadow() != nil)
				}
				if _, ok := out.Admit(); ok != (v.Mode == "on") {
					t.Fatal("admission differs")
				}
				py := pyAnswer{status: 200, body: []byte(c.Expect.Response)}
				result := (&fullShadow{}).safeCompare(out, py, nil, out.Pass)
				if unknown {
					// The base binary passed this arm without a decision. Keep its
					// class family; the suffix now identifies the unpublished async fact.
					baseRequest, _, why := requestOf(c.body(), &shadowCall{})
					if why != "" {
						t.Fatal(why)
					}
					baseRequest.At, baseRequest.HostedDecide = c.at(), false
					if in.Snapshot.Evaluate(baseRequest).Outcome != routingsnapshot.PassToPython {
						t.Fatal("base evaluator did not pass hosted decide")
					}
					base := (&fullShadow{}).safeCompare(Decided{Pass: PassEvaluate}, py, nil, PassEvaluate)
					if strings.Split(result.class, ".")[0] != strings.Split(base.class, ".")[0] ||
						result.class != ClassPythonAnsweredGoPassed+"."+PassHostedDecideAsyncUnknown || len(result.fields) != 0 {
						t.Fatalf("shadow=%s fields=%v", result.class, result.fields)
					}
				} else if result.class != ClassAgreeFull || len(result.fields) != 0 {
					t.Fatalf("shadow=%s fields=%v", result.class, result.fields)
				}
			})
		}
	}
}

func TestHostedDecideCohortGates(t *testing.T) {
	f := hostedGoldens(t)
	c := hostedCase(t, f, "answer:hosted:B")
	base, _ := caseInputs(t, f, c)
	for _, test := range []struct {
		name, reason string
		change       func(*gwstate.Read)
	}{
		{"BYOK policy", PassCandidatesBYOK, func(r *gwstate.Read) { r.Facts.BYOKProviders = []string{"openai"} }},
		{"credit stale", PassCacheWorkspace, func(r *gwstate.Read) { r.Credit = gwstate.Stale }},
		{"expired key", PassKeyExpired, func(r *gwstate.Read) { r.Facts.Key.ExpiresAt = base.Request.At }},
	} {
		t.Run(test.name, func(t *testing.T) {
			if out := Decide(withRead(t, base, test.change)); out.Pass != test.reason {
				t.Fatalf("pass=%s", out.Pass)
			}
		})
	}
	for _, test := range []struct {
		name, reason string
		change       func(*routingsnapshot.EdgePolicy)
	}{
		{"pilot", PassNotPilotWorkspace, func(p *routingsnapshot.EdgePolicy) { p.FastpathPilotWorkspaces = []string{"ws_other"} }},
		{"region", PassPilotRegion, func(p *routingsnapshot.EdgePolicy) { p.FastpathPilotRegions = []string{"other-region"} }},
		{"records", PassRecordsNotDurable, func(p *routingsnapshot.EdgePolicy) { p.RecordsDurable = false }},
	} {
		t.Run(test.name, func(t *testing.T) {
			in := base
			p := *in.Policy
			test.change(&p)
			in.Policy = &p
			if out := Decide(in); out.Pass != test.reason {
				t.Fatalf("pass=%s", out.Pass)
			}
		})
	}
}

// These HTTP path tests use the existing transport harness without listeners.
func TestHostedDecideAnswerAndDormancy(t *testing.T) {
	f := hostedGoldens(t)
	c := hostedCase(t, f, "answer:hosted:C:eligible")
	for _, enabled := range []bool{false, true} {
		v := c
		if !enabled {
			v = withHostedMember(t, f, c, nil)
		}
		transport := ticketTransport(func(r *http.Request) (*http.Response, error) {
			if r.URL.Path != ticketPath {
				return ticketResponse(418, `{"python":true}`), nil
			}
			var request ticketRequest
			if json.NewDecoder(r.Body).Decode(&request) != nil || request.RouteType != "decide" || request.Streamed {
				t.Error("ticket request differs")
			}
			return ticketResponse(200, ticketAnswerOf(t, c.Expect.Response)), nil
		})
		h := ticketEdge(t, f, v, transport)
		rec := h.send(t, v)
		if enabled {
			if rec.Code != 200 || withGoldenTiming(rec.Body.Bytes()) != c.Expect.Response || len(h.rows.inserted()) != 1 {
				t.Fatal("hosted answer or rows differ")
			}
			rowsAreGoldens(t, c, h.rows.inserted()[0])
		} else if rec.Code != 418 || len(h.rows.inserted()) != 0 || h.edge.DecideCounts().Passed[PassHostedDecideOff] != 1 {
			t.Fatal("dormant request touched rows or wrong pass counter")
		}
	}
}

func withHostedMember(t *testing.T, f authorizeFile, c authorizeCase, member *bool) authorizeCase {
	t.Helper()
	p := *policyOf(t, f, c)
	p.FastpathEdgeHostedDecide = member
	raw, err := json.Marshal(p)
	if err != nil {
		t.Fatal(err)
	}
	c.PolicyPatch = make(map[string]json.RawMessage)
	if json.Unmarshal(raw, &c.PolicyPatch) != nil {
		t.Fatal("policy encoding")
	}
	// Remove any existing member: unmarshalling into a populated map keeps it.
	if member == nil {
		delete(c.PolicyPatch, "fastpath_edge_hosted_decide")
	}
	return c
}

func TestHostedDecideTicketFailureAndTimeout(t *testing.T) {
	f := hostedGoldens(t)
	c := hostedCase(t, f, "answer:hosted:C:eligible")
	in, _ := caseInputs(t, f, c)
	fallback, err := Decide(in).ForShadow().Finish(c.Admitted.Envelope, endOfLifeOf(t, c))
	if err != nil {
		t.Fatal(err)
	}
	want := string(fallback.Render(c.Expect.timing(), false))
	for _, mode := range []string{"refused", "failure", "timeout"} {
		t.Run(mode, func(t *testing.T) {
			h := ticketEdge(t, f, c, ticketTransport(func(r *http.Request) (*http.Response, error) {
				switch mode {
				case "refused":
					return ticketResponse(200, `{"async_eligible":false}`), nil
				case "failure":
					return nil, errors.New("unavailable")
				default:
					select {
					case <-r.Context().Done():
					case <-time.After(2 * time.Second):
						t.Error("ticket context did not finish")
					}
					return nil, context.DeadlineExceeded
				}
			}))
			rec := h.send(t, c)
			if rec.Code != 200 || withGoldenTiming(rec.Body.Bytes()) != want || len(h.rows.inserted()) != 1 {
				t.Fatal("ticket failure changed the committed fallback")
			}
		})
	}
}

func TestHostedDecideDoesNotChangeExistingDecisions(t *testing.T) {
	for _, f := range []authorizeFile{authorizeGoldens(t), asyncAuthorizeGoldens(t)} {
		for _, c := range f.Cases {
			in, _ := caseInputs(t, f, c)
			before := Decide(in)
			p := *in.Policy
			on := true
			p.FastpathEdgeHostedDecide = &on
			in.Policy = &p
			after := Decide(in)
			if before.Pass != after.Pass || before.DecideGate != after.DecideGate || before.AsyncPop != after.AsyncPop || before.StreamStageD != after.StreamStageD ||
				before.FloatAt != after.FloatAt || before.Via != after.Via {
				t.Fatalf("%s: existing outcome changed", c.Name)
			}
			if d := after.ForShadow(); d != nil && c.Expect.Outcome == "answer" && c.Expect.AsyncShape != "C" {
				checkAnswered(t, c, d)
			}
		}
	}
}

func TestHostedDecideRoutingShadow(t *testing.T) {
	f := hostedGoldens(t)
	c := hostedCase(t, f, "answer:hosted:none")
	in, _ := caseInputs(t, f, c)
	request, _, reason := requestOf(c.body(), &shadowCall{})
	if reason != "" {
		t.Fatalf("routing shadow passed %s", reason)
	}
	request.At = c.at()
	answer := in.Snapshot.Evaluate(request)
	if answer.Outcome != routingsnapshot.Answered || !answer.HostedDecide || answer.ModelEstimate != c.Expect.Estimate {
		t.Fatal("routing shadow did not evaluate hosted decide")
	}
}

func TestHostedDecideKeyWindows(t *testing.T) {
	f := hostedGoldens(t)
	c := hostedCase(t, f, "answer:hosted:B")
	body := keyBodyWith(t, c, map[string]any{"limit_daily_microdollars": 5000})
	c.Key.KeyBody = &body
	for _, mode := range []string{"", routingsnapshot.KeyWindowsAlert, routingsnapshot.KeyWindowsEnforce} {
		in, _ := caseInputs(t, f, c)
		p := *in.Policy
		if mode != "" {
			p.FastpathPilotKeyWindows = &mode
		}
		in.Policy = &p
		out := Decide(in)
		want := PassKeyWindow
		if mode == routingsnapshot.KeyWindowsEnforce {
			want = ""
		}
		if out.Pass != want || out.ForShadow() == nil || out.ForShadow().Windows == nil {
			t.Fatalf("window mode %s passed %s", mode, out.Pass)
		}
	}
}

func TestDecideTicketRequestNeverStreams(t *testing.T) {
	f := hostedGoldens(t)
	c := hostedCase(t, f, "answer:hosted:C:eligible")
	in, _ := caseInputs(t, f, c)
	d := Decide(in).ForShadow()
	d.Input.Async.Request.Streamed = true
	request, ok := ticketRequestOf(d)
	if !ok || request.Streamed || request.RouteType != "decide" {
		t.Fatal("decide ticket must be non-streamed")
	}
}

func TestHostedDecideReplayPassesAndRefunds(t *testing.T) {
	f := hostedGoldens(t)
	c := hostedCase(t, f, "pass:hosted:replay")
	h := ticketEdge(t, f, c, ticketTransport(func(*http.Request) (*http.Response, error) {
		return ticketResponse(418, `{"python":true}`), nil
	}))
	outcome, code := insertOf(t, c)
	h.rows.insert, h.rows.code = func(context.Context) store.InsertOutcome { return outcome }, code
	rec := h.send(t, c)
	asked, refunds, _ := h.admitter.seen()
	if rec.Code != http.StatusTeapot || h.edge.DecideCounts().Passed[PassInsertScopeTaken] != 1 || len(asked) != 1 || len(refunds) != 1 {
		t.Fatal("replay did not refund and pass")
	}
}

func TestHostedDecideInFlightLimit(t *testing.T) {
	f := hostedGoldens(t)
	c := hostedCase(t, f, "answer:hosted:none")
	h := ticketEdge(t, f, c, ticketTransport(func(*http.Request) (*http.Response, error) {
		return ticketResponse(418, `{"python":true}`), nil
	}))
	h.policy.GatewayAuthorizeMaxInFlightPerKey = 1
	release, entered := make(chan struct{}), make(chan struct{})
	defer close(release)
	h.rows.insert = func(context.Context) store.InsertOutcome {
		close(entered)
		select {
		case <-release:
		case <-time.After(2 * time.Second):
			t.Error("insert was not released")
		}
		return store.Inserted
	}
	done := make(chan int, 1)
	go func() { done <- h.send(t, c).Code }()
	select {
	case <-entered:
	case <-time.After(2 * time.Second):
		t.Fatal("hosted request never reached insert")
	}
	second := h.send(t, c)
	select {
	case release <- struct{}{}:
	case <-time.After(2 * time.Second):
		t.Fatal("insert did not accept release")
	}
	var first int
	select {
	case first = <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("first hosted request did not finish")
	}
	if first != http.StatusOK || second.Code != http.StatusTeapot || h.edge.DecideCounts().Passed[PassInFlightKey] != 1 {
		t.Fatal("hosted in-flight rule differs")
	}
}

func TestHostedDecideOtherShapesKeepTheirCounters(t *testing.T) {
	f := hostedGoldens(t)
	c := hostedCase(t, f, "answer:hosted:none")
	in, _ := caseInputs(t, f, c)
	base, _, reason := requestOf(c.body(), &shadowCall{})
	if reason != "" {
		t.Fatal(reason)
	}
	base.At = c.at()
	chat := "chat.completions"
	for _, change := range []func(*routingsnapshot.Request){
		func(r *routingsnapshot.Request) { r.RouteType = &chat },
		func(r *routingsnapshot.Request) { r.RouteType = nil },
		func(r *routingsnapshot.Request) { r.Models = []string{"openai/gpt-4.1"} },
		func(r *routingsnapshot.Request) { r.Model += "+price" },
		func(r *routingsnapshot.Request) { r.Model += ":nitro" },
		func(r *routingsnapshot.Request) { r.Model += "-2026-10-01" },
	} {
		for _, provider := range []json.RawMessage{nil, json.RawMessage(`{"sort":"unknown"}`)} {
			request := base
			change(&request)
			request.Provider = provider
			old := request
			old.HostedDecide = false
			before := in.Snapshot.Evaluate(old)
			after := evaluateRouting(in.Snapshot, request)
			if !sameResult(before, after) || redact(before.Reason) != redact(after.Reason) {
				t.Fatal("an excluded hosted shape changed its outcome or counter")
			}
		}
	}
}

// Like TestOnlyAdmitHandsOutADecision, inspect production Go syntax so each
// future routing evaluation has to use the shared rule. keywindow.Evaluate
// is a separate package function, not a routing snapshot method.
func TestRoutingEvaluationsUseWrapper(t *testing.T) {
	files, err := filepath.Glob("*.go")
	if err != nil {
		t.Fatal(err)
	}
	seen := 0
	for _, name := range files {
		if strings.HasSuffix(name, "_test.go") {
			continue
		}
		raw, err := os.ReadFile(name)
		if err != nil {
			t.Fatal(err)
		}
		fs := gotoken.NewFileSet()
		file, err := parser.ParseFile(fs, name, raw, 0)
		if err != nil {
			t.Fatal(err)
		}
		for _, decl := range file.Decls {
			fn, ok := decl.(*ast.FuncDecl)
			wrapper := ok && name == "shadow.go" && fn.Name.Name == "evaluateRouting"
			ast.Inspect(decl, func(n ast.Node) bool {
				call, ok := n.(*ast.CallExpr)
				if !ok {
					return true
				}
				sel, ok := call.Fun.(*ast.SelectorExpr)
				if !ok || sel.Sel.Name != "Evaluate" {
					return true
				}
				if id, ok := sel.X.(*ast.Ident); ok && id.Name == "keywindow" {
					return true
				}
				if !wrapper {
					t.Errorf("%s: routing evaluation must use evaluateRouting", fs.Position(call.Pos()))
				} else {
					seen++
				}
				return true
			})
		}
	}
	if seen != 2 {
		t.Fatalf("wrapper has %d evaluations, want 2", seen)
	}
}

func TestHostedDecideSuffixCallers(t *testing.T) {
	f := hostedGoldens(t)
	c := hostedCase(t, f, "answer:hosted:none")
	for _, suffix := range []string{":nitro", "-2026-10-01"} {
		t.Run(suffix, func(t *testing.T) {
			in, _ := caseInputs(t, f, c)
			body := withFields(t, c.body(), map[string]any{"model": "fixture.invalid/hosted-decision" + suffix})
			req, _, why := requestOf(body, &shadowCall{})
			if why != "" {
				t.Fatal(why)
			}
			req.At = c.at()
			old := req
			old.HostedDecide = false
			before := in.Snapshot.Evaluate(old)
			if before.Outcome != routingsnapshot.Refused {
				t.Fatal("base must refuse suffix")
			}
			after := evaluateRouting(in.Snapshot, req)
			if !sameResult(before, after) || redact(before.Reason) != redact(after.Reason) {
				t.Fatal("suffix changed result or counter")
			}
			in.Request.Body = body
			if out := Decide(in); out.Pass != PassEvaluateRefused {
				t.Fatalf("Decide pass=%s", out.Pass)
			}
			refusal := before.Refusal
			response, err := json.Marshal(map[string]any{"error": map[string]any{
				"type": refusal.Type, "message": refusal.Message, "param": refusal.Param}})
			if err != nil {
				t.Fatal(err)
			}
			result := (&shadow{}).compare(comparison{
				call:   &shadowCall{held: &held{snapshot: in.Snapshot}, at: c.at(), body: body},
				status: refusal.Status, response: response, complete: true, done: c.at(),
			})
			if result.class != ClassAgree || result.goOutcome != "refused" || len(result.fields) != 0 {
				t.Fatalf("routing shadow=%s outcome=%s fields=%v", result.class, result.goOutcome, result.fields)
			}
		})
	}
}

func TestHostedDecideAsyncSupportIsNilSafe(t *testing.T) {
	var absent *routingsnapshot.AsyncSettle
	if absent.SupportsDecide() {
		t.Fatal("absent async policy permits decide")
	}
}
