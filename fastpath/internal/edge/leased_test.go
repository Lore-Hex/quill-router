package edge

import (
	"context"
	"maps"
	"net/http"
	"slices"
	"testing"

	"github.com/Lore-Hex/quill-router/fastpath/internal/frontdoor"
	"github.com/Lore-Hex/quill-router/fastpath/internal/gwauth"
	"github.com/Lore-Hex/quill-router/fastpath/internal/pyjson"
	"github.com/Lore-Hex/quill-router/fastpath/internal/routingsnapshot"
)

// Leased answers (docs/design/fast-admission-e2-g15-async.md, "Leased
// answers"; docs/design/fast-admission-widening.md section 9.1): with the
// policy's async_settle.edge_leased, the edge answers an async-v1 authorize
// of a workspace Python's admission covers when it leases the request
// itself, with Python's answer to a leased authorization (the admission-off
// answer, off the overlap list).

// leasedOn is admissionOn with the leased-answer switch on and overlap
// naming the overlap list's covered workspaces (none for nil).
func leasedOn(overlap []string, workspaces ...string) func(*routingsnapshot.AsyncSettle) {
	return func(a *routingsnapshot.AsyncSettle) {
		admissionOn(workspaces...)(a)
		yes := true
		a.EdgeLeased, a.OverlapWorkspaces, a.EdgeTickets, a.EdgeTicketWorkspaces = &yes, nil, nil, nil
		if overlap != nil {
			o := slices.Clone(overlap)
			a.OverlapWorkspaces = &o
		}
	}
}

// withoutTicket is a shape C golden as Python answers the same request
// leased in a workspace off the overlap list: the ticket and its status URL
// gone, async_eligible false (shape B).
func withoutTicket(t *testing.T, golden authorizeCase) authorizeCase {
	t.Helper()
	golden.Expect.Response = string(mutated(t, golden.Expect.Response, func(d pyjson.Object) pyjson.Object {
		d = slices.DeleteFunc(d, func(m pyjson.Member) bool {
			return m.Key == "settlement_ticket" || m.Key == "settlement_status_url"
		})
		return setMember(d, "async_eligible", false)
	}))
	return golden
}

// TestTheLeasedAnswerGate: with edge_leased, every answered golden's
// async-v1 request in a workspace admission covers (admission_workspaces
// absent or naming it, another excluded) is decided, a leased answer whose
// bytes are Python's admission-off 200 (its leased answer) byte for byte;
// overlap_workspaces naming the workspace passes it policy.async_admission
// with no decision, as without the member (Python tickets it leased); a
// workspace admission leaves out is decided as before, not a leased
// answer. A request without the header decides whatever the members say,
// never a leased answer.
func TestTheLeasedAnswerGate(t *testing.T) {
	f := asyncAuthorizeGoldens(t)
	leased, overlap, outside, plain := 0, 0, 0, 0
	for _, golden := range f.Cases {
		if golden.Expect.Outcome != "answer" || golden.Expect.Affinity != nil {
			continue
		}
		if golden.Expect.AsyncShape == "C" {
			golden = withoutTicket(t, golden)
		}
		ws := golden.Facts.Key.WorkspaceID
		for _, v := range []struct {
			name   string
			change func(*routingsnapshot.AsyncSettle)
			want   string // "leased", "overlap" or "outside"
		}{
			{"absent", leasedOn(nil), "leased"},
			{"naming the workspace", leasedOn(nil, "a_"+ws, ws), "leased"},
			{"absent, another on the overlap list", leasedOn([]string{"a_" + ws}), "leased"},
			{"absent, excluding another", func(a *routingsnapshot.AsyncSettle) {
				leasedOn(nil)(a)
				x := []string{"a_" + ws}
				a.AdmissionExcludedWorkspaces = &x
			}, "leased"},
			{"absent, on the overlap list", leasedOn([]string{ws}), "overlap"},
			{"naming it, on the overlap list", leasedOn([]string{ws}, ws, "z_"+ws), "overlap"},
			{"leaving it out", leasedOn(nil, "a_"+ws), "outside"},
			{"excluding it", func(a *routingsnapshot.AsyncSettle) {
				leasedOn(nil)(a)
				x := []string{ws}
				a.AdmissionExcludedWorkspaces = &x
			}, "outside"},
		} {
			c := withAsyncSettle(t, f, golden, v.change)
			in, _ := caseInputs(t, f, c)
			d := Decide(in)
			if asyncPopOf(golden) == "" {
				if d.Pass != "" || d.ForShadow() == nil || d.ForShadow().LeasedAnswer {
					t.Errorf("%s, %s: no header, passed %q", golden.Name, v.name, d.Pass)
					continue
				}
				plain++
				continue
			}
			if v.want == "overlap" {
				if d.Pass != PassAsyncAdmission || d.ForShadow() != nil {
					t.Errorf("%s, %s: passed %q (decision %v), want %q", golden.Name, v.name, d.Pass,
						d.ForShadow() != nil, PassAsyncAdmission)
					continue
				}
				overlap++
				continue
			}
			dec, ok := d.Admit()
			if !ok {
				t.Errorf("%s, %s: passed %q, want a decision", golden.Name, v.name, d.Pass)
				continue
			}
			if dec.LeasedAnswer != (v.want == "leased") || dec.Staged.Async().WantTicket {
				t.Errorf("%s, %s: leased answer %v, ticket %v", golden.Name, v.name, dec.LeasedAnswer,
					dec.Staged.Async().WantTicket)
				continue
			}
			if !checkAnswered(t, c, dec) {
				continue
			}
			if v.want == "leased" {
				leased++
			} else {
				outside++
			}
		}
	}
	t.Logf("%d leased answers, %d overlap passes, %d outside admission, %d without the header", leased, overlap,
		outside, plain)
	// 55 answered async-v1 goldens and the one without the header.
	if leased != 4*55 || overlap != 2*55 || outside != 2*55 || plain != 8 {
		t.Fatalf("leased %d, overlap %d, outside %d, plain %d: not the goldens' counts", leased, overlap, outside, plain)
	}
}

// TestTheLeasedAnswerComposeRefusesAWorkspacePythonTickets: Stage's own
// backstop, as if the admission gate were skipped. The async input for a
// covered workspace keeps admission on unless the policy's edge_leased
// says a leased authorization of it takes no ticket: without the member,
// or on the overlap list, Stage refuses it; with the member and off the
// list, Stage composes the admission-off answer. Edge tickets keep their
// own input (a ticket wanted).
func TestTheLeasedAnswerComposeRefusesAWorkspacePythonTickets(t *testing.T) {
	f := asyncAuthorizeGoldens(t)
	c := asyncCaseNamed(t, "answer:async.B:leased")
	in, _ := caseInputs(t, f, c)
	dec, ok := Decide(in).Admit()
	if !ok || !dec.LeasedAnswer {
		t.Fatal("the leased golden is not a leased answer")
	}
	ws := dec.Workspace
	yes := true
	for _, v := range []struct {
		name    string
		change  func(*routingsnapshot.AsyncSettle)
		refused bool
		ticket  bool
	}{
		{"edge_leased absent", func(a *routingsnapshot.AsyncSettle) { a.EdgeLeased = nil }, true, false},
		{"on the overlap list", func(a *routingsnapshot.AsyncSettle) {
			o := []string{ws}
			a.OverlapWorkspaces = &o
		}, true, false},
		{"edge_leased", func(*routingsnapshot.AsyncSettle) {}, false, false},
		{"edge tickets too", func(a *routingsnapshot.AsyncSettle) { a.EdgeTickets = &yes }, false, true},
	} {
		p := *policyOf(t, f, c).AsyncSettle
		v.change(&p)
		input := dec.Input
		input.Async = asyncComposeInput(input.Async.Request, p, ws)
		s, err := gwauth.Stage(input)
		if (err != nil) != v.refused {
			t.Errorf("%s: Stage error %v, want refused %v", v.name, err, v.refused)
			continue
		}
		if err == nil && s.Async().WantTicket != v.ticket {
			t.Errorf("%s: ticket wanted %v", v.name, s.Async().WantTicket)
		}
	}
}

// TestEdgeTicketsKeepTheirPathWithEdgeLeased: a workspace edge tickets
// cover is answered through the ticket path whatever edge_leased says: a
// ticket wanted, not a leased answer.
func TestEdgeTicketsKeepTheirPathWithEdgeLeased(t *testing.T) {
	f := asyncAuthorizeGoldens(t)
	c := withAsyncSettle(t, f, asyncCaseNamed(t, "answer:async.B:leased"), func(a *routingsnapshot.AsyncSettle) {
		yes := true
		a.EdgeTickets = &yes
	})
	in, _ := caseInputs(t, f, c)
	dec, ok := Decide(in).Admit()
	if !ok || dec.LeasedAnswer || !dec.Staged.Async().WantTicket {
		t.Fatalf("decision %v, leased answer %v, ticket %v", ok, dec != nil && dec.LeasedAnswer,
			dec != nil && dec.Staged.Async().WantTicket)
	}
	if _, ok := ticketRequestOf(dec); !ok {
		t.Fatal("no ticket request for an edge-ticket workspace")
	}
}

// TestALeasedAnswerMeetsEveryGate: a leased answer's request is gated as a
// workspace outside admission is, each gate passing it with its own reason
// (the counts read as for any other request): a key cap before the
// compose, with no decision; the switch copy, the decide row and the async
// switch after it, each a decision gate with a decision for the shadow.
// The control: every gate open, a decision to admit.
func TestALeasedAnswerMeetsEveryGate(t *testing.T) {
	f := asyncAuthorizeGoldens(t)
	keyCap := asyncCaseNamed(t, "pass:key.key_cap:async.C:leased")
	in, _ := caseInputs(t, f, keyCap)
	if d := Decide(in); d.Pass != PassKeyCap || d.ForShadow() != nil {
		t.Fatalf("key cap: passed %q (decision %v)", d.Pass, d.ForShadow() != nil)
	}
	c := asyncCaseNamed(t, "answer:async.B:leased")
	ws := c.Facts.Key.WorkspaceID
	for _, v := range []struct {
		name  string
		input func(*DecideInputs)
		gate  string
	}{
		{"open", func(*DecideInputs) {}, ""},
		{"not enabled", func(in *DecideInputs) { in.Enabled = func(string) bool { return false } }, PassWorkspaceNotEnabled},
		{"decide row off", func(in *DecideInputs) {
			in.Decide = DecideView{Rows: map[DecideKey]bool{}, ReadAt: in.Request.At,
				MaxAge: decideStaleness * DecideEvery}
		}, PassDecideOff},
		{"decide copy stale", func(in *DecideInputs) { in.Decide = DecideView{} }, PassCacheDecide},
		{"edge_decide false", func(in *DecideInputs) {
			p := *in.Policy
			a := *p.AsyncSettle
			a.EdgeDecide = false
			p.AsyncSettle = &a
			in.Policy = &p
		}, PassAsyncOff},
	} {
		in, _ := caseInputs(t, f, c)
		in.Enabled = func(w string) bool { return w == ws }
		v.input(&in)
		d := Decide(in)
		if d.Pass != v.gate || d.DecideGate != v.gate || d.ForShadow() == nil || !d.ForShadow().LeasedAnswer {
			t.Errorf("%s: passed %q (gate %q, decision %v), want %q", v.name, d.Pass, d.DecideGate,
				d.ForShadow() != nil, v.gate)
			continue
		}
		if _, ok := d.Admit(); ok != (v.gate == "") {
			t.Errorf("%s: admitted %v", v.name, ok)
		}
	}
}

// TestALeasedAnswerIsGivenOnlyUnderTheEdgesLease: through the decide path,
// the leased golden is answered with Python's leased bytes once the front
// door admits and the insert lands; the front door refusing it passes it
// to Python (owner.busy), nothing inserted, Python's answer the caller's.
// Without edge_leased the request passes policy.async_admission and the
// owner is never asked (the switch off is today's behaviour).
func TestALeasedAnswerIsGivenOnlyUnderTheEdgesLease(t *testing.T) {
	f := asyncAuthorizeGoldens(t)
	c := asyncCaseNamed(t, "answer:async.B:leased")
	py := newFakePython(t)
	for _, v := range []struct {
		name   string
		c      authorizeCase
		status frontdoor.Status
		pass   string
	}{
		{"admitted", c, frontdoor.Admitted, ""},
		{"busy", c, frontdoor.Busy, PassOwnerBusy},
		{"switch off", withAsyncSettle(t, f, c, func(a *routingsnapshot.AsyncSettle) { a.EdgeLeased = nil }),
			frontdoor.Admitted, PassAsyncAdmission},
	} {
		h := newDecideEdge(t, f, v.c, py)
		if v.status != frontdoor.Admitted {
			h.admitter.authorize = func(context.Context, frontdoor.AuthorizeOf) frontdoor.Authorized {
				return frontdoor.Authorized{Status: v.status}
			}
		}
		hits := py.hits.Load()
		rec := h.send(t, v.c)
		reached := py.hits.Load() - hits
		asked, _, _ := h.admitter.seen()
		counts := h.edge.DecideCounts()
		if v.pass == "" {
			if rec.Code != http.StatusOK || withGoldenTiming(rec.Body.Bytes()) != c.Expect.Response || reached != 0 ||
				len(asked) != 1 || len(h.rows.inserted()) != 1 || counts.Answered != 1 {
				t.Errorf("%s: %d %s, Python reached %d, owner asked %d", v.name, rec.Code, rec.Body.Bytes(), reached,
					len(asked))
			}
			continue
		}
		wantAsked := 1
		if v.pass == PassAsyncAdmission {
			wantAsked = 0
		}
		if rec.Code != http.StatusTeapot || reached != 1 || len(asked) != wantAsked || len(h.rows.inserted()) != 0 ||
			counts.Answered != 0 || !maps.Equal(counts.Passed, map[string]int64{v.pass: 1}) {
			t.Errorf("%s: %d, Python reached %d, owner asked %d, counts %+v", v.name, rec.Code, reached, len(asked), counts)
		}
	}
}

// TestTheShadowDoesNotCountAPythonTicketAgainstALeasedAnswer
// (leasedUnleased): a leased answer the decide path passed, which Python
// answered with a ticket (its own pilot did not lease it), is
// python_answered_go_passed under the decide path's pass, not a differ,
// with its decide gate and async counts (async_python_admitted) kept. The
// controls: Python's leased answer (shape B) agrees; a decision that is not
// a leased answer still differs on Python's ticket.
func TestTheShadowDoesNotCountAPythonTicketAgainstALeasedAnswer(t *testing.T) {
	f := asyncAuthorizeGoldens(t)
	c := asyncCaseNamed(t, "answer:async.B:leased")
	in, _ := caseInputs(t, f, c)
	in.Enabled = func(string) bool { return false }
	d := Decide(in)
	if d.Pass != PassWorkspaceNotEnabled || d.ForShadow() == nil || !d.ForShadow().LeasedAnswer {
		t.Fatalf("passed %q (decision %v)", d.Pass, d.ForShadow() != nil)
	}
	sh := &fullShadow{}
	ticket := pyAnswer{status: http.StatusOK, body: ticketed(t, c.Expect.Response)}
	r := sh.safeCompare(d, ticket, nil, PassWorkspaceNotEnabled)
	if r.class != ClassPythonAnsweredGoPassed+"."+PassWorkspaceNotEnabled || len(r.fields) != 0 || !r.decided ||
		r.gate != PassWorkspaceNotEnabled || !r.async.pythonAdmitted || r.async.pop != AsyncPlain || r.rows {
		t.Fatalf("Python's ticket: %+v", r)
	}
	if r := sh.safeCompare(d, ticket, nil, ""); r.class != ClassPythonAnsweredGoPassed+"."+inFlightNotDecided {
		t.Fatalf("Python's ticket with no decide path: %s", r.class)
	}
	if r := sh.safeCompare(d, pyAnswer{status: http.StatusOK, body: []byte(c.Expect.Response)}, nil,
		PassWorkspaceNotEnabled); r.class != ClassAgreeFull || r.async.compared != "agree" {
		t.Fatalf("Python's leased answer: %s %+v", r.class, r.async)
	}
	// The control: the same request in a workspace outside admission (the
	// reserved list) is no leased answer, and Python's ticket is a differ.
	reserved := asyncCaseNamed(t, "answer:async.B:leased:reserved")
	in, _ = caseInputs(t, f, reserved)
	in.Enabled = func(string) bool { return false }
	d = Decide(in)
	if d.ForShadow() == nil || d.ForShadow().LeasedAnswer {
		t.Fatal("the reserved golden is a leased answer")
	}
	r = sh.safeCompare(d, pyAnswer{status: http.StatusOK, body: ticketed(t, reserved.Expect.Response)}, nil,
		PassWorkspaceNotEnabled)
	if r.class != ClassDifferAny || !slices.Contains(r.fields, FieldAsyncShape) {
		t.Fatalf("a ticket against a decision outside admission: %s %v", r.class, r.fields)
	}
}
