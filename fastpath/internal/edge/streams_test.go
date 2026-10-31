package edge

// Streams on the fast-path lease, the edge's part
// (docs/design/fast-admission-streams.md sections 5.4, 8.1 and 8.4):
// without the edge policy's fastpath_edge_streams every stream passes as
// body.stream, composed for the shadow when nothing else passes it, and
// the shadow splits them by the Stage D reason the edge reads for them;
// with it the edge decides a stream Stage D lets in, as Python's pilot
// leases it.

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/gwauth"
	"github.com/Lore-Hex/quill-router/fastpath/internal/routingsnapshot"
)

// withBody is in with its body's members changed: set to a value, or
// removed when the value is nil. Numbers keep their literal form.
func withBody(t *testing.T, in DecideInputs, change map[string]any) DecideInputs {
	t.Helper()
	dec := json.NewDecoder(bytes.NewReader(in.Request.Body))
	dec.UseNumber()
	var body map[string]any
	if err := dec.Decode(&body); err != nil {
		t.Fatal(err)
	}
	for k, v := range change {
		if v == nil {
			delete(body, k)
		} else {
			body[k] = v
		}
	}
	raw, err := json.Marshal(body)
	if err != nil {
		t.Fatal(err)
	}
	in.Request.Body = raw
	return in
}

func TestAStreamPassesWithTheStageDReasonTheEdgeReads(t *testing.T) {
	base, _ := baseInputs(t)
	if d := Decide(base); d.StreamStageD != "" {
		t.Fatalf("a request that does not stream has a stream reason %q", d.StreamStageD)
	}
	stream := withBody(t, base, map[string]any{"stream": true, "route_type": "chat.completions"})
	open := func(p *routingsnapshot.EdgePolicy) {
		p.StageD = routingsnapshot.StageDSetting{EligibilityEnabled: true, HeartbeatEnabled: true}
	}
	for _, c := range []struct {
		name   string
		policy func(*routingsnapshot.EdgePolicy)
		body   map[string]any
		boot   bool
		want   string
	}{
		{"every reason read lets it through", open, nil, true, StreamUnread},
		{"responses", open, map[string]any{"route_type": "responses"}, true, StreamUnread},
		{"eligibility off", func(p *routingsnapshot.EdgePolicy) {
			open(p)
			p.StageD.EligibilityEnabled = false
		}, nil, true, StreamStageDDisabled},
		{"a Stage D pilot set", func(p *routingsnapshot.EdgePolicy) {
			open(p)
			p.StageD.PilotWorkspaces = []string{"ws-other"}
		}, nil, true, StreamPilotSet},
		{"heartbeats off", func(p *routingsnapshot.EdgePolicy) {
			open(p)
			p.StageD.HeartbeatEnabled = false
		}, nil, true, StreamHeartbeatsDisabled},
		{"a boot that declared no heartbeats", open, nil, false, StreamHeartbeatUndeclared},
		{"messages", open, map[string]any{"route_type": "messages"}, true, StreamRoute},
		{"no route type", open, map[string]any{"route_type": nil}, true, StreamRoute},
		{"the priority tier", open, map[string]any{"service_tier": "priority"}, true, StreamServiceTier},
		{"the auto tier", open, map[string]any{"service_tier": "auto"}, true, StreamServiceTier},
		{"the default tier", open, map[string]any{"service_tier": "default"}, true, StreamUnread},
		{"a typed store", func(p *routingsnapshot.EdgePolicy) {
			open(p)
			p.AsyncSettle = &routingsnapshot.AsyncSettle{TypedRecords: true}
		}, nil, true, StreamUnread},
	} {
		in := stream
		if c.body != nil {
			in = withBody(t, stream, c.body)
		}
		policy := *in.Policy
		c.policy(&policy)
		in.Policy = &policy
		in.Request.Boot.Boot.DeclaresUsageHeartbeat = c.boot
		d := Decide(in)
		if _, admit := d.Admit(); d.Pass != gwauth.ReasonStream || admit ||
			(d.ForShadow() != nil && d.DecideGate != gwauth.ReasonStream) {
			t.Errorf("%s: passed %q, admitted %v, gate %q: every stream still passes", c.name, d.Pass, admit, d.DecideGate)
		}
		if d.StreamStageD != c.want {
			t.Errorf("%s: %q, want %q", c.name, d.StreamStageD, c.want)
		}
	}
	in := stream
	in.Policy = nil
	if d := Decide(in); d.Pass != gwauth.ReasonStream || d.StreamStageD != StreamPolicyUnknown {
		t.Errorf("no policy: %q %q", d.Pass, d.StreamStageD)
	}
}

func TestTheShadowCountsStreamsByTheirStageDReason(t *testing.T) {
	var lines []string
	sh, err := newFullShadow(DecideShadowConfig{Cache: &recordingCache{}, Decides: &fakeDecides{}, Rows: &fakeRows{}},
		func(format string, args ...any) {
			if strings.HasPrefix(format, "fastpath edge decide shadow: %s") {
				lines = append(lines, format)
			}
		}, time.Now, 0)
	if err != nil {
		t.Fatal(err)
	}
	for _, reason := range []string{StreamUnread, StreamUnread, StreamRoute, ""} {
		r := compareFull(Decided{Pass: gwauth.ReasonStream, StreamStageD: reason}, pyAnswer{status: 200})
		sh.record("d", r)
	}
	sh.mu.Lock()
	got := clonedCounts(sh.totals.streams)
	sh.mu.Unlock()
	if len(got) != 2 || got[StreamUnread] != 2 || got[StreamRoute] != 1 {
		t.Fatalf("the stream counts: %v", got)
	}
	sh.flush()
	if len(lines) != 1 || !strings.HasSuffix(lines[0], " streams=%s") {
		t.Fatalf("the period's line: %q", lines)
	}
}

// streamsOn is in streaming, its policy letting the edge decide streams:
// Stage D open to every workspace, a typed store, fastpath_edge_streams
// true and a boot that declared heartbeats.
func streamsOn(t *testing.T, in DecideInputs, member bool) DecideInputs {
	t.Helper()
	in = withBody(t, in, map[string]any{"stream": true, "route_type": "chat.completions"})
	policy := *in.Policy
	policy.StageD = routingsnapshot.StageDSetting{EligibilityEnabled: true, HeartbeatEnabled: true}
	policy.AsyncSettle = &routingsnapshot.AsyncSettle{TypedRecords: true}
	policy.FastpathEdgeStreams = nil
	if member {
		on := true
		policy.FastpathEdgeStreams = &on
	}
	in.Policy = &policy
	in.Request.Boot.Boot.DeclaresUsageHeartbeat = true
	return in
}

// TestTheEdgeDecidesAStreamOnlyWithTheMember: a stream Stage D lets in is
// decided (its 200 eligible, its row carrying its pricing snapshot and
// heartbeat_seq 0) only when the policy carries fastpath_edge_streams;
// without it the same stream passes as body.stream, composed for the
// shadow alone.
func TestTheEdgeDecidesAStreamOnlyWithTheMember(t *testing.T) {
	base, c := baseInputs(t)
	on := Decide(streamsOn(t, base, true))
	dec, admit := on.Admit()
	if on.Pass != "" || !admit || on.StreamStageD != "" {
		t.Fatalf("with the member: passed %q, admitted %v, split %q", on.Pass, admit, on.StreamStageD)
	}
	if dec.Reservation != dec.Estimate || dec.Input.StageDReason != gwauth.StageDOK {
		t.Fatalf("reservation %d, estimate %d, Stage D %q", dec.Reservation, dec.Estimate, dec.Input.StageDReason)
	}
	composed, err := dec.Finish(c.Admitted.Envelope, endOfLifeOf(t, c))
	if err != nil {
		t.Fatal(err)
	}
	a := composed.Authorization
	if a.PricingSnapshot == nil || a.HeartbeatSeq == nil || *a.HeartbeatSeq != 0 ||
		!strings.Contains(a.Payload, `"heartbeat_seq":0`) {
		t.Fatalf("the row: pricing %v, heartbeat_seq %v", a.PricingSnapshot, a.HeartbeatSeq)
	}
	body := composed.Render(gwauth.Timing{}, false)
	for _, want := range []string{`"candidate_cost_reporting":true`, `"stage_d":{"eligible":true,"reason":"ok"}`,
		`"candidate_prices":[`, `,"cap_micro":`} {
		if !bytes.Contains(body, []byte(want)) {
			t.Errorf("the 200 lacks %s", want)
		}
	}

	off := Decide(streamsOn(t, base, false))
	if _, admit := off.Admit(); off.Pass != gwauth.ReasonStream || admit || off.ForShadow() == nil ||
		off.DecideGate != gwauth.ReasonStream || off.StreamStageD != StreamUnread {
		t.Fatalf("without the member: passed %q, admitted %v, gate %q, split %q", off.Pass, admit, off.DecideGate,
			off.StreamStageD)
	}
	// The shadow's decision is the decided one, the gate aside.
	shadow, err := off.ForShadow().Finish(c.Admitted.Envelope, endOfLifeOf(t, c))
	if err != nil {
		t.Fatal(err)
	}
	sa := shadow.Authorization
	if !bytes.Equal(shadow.Render(gwauth.Timing{}, false), body) || sa.Payload != a.Payload ||
		sa.PricingSnapshot == nil || *sa.PricingSnapshot != *a.PricingSnapshot {
		t.Fatal("the shadow's stream is not the one the member decides")
	}
}

// TestAStreamThePilotWouldNotLeasePasses: with the member, each condition
// of section 5.1 the edge reads passes the stream with Python's pilot
// reason; without it every one is body.stream, and a stream whose body
// names api_key_hash passes as that, the one reason before body.stream.
func TestAStreamThePilotWouldNotLeasePasses(t *testing.T) {
	base, _ := baseInputs(t)
	for _, c := range []struct {
		name   string
		change func(*DecideInputs)
		want   string
	}{
		{"eligibility off", func(in *DecideInputs) { in.Policy.StageD.EligibilityEnabled = false },
			PassStreamStageD + "stage_d_disabled"},
		{"not a Stage D pilot", func(in *DecideInputs) { in.Policy.StageD.PilotWorkspaces = []string{"ws-other"} },
			PassStreamStageD + "workspace_not_pilot"},
		{"heartbeats off", func(in *DecideInputs) { in.Policy.StageD.HeartbeatEnabled = false },
			PassStreamStageD + "heartbeats_disabled"},
		{"a declaration required", func(in *DecideInputs) {
			in.Policy.StageD.RequireHeartbeatDeclaration = true
			in.Request.Boot.Boot.DeclaresUsageHeartbeat = false
		}, PassStreamStageD + "heartbeat_undeclared"},
		{"a boot that declared none", func(in *DecideInputs) { in.Request.Boot.Boot.DeclaresUsageHeartbeat = false },
			PassStreamUndeclared},
		{"messages", func(in *DecideInputs) {
			*in = withBody(t, *in, map[string]any{"route_type": "messages"})
		}, PassStreamStageD + "route"},
		{"no async_settle object", func(in *DecideInputs) { in.Policy.AsyncSettle = nil },
			PassStreamStageD + "settlement_backend"},
		{"legacy records", func(in *DecideInputs) { in.Policy.AsyncSettle = &routingsnapshot.AsyncSettle{} },
			PassStreamStageD + "settlement_backend"},
		{"async-v1", func(in *DecideInputs) {
			in.Request.Header = in.Request.Header.Clone()
			in.Request.Header.Set("X-TR-Settlement-Mode", gwauth.SettlementModeAsyncV1)
		}, gwauth.ReasonStream},
	} {
		for _, member := range []bool{true, false} {
			in := streamsOn(t, base, member)
			c.change(&in)
			d := Decide(in)
			want := c.want
			if !member {
				want = gwauth.ReasonStream
			}
			if d.Pass != want || d.ForShadow() != nil {
				t.Errorf("%s (member %v): passed %q, decided %v; want %q", c.name, member, d.Pass,
					d.ForShadow() != nil, want)
			}
		}
	}
	for _, member := range []bool{true, false} {
		in := streamsOn(t, base, member)
		in = withBody(t, in, map[string]any{"api_key_hash": "key_e2_cohort", "api_key_lookup_hash": nil})
		if d := Decide(in); d.Pass != gwauth.ReasonAPIKeyHash || d.StreamStageD != "" {
			t.Errorf("api_key_hash (member %v): passed %q, split %q", member, d.Pass, d.StreamStageD)
		}
	}
}

// TestTheHoldTripwire: a stream whose reservation is not its estimate (a
// lowered stream reservation, which the lease would cut the stream at)
// passes as stream.hold; one whose hold is its ceiling goes on.
func TestTheHoldTripwire(t *testing.T) {
	var r gwauth.Request
	equal := routingsnapshot.Result{ModelEstimate: 10, Reservation: 10}
	if got := streamGate(r, gwauth.StageDOK, true, equal); got != "" {
		t.Fatalf("a hold at its ceiling: %q", got)
	}
	for _, res := range []routingsnapshot.Result{{ModelEstimate: 10, Reservation: 9}, {ModelEstimate: 10, Reservation: 11}} {
		if got := streamGate(r, gwauth.StageDOK, true, res); got != PassStreamHold {
			t.Errorf("reservation %d, estimate %d: %q", res.Reservation, res.ModelEstimate, got)
		}
	}
	// The gates before it name their own reason first.
	if got := streamGate(r, "route", false, routingsnapshot.Result{ModelEstimate: 10, Reservation: 9}); got != PassStreamStageD+"route" {
		t.Errorf("Stage D first: %q", got)
	}
	if got := streamGate(r, gwauth.StageDOK, false, routingsnapshot.Result{ModelEstimate: 10, Reservation: 9}); got != PassStreamUndeclared {
		t.Errorf("the declaration before the hold: %q", got)
	}
}
