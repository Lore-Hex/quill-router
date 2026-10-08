//go:build !race

package store

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"math/rand"
	"os"
	"reflect"
	"sort"
	"strconv"
	"strings"
	"testing"
	"time"

	"cloud.google.com/go/spanner"

	ac "github.com/Lore-Hex/quill-router/fastpath/internal/auditorcommit"
)

// The walks drive the store with random runs of AuditorCommit's shadow
// (proofs/AuditorCommit.tla, internal/auditorcommit). At each step the
// shadow takes, the store does what that step does to Spanner, from what the
// step's member knows; after it, the store's rows, read back, are the
// shadow's Spanner variables, the workspace keeps section 4.7's identity,
// and the shadow keeps its invariants, at bounds larger than TLC's. A
// member's write that the shadow does not allow, for a reason the store
// holds (the version, a gap, the lease's state, S, a drain row, a row past
// the member's read), is tried too: the store must refuse it, and write
// nothing.
//
// The shadow's numbers map onto the rows so (spike plan §4):
//
//   - Ver, Prog, Osum and S are commit_version, applied_seq, audit_osum and
//     boundary_seq; Gap and Alert are gap_seq and audit_fault_seq, set.
//   - Booked is consumed plus fault_usage, and Alloc, the grant plus the
//     raises less what is booked, is allocation plus returned less both,
//     since the close returns the remainder and the shadow's Close keeps it.
//   - Holds are the hold rows' running charges; Win is the first pack that
//     names each authorization; Drain is the drain log in commit order. An
//     owner record's winner has record ID o<seq>, a drain row d<index>.
//   - Raise is the owner's shortfall write; MarkDraining the owner's or the
//     auditor's draining write; FrontDoorAppend an append whose charge is
//     its estimate, so it raises nothing.

// walkConfig is the shadow's bounds for the walks: every bound the State
// holds, beyond the configurations TLC checks.
var walkConfig = ac.Config{
	Auths: []string{"a1", "a2"}, Members: []string{"m1", "m2"}, MaxSeq: 4, MaxSnap: 2, MaxDup: 2, MaxAhead: 1,
	MaxLate: 1, MaxRaise: 2, MaxAssign: 2, MaxCrash: 2, MaxAppend: 2, Lying: true,
}

const (
	walkEstimate  = 100
	walkSteps     = 160
	probesPerKind = 1
)

// walkWeights favors the steps that touch Spanner; every other step weighs
// 1. A walk still takes only steps the shadow allows.
var walkWeights = map[string]int{
	"Commit": 6, "Gap": 12, "Reap": 6, "Close": 6, "Load": 3, "LoadWinners": 3, "ApplyRow": 3,
	"FrontDoorAppend": 2, "Raise": 2,
}

// writes are the steps that write to Spanner. No other step changes the
// shadow's Spanner variables, which the walk checks.
var writes = map[string]bool{
	"Raise": true, "MarkDraining": true, "FrontDoorAppend": true, "Gap": true, "Reap": true, "Commit": true, "Close": true,
}

// The holds' deadline, and the fence tick's time T, past it and the grace:
// the walks' reaps are at T.
var (
	walkDeadline = time.Date(2026, 10, 8, 0, 0, 0, 0, time.UTC)
	walkTick     = time.Date(2026, 10, 8, 1, 0, 0, 0, time.UTC)
)

// holdPayload is what a hold's row keeps beside its snapshot's numbers, as
// the walk writes it for auth's heartbeat at seq: the snapshot's hash and
// usage, and the reap basis its first heartbeat brought. A reap after a
// takeover is rebuilt from them.
func holdPayload(auth string, seq int64) (hash, usage, basis []byte) {
	h := sha256.Sum256([]byte(fmt.Sprintf("%s@%d", auth, seq)))
	return h[:], []byte(fmt.Sprintf(`{"auth":%q,"seq":%d}`, auth, seq)), []byte("the basis of " + auth)
}

// payloadIs reports whether a hold row keeps the payload the walk wrote for
// its heartbeat at seq.
func payloadIs(h HoldRow, seq int64) bool {
	hash, usage, basis := holdPayload(h.AuthorizationID, seq)
	return bytes.Equal(h.SnapshotHash, hash) && bytes.Equal(h.SnapshotUsage, usage) && bytes.Equal(h.ReapBasis, basis)
}

// TestTheStoreWalksWithAuditorCommit runs FASTPATH_WALKS walks (default 12)
// from FASTPATH_WALK_SEED (default a fixed seed, so the walks CI runs are
// the same each time), each with its own grant and credit rows; every store
// call and every kind of refusal is made at least once across them.
func TestTheStoreWalksWithAuditorCommit(t *testing.T) {
	s := spikeStore(t)
	walks, seed := 12, int64(20261008)
	if v := os.Getenv("FASTPATH_WALKS"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n < 1 {
			t.Fatalf("FASTPATH_WALKS=%q", v)
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
		w := newWalker(t, s, seed+int64(i))
		w.run()
		for k, n := range w.seen {
			seen[k] += n
		}
	}
	keys := make([]string, 0, len(seen))
	for k := range seen {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	var b strings.Builder
	for _, k := range keys {
		fmt.Fprintf(&b, " %s=%d", k, seen[k])
	}
	t.Logf("store calls:%s", b.String())
	required := []string{"shortfall-write", "owner-mark-draining", "auditor-mark-draining", "append", "load",
		"load-winners", "read-drain-since", "stop-for-gap", "reap", "commit", "close"}
	// Each guard the store has, refused at least once for its own reason.
	for kind, reasons := range map[string][]Refusal{
		"commit":         {RefusedVersion, RefusedClosed, RefusedGap, RefusedBoundary},
		"commit below S": {RefusedBoundary},
		"gap":            {RefusedVersion, RefusedGap, RefusedBoundary, "the record is the next after the stored progress"},
		"reap": {RefusedVersion, RefusedGap, RefusedNotDraining, RefusedNoBoundary, RefusedTerminal, RefusedNoHold,
			RefusedNotDue},
		"close": {RefusedVersion, RefusedGap, RefusedNotDraining, RefusedNoBoundary, RefusedTooSoon, RefusedHoldsOpen,
			RefusedRowsBeyond, RefusedUndecided},
		"draining write":  {"the lease is not open"},
		"shortfall-write": {RefusedClosed},
		"append":          {RefusedClosed},
	} {
		for _, r := range reasons {
			required = append(required, "refused "+kind+": "+string(r))
		}
	}
	for _, k := range required {
		if seen[k] == 0 {
			t.Errorf("no walk made a %s", k)
		}
	}
}

// memberSide is what the walk keeps for a member beside the shadow's memory:
// what its next commit sends, and where it has read the drain log to.
type memberSide struct {
	decided  []int8
	faultSeq int64
	holdSeq  [ac.MaxAuths]int64
	buffer   []DrainRow
	cursor   time.Time
	through  time.Time
}

type walker struct {
	t          *testing.T
	s          *Store
	c          ac.Config
	ref        LeaseRef
	rng        *rand.Rand
	seed       int64
	state      ac.State
	shortfall  int64
	members    [ac.MaxMembers]memberSide
	history    []string
	seen       map[string]int
	probes     int
	tries      map[string]int
	justClosed bool
	// fault is the audit fault the lease's row holds: the first a commit
	// stored, or 0.
	fault int64
	// consumed and faulted are what the commits booked, matched against the
	// allocation and beyond it, as the store books (§4.2).
	consumed, faulted int64
	// What the projection onto the shadow leaves out, the walk keeps from
	// what it wrote: the gap's sequence number, who marked the lease
	// draining, each stored hold's snapshot sequence number, each drain
	// row as appended or reaped, and each pack's version and winners, one
	// pack per commit, in order.
	gapSeq    int64
	drainedBy string
	holdSeq   [ac.MaxAuths]int64
	rows      map[string]DrainRow
	packVers  []int64
	packWins  [][]Winner
}

func newWalker(t *testing.T, s *Store, seed int64) *walker {
	t.Helper()
	rng := rand.New(rand.NewSource(seed))
	c := walkConfig
	c.Grant = 1 + rng.Intn(4)
	c.Holder = rng.Intn(len(c.Members))
	if err := c.Validate(); err != nil {
		t.Fatal(err)
	}
	// One donor or two, and room left on a third shard or none, so raises
	// and faults reach debt in some walks.
	first := int64(rng.Intn(c.Grant + 1))
	ref := grantLease(t, s, int64(c.Grant), first, int64(c.Grant)-first+int64(rng.Intn(2)), int64(rng.Intn(3)))
	return &walker{t: t, s: s, c: c, ref: ref, rng: rng, seed: seed, state: c.Init(), seen: map[string]int{},
		tries: map[string]int{}, rows: map[string]DrainRow{}}
}

func (w *walker) fatalf(format string, args ...any) {
	w.t.Helper()
	w.t.Fatalf("walk %d (grant %d, holder %s), after %s: %s", w.seed, w.c.Grant, w.c.Members[w.c.Holder],
		strings.Join(w.history, " "), fmt.Sprintf(format, args...))
}

func (w *walker) run() {
	w.t.Helper()
	w.check()
	defer func() {
		w.t.Logf("walk %d: %d steps, grant %d, %s at version %d, gap %v, %d refusals", w.seed, len(w.history), w.c.Grant,
			stateName(w.state.St), w.state.Ver, w.state.Gap, w.probes)
	}()
	for range walkSteps {
		next := w.c.Next(w.state)
		if len(next) == 0 {
			return
		}
		tr := w.choose(next)
		w.history = append(w.history, tr.Action)
		w.apply(tr)
		from := w.state
		w.state = tr.To
		if name, _ := label(tr.Action); writes[name] {
			w.check()
		} else {
			if viewOf(from) != viewOf(w.state) {
				w.fatalf("%s changes the shadow's Spanner variables", tr.Action)
			}
			w.invariants()
		}
		if w.probe() {
			w.check()
		}
	}
}

func (w *walker) choose(next []ac.Transition) ac.Transition {
	total := 0
	for _, tr := range next {
		name, _ := label(tr.Action)
		total += max(1, walkWeights[name])
	}
	n := w.rng.Intn(total)
	for _, tr := range next {
		name, _ := label(tr.Action)
		if n -= max(1, walkWeights[name]); n < 0 {
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

// apply does a step's Spanner part, from the state before it.
func (w *walker) apply(tr ac.Transition) {
	w.t.Helper()
	from := w.state
	name, args := label(tr.Action)
	member := func() int8 { return index(w.c.Members, args[0]) }
	switch name {
	case "Raise":
		w.raise(true)
	case "MarkDraining":
		w.markDraining(true)
	case "FrontDoorAppend":
		w.appendDoor(index(w.c.Auths, args[0]), fmt.Sprintf("d%d", from.DrainLen+1), true)
	case "Load":
		w.load(member(), tr.To)
	case "LoadWinners":
		w.loadWinners(from)
	case "ApplyRecord":
		w.applyRecord(member(), from)
	case "ApplyRow":
		w.applyRow(member(), from)
	case "Gap":
		m := member()
		w.gap(m, from.Log[from.Pos[m]-1].Seq, true)
	case "Reap":
		m, a := member(), index(w.c.Auths, args[1])
		// Until its hold's deadline plus the grace the store refuses it,
		// though the shadow, which has no clock, allows it: a tick a
		// microsecond before is refused, and one at it taken.
		due := walkDeadline.Add(w.s.cfg.Grace)
		w.timed("reap", RefusedNotDue, func() {
			w.reap(m, a, from, due.Add(-time.Microsecond), fmt.Sprintf("p%d", w.probes), RefusedNotDue)
		})
		w.reap(m, a, from, due, fmt.Sprintf("d%d", from.DrainLen+1), "")
	case "Commit":
		w.commit(member(), from, tr.To, "")
	case "Close":
		m := member()
		// Its holds were never listed, so until the expiry plus MaxLife
		// and the grace the store refuses it: a microsecond before, and
		// it closes at it.
		due := readLease(w.t, w.s, w.ref).Expiry.Add(w.s.cfg.MaxLife + w.s.cfg.Grace)
		w.timed("close", RefusedTooSoon, func() {
			w.close(m, from, from, due.Add(-time.Microsecond), RefusedTooSoon)
		})
		w.close(m, from, tr.To, due, "")
	case "Reread", "Crash":
		w.members[member()] = memberSide{}
	}
}

func (w *walker) raise(taken bool) {
	w.t.Helper()
	w.seen["shortfall-write"]++
	got, err := w.s.ShortfallWrite(context.Background(), owner, w.ref, w.shortfall+1)
	switch {
	case err != nil:
		w.fatalf("the shortfall write: %v", err)
	case taken && (got.Refused != "" || got.Rise != 1):
		w.fatalf("the shortfall write: %+v", got)
	case !taken && got.Refused != RefusedClosed:
		w.fatalf("a shortfall write on a closed lease: %+v", got)
	}
	if taken {
		w.shortfall++
	}
}

func (w *walker) markDraining(taken bool) {
	w.t.Helper()
	ctx := context.Background()
	var ok bool
	var err error
	by := "owner"
	if w.rng.Intn(2) == 0 {
		w.seen["owner-mark-draining"]++
		ok, _, err = w.s.OwnerMarkDraining(ctx, owner, w.ref)
	} else {
		w.seen["auditor-mark-draining"]++
		by = "auditor"
		var l Lease
		if l, _, err = w.s.ReadLease(ctx, w.ref); err == nil {
			ok, _, err = w.s.AuditorMarkDraining(ctx, w.ref, l.Expiry)
		}
	}
	if err != nil || ok != taken {
		w.fatalf("the draining write: took it %v, want %v, %v", ok, taken, err)
	}
	if taken {
		w.drainedBy = by
	}
}

func (w *walker) appendDoor(a int8, record string, taken bool) {
	w.t.Helper()
	w.seen["append"]++
	got, err := w.s.Append(context.Background(), DrainTerminal{Ref: w.ref, AuthorizationID: w.c.Auths[a], RecordID: record,
		Kind: "settle", Charge: ac.DoorCharge, Estimate: ac.DoorCharge, Digest: []byte(record), Money: []byte(`{}`),
		Cause: "walk"})
	switch {
	case err != nil:
		w.fatalf("the append: %v", err)
	case taken && (got.Refused != "" || got.Raise != 0):
		w.fatalf("the append: %+v", got)
	case !taken && got.Refused != RefusedClosed:
		w.fatalf("an append to a closed lease: %+v", got)
	}
	if taken {
		w.rows[record] = DrainRow{AuthorizationID: w.c.Auths[a], RecordID: record, Kind: "settle",
			Charge: ac.DoorCharge, Estimate: ac.DoorCharge, Digest: []byte(record), Money: []byte(`{}`), Cause: "walk",
			CommitTS: got.CommitTS}
	}
}

func (w *walker) load(m int8, to ac.State) {
	w.t.Helper()
	w.seen["load"]++
	loaded, err := w.s.Load(context.Background(), w.ref)
	if err != nil {
		w.fatalf("the load: %v", err)
	}
	mem := to.Mem[m]
	l := loaded.Lease
	got := ac.Mem{Loaded: true, Ver: small(w, l.CommitVersion), Prog: small(w, l.AppliedSeq),
		Alloc: small(w, l.Allocation+l.Returned-l.Consumed-l.FaultUsage), Holds: w.holdsOf(loaded.Holds),
		WL: l.State != "open", Osum: small(w, l.AuditOsum), S: boundaryOf(w, l)}
	for a := range w.c.Auths {
		got.Win[a] = ac.NoWin
	}
	if l.State != "open" {
		got.Win = w.winsOf(loaded.Packs)
		w.checkPacks(loaded.Packs, "the load's")
	} else if len(loaded.Packs) != 0 {
		w.fatalf("an open lease's load has winners: %+v", loaded.Packs)
	}
	if got != mem {
		w.fatalf("member %s loads %+v, and the shadow's has %+v", w.c.Members[m], got, mem)
	}
	side := memberSide{}
	for _, h := range loaded.Holds {
		if !payloadIs(h, h.SnapshotOwnerSeq.Int64) {
			w.fatalf("member %s loads a hold row %+v, and the walk wrote another payload for its snapshot", w.c.Members[m], h)
		}
		side.holdSeq[index(w.c.Auths, h.AuthorizationID)] = h.SnapshotOwnerSeq.Int64
	}
	w.members[m] = side
}

func (w *walker) loadWinners(from ac.State) {
	w.t.Helper()
	w.seen["load-winners"]++
	packs, _, err := w.s.LoadWinners(context.Background(), w.ref)
	if err != nil {
		w.fatalf("the winners' load: %v", err)
	}
	if got := w.winsOf(packs); got != from.Win {
		w.fatalf("the winners load as %+v, and the shadow stored %+v", got, from.Win)
	}
}

// applyRecord keeps what the member's next commit sends: the decisions in
// order, the first audit fault, and each hold's heartbeat.
func (w *walker) applyRecord(m int8, from ac.State) {
	r := from.Log[from.Pos[m]-1]
	mem, side := from.Mem[m], &w.members[m]
	if r.K == ac.KHb && mem.Win[r.A] == ac.NoWin {
		side.holdSeq[r.A] = int64(r.Seq)
	}
	if ac.OwnerTerminal(r) && mem.Win[r.A] == ac.NoWin {
		side.decided = append(side.decided, r.A)
	}
	if r.K == ac.KCkpt && r.C != mem.Osum && side.faultSeq == 0 {
		side.faultSeq = int64(r.Seq)
	}
}

// applyRow reads the drain log past the member's cursor when it has no row
// read and not applied, and applies the first.
func (w *walker) applyRow(m int8, from ac.State) {
	w.t.Helper()
	side := &w.members[m]
	if len(side.buffer) == 0 {
		w.seen["read-drain-since"]++
		rows, read, err := w.s.ReadDrainSince(context.Background(), w.ref, side.cursor)
		if err != nil || len(rows) == 0 {
			w.fatalf("member %s reads no row to apply: %v", w.c.Members[m], err)
		}
		side.buffer, side.cursor = rows, read
	}
	row := side.buffer[0]
	side.buffer = side.buffer[1:]
	want := from.Drain[from.Dpos[m]-1]
	if got := w.recOfRow(row); got != want {
		w.fatalf("member %s applies %+v, and the shadow's row is %+v", w.c.Members[m], got, want)
	}
	if len(side.buffer) == 0 {
		side.through = side.cursor
	}
	if from.Mem[m].Win[want.A] == ac.NoWin {
		side.decided = append(side.decided, want.A)
	}
}

func (w *walker) gap(m, seq int8, taken bool) {
	w.t.Helper()
	w.seen["stop-for-gap"]++
	ok, _, err := w.s.StopForGap(context.Background(), w.ref, int64(w.state.Mem[m].Ver), int64(seq))
	if err != nil || ok != taken {
		w.fatalf("member %s's gap at %d: took it %v, want %v, %v", w.c.Members[m], seq, ok, taken, err)
	}
	if taken {
		w.gapSeq = int64(seq)
	}
}

func (w *walker) reap(m, a int8, from ac.State, tick time.Time, record string, want Refusal) {
	w.t.Helper()
	w.seen["reap"]++
	mem := from.Mem[m]
	charge := int64(max(mem.Holds[a], 0))
	got, committed, err := w.s.Reap(context.Background(), w.ref, int64(mem.Ver), tick, ReapRow{AuthorizationID: w.c.Auths[a],
		RecordID: record, Charge: charge, Estimate: walkEstimate,
		SnapshotOwnerSeq: w.members[m].holdSeq[a], Digest: []byte(record), Money: []byte(`{}`)})
	if err != nil || got != want {
		w.fatalf("member %s's reap of %s: %q, want %q, %v", w.c.Members[m], w.c.Auths[a], got, want, err)
	}
	if want == "" {
		w.rows[record] = DrainRow{AuthorizationID: w.c.Auths[a], RecordID: record, Kind: "reap", Charge: charge,
			Estimate: walkEstimate, Digest: []byte(record), Money: []byte(`{}`), Cause: "reap",
			SnapshotOwnerSeq: spanner.NullInt64{Int64: w.members[m].holdSeq[a], Valid: true}, CommitTS: committed}
	}
}

// request is the commit a member sends: its version and progress, its
// decisions in order, each booked, its open holds, S once it knows it, and
// the first audit fault.
func (w *walker) request(m int8, from ac.State) CommitRequest {
	mem, side := from.Mem[m], w.members[m]
	req := CommitRequest{Ref: w.ref, ReadVersion: int64(mem.Ver), AppliedSeq: int64(mem.Prog), AuditOsum: int64(mem.Osum)}
	for _, a := range side.decided {
		win := mem.Win[a]
		req.Winners = append(req.Winners, w.winnerOf(win))
		if win.C > 0 {
			req.Money = append(req.Money, Book(int64(win.C), 0))
		}
	}
	for a := range w.c.Auths {
		if mem.Holds[a] != ac.NoHold {
			hash, usage, basis := holdPayload(w.c.Auths[a], side.holdSeq[a])
			req.PutHolds = append(req.PutHolds, HoldRow{AuthorizationID: w.c.Auths[a], Estimate: walkEstimate,
				Deadline: walkDeadline, SnapshotSeq: spanner.NullInt64{Int64: side.holdSeq[a], Valid: true},
				SnapshotHash: hash, SnapshotUsage: usage,
				RunningCharge:    spanner.NullInt64{Int64: int64(mem.Holds[a]), Valid: true},
				SnapshotOwnerSeq: spanner.NullInt64{Int64: side.holdSeq[a], Valid: true}, ReapBasis: basis})
		}
	}
	if mem.S != ac.NoS {
		req.Boundary = &Boundary{S: int64(mem.S), T: walkTick}
	}
	if mem.Fault {
		fault := side.faultSeq
		req.AuditFault = &fault
	}
	return req
}

func (w *walker) commit(m int8, from, to ac.State, want Refusal) {
	w.t.Helper()
	w.seen["commit"]++
	mem := from.Mem[m]
	if want == "" {
		// The walk's decisions are the shadow's new winners.
		decided := map[int8]bool{}
		for _, a := range w.members[m].decided {
			decided[a] = true
		}
		for a := range int8(len(w.c.Auths)) {
			if decided[a] != (mem.Win[a] != ac.NoWin && from.Win[a] == ac.NoWin) {
				w.fatalf("the walk sends %v as decided, and the shadow's memory is %+v", w.members[m].decided, mem)
			}
		}
	}
	req := w.request(m, from)
	got, _, err := w.s.Commit(context.Background(), []CommitRequest{req})
	if err != nil || len(got) != 1 {
		w.fatalf("member %s's commit: %+v %v", w.c.Members[m], got, err)
	}
	if got[0].Refused != want {
		w.fatalf("member %s's commit: refused %q, want %q", w.c.Members[m], got[0].Refused, want)
	}
	if want != "" {
		return
	}
	if got[0].NewVersion != int64(to.Ver) || got[0].State != stateName(from.St) {
		w.fatalf("member %s's commit: %+v, and the shadow's version is %d", w.c.Members[m], got[0], to.Ver)
	}
	allocation := int64(w.c.Grant) + int64(from.Raised)
	for _, op := range req.Money {
		matched := min(op.Charge, max(allocation-w.consumed, 0))
		w.consumed += matched
		w.faulted += op.Charge - matched
	}
	w.members[m].decided, w.members[m].faultSeq = nil, 0
	if req.AuditFault != nil {
		if w.fault == 0 {
			w.fault = *req.AuditFault
		} else {
			w.seen["a later audit fault"]++
		}
	}
	for _, h := range req.PutHolds {
		w.holdSeq[index(w.c.Auths, h.AuthorizationID)] = h.SnapshotSeq.Int64
	}
	w.packVers = append(w.packVers, got[0].NewVersion)
	w.packWins = append(w.packWins, req.Winners)
}

// commitOff commits member m's progress off the stored S by off, which the
// store must refuse: past S no owner record is applied, and below it the
// progress would go back.
func (w *walker) commitOff(m int8, from ac.State, off int64) {
	w.t.Helper()
	w.seen["commit"]++
	req := w.request(m, from)
	req.AppliedSeq, req.Boundary = int64(from.S)+off, nil
	got, _, err := w.s.Commit(context.Background(), []CommitRequest{req})
	if err != nil || len(got) != 1 || got[0].Refused != RefusedBoundary {
		w.fatalf("member %s's commit %+d off S: %+v %v", w.c.Members[m], off, got, err)
	}
}

func (w *walker) close(m int8, from, to ac.State, now time.Time, want Refusal) {
	w.t.Helper()
	w.seen["close"]++
	mem := from.Mem[m]
	got, err := w.s.CloseLease(context.Background(), w.ref, int64(mem.Ver), w.members[m].through, now)
	if err != nil || got.Refused != want {
		w.fatalf("member %s's close: %+v, want %q, %v", w.c.Members[m], got, want, err)
	}
	if want == "" && got.NewVersion != int64(to.Ver) {
		w.fatalf("member %s's close: version %d, and the shadow's is %d", w.c.Members[m], got.NewVersion, to.Ver)
	}
	w.justClosed = want == ""
}

// probe tries writes the store must refuse, up to probesPerKind of each
// kind and reason in a walk, and reports whether it tried any. A member
// makes only the writes the shadow allows it, but the store refuses a write
// for its own reasons whatever the member knows; so the probes are any
// loaded member's write, at its version, that the store's guards, read off
// the shadow's state, refuse: a stale version, a closed lease, a gap, the
// lease's state, S, a drain row, a hold, a row past the member's read. A
// write the store would take is left to the shadow's own steps.
func (w *walker) probe() bool {
	w.t.Helper()
	s, tried := w.state, false
	try := func(kind string, reason Refusal, f func()) {
		if w.quota(kind, reason) {
			tried = true
			w.unchanged(kind, f)
		}
	}
	for m := range int8(len(w.c.Members)) {
		mem := s.Mem[m]
		if !mem.Loaded {
			continue
		}
		if want := w.commitRefusal(m); want != "" {
			try("commit", want, func() { w.commit(m, s, s, want) })
		} else if s.S != ac.NoS {
			// Past a stored S no owner record is applied, and the progress
			// never goes back below it: a commit of progress S + 1, or of
			// S - 1, at the member's version is refused.
			try("commit", RefusedBoundary, func() { w.commitOff(m, s, 1) })
			if s.S > 0 {
				try("commit below S", RefusedBoundary, func() { w.commitOff(m, s, -1) })
			}
		}
		// A gap past the member's progress, and the stored progress's next
		// record, which is no gap.
		for _, seq := range []int8{mem.Prog + 2, s.Prog + 1} {
			if why := w.gapRefusal(m, seq); why != "" {
				try("gap", why, func() { w.gap(m, seq, false) })
			}
		}
		// A reap of any authorization, held or not.
		for a := range int8(len(w.c.Auths)) {
			if want := w.reapRefusal(m, a); want != "" {
				try("reap", want, func() { w.reap(m, a, s, walkTick, fmt.Sprintf("p%d", w.probes), want) })
			}
		}
		if want := w.closeRefusal(m); want != "" {
			try("close", want, func() { w.close(m, s, s, time.Now().Add(time.Hour), want) })
		}
	}
	if s.St != ac.Open && w.rng.Intn(4) == 0 {
		try("draining write", "the lease is not open", func() { w.markDraining(false) })
	}
	if s.St == ac.Closed && (w.justClosed || w.rng.Intn(4) == 0) {
		w.justClosed = false
		try("shortfall-write", RefusedClosed, func() { w.raise(false) })
		try("append", RefusedClosed, func() {
			w.appendDoor(int8(w.rng.Intn(len(w.c.Auths))), fmt.Sprintf("p%d", w.probes), false)
		})
	}
	return tried
}

// quota counts a refusal of a kind for a reason, and reports whether the
// walk has room for another.
func (w *walker) quota(kind string, reason Refusal) bool {
	key := "refused " + kind + ": " + string(reason)
	if w.tries[key] == probesPerKind {
		return false
	}
	w.tries[key]++
	w.seen[key]++
	w.probes++
	return true
}

// timed runs a probe of a guard on time, which the shadow has no clock for,
// within the quota, then checks the store wrote nothing.
func (w *walker) timed(kind string, reason Refusal, f func()) {
	w.t.Helper()
	if w.quota(kind, reason) {
		w.unchanged(kind, f)
		w.check()
	}
}

// unchanged runs a write the store must refuse, and requires the
// workspace's credit rows and the lease's donors as they were: the lease's
// row the check after it holds to the shadow.
func (w *walker) unchanged(kind string, f func()) {
	w.t.Helper()
	before := w.credit()
	f()
	if after := w.credit(); after != before {
		w.fatalf("a refused %s changed the credit rows and donors from %s to %s", kind, before, after)
	}
}

// credit is the workspace's credit rows and the lease's donors, as text.
func (w *walker) credit() string {
	w.t.Helper()
	out := fmt.Sprintf("%+v", readRows(w.t, w.ref.Workspace))
	err := shared.Single().Query(context.Background(), spanner.Statement{
		SQL: `SELECT credit_shard, allocation, consumed FROM tr_lease_donor WHERE workspace_id = @w AND lease_id = @l
		       ORDER BY credit_shard`,
		Params: w.ref.params(),
	}).Do(func(r *spanner.Row) error {
		var shard, allocation, consumed int64
		if err := r.Columns(&shard, &allocation, &consumed); err != nil {
			return err
		}
		out += fmt.Sprintf(" donor %d: %d of %d", shard, consumed, allocation)
		return nil
	})
	if err != nil {
		w.fatalf("reading the credit: %v", err)
	}
	return out
}

// commitRefusal is the store's refusal of a commit from member m, in the
// order the store checks, or "" where the store would take one.
func (w *walker) commitRefusal(m int8) Refusal {
	s, mem := w.state, w.state.Mem[m]
	switch {
	case s.Ver != mem.Ver:
		return RefusedVersion
	case s.St == ac.Closed:
		return RefusedClosed
	case s.Gap:
		return RefusedGap
	}
	return ""
}

// gapRefusal is why the store would not take member m's gap at seq, or ""
// where it would: StopForGap reports no reason, so the walk names it.
func (w *walker) gapRefusal(m, seq int8) Refusal {
	s, mem := w.state, w.state.Mem[m]
	switch {
	case s.Ver != mem.Ver:
		return RefusedVersion
	case s.Gap:
		return RefusedGap
	case s.S != ac.NoS:
		return RefusedBoundary
	case s.Prog >= seq-1:
		return "the record is the next after the stored progress"
	}
	return ""
}

// reapRefusal is the store's refusal of member m's reap of a, at its
// snapshot and a tick past the deadline, in the order the store checks, or
// "" where the store would take it.
func (w *walker) reapRefusal(m, a int8) Refusal {
	s, mem := w.state, w.state.Mem[m]
	switch {
	case s.Ver != mem.Ver:
		return RefusedVersion
	case s.Gap:
		return RefusedGap
	case s.St != ac.Draining:
		return RefusedNotDraining
	case s.S == ac.NoS:
		return RefusedNoBoundary
	}
	for i := range s.DrainLen {
		if s.Drain[i].A == a {
			return RefusedTerminal
		}
	}
	if s.Holds[a] == ac.NoHold {
		return RefusedNoHold
	}
	return ""
}

// closeRefusal is the store's refusal of member m's close, long past the
// expiry, in the order the store checks, or "" where the store would take
// it.
func (w *walker) closeRefusal(m int8) Refusal {
	s, mem := w.state, w.state.Mem[m]
	switch {
	case s.Ver != mem.Ver:
		return RefusedVersion
	case s.Gap:
		return RefusedGap
	case s.St != ac.Draining:
		return RefusedNotDraining
	case s.S == ac.NoS:
		return RefusedNoBoundary
	}
	for a := range w.c.Auths {
		if s.Holds[a] != ac.NoHold {
			return RefusedHoldsOpen
		}
	}
	if s.Dpos[m] != s.DrainLen+1 {
		return RefusedRowsBeyond
	}
	for i := range s.DrainLen {
		if s.Win[s.Drain[i].A] == ac.NoWin {
			return RefusedUndecided
		}
	}
	return ""
}

// view is the shadow's Spanner variables.
type view struct {
	St, Ver, Prog, Booked, Alloc, Osum, S int8
	Holds                                 [ac.MaxAuths]int8
	Win                                   [ac.MaxAuths]ac.Rec
	Drain                                 [ac.MaxRows]ac.Rec
	DrainLen                              int8
	Gap, Alert                            bool
}

func viewOf(s ac.State) view {
	return view{St: s.St, Ver: s.Ver, Prog: s.Prog, Booked: s.Booked, Alloc: s.Alloc, Osum: s.Osum, S: s.S,
		Holds: s.Holds, Win: s.Win, Drain: s.Drain, DrainLen: s.DrainLen, Gap: s.Gap, Alert: s.Alert}
}

// check holds the store's rows to the shadow's Spanner variables, the
// workspace to its identity, and the shadow to its invariants.
func (w *walker) check() {
	w.t.Helper()
	ctx := context.Background()
	loaded, err := w.s.Load(ctx, w.ref)
	if err != nil {
		w.fatalf("reading the lease: %v", err)
	}
	packs, _, err := w.s.LoadWinners(ctx, w.ref)
	if err != nil {
		w.fatalf("reading the winners: %v", err)
	}
	rows, _, err := w.s.ReadDrainSince(ctx, w.ref, time.Time{})
	if err != nil {
		w.fatalf("reading the drain log: %v", err)
	}
	l := loaded.Lease
	got := view{St: stateOf(w, l.State), Ver: small(w, l.CommitVersion), Prog: small(w, l.AppliedSeq),
		Booked: small(w, l.Consumed+l.FaultUsage), Alloc: small(w, l.Allocation+l.Returned-l.Consumed-l.FaultUsage),
		Osum: small(w, l.AuditOsum), S: boundaryOf(w, l), Holds: w.holdsOf(loaded.Holds), Win: w.winsOf(packs),
		Gap: l.GapSeq.Valid, Alert: l.AuditFaultSeq.Valid}
	if len(rows) > ac.MaxRows {
		w.fatalf("the drain log has %d rows", len(rows))
	}
	for i, r := range rows {
		got.Drain[i] = w.recOfRow(r)
		if got.Drain[i].Idx != int8(i+1) {
			w.fatalf("drain row %d is %s", i+1, r.RecordID)
		}
	}
	got.DrainLen = int8(len(rows))
	if want := viewOf(w.state); got != want {
		w.fatalf("the store holds\n%+v\nand the shadow\n%+v", got, want)
	}
	// The projection adds the returned money back, so the columns are held
	// exactly too: the shadow returns nothing until the close, raises only
	// by the owner's shortfall write, and its front doors' appends raise
	// nothing; the close returns all that is not booked. The alert is the
	// first audit fault a commit stored, and only it revokes the lease.
	raised := int64(w.state.Raised)
	switch {
	case l.Granted != int64(w.c.Grant) || l.ShortfallTotal != raised || l.DoorRaised != 0:
		w.fatalf("granted %d, shortfall %d, door raises %d; the shadow granted %d and raised %d", l.Granted,
			l.ShortfallTotal, l.DoorRaised, w.c.Grant, raised)
	case w.state.St != ac.Closed && (l.Returned != 0 || l.Allocation != l.Granted+raised):
		w.fatalf("an open lease returned %d, allocation %d", l.Returned, l.Allocation)
	case w.state.St == ac.Closed && (l.Allocation != l.Consumed || l.Returned != l.Granted+raised-l.Consumed):
		w.fatalf("a closed lease's allocation %d, consumed %d, returned %d", l.Allocation, l.Consumed, l.Returned)
	case l.AuditFaultSeq != (spanner.NullInt64{Int64: w.fault, Valid: w.fault != 0}) || l.Revoked != (w.fault != 0):
		w.fatalf("audit fault %v and revoked %v; the walk stored fault %d", l.AuditFaultSeq, l.Revoked, w.fault)
	case l.Consumed != w.consumed || l.FaultUsage != w.faulted:
		w.fatalf("consumed %d and fault usage %d; the walk booked %d matched and %d beyond", l.Consumed, l.FaultUsage,
			w.consumed, w.faulted)
	}
	// Every drain row, field by field, is what its append or reap wrote, and
	// raised nothing.
	if len(rows) != len(w.rows) {
		w.fatalf("%d drain rows, and the walk wrote %d", len(rows), len(w.rows))
	}
	for _, r := range rows {
		// The row's commit timestamp is the one its append or reap
		// returned: a row stamped otherwise could sort behind a cursor
		// already read past it.
		want, ok := w.rows[r.RecordID]
		got, wantTS := r, want.CommitTS
		got.CommitTS, want.CommitTS = time.Time{}, time.Time{}
		if !ok || !reflect.DeepEqual(got, want) || wantTS.IsZero() || !r.CommitTS.Equal(wantTS) {
			w.fatalf("drain row %+v, and the walk wrote %+v at %v", r, want, wantTS)
		}
	}
	w.checkUnprojected(l, loaded.Holds, packs)
	// A load gives a member the lease's packs once it is not open, closed
	// too, as the winners' read does.
	if l.State != "open" {
		w.checkPacks(loaded.Packs, "the load's")
	} else if len(loaded.Packs) != 0 {
		w.fatalf("an open lease's load has winners: %+v", loaded.Packs)
	}
	if problems, err := w.s.CheckIdentity(ctx, w.ref.Workspace); err != nil || len(problems) > 0 {
		w.fatalf("the identity: %v %v", problems, err)
	}
	w.invariants()
}

// checkUnprojected holds the columns the projection leaves out to what the
// walk wrote: the gap's and the alert's sequence numbers, S's time T, the
// draining write's author and the fence, the close, the holds' snapshots,
// and one pack per commit, at its version, none marked done.
func (w *walker) checkUnprojected(l Lease, holds []HoldRow, packs []Pack) {
	w.t.Helper()
	closed := w.state.St == ac.Closed
	closeKind := spanner.NullString{}
	if closed {
		closeKind = spanner.NullString{StringVal: "auditor", Valid: true}
	}
	// The row may go only once closed with no pack's work pending; the walk
	// marks none done, and every commit stores a pack.
	var retire spanner.NullTime
	err := shared.Single().Query(context.Background(), spanner.Statement{
		SQL:    `SELECT retire_at FROM tr_lease WHERE workspace_id = @w AND lease_id = @l`,
		Params: w.ref.params(),
	}).Do(func(r *spanner.Row) error { return r.Column(0, &retire) })
	if err != nil {
		w.fatalf("reading retire_at: %v", err)
	}
	fence := l.Expiry.Add(w.s.cfg.Skew + w.s.cfg.PublishDeadline)
	switch {
	case retire.Valid != (closed && len(w.packVers) == 0):
		w.fatalf("retire_at %v, the lease %s with %d packs pending", retire, stateName(w.state.St), len(w.packVers))
	case l.FenceTime.Valid && !l.FenceTime.Time.Equal(fence):
		w.fatalf("the fence is %v, and the expiry %v with the skew and the publish deadline is %v", l.FenceTime,
			l.Expiry, fence)
	case l.GapSeq != (spanner.NullInt64{Int64: w.gapSeq, Valid: w.gapSeq != 0}):
		w.fatalf("the gap is at %v, and the walk's at %d", l.GapSeq, w.gapSeq)
	case l.BoundarySeq.Valid != l.BoundaryPublishTime.Valid ||
		(l.BoundaryPublishTime.Valid && !l.BoundaryPublishTime.Time.Equal(walkTick)):
		w.fatalf("S %v at %v, and the walk's tick was at %v", l.BoundarySeq, l.BoundaryPublishTime, walkTick)
	case l.DrainedBy != (spanner.NullString{StringVal: w.drainedBy, Valid: w.drainedBy != ""}) ||
		l.FenceTime.Valid != (w.drainedBy != ""):
		w.fatalf("drained by %v with fence %v, and the walk's draining write was the %q's", l.DrainedBy, l.FenceTime,
			w.drainedBy)
	case l.ClosedAt.Valid != closed || l.CloseKind != closeKind:
		w.fatalf("closed at %v by %v, and the lease is %s", l.ClosedAt, l.CloseKind, stateName(w.state.St))
	case l.HoldsListedSeq.Valid || l.LastTick != 0:
		w.fatalf("holds listed at %v, last tick %d, and the walk sent neither", l.HoldsListedSeq, l.LastTick)
	}
	w.checkPacks(packs, "the winners'")
	for _, h := range holds {
		seq := spanner.NullInt64{Int64: w.holdSeq[index(w.c.Auths, h.AuthorizationID)], Valid: true}
		if h.SnapshotSeq != seq || h.SnapshotOwnerSeq != seq || h.Estimate != walkEstimate ||
			!h.Deadline.Equal(walkDeadline) || h.Listed || !payloadIs(h, seq.Int64) {
			w.fatalf("a hold row %+v, and the walk stored its snapshot at %d", h, seq.Int64)
		}
	}
}

// checkPacks holds packs as a read returned them to the walk's commits:
// one pack per commit, at its version, none marked done, each with its own
// commit's winners, work and all.
func (w *walker) checkPacks(packs []Pack, read string) {
	w.t.Helper()
	if len(packs) != len(w.packVers) {
		w.fatalf("%s read has %d packs, and the walk's commits stored %d", read, len(packs), len(w.packVers))
	}
	for i, p := range packs {
		if p.CommitVersion != w.packVers[i] || p.WorkDoneAt.Valid {
			w.fatalf("%s read's pack %d is at version %d, done %v, and the walk's commit was at %d", read, i,
				p.CommitVersion, p.WorkDoneAt, w.packVers[i])
		}
		if len(p.Winners) != len(w.packWins[i]) || (len(p.Winners) > 0 && !reflect.DeepEqual(p.Winners, w.packWins[i])) {
			w.fatalf("%s read's pack %d at version %d holds %+v, and its commit stored %+v", read, i, p.CommitVersion,
				p.Winners, w.packWins[i])
		}
	}
}

func (w *walker) invariants() {
	w.t.Helper()
	for _, inv := range w.c.Invariants() {
		if !inv.Holds(w.state) {
			w.fatalf("the shadow does not keep %s", inv.Name)
		}
	}
}

func (w *walker) holdsOf(rows []HoldRow) [ac.MaxAuths]int8 {
	w.t.Helper()
	var out [ac.MaxAuths]int8
	for a := range w.c.Auths {
		out[a] = ac.NoHold
	}
	for _, h := range rows {
		a := index(w.c.Auths, h.AuthorizationID)
		if a < 0 || !h.RunningCharge.Valid || out[a] != ac.NoHold {
			w.fatalf("a hold row %+v, none of the walk's or read twice", h)
		}
		out[a] = small(w, h.RunningCharge.Int64)
	}
	return out
}

// winsOf is each authorization's winner: the first pack's that names it.
// No later pack names it again.
func (w *walker) winsOf(packs []Pack) [ac.MaxAuths]ac.Rec {
	w.t.Helper()
	var out [ac.MaxAuths]ac.Rec
	for a := range w.c.Auths {
		out[a] = ac.NoWin
	}
	for _, p := range packs {
		for _, win := range p.Winners {
			a := index(w.c.Auths, win.AuthorizationID)
			if a < 0 || out[a] != ac.NoWin {
				w.fatalf("pack %d names %s, stored already or none of the walk's", p.CommitVersion, win.AuthorizationID)
			}
			out[a] = w.recOfWinner(win)
		}
	}
	return out
}

var kinds = map[int8]string{ac.KSettle: "settle", ac.KRefund: "refund", ac.KReap: "reap"}

func kindOf(w *walker, name string) int8 {
	for k, n := range kinds {
		if n == name {
			return k
		}
	}
	w.fatalf("a record of kind %q", name)
	return ac.KNone
}

// winnerOf is the winner a commit stores for the shadow's record r.
// winnerOf is the winner the walk commits for the shadow's, with pending
// work of its own (workOf), which every read of its pack must give back.
func (w *walker) winnerOf(r ac.Rec) Winner {
	out := Winner{AuthorizationID: w.c.Auths[r.A], Kind: kinds[r.K], Charge: int64(r.C)}
	if r.Idx > 0 {
		out.FromDrain, out.RecordID = true, fmt.Sprintf("d%d", r.Idx)
	} else {
		out.RecordID = fmt.Sprintf("o%d", r.Seq)
	}
	out.Work = workOf(out.AuthorizationID, out.RecordID)
	return out
}

// workOf is the pending work the walk stores with a winner: its own for
// each authorization and record.
func workOf(auth, recordID string) json.RawMessage {
	return json.RawMessage(fmt.Sprintf(`{"walk":%q}`, auth+"/"+recordID))
}

func (w *walker) recOfWinner(win Winner) ac.Rec {
	w.t.Helper()
	r := ac.Rec{K: kindOf(w, win.Kind), A: index(w.c.Auths, win.AuthorizationID), C: small(w, win.Charge)}
	if win.FromDrain {
		r.Idx = w.recordNumber(win.RecordID, "d")
	} else {
		r.Seq = w.recordNumber(win.RecordID, "o")
	}
	if !bytes.Equal(win.Work, workOf(win.AuthorizationID, win.RecordID)) {
		w.fatalf("winner %+v's pending work %s, and the walk stored %s", win, win.Work,
			workOf(win.AuthorizationID, win.RecordID))
	}
	return r
}

func (w *walker) recOfRow(row DrainRow) ac.Rec {
	w.t.Helper()
	return ac.Rec{K: kindOf(w, row.Kind), A: index(w.c.Auths, row.AuthorizationID), C: small(w, row.Charge),
		Idx: w.recordNumber(row.RecordID, "d")}
}

// recordNumber is the number in a record ID the walk wrote, which must be
// that ID exactly, so no two records read as one.
func (w *walker) recordNumber(id, prefix string) int8 {
	w.t.Helper()
	n, err := strconv.Atoi(strings.TrimPrefix(id, prefix))
	if err != nil || n < 1 || n > 127 || id != prefix+strconv.Itoa(n) {
		w.fatalf("a record ID %q", id)
	}
	return int8(n)
}

func small(w *walker, v int64) int8 {
	w.t.Helper()
	if v < -128 || v > 127 {
		w.fatalf("%d is no number of the shadow's", v)
	}
	return int8(v)
}

func boundaryOf(w *walker, l Lease) int8 {
	if !l.BoundarySeq.Valid {
		return ac.NoS
	}
	return small(w, l.BoundarySeq.Int64)
}

var states = map[string]int8{"open": ac.Open, "draining": ac.Draining, "closed": ac.Closed}

func stateOf(w *walker, name string) int8 {
	w.t.Helper()
	st, ok := states[name]
	if !ok {
		w.fatalf("a lease in state %q", name)
	}
	return st
}

func stateName(st int8) string {
	for name, v := range states {
		if v == st {
			return name
		}
	}
	return ""
}
