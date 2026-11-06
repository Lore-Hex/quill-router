package auditor

import (
	"context"
	"errors"
	"fmt"
	"maps"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"cloud.google.com/go/spanner"

	"github.com/Lore-Hex/quill-router/fastpath/internal/ring"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store/storetest"
)

// fakeMembers is a view of the members a test changes as it goes.
type fakeMembers struct {
	mu   sync.Mutex
	view ring.View
}

func (m *fakeMembers) View() (ring.View, time.Time) {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.view, time.Now()
}

// serving sets the view to the addresses, each live and serving as an
// owner, but for those leaving.
func (m *fakeMembers) serving(addresses []string, leaving ...string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.view = ring.View{}
	for _, a := range addresses {
		state := store.Serving
		if slices.Contains(leaving, a) {
			state = store.Leaving
		}
		m.view.Members = append(m.view.Members, store.Member{Address: a, Roles: []string{ring.OwnerRole}, State: state,
			Live: true})
	}
}

// fakeFleetStore answers the workspaces due and their checks as the test
// sets them, and notes each workspace's audit, by when its first check
// began on the test's clock.
type fakeFleetStore struct {
	mu       sync.Mutex
	clock    *fakeClock
	due      []store.FleetWorkspace
	dueErr   error
	failing  map[string]error    // a workspace's every check fails with it
	problems map[string][]string // a workspace's identity problems
	// took is how long each check takes, on the test's clock.
	took time.Duration
	// onAudit, when set, runs as each workspace's audit begins.
	onAudit func(n int)
	audits  []string
	at      []time.Time
}

func (s *fakeFleetStore) FleetDue(context.Context, time.Time) ([]store.FleetWorkspace, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return slices.Clone(s.due), s.dueErr
}

func (s *fakeFleetStore) check(workspace string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.clock.advance(s.took)
	return s.failing[workspace]
}

func (s *fakeFleetStore) CheckIdentity(_ context.Context, workspace string) ([]string, error) {
	s.mu.Lock()
	s.audits, s.at = append(s.audits, workspace), append(s.at, s.clock.now())
	n, hook := len(s.audits), s.onAudit
	s.mu.Unlock()
	if hook != nil {
		hook(n)
	}
	if err := s.check(workspace); err != nil {
		return nil, err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.problems[workspace], nil
}

func (s *fakeFleetStore) CheckWinners(_ context.Context, workspace string) ([]string, error) {
	return nil, s.check(workspace)
}

func (s *fakeFleetStore) CheckBookedUsage(_ context.Context, workspace string) ([]string, error) {
	return nil, s.check(workspace)
}

func (s *fakeFleetStore) AuditSums(_ context.Context, workspace string) (store.Sums, error) {
	return store.Sums{}, s.check(workspace)
}

func (s *fakeFleetStore) audited() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return slices.Clone(s.audits)
}

func (s *fakeFleetStore) reset() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.audits, s.at = nil, nil
}

// fakeClock is a test's clock, which a pass's waits advance.
type fakeClock struct {
	mu sync.Mutex
	t  time.Time
}

func (c *fakeClock) now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.t
}

func (c *fakeClock) advance(d time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.t = c.t.Add(d)
}

// fleetFixture is a fleet audit on a fake store and view, its alerts and
// lines kept.
type fleetFixture struct {
	clock   *fakeClock
	store   *fakeFleetStore
	members *fakeMembers
	mu      sync.Mutex
	alerts  []string // kind subject: what
	lines   []string
}

func newFleetFixture() *fleetFixture {
	clock := &fakeClock{t: time.Date(2026, 11, 6, 0, 0, 0, 0, time.UTC)}
	return &fleetFixture{clock: clock, store: &fakeFleetStore{clock: clock}, members: &fakeMembers{}}
}

func (f *fleetFixture) fleet(t *testing.T, self string, changes ...func(*FleetConfig)) *Fleet {
	t.Helper()
	cfg := FleetConfig{Store: f.store, Members: f.members, Self: self, Every: time.Hour, Check: time.Minute,
		Within: 26 * time.Hour, Rate: 5, Deadline: 10 * time.Second, Stale: 3 * time.Hour, Clock: f.clock.now,
		Alert: func(kind, subject, what string) {
			f.mu.Lock()
			defer f.mu.Unlock()
			f.alerts = append(f.alerts, kind+" "+subject+": "+what)
		},
		sleep: func(ctx context.Context, d time.Duration) error {
			if d > 0 {
				f.clock.advance(d)
			}
			return ctx.Err()
		},
		logf: func(format string, args ...any) {
			f.mu.Lock()
			defer f.mu.Unlock()
			f.lines = append(f.lines, fmt.Sprintf(format, args...))
		}}
	for _, change := range changes {
		change(&cfg)
	}
	fl, err := NewFleet(cfg)
	if err != nil {
		t.Fatal(err)
	}
	return fl
}

func (f *fleetFixture) told() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return slices.Clone(f.alerts)
}

func (f *fleetFixture) summaries() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	var out []string
	for _, l := range f.lines {
		if strings.HasPrefix(l, "fastpath fleet audit: ") {
			out = append(out, l)
		}
	}
	return out
}

// TestTheFleetAuditsInItsOrder: leased workspaces first, then those whose
// switch changed within the window, then the rest, each by workspace while
// none has been audited; a pass that reaches Every stops and says so; the
// next begins with the least recently audited of each group, so the rest
// rotate.
func TestTheFleetAuditsInItsOrder(t *testing.T) {
	f := newFleetFixture()
	f.members.serving([]string{"node-a"})
	now, old := f.clock.now(), f.clock.now().Add(-30*24*time.Hour)
	f.store.due = []store.FleetWorkspace{
		{Workspace: "r-c", Enabled: true, ChangedAt: old},
		{Workspace: "r-a", Enabled: true, ChangedAt: old},
		{Workspace: "c-x", Enabled: false, ChangedAt: now.Add(-time.Hour)},
		{Workspace: "l-z", Enabled: true, ChangedAt: old, Leased: true},
		{Workspace: "r-b", Enabled: true, ChangedAt: old},
		{Workspace: "l-y", Enabled: true, ChangedAt: now.Add(-2 * time.Hour), Leased: true},
	}
	// One pass a second at five workspaces a second: five a pass.
	fl := f.fleet(t, "node-a", func(c *FleetConfig) { c.Every = time.Second })
	fl.round(context.Background())
	if got, want := f.store.audited(), []string{"l-y", "l-z", "c-x", "r-a", "r-b"}; !slices.Equal(got, want) {
		t.Fatalf("the first pass audited %v, want %v", got, want)
	}
	if got := f.summaries(); len(got) != 1 || !strings.HasPrefix(got[0],
		"fastpath fleet audit: due=6 audited=5 leased_due=2 leased_audited=2 faults=0 failed=0 stale=0 complete=false ") {
		t.Fatalf("the first pass's summary: %q", got)
	}
	// The pass ran its whole second, so the next is due at once.
	f.store.reset()
	fl.round(context.Background())
	if got, want := f.store.audited(), []string{"l-y", "l-z", "c-x", "r-c", "r-a"}; !slices.Equal(got, want) {
		t.Fatalf("the second pass audited %v, want %v", got, want)
	}
	f.store.reset()
	fl.round(context.Background())
	if got, want := f.store.audited(), []string{"l-y", "l-z", "c-x", "r-b", "r-c"}; !slices.Equal(got, want) {
		t.Fatalf("the third pass audited %v, want %v", got, want)
	}
}

// TestTheFleetAuditKeepsItsRate: each workspace's checks begin a fifth of
// a second after the last's began, at five a second, however long the
// checks take, so twelve workspaces take over two seconds.
func TestTheFleetAuditKeepsItsRate(t *testing.T) {
	for _, took := range []time.Duration{0, 30 * time.Millisecond} {
		f := newFleetFixture()
		f.members.serving([]string{"node-a"})
		f.store.took = took
		for i := range 12 {
			f.store.due = append(f.store.due, store.FleetWorkspace{Workspace: fmt.Sprintf("w-%02d", i), Enabled: true})
		}
		began := f.clock.now()
		f.fleet(t, "node-a").round(context.Background())
		f.store.mu.Lock()
		at := slices.Clone(f.store.at)
		f.store.mu.Unlock()
		if len(at) != 12 || !at[0].Equal(began) {
			t.Fatalf("checks taking %v: %d audits, the first at %v", took, len(at), at)
		}
		for i := 1; i < len(at); i++ {
			if gap := at[i].Sub(at[i-1]); gap != 200*time.Millisecond {
				t.Fatalf("checks taking %v: audit %d began %v after the last", took, i, gap)
			}
		}
		if spent := at[11].Sub(began); spent < 2200*time.Millisecond {
			t.Fatalf("checks taking %v: twelve workspaces in %v", took, spent)
		}
	}
}

// TestTheFleetAuditRunsOnTheKeysOwnerAlone: of two nodes, the one the ring
// picks for FleetKey audits and the other does not; when the owner leaves,
// the other begins at its next round, and the one that left audits nothing
// more; and an owner that loses the key in a pass stops it after the
// workspace under way, telling of no stale workspace, its summary saying
// it was not complete.
func TestTheFleetAuditRunsOnTheKeysOwnerAlone(t *testing.T) {
	ctx := context.Background()
	nodes := []string{"10.0.0.1:8080", "10.0.0.2:8080"}
	f := newFleetFixture()
	f.members.serving(nodes)
	view, _ := f.members.View()
	owner, ok := view.Owner(FleetKey)
	if !ok {
		t.Fatal("no owner")
	}
	first, second := owner.Address, nodes[0]
	if second == first {
		second = nodes[1]
	}
	for i := range 4 {
		f.store.due = append(f.store.due, store.FleetWorkspace{Workspace: fmt.Sprintf("w-%d", i), Enabled: true,
			Leased: true})
	}
	a, b := f.fleet(t, first), f.fleet(t, second)
	a.round(ctx)
	b.round(ctx)
	if got := f.store.audited(); len(got) != 4 || len(f.summaries()) != 1 {
		t.Fatalf("with %s the owner: audits %v, summaries %q", first, got, f.summaries())
	}
	// Once a pass a tenure's Every: a round sooner runs none.
	f.clock.advance(59 * time.Minute)
	a.round(ctx)
	b.round(ctx)
	if got := f.store.audited(); len(got) != 4 || len(f.summaries()) != 1 {
		t.Fatalf("a round within Every: audits %v, summaries %q", got, f.summaries())
	}
	// The owner leaves: the other takes the key at its next round, at once
	// though Every has not passed, and the one that left audits nothing.
	f.store.reset()
	f.members.serving(nodes, first)
	f.clock.advance(time.Hour)
	a.round(ctx)
	if got := f.store.audited(); len(got) != 0 {
		t.Fatalf("the node that left audited %v", got)
	}
	b.round(ctx)
	if got := f.store.audited(); len(got) != 4 || len(f.summaries()) != 2 {
		t.Fatalf("the new owner: audits %v, summaries %q", got, f.summaries())
	}
	// The key moves back in the new owner's next pass, after its second
	// workspace's checks began: it stops there.
	f.store.reset()
	f.store.onAudit = func(n int) {
		if n == 2 {
			f.members.serving(nodes)
		}
	}
	f.clock.advance(time.Hour)
	b.round(ctx)
	f.store.onAudit = nil
	if got := f.store.audited(); len(got) != 2 {
		t.Fatalf("a pass whose key moved audited %v", got)
	}
	sums := f.summaries()
	if len(sums) != 3 || !strings.Contains(sums[2], "due=4 audited=2 ") || !strings.Contains(sums[2], "complete=false") {
		t.Fatalf("the moved pass's summary: %q", sums)
	}
	// The first owner takes it again, afresh: a pass at once. It loses it
	// a minute later and takes it back a minute after that, and passes at
	// once again, though Every has not passed since its last: what it
	// remembers is its tenure's.
	f.store.reset()
	a.round(ctx)
	if got := f.store.audited(); len(got) != 4 {
		t.Fatalf("the key's owner again audited %v", got)
	}
	f.members.serving(nodes, first)
	f.clock.advance(time.Minute)
	a.round(ctx)
	f.members.serving(nodes)
	f.clock.advance(time.Minute)
	f.store.reset()
	a.round(ctx)
	if got := f.store.audited(); len(got) != 4 {
		t.Fatalf("the key taken back within Every: audited %v", got)
	}
	if got := f.told(); len(got) != 0 {
		t.Fatalf("alerts %q", got)
	}
}

// TestAStaleLeasedWorkspaceIsTold: a leased workspace whose checks keep
// failing is told once three hours have passed since it was first seen
// leased, and at each pass after while it stays so; a workspace audited, or
// one whose lease ends, is not; and a node that takes the key over, or
// takes it back, counts from its own tenure's first sight.
func TestAStaleLeasedWorkspaceIsTold(t *testing.T) {
	ctx := context.Background()
	f := newFleetFixture()
	f.members.serving([]string{"node-a", "node-b"})
	view, _ := f.members.View()
	owner, _ := view.Owner(FleetKey)
	other := "node-a"
	if other == owner.Address {
		other = "node-b"
	}
	f.store.due = []store.FleetWorkspace{{Workspace: "broken", Enabled: true, Leased: true},
		{Workspace: "fine", Enabled: true, Leased: true}, {Workspace: "idle", Enabled: true}}
	f.store.failing = map[string]error{"broken": errors.New("unavailable"), "idle": errors.New("unavailable")}
	fl := f.fleet(t, owner.Address)
	for hour := range 3 {
		fl.round(ctx)
		if got := f.told(); len(got) != 0 {
			t.Fatalf("hour %d: told %q", hour, got)
		}
		f.clock.advance(time.Hour)
	}
	fl.round(ctx)
	got := f.told()
	if len(got) != 1 || !strings.HasPrefix(got[0], "fleet_audit_stale broken: the fleet audit has not audited the "+
		"workspace, which has a lease open or draining, for 3h0m0s") {
		t.Fatalf("at three hours: told %q", got)
	}
	sums := f.summaries()
	if !strings.Contains(sums[len(sums)-1], "due=3 audited=1 leased_due=2 leased_audited=1 faults=0 failed=2 stale=1 ") {
		t.Fatalf("the stale pass's summary: %q", sums[len(sums)-1])
	}
	// Audited again: no longer stale.
	f.store.failing = nil
	f.clock.advance(time.Hour)
	fl.round(ctx)
	if got := f.told(); len(got) != 1 {
		t.Fatalf("an audited workspace was told stale: %q", got)
	}
	// Failing again, it counts from that audit; its lease ending forgets it.
	f.store.failing = map[string]error{"broken": errors.New("unavailable")}
	for range 2 {
		f.clock.advance(time.Hour)
		fl.round(ctx)
	}
	f.store.due[0].Leased = false
	f.clock.advance(time.Hour)
	fl.round(ctx)
	if got := f.told(); len(got) != 1 {
		t.Fatalf("a workspace whose lease ended was told stale: %q", got)
	}
	// Leased again and failing: the key moves to the other node two hours
	// after this one first sees it so, and back an hour later. Each counts
	// from its own tenure's first sight: the other is not told at its
	// first pass, nor this one on taking the key back, three hours after
	// it first saw it; it is told three hours into its new tenure.
	f.store.due[0].Leased = true
	for range 3 {
		f.clock.advance(time.Hour)
		fl.round(ctx)
	}
	f.members.serving([]string{"node-a", "node-b"}, owner.Address)
	fl.round(ctx)
	next := f.fleet(t, other)
	f.clock.advance(time.Hour)
	next.round(ctx)
	f.members.serving([]string{"node-a", "node-b"})
	next.round(ctx)
	for hour := range 3 {
		f.clock.advance(time.Hour)
		fl.round(ctx)
		if got := f.told(); len(got) != 1 {
			t.Fatalf("hour %d of the tenure taken back: told %q", hour, got)
		}
	}
	f.clock.advance(time.Hour)
	fl.round(ctx)
	if got := f.told(); len(got) != 2 || !strings.HasPrefix(got[1], "fleet_audit_stale broken: ") {
		t.Fatalf("three hours into the tenure taken back: told %q", got)
	}
}

// TestEveryPassWritesItsSummary: a pass with nothing due writes its line,
// complete, so the line's absence means no pass ran, not that nothing was
// due; a read of the workspaces due that fails writes none and is tried at
// the next round, not an Every later.
func TestEveryPassWritesItsSummary(t *testing.T) {
	ctx := context.Background()
	f := newFleetFixture()
	f.members.serving([]string{"node-a"})
	f.store.dueErr = errors.New("unavailable")
	fl := f.fleet(t, "node-a")
	fl.round(ctx)
	if got := f.summaries(); len(got) != 0 {
		t.Fatalf("a failed read wrote %q", got)
	}
	f.mu.Lock()
	logged := slices.Clone(f.lines)
	f.mu.Unlock()
	if len(logged) != 1 || logged[0] != "auditor: fleet audit: reading the workspaces due failed: other" {
		t.Fatalf("a failed read logged %q", logged)
	}
	f.store.dueErr = nil
	f.clock.advance(time.Minute)
	fl.round(ctx)
	got := f.summaries()
	if len(got) != 1 || got[0] != "fastpath fleet audit: due=0 audited=0 leased_due=0 leased_audited=0 faults=0 "+
		"failed=0 stale=0 complete=true took_ms=0" {
		t.Fatalf("a pass with nothing due: %q", got)
	}
	// A node that does not own the key writes nothing.
	f.fleet(t, "node-b").round(ctx)
	if got := f.summaries(); len(got) != 1 {
		t.Fatalf("a node not the owner wrote %q", got)
	}
}

// TestAFaultNamesTheWorkspaceAndTheCheck: a check's problems are a fault of
// the workspace naming the check, its first three problems and how many
// more; a store's refusal on what the rows hold is a fault, any other
// failure is not.
func TestAFaultNamesTheWorkspaceAndTheCheck(t *testing.T) {
	f := newFleetFixture()
	f.members.serving([]string{"node-a"})
	f.store.due = []store.FleetWorkspace{{Workspace: "many", Enabled: true}, {Workspace: "rows", Enabled: true},
		{Workspace: "down", Enabled: true}}
	f.store.problems = map[string][]string{"many": {"p1", "p2", "p3", "p4", "p5"}}
	f.store.failing = map[string]error{"rows": fmt.Errorf("reading: %w", store.ErrCreditRowsIncomplete),
		"down": errors.New("unavailable")}
	f.fleet(t, "node-a").round(context.Background())
	got := f.told()
	want := []string{
		"fleet_audit_fault many: the fleet audit's identity check: p1; p2; p3 (and 2 more)",
	}
	for _, check := range []string{"identity", "winners", "booked_usage", "sums"} {
		want = append(want, "fleet_audit_fault rows: the fleet audit's "+check+" check: reading: "+
			store.ErrCreditRowsIncomplete.Error())
	}
	if !slices.Equal(got, want) {
		t.Fatalf("told %q, want %q", got, want)
	}
	if sums := f.summaries(); len(sums) != 1 || !strings.Contains(sums[0], "due=3 audited=2 ") ||
		!strings.Contains(sums[0], "faults=5 failed=1 ") {
		t.Fatalf("summary %q", sums)
	}
}

// TestTheFleetAuditFindsSeededFaults, on the emulator: of four workspaces
// with a lease and a booking, one whose credit row reserves more than its
// lease holds, one whose lease decides an authorization twice and one
// whose usage is below what its lease booked each raise their own check's
// fault, and the one whose rows keep them raises none.
func TestTheFleetAuditFindsSeededFaults(t *testing.T) {
	if emulator == nil {
		t.Skip(skipped)
	}
	ctx := context.Background()
	db, err := emulator.Database(ctx, storetest.UniqueID("fleet")[:20], nil)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	s, err := store.New(db, store.Config{LiveFor: time.Hour, Window: 30 * time.Second, Skew: 2 * time.Second,
		PublishDeadline: 5 * time.Second, MaxLife: 5 * time.Minute, Grace: time.Minute, Allowance: 1_000_000,
		RequiredTier: 3})
	if err != nil {
		t.Fatal(err)
	}
	exec := func(sql string, params map[string]any) {
		t.Helper()
		if _, err := db.ReadWriteTransaction(ctx, func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
			_, err := txn.Update(ctx, spanner.Statement{SQL: sql, Params: params})
			return err
		}); err != nil {
			t.Fatal(err)
		}
	}
	// booked is a workspace with a lease of 30 and one settle of 10 booked
	// with its winner; twice books the same winner again.
	booked := func(twice bool) string {
		t.Helper()
		ws := storetest.UniqueID("ws")
		if _, err := db.Apply(ctx, []*spanner.Mutation{storetest.Enabled(ws), storetest.Member(grantee.Node, grantee.Epoch),
			spanner.InsertMap("tr_credit_balance", map[string]any{"workspace_id": ws, "shard": int64(0),
				"total_credits": int64(100_000), "trust_tier": int64(3)})}); err != nil {
			t.Fatal(err)
		}
		ref := store.LeaseRef{Workspace: ws, LeaseID: store.NewLeaseID()}
		got, err := s.Grant(ctx, store.GrantRequest{Workspace: ws, LeaseID: ref.LeaseID, Region: "us-central1",
			Owner: grantee, Amount: 30, KeyStatusVersion: 7})
		if err != nil || got.Refused != "" {
			t.Fatalf("the grant: %+v %v", got, err)
		}
		version := int64(0)
		for seq := int64(1); seq <= 1 || (twice && seq <= 2); seq++ {
			results, _, err := s.Commit(ctx, []store.CommitRequest{{Ref: ref, ReadVersion: version, AppliedSeq: seq,
				Money:   []store.MoneyOp{store.Book(10, 0)},
				Winners: []store.Winner{{AuthorizationID: "auth-1", Kind: "settle", Charge: 10, RecordID: "rec-1"}}}})
			if err != nil || len(results) != 1 || results[0].Refused != "" {
				t.Fatalf("the commit at %d: %+v %v", version, results, err)
			}
			version = results[0].NewVersion
		}
		return ws
	}
	clean, identity, winners, usage := booked(false), booked(false), booked(true), booked(false)
	exec(`UPDATE tr_credit_balance SET reserved = reserved + 5 WHERE workspace_id = @w`, map[string]any{"w": identity})
	exec(`UPDATE tr_credit_balance SET total_usage = 0 WHERE workspace_id = @w`, map[string]any{"w": usage})

	f := newFleetFixture()
	f.members.serving([]string{"node-a"})
	fl := f.fleet(t, "node-a", func(c *FleetConfig) { c.Store, c.Clock, c.sleep = s, time.Now, nil })
	fl.round(ctx)
	byWorkspace := map[string][]string{}
	for _, alert := range f.told() {
		kind, rest, _ := strings.Cut(alert, " ")
		ws, what, _ := strings.Cut(rest, ": ")
		if kind != KindFleetAuditFault {
			t.Fatalf("an alert of kind %q: %q", kind, alert)
		}
		byWorkspace[ws] = append(byWorkspace[ws], what)
	}
	want := map[string]string{
		identity: "the fleet audit's identity check: shard 0 reserves 25; its live leases' donors hold 20",
		winners:  "the fleet audit's winners check: lease ",
		usage:    "the fleet audit's booked_usage check: shard 0 books 0 of usage, below the 10",
	}
	if len(byWorkspace) != len(want) {
		t.Fatalf("faults %v, want one each for %v", byWorkspace, slices.Collect(maps.Keys(want)))
	}
	for ws, prefix := range want {
		if got := byWorkspace[ws]; len(got) != 1 || !strings.HasPrefix(got[0], prefix) {
			t.Errorf("%s: faults %q, want one beginning %q", ws, got, prefix)
		}
	}
	if got := byWorkspace[winners]; len(got) == 1 && !strings.Contains(got[0], "decides authorization auth-1 twice") {
		t.Errorf("the duplicate winner's fault: %q", got)
	}
	if _, ok := byWorkspace[clean]; ok {
		t.Errorf("the clean workspace: %q", byWorkspace[clean])
	}
	if sums := f.summaries(); len(sums) != 1 || !strings.HasPrefix(sums[0],
		"fastpath fleet audit: due=4 audited=4 leased_due=4 leased_audited=4 faults=3 failed=0 stale=0 complete=true ") {
		t.Fatalf("summary %q", sums)
	}
}
