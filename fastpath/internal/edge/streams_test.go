package edge

// Streams on the fast-path lease, the edge's part in this increment
// (docs/design/fast-admission-streams.md sections 5.4 and 8.1): every
// stream still passes as body.stream, whatever the policy says, and the
// shadow splits them by the Stage D reason the edge reads for them.

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
		{"the edge's own switch on", func(p *routingsnapshot.EdgePolicy) {
			open(p)
			on := true
			p.FastpathEdgeStreams = &on
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
		if d.Pass != gwauth.ReasonStream || d.ForShadow() != nil {
			t.Errorf("%s: passed %q, decided %v: every stream still passes", c.name, d.Pass, d.ForShadow() != nil)
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
