package auditor

import (
	"errors"
	"fmt"
	"math/rand"
	"os"
	"reflect"
	"slices"
	"strconv"
	"strings"
	"testing"
	"time"

	"cloud.google.com/go/spanner"

	ac "github.com/Lore-Hex/quill-router/fastpath/internal/auditorcommit"
	"github.com/Lore-Hex/quill-router/fastpath/internal/record"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// The member walks run AuditorCommit's shadow (internal/auditorcommit) at
// random, a member of this package beside each of the shadow's members.
// Each step a member takes in the shadow, the walk gives the real member
// the same record, drain-log row, load or commit, and after it holds the
// real member's memory to the shadow's: progress, version, the audit's sum,
// open holds, winners, whether it loaded them, what it would book, S, the
// fault and whether it has anything to commit. Each commit is held to the
// rows the shadow's Commit stores.
var walkConfig = ac.Config{
	Auths: []string{"a1", "a2"}, Members: []string{"m1", "m2"}, MaxSeq: 4, MaxSnap: 2, MaxDup: 2, MaxAhead: 1,
	MaxLate: 1, MaxRaise: 2, MaxAssign: 2, MaxCrash: 2, MaxAppend: 2, Lying: true,
}

const (
	walkSteps = 160
	walkSkew  = 2 * time.Second
)

// walkWeights: the lease drains, and members crash, less often than the
// shadow's other steps, so that walks spend longer on an open lease.
var walkWeights = map[string]int{"MarkDraining": 1, "Crash": 1}

const walkWeight = 4

// TestTheMemberWalksWithAuditorCommit runs FASTPATH_MEMBER_WALKS walks
// (default 3000) from FASTPATH_WALK_SEED (default a fixed seed).
func TestTheMemberWalksWithAuditorCommit(t *testing.T) {
	walks, seed := 3000, int64(20261008)
	if v := os.Getenv("FASTPATH_MEMBER_WALKS"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n < 1 {
			t.Fatalf("FASTPATH_MEMBER_WALKS=%q", v)
		}
		walks = n
	}
	if v := os.Getenv("FASTPATH_WALK_SEED"); v != "" {
		n, err := strconv.ParseInt(v, 10, 64)
		if err != nil {
			t.Fatalf("FASTPATH_WALK_SEED=%q", v)
		}
		seed = n
	}
	t.Logf("FASTPATH_WALK_SEED=%d", seed)
	seen := map[string]int{}
	for i := range walks {
		w := newMemberWalk(t, seed+int64(i))
		w.run()
		for k, n := range w.seen {
			seen[k] += n
		}
	}
	keys := make([]string, 0, len(seen))
	for k := range seen {
		keys = append(keys, k)
	}
	slices.Sort(keys)
	var b strings.Builder
	for _, k := range keys {
		fmt.Fprintf(&b, " %s=%d", k, seen[k])
	}
	t.Logf("steps:%s", b.String())
	for _, k := range []string{"Load open", "Load draining", "LoadWinners", "ApplyRecord heartbeat",
		"ApplyRecord settle", "ApplyRecord refund", "ApplyRecord ckpt", "a wrong checkpoint", "SkipRecord", "SkipRecord past S", "Gap", "ApplyTick", "ApplyTick behind",
		"ApplyTick with S known", "ApplyRow", "ApplyRow settle", "ApplyRow reap", "a row with a winner",
		"Commit", "Commit with S", "Commit with a fault", "Commit from the drain log", "Commit found draining",
		"Reread", "Crash", "Close"} {
		if seen[k] == 0 {
			t.Errorf("no walk made a %s", k)
		}
	}
}

type memberWalk struct {
	t       *testing.T
	c       ac.Config
	rng     *rand.Rand
	seed    int64
	state   ac.State
	members [ac.MaxMembers]*Lease
	// tick is the last tick a commit stored, which a load reads.
	tick    int64
	history []string
	seen    map[string]int
}

func newMemberWalk(t *testing.T, seed int64) *memberWalk {
	t.Helper()
	rng := rand.New(rand.NewSource(seed))
	c := walkConfig
	c.Grant = 1 + rng.Intn(4)
	c.Holder = rng.Intn(len(c.Members))
	if err := c.Validate(); err != nil {
		t.Fatal(err)
	}
	return &memberWalk{t: t, c: c, rng: rng, seed: seed, state: c.Init(), seen: map[string]int{}}
}

func (w *memberWalk) fatalf(format string, args ...any) {
	w.t.Helper()
	w.t.Fatalf("member walk %d (grant %d, holder %s), after %s: %s", w.seed, w.c.Grant, w.c.Members[w.c.Holder],
		strings.Join(w.history, " "), fmt.Sprintf(format, args...))
}

func (w *memberWalk) run() {
	w.t.Helper()
	for range walkSteps {
		next := w.c.Next(w.state)
		if len(next) == 0 {
			return
		}
		tr := w.choose(next)
		w.history = append(w.history, tr.Action)
		if !w.step(tr) {
			return
		}
		w.state = tr.To
		for m := range int8(len(w.c.Members)) {
			w.compare(m)
		}
	}
}

func (w *memberWalk) choose(next []ac.Transition) ac.Transition {
	weight := func(tr ac.Transition) int {
		name, _ := label(tr.Action)
		if n, ok := walkWeights[name]; ok {
			return n
		}
		return walkWeight
	}
	total := 0
	for _, tr := range next {
		total += weight(tr)
	}
	n := w.rng.Intn(total)
	for _, tr := range next {
		if n -= weight(tr); n < 0 {
			return tr
		}
	}
	panic("unreachable")
}

func label(action string) (string, []string) {
	i := strings.IndexByte(action, '(')
	if i < 0 {
		return action, nil
	}
	return action[:i], strings.Split(action[i+1:len(action)-1], ",")
}

func index(names []string, name string) int8 {
	for i, n := range names {
		if n == name {
			return int8(i)
		}
	}
	return -1
}

var stateNames = map[int8]string{ac.Open: "open", ac.Draining: "draining", ac.Closed: "closed"}

// step gives the real member the step a shadow member takes, from the state
// before it, and reports whether the walk goes on: it ends at the close,
// which this part of the auditor does not make.
func (w *memberWalk) step(tr ac.Transition) bool {
	w.t.Helper()
	from := w.state
	name, args := label(tr.Action)
	if len(args) == 0 || index(w.c.Members, args[0]) < 0 {
		return true // the owner's, the log's, a front door's or the lease's
	}
	m := index(w.c.Members, args[0])
	l := w.members[m]
	switch name {
	case "Load":
		got, err := Load(ref, w.loaded(from), walkSkew)
		if err != nil {
			w.fatalf("member %s's load: %v", args[0], err)
		}
		w.members[m] = got
		w.seen["Load "+stateNames[from.St]]++
	case "LoadWinners":
		if err := l.LoadWinners(w.leaseRow(from), w.packs(from)); err != nil {
			w.fatalf("member %s's winners: %v", args[0], err)
		}
		w.seen[name]++
	case "ApplyRecord", "SkipRecord", "Gap":
		r := from.Log[from.Pos[m]-1]
		want := map[string]Outcome{"ApplyRecord": Applied, "SkipRecord": Skipped, "Gap": Gap}[name]
		if got, err := l.Apply(w.record(r), start); err != nil || got != want {
			w.fatalf("member %s applies %+v: %v %v, and the shadow's step is %s", args[0], r, got, err, name)
		}
		w.count(name, r, from.Mem[m])
	case "ApplyTick":
		w.applyTick(m, from)
	case "ApplyRow":
		r := from.Drain[from.Dpos[m]-1]
		if err := l.ApplyRow(w.row(r)); err != nil {
			w.fatalf("member %s applies row %+v: %v", args[0], r, err)
		}
		w.seen[name]++
		w.seen["ApplyRow "+map[int8]string{ac.KSettle: "settle", ac.KReap: "reap"}[r.K]]++
		if from.Mem[m].Win[r.A] != ac.NoWin {
			w.seen["a row with a winner"]++
		}
	case "Commit":
		w.commit(m, from, tr.To)
	case "Reread":
		if l != nil {
			if err := l.Committed(store.CommitResult{Ref: ref, Refused: store.RefusedVersion}); !errors.Is(err, ErrReread) {
				w.fatalf("member %s's refused commit: %v", args[0], err)
			}
		}
		w.members[m] = nil
		w.seen[name]++
	case "Crash":
		w.members[m] = nil
		w.seen[name]++
	case "Close":
		w.seen[name]++
		return false
	}
	return true
}

func (w *memberWalk) count(name string, r ac.Rec, mem ac.Mem) {
	w.seen[name]++
	switch {
	case name == "ApplyRecord":
		w.seen["ApplyRecord "+map[int8]string{ac.KHb: "heartbeat", ac.KSettle: "settle", ac.KRefund: "refund",
			ac.KCkpt: "ckpt"}[r.K]]++
		if r.K == ac.KCkpt && r.C != mem.Osum {
			w.seen["a wrong checkpoint"]++
		}
	case name == "SkipRecord" && mem.S != ac.NoS && r.Seq > mem.Prog:
		w.seen["SkipRecord past S"]++
	}
}

// applyTick gives the member the fence tick. One that loaded the lease open
// has not read its fence: it reads the lease's row, as the auditor's
// runtime does when Apply answers Behind, and applies the tick again.
func (w *memberWalk) applyTick(m int8, from ac.State) {
	w.t.Helper()
	l := w.members[m]
	r := tick(1, fence.Add(walkSkew))
	got, err := l.Apply(r, start)
	if err == nil && got == Behind {
		w.seen["ApplyTick behind"]++
		if err := l.Drained(w.leaseRow(from)); err != nil {
			w.fatalf("member %s reads the fence: %v", w.c.Members[m], err)
		}
		got, err = l.Apply(r, start)
	}
	if err != nil || (got != Applied && got != Skipped) {
		w.fatalf("member %s applies the fence tick: %v %v", w.c.Members[m], got, err)
	}
	w.seen["ApplyTick"]++
	if from.Mem[m].S != ac.NoS {
		w.seen["ApplyTick with S known"]++
	}
}

// commit holds member m's commit to the shadow's Commit: from the state
// before it, the stored rows after it.
func (w *memberWalk) commit(m int8, from, to ac.State) {
	w.t.Helper()
	l, mem := w.members[m], from.Mem[m]
	req := l.Request()
	w.seen["Commit"]++
	if req.Ref != ref || req.ReadVersion != int64(mem.Ver) || req.AppliedSeq != int64(to.Prog) ||
		req.AuditOsum != int64(to.Osum) || (req.AuditFault != nil) != mem.Fault || req.LastTick < w.tick {
		w.fatalf("member %s commits %+v, and the shadow's commit stores %+v from %+v", w.c.Members[m], req, to, mem)
	}
	var booked int64
	for _, op := range req.Money {
		if !op.Book || op.ShortfallTotal != 0 {
			w.fatalf("member %s commits money %+v", w.c.Members[m], req.Money)
		}
		booked += op.Charge
	}
	if booked != int64(mem.Dbooked) || int64(to.Booked) != int64(from.Booked)+booked {
		w.fatalf("member %s books %d, and the shadow's member %d", w.c.Members[m], booked, mem.Dbooked)
	}
	switch {
	case req.Boundary != nil:
		if from.S != ac.NoS || to.S != int8(req.Boundary.S) {
			w.fatalf("member %s stores S %+v, and the shadow's S was %d and is %d", w.c.Members[m], req.Boundary,
				from.S, to.S)
		}
		w.seen["Commit with S"]++
	case to.S != from.S:
		w.fatalf("the shadow's commit stores S %d, and member %s's stores none", to.S, w.c.Members[m])
	}
	if req.AuditFault != nil {
		w.seen["Commit with a fault"]++
	}
	// The winners: the shadow's new ones, each as decided.
	var won [ac.MaxAuths]bool
	for _, win := range req.Winners {
		a := index(w.c.Auths, win.AuthorizationID)
		if a < 0 || won[a] || from.Win[a] != ac.NoWin || !reflect.DeepEqual(win, w.winner(to.Win[a])) {
			w.fatalf("member %s commits winner %+v, and the shadow's is %+v", w.c.Members[m], win, to.Win[a])
		}
		won[a] = true
		if win.FromDrain {
			w.seen["Commit from the drain log"]++
		}
	}
	// The holds: the rows the commit leaves, those before it with the
	// changed ones put and the decided ones gone, are the shadow's.
	holds := from.Holds
	for _, h := range req.PutHolds {
		a := index(w.c.Auths, h.AuthorizationID)
		if a < 0 || !h.RunningCharge.Valid {
			w.fatalf("member %s puts hold %+v", w.c.Members[m], h)
		}
		holds[a] = int8(h.RunningCharge.Int64)
	}
	for a := range w.c.Auths {
		if won[a] {
			holds[a] = ac.NoHold
		}
		if (to.Win[a] != ac.NoWin && from.Win[a] == ac.NoWin) != won[a] {
			w.fatalf("member %s commits winners %+v, and the shadow's are %+v", w.c.Members[m], req.Winners, to.Win)
		}
	}
	if holds != to.Holds {
		w.fatalf("member %s's commit leaves holds %v, and the shadow's %v", w.c.Members[m], holds, to.Holds)
	}
	state := stateNames[from.St]
	if state != "open" {
		w.seen["Commit found draining"]++
	}
	if err := l.Committed(store.CommitResult{Ref: ref, NewVersion: int64(to.Ver), State: state}); err != nil {
		w.fatalf("member %s's commit: %v", w.c.Members[m], err)
	}
	w.tick = req.LastTick
}

// compare holds the real member's memory to the shadow's.
func (w *memberWalk) compare(m int8) {
	w.t.Helper()
	mem, l := w.state.Mem[m], w.members[m]
	if !mem.Loaded {
		if l != nil {
			w.fatalf("member %s has a lease in memory, and the shadow's has none", w.c.Members[m])
		}
		return
	}
	if l == nil {
		w.fatalf("member %s has no lease in memory, and the shadow's has %+v", w.c.Members[m], mem)
	}
	var booked int64
	for _, op := range l.money {
		booked += op.Charge
	}
	switch {
	case l.version != int64(mem.Ver), l.applied != int64(mem.Prog), l.osum != int64(mem.Osum),
		l.dirty != mem.Dirty, l.winnersLoaded != mem.WL, (l.fault != nil) != mem.Fault, booked != int64(mem.Dbooked),
		l.sKnown != (mem.S != ac.NoS), l.sKnown && l.s != int64(mem.S):
		w.fatalf("member %s: version %d, progress %d, sum %d, dirty %v, winners loaded %v, fault %v, booking %d, S %v %d; "+
			"the shadow's %+v", w.c.Members[m], l.version, l.applied, l.osum, l.dirty, l.winnersLoaded, l.fault != nil, booked,
			l.sKnown, l.s, mem)
	}
	for a, auth := range w.c.Auths {
		h := l.holds[auth]
		if (h != nil) != (mem.Holds[a] != ac.NoHold) || (h != nil && h.row.RunningCharge.Int64 != int64(mem.Holds[a])) {
			w.fatalf("member %s holds %s as %+v, and the shadow's at %d", w.c.Members[m], auth, h, mem.Holds[a])
		}
		if l.winners[auth] != (mem.Win[a] != ac.NoWin) {
			w.fatalf("member %s's winner of %s is %v, and the shadow's %+v", w.c.Members[m], auth, l.winners[auth],
				mem.Win[a])
		}
	}
}

// record is the real record of a shadow's log record: a heartbeat's
// running charge is its snapshot index, the first its first.
func (w *memberWalk) record(r ac.Rec) record.Record {
	w.t.Helper()
	var out record.Record
	switch r.K {
	case ac.KHb:
		out = hb(int64(r.Seq), w.c.Auths[r.A], int64(r.C)+1, int64(r.C))
	case ac.KSettle:
		out = settle(int64(r.Seq), w.c.Auths[r.A], int64(r.C), 0)
	case ac.KRefund:
		out = refund(int64(r.Seq), w.c.Auths[r.A])
	case ac.KCkpt:
		out = ckpt(int64(r.Seq), record.CheckpointOf{Consumed: int64(r.C), KeyStatus: 7})
	default:
		w.fatalf("a log record %+v", r)
	}
	if err := out.Validate(); err != nil {
		w.fatalf("the walk's record %+v: %v", out, err)
	}
	return out
}

// row is the drain-log row of a shadow's: its record ID is its index.
func (w *memberWalk) row(r ac.Rec) store.DrainRow {
	kind := map[int8]string{ac.KSettle: "settle", ac.KReap: "reap"}[r.K]
	if kind == "" {
		w.fatalf("a drain row %+v", r)
	}
	return store.DrainRow{AuthorizationID: w.c.Auths[r.A], RecordID: fmt.Sprintf("d%d", r.Idx), Kind: kind,
		Charge: int64(r.C)}
}

// winner is the winner a commit stores for a shadow's: an owner terminal's
// record ID is its sequence number, a drain row's its index.
func (w *memberWalk) winner(r ac.Rec) store.Winner {
	if r.Idx > 0 {
		row := w.row(r)
		return store.Winner{AuthorizationID: row.AuthorizationID, Kind: row.Kind, Charge: row.Charge, FromDrain: true,
			RecordID: row.RecordID}
	}
	return store.Winner{AuthorizationID: w.c.Auths[r.A], Kind: map[int8]string{ac.KSettle: "settle",
		ac.KRefund: "refund"}[r.K], Charge: int64(r.C), RecordID: fmt.Sprintf("o%d", r.Seq)}
}

// leaseRow is the lease's row as a member reads it.
func (w *memberWalk) leaseRow(s ac.State) store.Lease {
	l := store.Lease{Ref: ref, State: stateNames[s.St], Granted: int64(w.c.Grant), Allocation: int64(s.Alloc),
		CommitVersion: int64(s.Ver), AppliedSeq: int64(s.Prog), AuditOsum: int64(s.Osum), LastTick: w.tick}
	if s.St != ac.Open {
		l.FenceTime = spanner.NullTime{Time: fence, Valid: true}
	}
	if s.S != ac.NoS {
		l.BoundarySeq = spanner.NullInt64{Int64: int64(s.S), Valid: true}
	}
	return l
}

// packs are the stored winners, one pack.
func (w *memberWalk) packs(s ac.State) []store.Pack {
	var p store.Pack
	for a := range w.c.Auths {
		if s.Win[a] != ac.NoWin {
			p.Winners = append(p.Winners, w.winner(s.Win[a]))
		}
	}
	return []store.Pack{p}
}

// loaded is what store.Load reads: the row, the open holds, and the
// winners once the lease is not open.
func (w *memberWalk) loaded(s ac.State) store.Loaded {
	out := store.Loaded{Lease: w.leaseRow(s)}
	for a, auth := range w.c.Auths {
		if s.Holds[a] != ac.NoHold {
			out.Holds = append(out.Holds, store.HoldRow{AuthorizationID: auth, Estimate: 100, Deadline: deadline,
				RunningCharge: spanner.NullInt64{Int64: int64(s.Holds[a]), Valid: true}})
		}
	}
	if s.St != ac.Open {
		out.Packs = w.packs(s)
	}
	return out
}
