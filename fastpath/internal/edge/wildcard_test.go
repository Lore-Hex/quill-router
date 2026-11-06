package edge

import (
	"bytes"
	"context"
	"fmt"
	"maps"
	"net/http"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/gwstate"
	"github.com/Lore-Hex/quill-router/fastpath/internal/routingsnapshot"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// The widening's edge side (docs/design/fast-admission-widening.md section
// 6.1 and section 11, PR 3): the wildcard decide row with a workspace's own
// row winning over it, the switch copy as a decision gate, the policy's
// all mode, and the decide path's and the shadow's new counts.

// TestTheDecideRowTable: a workspace's own row, on or off, against the
// wildcard on, off or absent, by section 6.1's rule, with where the answer
// came from; a stale copy is cache.decide whatever its rows. Each is
// checked on the view and through Decide, which composes the decision for
// the shadow whatever the row says.
func TestTheDecideRowTable(t *testing.T) {
	in, c := baseInputs(t)
	ws, at := c.Facts.Key.WorkspaceID, in.Request.At
	own, wildcard := DecideKey{ws, "gcp", "us-central1"}, DecideKey{store.DecideWildcard, "gcp", "us-central1"}
	for _, v := range []struct {
		name          string
		rows          map[DecideKey]bool
		stale         bool
		gate, wantVia string
	}{
		{"own on, wildcard on", map[DecideKey]bool{own: true, wildcard: true}, false, "", store.DecideViaOwn},
		{"own on, wildcard off", map[DecideKey]bool{own: true, wildcard: false}, false, "", store.DecideViaOwn},
		{"own on, no wildcard", map[DecideKey]bool{own: true}, false, "", store.DecideViaOwn},
		{"own off, wildcard on", map[DecideKey]bool{own: false, wildcard: true}, false, PassDecideOff, store.DecideViaOwn},
		{"own off, wildcard off", map[DecideKey]bool{own: false, wildcard: false}, false, PassDecideOff,
			store.DecideViaOwn},
		{"own off, no wildcard", map[DecideKey]bool{own: false}, false, PassDecideOff, store.DecideViaOwn},
		{"no own row, wildcard on", map[DecideKey]bool{wildcard: true}, false, "", store.DecideViaWildcard},
		{"no own row, wildcard off", map[DecideKey]bool{wildcard: false}, false, PassDecideOff, store.DecideViaWildcard},
		{"no rows", map[DecideKey]bool{}, false, PassDecideOff, store.DecideViaNone},
		// The wildcard is its cloud's and region's alone.
		{"another region's wildcard", map[DecideKey]bool{{store.DecideWildcard, "gcp", "us-east4"}: true}, false,
			PassDecideOff, store.DecideViaNone},
		{"another cloud's wildcard", map[DecideKey]bool{{store.DecideWildcard, "aws", "us-central1"}: true}, false,
			PassDecideOff, store.DecideViaNone},
		// Another workspace's row is not this one's.
		{"another workspace's off row, wildcard on", map[DecideKey]bool{{"ws_other", "gcp", "us-central1"}: false,
			wildcard: true}, false, "", store.DecideViaWildcard},
		{"stale, own on", map[DecideKey]bool{own: true}, true, PassCacheDecide, ""},
		{"stale, wildcard on", map[DecideKey]bool{wildcard: true}, true, PassCacheDecide, ""},
		{"stale, own off, wildcard on", map[DecideKey]bool{own: false, wildcard: true}, true, PassCacheDecide, ""},
	} {
		view := DecideView{Rows: v.rows, ReadAt: at, MaxAge: 15 * time.Second}
		if v.stale {
			view.ReadAt = at.Add(-16 * time.Second)
		}
		if gate, via := view.decides(ws, "gcp", "us-central1", at); gate != v.gate || via != v.wantVia {
			t.Errorf("%s: the view's gate %q via %q, want %q via %q", v.name, gate, via, v.gate, v.wantVia)
		}
		in := in
		in.Decide = view
		got := Decide(in)
		if got.Pass != v.gate || got.DecideGate != v.gate || got.Via != v.wantVia || got.ForShadow() == nil {
			t.Errorf("%s: pass %q gate %q via %q decision %v, want %q via %q", v.name, got.Pass, got.DecideGate, got.Via,
				got.ForShadow() != nil, v.gate, v.wantVia)
		}
		if _, ok := got.Admit(); ok != (v.gate == "") {
			t.Errorf("%s: admittable %v", v.name, ok)
		}
	}
	// The never-read copy decides nobody, under any wildcard.
	if gate, via := (DecideView{Rows: map[DecideKey]bool{wildcard: true}}).decides(ws, "gcp", "us-central1", at); gate != PassCacheDecide || via != "" {
		t.Errorf("a copy never read: %q via %q", gate, via)
	}
	// The wildcard row is never a workspace's own: a workspace named as it
	// reads as one without a row.
	view := DecideView{Rows: map[DecideKey]bool{wildcard: false}, ReadAt: at, MaxAge: 15 * time.Second}
	if gate, via := view.decides(store.DecideWildcard, "gcp", "us-central1", at); gate != PassDecideOff ||
		via != store.DecideViaWildcard {
		t.Errorf("a workspace named as the wildcard: %q via %q", gate, via)
	}
}

// TestTheDecideCopyKeepsOffRowsAndTheWildcard: the copy a read makes holds
// every row, off ones too, so an off row is an override under the wildcard,
// and the wildcard decides for a workspace with no row of its own.
func TestTheDecideCopyKeepsOffRowsAndTheWildcard(t *testing.T) {
	now := time.Date(2026, 11, 6, 2, 0, 0, 0, time.UTC)
	r := &fakeDecides{rows: []store.DecideRow{
		{Workspace: store.DecideWildcard, Cloud: "gcp", Region: "us-central1", Decide: true},
		{Workspace: "ws_a", Cloud: "gcp", Region: "us-central1", Decide: true},
		{Workspace: "ws_b", Cloud: "gcp", Region: "us-central1", Decide: false},
		{Workspace: "ws_b", Cloud: "gcp", Region: "us-east4", Decide: true},
	}}
	d := newDecideCopy(r, 10*time.Millisecond, func() time.Time { return now }, func(string, ...any) {})
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		defer close(done)
		d.run(ctx)
	}()
	defer func() {
		cancel()
		<-done
	}()
	waitFor(t, func() bool { return d.reads.Load() >= 1 && d.current.Load() != nil })
	v := d.view()
	if len(v.Rows) != 4 {
		t.Fatalf("the copy holds %d rows, want every one: %+v", len(v.Rows), v.Rows)
	}
	for _, w := range []struct{ workspace, region, gate, via string }{
		{"ws_a", "us-central1", "", store.DecideViaOwn},
		{"ws_b", "us-central1", PassDecideOff, store.DecideViaOwn},
		{"ws_c", "us-central1", "", store.DecideViaWildcard},
		{"ws_b", "us-east4", "", store.DecideViaOwn},
		{"ws_a", "us-east4", PassDecideOff, store.DecideViaNone},
	} {
		if gate, via := v.decides(w.workspace, "gcp", w.region, now); gate != w.gate || via != w.via {
			t.Errorf("%s in %s: %q via %q, want %q via %q", w.workspace, w.region, gate, via, w.gate, w.via)
		}
	}
}

// TestAWildcardRowReadByTheOldRuleDecidesNobody (section 6.3: Go before the
// wildcard row): a node from before this change keeps only the rows on, by
// their exact key, and looks a request up by its workspace's id; it reads
// '*' as a workspace's id. No workspace its policy covers can be '*' (the
// explicit list refuses the id), and a key whose workspace were '*' would
// pass the policy's own gate first, so a wildcard row decides nobody there.
func TestAWildcardRowReadByTheOldRuleDecidesNobody(t *testing.T) {
	rows := []store.DecideRow{{Workspace: store.DecideWildcard, Cloud: "gcp", Region: "us-central1", Decide: true}}
	// The copy and the gate before this change (decideCopy.run kept
	// row.Decide rows alone; DecideView.gate looked the exact key up).
	oldOn := map[DecideKey]bool{}
	for _, row := range rows {
		if row.Decide {
			oldOn[DecideKey{row.Workspace, row.Cloud, row.Region}] = true
		}
	}
	in, c := baseInputs(t)
	for _, ws := range append(slices.Clone(in.Policy.FastpathPilotWorkspaces), c.Facts.Key.WorkspaceID, "ws_any") {
		if oldOn[DecideKey{ws, "gcp", "us-central1"}] {
			t.Errorf("the old rule decides %s under the wildcard row", ws)
		}
	}
	// The policy's explicit list cannot name '*'.
	body, err := readTestdata("edge_policy_v1.body.json")
	if err != nil {
		t.Fatal(err)
	}
	from := []byte(`"fastpath_pilot_workspaces":["ws-synthetic-pilot-1",`)
	if bytes.Count(body, from) != 1 {
		t.Fatal("the golden policy has no single pilot list")
	}
	if _, err := routingsnapshot.ParseEdgePolicy(bytes.Replace(body, from,
		[]byte(`"fastpath_pilot_workspaces":["*","ws-synthetic-pilot-1",`), 1)); err == nil {
		t.Fatal("a policy naming '*' in its pilot list was accepted")
	}
	// A key whose workspace were '*' passes the list's gate before the
	// decide row is read: the edge never asks the row for it.
	star := withRead(t, in, func(r *gwstate.Read) { r.Facts.Key.WorkspaceID = store.DecideWildcard })
	star.Decide = DecideView{Rows: oldOn, ReadAt: in.Request.At, MaxAge: 15 * time.Second}
	if got := Decide(star); got.Pass != PassNotPilotWorkspace || got.ForShadow() != nil {
		t.Fatalf("a '*' workspace under the explicit list: %q, decision %v", got.Pass, got.ForShadow() != nil)
	}
}

// TestTheSwitchCopyIsADecisionGate (section 6.1): a workspace the switch
// copy does not enable passes workspace.not_enabled after the compose, so
// the shadow still compares it, and is never admittable; the decide row is
// read first, a stale copy first of all; without a switch copy (no front
// door bound) the gate is not read.
func TestTheSwitchCopyIsADecisionGate(t *testing.T) {
	in, c := baseInputs(t)
	ws := c.Facts.Key.WorkspaceID
	off := func(w string) bool { return w != ws }
	on := func(string) bool { return true }
	wildcard := DecideView{Rows: map[DecideKey]bool{{store.DecideWildcard, "gcp", "us-central1"}: true},
		ReadAt: in.Request.At, MaxAge: 15 * time.Second}
	for _, v := range []struct {
		name    string
		enabled func(string) bool
		view    *DecideView
		want    string
	}{
		{"enabled", on, nil, ""},
		{"no switch copy", nil, nil, ""},
		{"not enabled", off, nil, PassWorkspaceNotEnabled},
		{"not enabled, deciding under the wildcard", off, &wildcard, PassWorkspaceNotEnabled},
		{"not enabled, its row off", off, &DecideView{Rows: map[DecideKey]bool{}, ReadAt: in.Request.At,
			MaxAge: 15 * time.Second}, PassDecideOff},
		{"not enabled, the copy stale", off, &DecideView{}, PassCacheDecide},
	} {
		in := in
		in.Enabled = v.enabled
		if v.view != nil {
			in.Decide = *v.view
		}
		got := Decide(in)
		if got.Pass != v.want || got.DecideGate != v.want || got.ForShadow() == nil {
			t.Errorf("%s: pass %q gate %q decision %v, want %q", v.name, got.Pass, got.DecideGate,
				got.ForShadow() != nil, v.want)
		}
		if _, ok := got.Admit(); ok != (v.want == "") {
			t.Errorf("%s: admittable %v", v.name, ok)
		}
	}
	// Through the decide path: the front door's own copy, asked before any
	// admit; a workspace it does not enable is passed by name, and the
	// front door is never asked.
	h, hc := baseDecide(t)
	h.admitter.mu.Lock()
	h.admitter.off = map[string]bool{hc.Facts.Key.WorkspaceID: true}
	h.admitter.mu.Unlock()
	if rec := h.send(t, hc); rec.Code != http.StatusTeapot {
		t.Fatalf("a workspace not enabled was answered: %d", rec.Code)
	}
	asked, _, _ := h.admitter.seen()
	if got := h.edge.DecideCounts(); got.Passed[PassWorkspaceNotEnabled] != 1 || len(asked) != 0 || got.Answered != 0 {
		t.Fatalf("not enabled: %+v, %d admits", got, len(asked))
	}
	// Enabled again, the same request is answered (the positive control).
	h.admitter.mu.Lock()
	h.admitter.off = nil
	h.admitter.mu.Unlock()
	if rec := h.send(t, hc); rec.Code != http.StatusOK {
		t.Fatalf("enabled: %d", rec.Code)
	}
}

// TestAllModeCoversEveryWorkspaceButTheExcluded: the policy's all mode in
// place of the explicit list, in the keyless gate and in policyPass
// (Covers): every workspace is covered but the exclude list's, which pass
// policy.not_pilot_workspace once the key is read; the explicit list
// empty without all mode covers nobody, and passes before any key is read.
func TestAllModeCoversEveryWorkspaceButTheExcluded(t *testing.T) {
	in, c := baseInputs(t)
	ws := c.Facts.Key.WorkspaceID
	all := func(exclude ...string) *routingsnapshot.EdgePolicy {
		p := *in.Policy
		yes := true
		x := slices.Clone(exclude)
		if x == nil {
			x = []string{}
		}
		p.FastpathPilotAll, p.FastpathPilotExclude, p.FastpathPilotWorkspaces = &yes, &x, []string{}
		return &p
	}
	never := func(string, time.Time) gwstate.Read {
		t.Fatal("the key was read")
		return gwstate.Read{}
	}
	for _, v := range []struct {
		name     string
		policy   *routingsnapshot.EdgePolicy
		keyless  bool
		want     string
		decision bool
	}{
		{"all mode", all(), false, "", true},
		{"all mode, others excluded", all("ws_other_a", "ws_other_b"), false, "", true},
		{"all mode, the workspace excluded", all(ws), false, PassNotPilotWorkspace, false},
		{"the explicit list without the workspace", func() *routingsnapshot.EdgePolicy {
			p := *in.Policy
			p.FastpathPilotWorkspaces = []string{"ws_other"}
			return &p
		}(), false, PassNotPilotWorkspace, false},
		{"the explicit list empty", func() *routingsnapshot.EdgePolicy {
			p := *in.Policy
			p.FastpathPilotWorkspaces = []string{}
			return &p
		}(), true, PassNotPilotWorkspace, false},
	} {
		in := in
		in.Policy = v.policy
		if v.keyless {
			in.Lookup = never
		}
		got := Decide(in)
		if got.Pass != v.want || (got.ForShadow() != nil) != v.decision {
			t.Errorf("%s: pass %q decision %v, want %q", v.name, got.Pass, got.ForShadow() != nil, v.want)
		}
		if !v.keyless && got.Workspace != ws {
			t.Errorf("%s: the key's workspace was not read: %q", v.name, got.Workspace)
		}
	}
}

// TestTheDecidePathCountsEachAnswerByItsSource: an answer under the
// wildcard is counted decided_via wildcard, one under the workspace's own
// row own, in the counts and in the period's line; a pass is counted by
// neither.
func TestTheDecidePathCountsEachAnswerByItsSource(t *testing.T) {
	h, c := baseDecide(t)
	if rec := h.send(t, c); rec.Code != http.StatusOK {
		t.Fatalf("under its own row: %d", rec.Code)
	}
	view := DecideView{Rows: map[DecideKey]bool{{store.DecideWildcard, "gcp", "us-central1"}: true}, ReadAt: c.at(),
		MaxAge: decideStaleness * DecideEvery}
	h.edge.decider.decides.current.Store(&view)
	for range 2 {
		if rec := h.send(t, c); rec.Code != http.StatusOK {
			t.Fatalf("under the wildcard: %d", rec.Code)
		}
	}
	// An off override under the wildcard: passed, counted by neither.
	view = DecideView{Rows: map[DecideKey]bool{{store.DecideWildcard, "gcp", "us-central1"}: true,
		{c.Facts.Key.WorkspaceID, "gcp", "us-central1"}: false}, ReadAt: c.at(), MaxAge: decideStaleness * DecideEvery}
	h.edge.decider.decides.current.Store(&view)
	if rec := h.send(t, c); rec.Code != http.StatusTeapot {
		t.Fatalf("an off override under the wildcard: %d", rec.Code)
	}
	got := h.edge.DecideCounts()
	if !maps.Equal(got.DecidedVia, map[string]int64{store.DecideViaOwn: 1, store.DecideViaWildcard: 2}) ||
		got.Answered != 3 || got.Passed[PassDecideOff] != 1 {
		t.Fatalf("the counts: %+v", got)
	}
	h.edge.decider.flush()
	if logs := h.logs.all(); !strings.Contains(logs, `decided_via={"own":1,"wildcard":2}`) {
		t.Fatalf("the line: %s", logs)
	}
}

// TestDistinctKeysCountsAKeyOnceAndIsBounded: distinct_keys counts the
// lookup hashes of the comparable requests (agree_full and differ) since
// the shadow started, each once however often and in whichever class it
// is seen, never one of another class, and at most maxDistinctKeys; the
// line prints the number only.
func TestDistinctKeysCountsAKeyOnceAndIsBounded(t *testing.T) {
	f := authorizeGoldens(t)
	c := authorizeCaseNamed(t, "answer:app_categories_empty")
	h := newFullEdge(t, f, c, nil, nil)
	sh := h.edge.full
	const key = "4e9c5d738f624dd19d1f443e408c1c87lookuphashfortheshadowtest00000"
	for _, r := range []fullResult{
		{class: ClassAgreeFull, key: key}, {class: ClassAgreeFull, key: key}, {class: ClassDifferAny, key: key},
		{class: ClassStale + ".key", key: "another"}, {class: ClassWouldAdmit + ".403.forbidden", key: "a third"},
		{class: ClassAgreeFull},
	} {
		sh.record("d", r)
	}
	if got := h.edge.DecideShadowCounts().DistinctKeys; got != 1 {
		t.Fatalf("distinct_keys %d, want 1: one key compared three times", got)
	}
	sh.record("d", fullResult{class: ClassDifferAny, key: "a second"})
	if got := h.edge.DecideShadowCounts().DistinctKeys; got != 2 {
		t.Fatalf("distinct_keys %d, want 2", got)
	}
	for i := range maxDistinctKeys + 50 {
		sh.record("d", fullResult{class: ClassAgreeFull, key: fmt.Sprintf("key-%d", i)})
	}
	if got := h.edge.DecideShadowCounts().DistinctKeys; got != maxDistinctKeys {
		t.Fatalf("distinct_keys %d, want the bound %d", got, maxDistinctKeys)
	}
	sh.flush()
	logs := h.logs.all()
	// When the set began (the edge's clock at the shadow's start) is beside
	// it, so a reader can tell a set older than its span.
	if got := h.edge.DecideShadowCounts().DistinctKeysSince; !got.Equal(c.at()) ||
		!strings.Contains(logs, fmt.Sprintf(" distinct_keys_since=%d ", c.at().Unix())) {
		t.Fatalf("distinct_keys_since %v, want the shadow's start %v: %s", got, c.at(), logs)
	}
	if !strings.Contains(logs, fmt.Sprintf(" distinct_keys=%d ", maxDistinctKeys)) || strings.Contains(logs, key) ||
		strings.Contains(logs, "key-1") {
		t.Fatalf("the line: %s", logs)
	}
	// The count is no period's: the next period's line carries it still.
	sh.flush()
	if n := strings.Count(h.logs.all(), fmt.Sprintf(" distinct_keys=%d ", maxDistinctKeys)); n != 2 {
		t.Fatalf("distinct_keys in %d lines, want both", n)
	}
}
