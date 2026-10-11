//go:build !race

package store

import (
	"bytes"
	"context"
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
)

// The debt walks run random operations on one workspace, three credit
// shards and up to four leases at a time: grants, the owner's shortfall
// writes, bookings past the allocation, returns, draining, front doors'
// raises and closes. The walk keeps its own ledger of each lease's donors,
// as section 4.2 books them, and its drain rows. After each operation, the
// rows keep CreditDebt's claims (proofs/CreditDebt.tla) on what the store
// wrote, held to that ledger, closed leases' too:
//
//   - ShardIdentity, AllocationAccounted, DebtMarksEveryShard and
//     MarkMeansDebt, through CheckIdentity and the lease row's checks;
//   - UsageIsBooked: each shard's usage is what the walk booked against the
//     leases' donors on it, and their faults on the first donor's shard, and
//     its reservation what they hold and have not booked;
//   - CreditConserved: covering and repaying move credit between shards and
//     never make or lose any;
//   - RepaysDebtFirst: while a shard is negative after an operation, the
//     operation raised only shards that were negative, each at most to zero,
//     and none while a lower shard stays negative.

const debtSteps = 120

// TestTheStoreKeepsCreditDebtsClaims runs FASTPATH_DEBT_WALKS walks (default
// 8) from FASTPATH_WALK_SEED (default a fixed seed).
func TestTheStoreKeepsCreditDebtsClaims(t *testing.T) {
	if emulator == nil {
		t.Skip(skipped)
	}
	// Eight walks: the grant probed beside each listed lease steers the
	// walks after it, and every kind of row order must still be seen.
	walks, seed := 8, int64(20261008)
	if v := os.Getenv("FASTPATH_DEBT_WALKS"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n < 1 {
			t.Fatalf("FASTPATH_DEBT_WALKS=%q", v)
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
		// Every other walk's store has a floor, so a grant the headroom alone
		// would take is refused for the floor alone, which the walks must
		// see; the rest walk as before, their floor zero.
		floor := int64(10 * (i % 2))
		d := newDebtWalk(t, spikeStore(t, func(c *Config) { c.Allowance, c.Floor = 30, floor }), seed+int64(i))
		d.run()
		for k, n := range d.seen {
			seen[k] += n
		}
	}
	t.Logf("operations: %v", seen)
	for _, k := range []string{"grant", "refused grant", "shortfall-write", "booking", "fault", "faults in one commit",
		"return", "drain",
		"door raise", "close", "refused close: undecided", "refused commit", "debt", "debt repaid", "a lease of two donors or more",
		"a booking past the first donor", "a return before the last donor", "a batch its order changes",
		"a return its place changes", "a pack of several winners", "refused grant: " + string(RefusedInDebt),
		"drain rows whose IDs sort before an earlier row's", "drain rows whose IDs sort after an earlier row's",
		"refused grant: " + string(RefusedAllowance), "refused grant: " + string(RefusedFloor),
		"refused grant: the floor alone", "a grant beside a listed lease"} {
		if seen[k] == 0 {
			t.Errorf("no walk made a %s", k)
		}
	}
}

// debtLease is a lease the walk holds, with the money its row must show:
// granted, the shortfall total, the front doors' raises, what was returned,
// what was booked matched against the allocation and what as a fault; its
// donors; its drain rows by record ID, and the record IDs whose winners are
// committed.
type debtLease struct {
	ref        LeaseRef
	state      string
	version    int64
	seq        int64
	stored     bool
	appends    int
	granted    int64
	shortfall  int64
	doorRaised int64
	returned   int64
	consumed   int64
	fault      int64
	donors     []donor
	rows       map[string]DrainTerminal
	raises     map[string]int64
	stamped    map[string]time.Time // each row's commit timestamp, as its append returned it
	committed  map[string]bool
	packs      []Pack   // each commit's version and winners, in commit order
	order      []string // the drain rows' record IDs, in the order appended
	// req is the lease's grant and expiry its answer's; boundary is the S
	// its commit stored, or 0; closedAt is when the close's statement ran.
	req      GrantRequest
	expiry   time.Time
	boundary int64
	closedAt within
}

// donor is one of a lease's donors as the walk books it: its credit shard,
// its part of the allocation, and what is booked against it.
type donor struct {
	shard, allocation, consumed int64
}

// allocation is what the lease's row must hold as its allocation.
func (l *debtLease) allocation() int64 { return l.granted + l.shortfall + l.doorRaised - l.returned }

// raised adds a raise to the allocation's first donor (§4.2).
func (l *debtLease) raised(rise int64) { l.donors[0].allocation += rise }

// booked books a charge as the store does (§4.2): the shortfall total rises
// to the record's, the rise to the first donor; the charge is matched first
// donor first, each up to its room; the rest is a fault. It reports whether
// a donor after the first took part, and the fault.
func (l *debtLease) booked(charge, total int64) (pastFirst bool, fault int64) {
	if rise := total - l.shortfall; rise > 0 {
		l.shortfall = total
		l.raised(rise)
	}
	left := charge
	for i := range l.donors {
		x := min(l.donors[i].allocation-l.donors[i].consumed, left)
		if x <= 0 {
			continue
		}
		l.donors[i].consumed += x
		l.consumed += x
		left -= x
		pastFirst = pastFirst || i > 0
	}
	l.fault += left
	return pastFirst, left
}

// gaveBack returns amount of what the lease has not booked, last donor
// first (§4.2), and reports whether a donor before the last took part.
func (l *debtLease) gaveBack(amount int64) (beforeLast bool) {
	left := amount
	for i := len(l.donors) - 1; i >= 0 && left > 0; i-- {
		z := min(l.donors[i].allocation-l.donors[i].consumed, left)
		if z <= 0 {
			continue
		}
		l.donors[i].allocation -= z
		left -= z
		beforeLast = beforeLast || i < len(l.donors)-1
	}
	l.returned += amount
	return beforeLast
}

// room is what the lease holds and has not booked.
func (l *debtLease) room() int64 { return l.allocation() - l.consumed }

// bookedEach books and returns a commit's money one operation at a time,
// in order, and reports whether a booking reached a donor after the first,
// and each booking's fault, in order, as a commit's result lists them.
func (l *debtLease) bookedEach(money []MoneyOp) (pastFirst bool, faults []int64) {
	for _, op := range money {
		if !op.Book {
			l.gaveBack(op.Amount)
			continue
		}
		past, fault := l.booked(op.Charge, op.ShortfallTotal)
		pastFirst = pastFirst || past
		if fault > 0 {
			faults = append(faults, fault)
		}
	}
	return pastFirst, faults
}

// returnsFirst is money with its returns moved before its bookings.
func returnsFirst(money []MoneyOp) []MoneyOp {
	var returns, books []MoneyOp
	for _, op := range money {
		if op.Book {
			books = append(books, op)
		} else {
			returns = append(returns, op)
		}
	}
	return append(returns, books...)
}

// copied is the lease's ledger, to book on apart from it.
func (l *debtLease) copied() *debtLease {
	c := *l
	c.donors = slices.Clone(l.donors)
	return &c
}

// orderMatters reports whether booking money in another order would book
// other amounts, against the allocation or as faults, than in its order.
func (l *debtLease) orderMatters(money, other []MoneyOp) bool {
	inOrder, otherwise := l.copied(), l.copied()
	inOrder.bookedEach(money)
	otherwise.bookedEach(other)
	return inOrder.consumed != otherwise.consumed || inOrder.fault != otherwise.fault
}

type debtWalk struct {
	t       *testing.T
	s       *Store
	ws      string
	rng     *rand.Rand
	probes  *rand.Rand // the refused commits', apart, so they leave the walk's own choices as they were
	batches *rand.Rand // the bookings after a commit's first, apart for the same reason
	ids     *rand.Rand // the drain rows' record IDs, apart for the same reason
	seed    int64
	credits int64
	leases  []*debtLease
	history []string
	seen    map[string]int
	stamps  stamps // the writes' commit timestamps
}

func newDebtWalk(t *testing.T, s *Store, seed int64) *debtWalk {
	t.Helper()
	rng := rand.New(rand.NewSource(seed))
	credits := []int64{int64(rng.Intn(30)), int64(rng.Intn(30)), int64(rng.Intn(30))}
	d := &debtWalk{t: t, s: s, ws: seedWorkspace(t, credits...), rng: rng, probes: rand.New(rand.NewSource(^seed)),
		batches: rand.New(rand.NewSource(seed ^ 0x5eed)), ids: rand.New(rand.NewSource(seed ^ 0x1d5)), seed: seed,
		seen: map[string]int{}}
	for _, c := range credits {
		d.credits += c
	}
	return d
}

func (d *debtWalk) fatalf(format string, args ...any) {
	d.t.Helper()
	d.t.Fatalf("debt walk %d, after %s: %s", d.seed, strings.Join(d.history, "; "), fmt.Sprintf(format, args...))
}

func (d *debtWalk) live() []*debtLease {
	var out []*debtLease
	for _, l := range d.leases {
		if l.state != "closed" {
			out = append(out, l)
		}
	}
	return out
}

func (d *debtWalk) run() {
	d.t.Helper()
	ctx := context.Background()
	before := headrooms(readRows(d.t, d.ws))
	for range debtSteps {
		live := d.live()
		var l *debtLease
		if len(live) > 0 {
			l = live[d.rng.Intn(len(live))]
		}
		switch op := d.rng.Intn(10); {
		case l == nil:
			// With no lease left, a walk ends once it is granted none.
			if !d.grant(ctx) {
				d.check(before, headrooms(readRows(d.t, d.ws)))
				d.checkAllPacks()
				return
			}
		case op == 0 && len(live) < 4:
			d.grant(ctx)
		case op <= 2:
			d.raise(ctx, l)
		case op <= 5:
			d.book(ctx, l)
		case op == 6:
			d.giveBack(ctx, l)
		case op == 7 && l.state == "open":
			d.drain(ctx, l)
		case op <= 8 && l.state != "open":
			// Two ops in ten, so a draining lease takes a second append
			// before its close often enough for both orders of drain IDs.
			d.doorRaise(ctx, l)
		case l.state == "draining":
			d.close(ctx, l)
		default:
			d.drain(ctx, l)
		}
		after := headrooms(readRows(d.t, d.ws))
		d.check(before, after)
		before = after
	}
	d.checkAllPacks()
}

// checkAllPacks holds every lease's packs, a closed one's too, at the walk's
// end, so a pack written to the wrong lease shows.
func (d *debtWalk) checkAllPacks() {
	d.t.Helper()
	for _, l := range d.leases {
		d.checkPacks(l)
	}
}

func (d *debtWalk) note(format string, args ...any) {
	d.history = append(d.history, fmt.Sprintf(format, args...))
}

// landed requires a write's commit timestamp after the walk's last.
func (d *debtWalk) landed(kind string, ts time.Time) {
	d.t.Helper()
	if err := d.stamps.next(ts); err != nil {
		d.fatalf("the %s: %v", kind, err)
	}
}

// leaseWant is a lease's row as the walk's writes left it, every column.
func (d *debtWalk) leaseWant(l *debtLease) leaseWant {
	listed := spanner.NullInt64{Int64: l.boundary, Valid: l.stored}
	want := leaseWant{
		"workspace_id": l.ref.Workspace, "lease_id": l.ref.LeaseID, "region": l.req.Region,
		"workspace_shard": l.req.WorkspaceShard, "owner_node": l.req.Owner.Node, "owner_epoch": l.req.Owner.Epoch,
		"key_status_version": l.req.KeyStatusVersion, "state": l.state, "granted": l.granted,
		"allocation": l.allocation(), "consumed": l.consumed, "shortfall_total": l.shortfall,
		"door_raised": l.doorRaised, "returned": l.returned, "fault_usage": l.fault, "expiry": l.expiry,
		"revoked": false, "revoked_at": spanner.NullTime{}, "commit_version": l.version, "applied_seq": l.seq,
		"last_tick": int64(0), "audit_osum": int64(0), "holds_listed_seq": listed,
		"audit_fault_seq": spanner.NullInt64{}, "gap_seq": spanner.NullInt64{}, "fence_time": spanner.NullTime{},
		"boundary_seq": listed, "boundary_publish_time": spanner.NullTime{Time: walkTick, Valid: l.stored},
		"drained_by": spanner.NullString{}, "closed_at": spanner.NullTime{}, "close_kind": spanner.NullString{},
		"retire_at": spanner.NullTime{},
	}
	if !l.stored {
		want["boundary_publish_time"] = spanner.NullTime{}
	}
	if l.state != "open" {
		want["drained_by"] = spanner.NullString{StringVal: "owner", Valid: true}
		want["fence_time"] = spanner.NullTime{Time: l.expiry.Add(d.s.cfg.Skew + d.s.cfg.PublishDeadline), Valid: true}
	}
	if l.state == "closed" {
		// Every commit stores a pack whose work the walk never marks done,
		// so a closed lease's row is kept: no retire_at.
		want["closed_at"], want["close_kind"] = l.closedAt, spanner.NullString{StringVal: "auditor", Valid: true}
		if len(l.packs) == 0 {
			want["retire_at"] = l.closedAt
		}
	}
	return want
}

// grantRefusal is the store's answer to a grant of amount, as the store
// checks, in its order, from what the walk knows: a debt mark on a credit
// row; the workspace's exposure, every lease's unbooked allocation while it
// is open or draining with its holds not listed, against the allowance; and
// the headroom outside leases against the floor. "" is a grant. The walk's
// workspace is never paused and keeps the tier that allows leases, which
// the store checks before the exposure.
func (d *debtWalk) grantRefusal(rows []row, amount int64) Refusal {
	var exposure, headroom int64
	for _, l := range d.leases {
		if l.state == "open" || l.state == "draining" && !l.stored {
			exposure += max(l.room(), 0)
		}
	}
	for _, r := range rows {
		headroom += r.headroom()
	}
	switch {
	case slices.ContainsFunc(rows, func(r row) bool { return r.marked }):
		return RefusedInDebt
	case exposure > d.s.cfg.Allowance-amount:
		return RefusedAllowance
	case headroom < d.s.cfg.Floor+amount:
		if headroom >= amount {
			d.seen["refused grant: the floor alone"]++
		}
		return RefusedFloor
	}
	return ""
}

// rows is every row a write to the lease could touch (everyRow).
func (d *debtWalk) rows(ref LeaseRef) string {
	d.t.Helper()
	out, err := everyRow(ref)
	if err != nil {
		d.fatalf("reading the rows: %v", err)
	}
	return out
}

// unchanged runs a write the store must refuse, and requires every row it
// could touch as it was, read at once after it.
func (d *debtWalk) unchanged(ref LeaseRef, kind string, f func()) {
	d.t.Helper()
	before := d.rows(ref)
	f()
	if after := d.rows(ref); after != before {
		d.fatalf("a refused %s changed the rows from %s to %s", kind, before, after)
	}
}

// grant reports whether the workspace granted a lease.
func (d *debtWalk) grant(ctx context.Context) bool {
	d.t.Helper()
	return d.grantFrom(ctx, d.rng)
}

// grantFrom is a grant whose amount comes from rng: the walk's own, or the
// probes' source for a grant made beside the walk's steps, so the walk's
// draws are as they were.
func (d *debtWalk) grantFrom(ctx context.Context, rng *rand.Rand) bool {
	d.t.Helper()
	req := grantOf(d.ws, int64(1+rng.Intn(20)))
	req.WorkspaceShard, req.KeyStatusVersion = int64(1+d.probes.Intn(9)), int64(10+d.probes.Intn(90))
	d.note("grant %d", req.Amount)
	rows := readRows(d.t, d.ws)
	ref := LeaseRef{d.ws, req.LeaseID}
	before := d.rows(ref)
	want := d.grantRefusal(rows, req.Amount)
	start := spannerNow(d.t)
	got, err := d.s.Grant(ctx, detached(req))
	if err != nil {
		d.fatalf("the grant: %v", err)
	}
	end := spannerNow(d.t)
	if got.Refused != want {
		d.fatalf("the grant of %d was refused %q, and the walk's checks say %q", req.Amount, got.Refused, want)
	}
	if got.Refused != "" {
		// Nothing of the workspace's moves, and no row of the lease is
		// written.
		d.seen["refused grant"]++
		d.seen["refused grant: "+string(got.Refused)]++
		if after := d.rows(ref); after != before {
			d.fatalf("a refused grant changed the rows from %s to %s", before, after)
		}
		return false
	}
	// Its expiry is a window past Spanner's time as it granted, which the
	// walk then takes as the lease's.
	if from := got.Expiry.Add(-d.s.cfg.Window); from.Before(start) || from.After(end) {
		d.fatalf("the grant's expiry %v is not a window of %v past Spanner's time during it, %v to %v", got.Expiry,
			d.s.cfg.Window, start, end)
	}
	// A grant reserves L on donor shards in ascending order, each up to its
	// headroom.
	l := &debtLease{ref: ref, state: "open", granted: req.Amount, req: req, expiry: got.Expiry,
		rows: map[string]DrainTerminal{}, raises: map[string]int64{}, stamped: map[string]time.Time{},
		committed: map[string]bool{}}
	left := req.Amount
	for shard, h := range headrooms(rows) {
		if x := min(h, left); x > 0 {
			l.donors = append(l.donors, donor{shard: int64(shard), allocation: x})
			left -= x
		}
	}
	if left != 0 {
		d.fatalf("a grant of %d past the headroom %v", req.Amount, headrooms(rows))
	}
	var donors []Donor
	for _, dn := range l.donors {
		donors = append(donors, Donor{Shard: dn.shard, Allocation: dn.allocation})
	}
	if !slices.Equal(got.Donors, donors) {
		d.fatalf("the grant's donors %+v, and the walk's %+v", got.Donors, donors)
	}
	if row := readLease(d.t, d.s, ref); !got.Expiry.Equal(row.Expiry) {
		d.fatalf("the grant's expiry %v, and its row's %v", got.Expiry, row.Expiry)
	}
	d.landed("grant", got.CommitTS)
	if len(l.donors) > 1 {
		d.seen["a lease of two donors or more"]++
	}
	d.seen["grant"]++
	d.leases = append(d.leases, l)
	return true
}

func (d *debtWalk) raise(ctx context.Context, l *debtLease) {
	d.t.Helper()
	total := l.shortfall + int64(1+d.rng.Intn(8))
	d.note("shortfall %s %d", l.ref.LeaseID, total)
	got, err := d.s.ShortfallWrite(ctx, owner, l.ref, total)
	if err != nil || got.Refused != "" || got.Rise != total-l.shortfall {
		d.fatalf("the shortfall write: %+v %v", got, err)
	}
	d.seen["shortfall-write"]++
	d.landed("shortfall write", got.CommitTS)
	l.raised(total - l.shortfall)
	l.shortfall = total
}

// commit commits one lease's money at the next sequence number, or at S
// once S is stored, and reports the result.
func (d *debtWalk) commit(ctx context.Context, l *debtLease, money []MoneyOp, boundary bool) CommitResult {
	d.t.Helper()
	seq := l.seq
	if !l.stored {
		seq++
	}
	req := CommitRequest{Ref: l.ref, ReadVersion: l.version, AppliedSeq: seq, Money: money}
	if boundary {
		req.Boundary, req.HoldsListedSeq = &Boundary{S: seq, T: walkTick}, &seq
	}
	// The store gets a copy (walkCommit), so what the walk expects, from
	// money and req, is what it generated, whatever the call does with its
	// request.
	result, ts, err := walkCommit(d.s, req, "")
	if err != nil {
		d.fatalf("the commit %+v: %v", req, err)
	}
	d.landed("commit", ts)
	if result.State != l.state {
		d.fatalf("the commit left lease %s %q, and the walk has it %q", l.ref.LeaseID, result.State, l.state)
	}
	got := []CommitResult{result}
	if !slices.ContainsFunc(money, func(op MoneyOp) bool { return op.Book }) && len(got[0].Faults) != 0 {
		d.fatalf("a commit that books nothing faulted %v", got[0].Faults)
	}
	l.advance(d, "the commit", got[0].NewVersion)
	l.seq = seq
	l.packs = append(l.packs, Pack{CommitVersion: l.version, Winners: req.Winners})
	d.checkPacks(l)
	if boundary {
		l.boundary = seq
	}
	l.stored = l.stored || boundary
	return got[0]
}

// book books a charge, carrying a shortfall total that sometimes raises the
// stored one, and in a quarter of the commits one or two more after it,
// each carrying a total that sometimes raises the one before; in another
// quarter, a charge past the room left and then a raise past the charge
// after it, whose order shows; in another, a charge past the room, a raise,
// and a return of part of what the raise left, whose place shows. A charge
// past the allocation is a fault. A commit books and returns in its order:
// one whose reverse, or whose returns first, would book otherwise is
// counted.
func (d *debtWalk) book(ctx context.Context, l *debtLease) {
	d.t.Helper()
	charge, total := int64(d.rng.Intn(16)), l.shortfall
	if d.rng.Intn(3) == 0 {
		total += int64(d.rng.Intn(6))
	}
	money := []MoneyOp{Book(charge, total)}
	switch d.batches.Intn(4) {
	case 1:
		for range 1 + d.batches.Intn(2) {
			charge := int64(d.batches.Intn(16))
			if d.batches.Intn(2) == 0 {
				total += int64(d.batches.Intn(6))
			}
			money = append(money, Book(charge, total))
		}
	case 2:
		// Booked in reverse, the raise would make room for the charge
		// past the room.
		after := l.copied()
		after.bookedEach(money)
		past := max(after.room(), 0) + 1 + int64(d.batches.Intn(4))
		raise := 1 + int64(d.batches.Intn(8))
		money = append(money, Book(past, total), Book(int64(d.batches.Intn(int(raise))), total+raise))
	case 3:
		// Moved before the bookings, the return would take room the charge
		// past it had: so it may only take what both orders leave.
		after := l.copied()
		after.bookedEach(money)
		room := max(after.room(), 0)
		raise := 1 + int64(d.batches.Intn(8))
		money = append(money, Book(room+1+int64(d.batches.Intn(4)), total), Book(0, total+raise))
		if most := min(room, raise); most > 0 {
			money = append(money, Return(1+d.batches.Int63n(most)))
		}
	}
	d.note("book %s %v", l.ref.LeaseID, money)
	if d.probes.Intn(4) == 0 {
		// A member that read a later version than the lease's, its
		// progress past the stored one: the store refuses its commit and
		// changes nothing, every row read at once after it, and the check
		// after holding every row to the walk's ledger.
		d.unchanged(l.ref, "commit", func() {
			if _, _, err := walkCommit(d.s, CommitRequest{Ref: l.ref, ReadVersion: l.version + 1, AppliedSeq: l.seq + 1,
				Money: money}, RefusedVersion); err != nil {
				d.fatalf("a commit at a version the lease never had: %v", err)
			}
		})
		d.seen["refused commit"]++
	}
	back := slices.Clone(money)
	slices.Reverse(back)
	if !slices.ContainsFunc(money, func(op MoneyOp) bool { return !op.Book }) && l.orderMatters(money, back) {
		d.seen["a batch its order changes"]++
	}
	if l.orderMatters(money, returnsFirst(money)) {
		d.seen["a return its place changes"]++
	}
	got := d.commit(ctx, l, money, false)
	pastFirst, faults := l.bookedEach(money)
	if pastFirst {
		d.seen["a booking past the first donor"]++
	}
	d.seen["booking"]++
	if !slices.Equal(got.Faults, faults) {
		d.fatalf("the commit's faults %v, and the walk's %v", got.Faults, faults)
	}
	if len(faults) > 0 {
		d.seen["fault"]++
	}
	if len(faults) > 1 {
		d.seen["faults in one commit"]++
	}
}

// giveBack returns part of what the lease has not booked.
func (d *debtWalk) giveBack(ctx context.Context, l *debtLease) {
	d.t.Helper()
	room := l.room()
	if room <= 0 {
		d.book(ctx, l)
		return
	}
	amount := 1 + d.rng.Int63n(room)
	d.note("return %s %d", l.ref.LeaseID, amount)
	d.commit(ctx, l, []MoneyOp{Return(amount)}, false)
	if l.gaveBack(amount) {
		d.seen["a return before the last donor"]++
	}
	d.seen["return"]++
}

func (d *debtWalk) drain(ctx context.Context, l *debtLease) {
	d.t.Helper()
	d.note("drain %s", l.ref.LeaseID)
	ok, ts, err := d.s.OwnerMarkDraining(ctx, owner, l.ref)
	if err != nil || !ok {
		d.fatalf("the draining write: %v %v", ok, err)
	}
	d.landed("draining write", ts)
	l.state = "draining"
	d.seen["drain"]++
}

// doorRaise appends a front door's settle at or past its estimate, which
// raises the lease by the difference, none when there is none.
func (d *debtWalk) doorRaise(ctx context.Context, l *debtLease) {
	d.t.Helper()
	l.appends++
	raise := int64(d.rng.Intn(7))
	// A record ID is random, so the IDs' order is not the rows' (a drain
	// log read in ID order shows).
	record := fmt.Sprintf("d%06x", d.ids.Intn(1<<24))
	for _, ok := l.rows[record]; ok; _, ok = l.rows[record] {
		record = fmt.Sprintf("d%06x", d.ids.Intn(1<<24))
	}
	d.note("door %s +%d", l.ref.LeaseID, raise)
	t := DrainTerminal{Ref: l.ref, AuthorizationID: "a" + record, RecordID: record, Kind: "settle", Charge: 2 + raise,
		Estimate: 2, Digest: []byte(l.ref.LeaseID + record), Money: []byte(`{"walk":"debt"}`), Cause: "debt walk"}
	got, err := d.s.Append(ctx, detached(t))
	if err != nil || got.Refused != "" || got.Raise != raise {
		d.fatalf("the append: %+v %v", got, err)
	}
	d.landed("append", got.CommitTS)
	l.doorRaised += raise
	l.raised(raise)
	l.rows[record], l.raises[record], l.stamped[record] = t, raise, got.CommitTS
	l.order = append(l.order, record)
	d.seen["door raise"]++
}

// close stores S if it is not, reads the drain log to the end, commits the
// winners of its rows, which a close before then must refuse, and closes the
// lease, which returns its remainder.
func (d *debtWalk) close(ctx context.Context, l *debtLease) {
	d.t.Helper()
	d.note("close %s", l.ref.LeaseID)
	if !l.stored {
		// The whole workspace is checked after this commit on its own: a
		// listing commit that moved a credit row, a mark cleared, would be
		// covered by the close's repair before the walk's check otherwise.
		before := headrooms(readRows(d.t, d.ws))
		d.commit(ctx, l, nil, true)
		d.checkLeaseRow(l, "S's commit")
		d.check(before, headrooms(readRows(d.t, d.ws)))
	}
	// With the holds listed, the lease's exposure is its listed holds',
	// none here, not its remaining allocation: a grant beside it is
	// answered so, and the close goes on with the lease.
	if d.grantFrom(ctx, d.probes) {
		d.seen["a grant beside a listed lease"]++
	}
	rows, read, err := d.s.ReadDrainSince(ctx, l.ref, time.Time{})
	if err != nil {
		d.fatalf("reading the drain log: %v", err)
	}
	// The log reads in the order appended, whatever the IDs' order.
	if len(rows) != len(l.order) {
		d.fatalf("lease %s's drain log reads %d rows, and the walk appended %d", l.ref.LeaseID, len(rows), len(l.order))
	}
	for i, r := range rows {
		if r.RecordID != l.order[i] {
			d.fatalf("lease %s's drain row %d is %s, and the walk appended %s", l.ref.LeaseID, i, r.RecordID, l.order[i])
		}
		if i > 0 && r.RecordID < rows[i-1].RecordID {
			d.seen["drain rows whose IDs sort before an earlier row's"]++
		}
		if i > 0 && r.RecordID > rows[i-1].RecordID {
			d.seen["drain rows whose IDs sort after an earlier row's"]++
		}
	}
	// The winners are the rows as read back, each its authorization's first
	// terminal, held to what the walk appended.
	var winners []Winner
	var money []MoneyOp
	for _, r := range rows {
		d.checkRow(l, r)
		if !l.committed[r.RecordID] {
			winners = append(winners, Winner{AuthorizationID: r.AuthorizationID, Kind: r.Kind, Charge: r.Charge,
				FromDrain: true, RecordID: r.RecordID})
			money = append(money, Book(r.Charge, l.shortfall))
		}
	}
	// The holds are listed (S's commit listed them), so the lease closes
	// before its holds' time runs out: at its expiry, long before the
	// expiry plus MaxLife and the grace.
	row := readLease(d.t, d.s, l.ref)
	if err := checkDecoded(row, d.leaseWant(l)); err != nil {
		d.fatalf("lease %s as ReadLease decodes it: %v", l.ref.LeaseID, err)
	}
	if row.HoldsListedSeq != (spanner.NullInt64{Int64: l.seq, Valid: true}) {
		d.fatalf("lease %s's holds listed at %v, and S's commit listed them at %d", l.ref.LeaseID, row.HoldsListedSeq,
			l.seq)
	}
	now := row.Expiry
	if len(winners) > 0 {
		// Still, a row whose winner is not stored holds the close back,
		// and the refusal moves nothing, every row read at once after it.
		d.unchanged(l.ref, "close", func() {
			got, err := d.s.CloseLease(ctx, l.ref, l.version, read, now)
			if err != nil || got.Refused != RefusedUndecided || got.Released != 0 {
				d.fatalf("a close over undecided drain rows: %+v %v", got, err)
			}
		})
		d.seen["refused close: undecided"]++
		before := headrooms(readRows(d.t, d.ws))
		result, ts, err := walkCommit(d.s, CommitRequest{Ref: l.ref, ReadVersion: l.version, AppliedSeq: l.seq,
			Money: money, Winners: winners}, "")
		if err != nil {
			d.fatalf("committing the drain log's winners: %v", err)
		}
		d.landed("commit", ts)
		if result.State != l.state {
			d.fatalf("the winners' commit left lease %s %q, and the walk has it %q", l.ref.LeaseID, result.State,
				l.state)
		}
		got2 := []CommitResult{result}
		l.advance(d, "the drain log's winners' commit", got2[0].NewVersion)
		l.packs = append(l.packs, Pack{CommitVersion: l.version, Winners: winners})
		d.checkPacks(l)
		if len(winners) > 1 {
			d.seen["a pack of several winners"]++
		}
		pastFirst, faults := l.bookedEach(money)
		if pastFirst {
			d.seen["a booking past the first donor"]++
		}
		if !slices.Equal(got2[0].Faults, faults) {
			d.fatalf("the drain log's winners' commit faulted %v, and the walk's ledger %v", got2[0].Faults, faults)
		}
		for _, w := range winners {
			l.committed[w.RecordID] = true
		}
		d.checkLeaseRow(l, "the winners' commit")
		// The whole workspace, before the close could cover what the
		// winners' commit did to a credit row.
		d.check(before, headrooms(readRows(d.t, d.ws)))
	}
	room := l.room()
	got, err := d.s.CloseLease(ctx, l.ref, l.version, read, now)
	if err != nil || got.Refused != "" || got.Released != room {
		d.fatalf("the close of lease %s, room %d: %+v %v", l.ref.LeaseID, room, got, err)
	}
	l.closedAt = within{d.stamps.last, got.CommitTS}
	d.landed("close", got.CommitTS)
	l.state = "closed"
	l.advance(d, "the close", got.NewVersion)
	if l.gaveBack(got.Released) {
		d.seen["a return before the last donor"]++
	}
	d.seen["close"]++
}

// checkLeaseRow holds the lease's row, every column, to the walk's account
// after a write inside a step, before a later write could cover what it
// did.
func (d *debtWalk) checkLeaseRow(l *debtLease, after string) {
	d.t.Helper()
	if err := checkReads(d.s, l.ref, d.leaseWant(l)); err != nil {
		d.fatalf("lease %s after %s: %v", l.ref.LeaseID, after, err)
	}
}

// advance moves the lease's version on by one, as each commit and close
// that lands does, and holds the version the store answered what to it.
func (l *debtLease) advance(d *debtWalk, what string, answered int64) {
	d.t.Helper()
	l.version++
	if answered != l.version {
		d.fatalf("%s of lease %s answered version %d, and the walk's is %d", what, l.ref.LeaseID, answered, l.version)
	}
}

// checkRow holds a drain row, field by field, to what the walk appended.
func (d *debtWalk) checkRow(l *debtLease, r DrainRow) {
	d.t.Helper()
	t, ok := l.rows[r.RecordID]
	if !ok || r.AuthorizationID != t.AuthorizationID || r.Kind != t.Kind || r.Charge != t.Charge ||
		r.Estimate != t.Estimate || r.DoorRaise != l.raises[r.RecordID] || !bytes.Equal(r.Digest, t.Digest) ||
		!bytes.Equal(r.Money, t.Money) || r.Cause != t.Cause || r.SnapshotOwnerSeq.Valid ||
		l.stamped[r.RecordID].IsZero() || !r.CommitTS.Equal(l.stamped[r.RecordID]) {
		d.fatalf("lease %s's drain row %+v, and the walk appended %+v raising %d", l.ref.LeaseID, r, t,
			l.raises[r.RecordID])
	}
}

// checkPacks holds each of the lease's packs, its stored winner count and
// its winners as a read decodes them, to its commit's, after a commit.
func (d *debtWalk) checkPacks(l *debtLease) {
	d.t.Helper()
	counts, err := winnerCounts(l.ref)
	if err != nil {
		d.fatalf("reading lease %s's winner counts: %v", l.ref.LeaseID, err)
	}
	want := make([]int64, len(l.packs))
	for i, p := range l.packs {
		want[i] = int64(len(p.Winners))
	}
	if !slices.Equal(counts, want) {
		d.fatalf("lease %s's packs' winner counts are %v, and the walk's commits stored %v", l.ref.LeaseID, counts,
			want)
	}
	ctx := context.Background()
	lo := spannerNow(d.t)
	packs, read, err := d.s.LoadWinners(ctx, l.ref)
	hi := spannerNow(d.t)
	if err != nil {
		d.fatalf("lease %s's packs: %v", l.ref.LeaseID, err)
	}
	if read.Before(lo) || read.After(hi) {
		d.fatalf("lease %s's winners read at %v, between reads at %v and %v", l.ref.LeaseID, read, lo, hi)
	}
	if why := samePacks(packs, l.packs); why != "" {
		d.fatalf("lease %s's packs as LoadWinners reads them: %s", l.ref.LeaseID, why)
	}
	// Load reads the packs once the lease is not open, listed holds or
	// not, and none while it is: the same packs then.
	loaded, err := d.s.Load(ctx, l.ref)
	if err != nil {
		d.fatalf("lease %s's load: %v", l.ref.LeaseID, err)
	}
	if l.state == "open" {
		if len(loaded.Packs) != 0 {
			d.fatalf("lease %s open, and Load reads %d packs", l.ref.LeaseID, len(loaded.Packs))
		}
	} else if why := samePacks(loaded.Packs, l.packs); why != "" {
		d.fatalf("lease %s's packs as Load reads them: %s", l.ref.LeaseID, why)
	}
}

// samePacks says how the packs read differ from the walk's, if they do:
// their number, and each pack's version, work not done, and winners.
func samePacks(got, want []Pack) string {
	if len(got) != len(want) {
		return fmt.Sprintf("%d packs read, and the walk's commits stored %d", len(got), len(want))
	}
	for i, p := range got {
		w := want[i]
		if p.CommitVersion != w.CommitVersion || p.WorkDoneAt.Valid || len(p.Winners) != len(w.Winners) ||
			(len(w.Winners) > 0 && !reflect.DeepEqual(p.Winners, w.Winners)) {
			return fmt.Sprintf("pack %d is %+v, and its commit stored %+v", i, p, w)
		}
	}
	return ""
}

// check holds the workspace's rows to CreditDebt's claims, after an
// operation that moved the headroom from before to after.
func (d *debtWalk) check(before, after []int64) {
	d.t.Helper()
	ctx := context.Background()
	if problems, err := d.s.CheckIdentity(ctx, d.ws); err != nil || len(problems) > 0 {
		d.fatalf("the identity: %v %v", problems, err)
	}
	rows := readRows(d.t, d.ws)
	var credits int64
	for _, r := range rows {
		credits += r.credits
	}
	if credits != d.credits {
		d.fatalf("credit %d, and %d was seeded", credits, d.credits)
	}
	// Each shard's usage and reservation are the walk's ledger's, and every
	// lease's donor rows, a closed lease's too, are its donors.
	booked, held := make([]int64, len(rows)), make([]int64, len(rows))
	want := map[string]donor{}
	for _, l := range d.leases {
		var allocation int64
		for _, dn := range l.donors {
			booked[dn.shard] += dn.consumed
			held[dn.shard] += dn.allocation - dn.consumed
			allocation += dn.allocation
			want[fmt.Sprintf("%s/%d", l.ref.LeaseID, dn.shard)] = dn
		}
		booked[l.donors[0].shard] += l.fault
		if allocation != l.allocation() {
			d.fatalf("lease %s's donors hold %d, and its allocation is %d", l.ref.LeaseID, allocation, l.allocation())
		}
	}
	for shard, r := range rows {
		if r.usage != booked[shard] || r.reserved != held[shard] {
			d.fatalf("shard %d's usage is %d and reservation %d; the walk booked %d on it and holds %d", shard,
				r.usage, r.reserved, booked[shard], held[shard])
		}
	}
	donors := 0
	err := shared.Single().Query(ctx, spanner.Statement{
		SQL:    `SELECT lease_id, credit_shard, allocation, consumed FROM tr_lease_donor WHERE workspace_id = @w`,
		Params: map[string]any{"w": d.ws},
	}).Do(func(r *spanner.Row) error {
		var lease string
		var got donor
		if err := r.Columns(&lease, &got.shard, &got.allocation, &got.consumed); err != nil {
			return err
		}
		donors++
		if dn, ok := want[fmt.Sprintf("%s/%d", lease, got.shard)]; !ok || dn != got {
			d.fatalf("lease %s's donor %+v, and the walk's is %+v", lease, got, dn)
		}
		return nil
	})
	if err != nil {
		d.fatalf("reading the donors: %v", err)
	}
	if donors != len(want) {
		d.fatalf("%d donor rows, and the walk's leases have %d donors", donors, len(want))
	}
	for _, l := range d.leases {
		if err := checkReads(d.s, l.ref, d.leaseWant(l)); err != nil {
			d.fatalf("lease %s: %v", l.ref.LeaseID, err)
		}
	}
	// Each lease's money is what the walk did to it, column by column, and
	// its front doors' raises are its drain rows', in two reads of the whole
	// workspace.
	type money struct {
		granted, shortfall, doorRaised, returned, consumed, fault, allocation, appends, raised int64
		state                                                                                  string
		raises                                                                                 map[string]int64
	}
	got := map[string]*money{}
	err = shared.Single().Query(ctx, spanner.Statement{
		SQL: `SELECT lease_id, granted, shortfall_total, door_raised, returned, consumed, fault_usage, allocation, state
		        FROM tr_lease WHERE workspace_id = @w`,
		Params: map[string]any{"w": d.ws},
	}).Do(func(r *spanner.Row) error {
		var lease string
		var m money
		if err := r.Columns(&lease, &m.granted, &m.shortfall, &m.doorRaised, &m.returned, &m.consumed, &m.fault,
			&m.allocation, &m.state); err != nil {
			return err
		}
		got[lease] = &m
		return nil
	})
	if err == nil {
		leases := map[string]*debtLease{}
		for _, l := range d.leases {
			leases[l.ref.LeaseID] = l
		}
		err = shared.Single().Query(ctx, spanner.Statement{
			SQL:    `SELECT lease_id, ` + drainColumns + ` FROM tr_lease_drain WHERE workspace_id = @w`,
			Params: map[string]any{"w": d.ws},
		}).Do(func(r *spanner.Row) error {
			var lease string
			var row DrainRow
			if err := r.Columns(&lease, &row.AuthorizationID, &row.RecordID, &row.Kind, &row.Charge, &row.Estimate,
				&row.DoorRaise, &row.Digest, &row.Money, &row.SnapshotOwnerSeq, &row.Cause, &row.CommitTS); err != nil {
				return err
			}
			record, raised := row.RecordID, row.DoorRaise
			if l := leases[lease]; l != nil {
				d.checkRow(l, row)
			}
			if m := got[lease]; m != nil {
				m.appends++
				m.raised += raised
				if m.raises == nil {
					m.raises = map[string]int64{}
				}
				m.raises[record] = raised
			}
			return nil
		})
	}
	if err != nil {
		d.fatalf("reading the leases: %v", err)
	}
	if len(got) != len(d.leases) {
		d.fatalf("the workspace has %d leases, and the walk granted %d", len(got), len(d.leases))
	}
	for _, l := range d.leases {
		m := got[l.ref.LeaseID]
		if m == nil || m.granted != l.granted || m.shortfall != l.shortfall || m.doorRaised != l.doorRaised ||
			m.returned != l.returned || m.consumed != l.consumed || m.fault != l.fault || m.state != l.state ||
			m.allocation != l.allocation() || (l.state == "closed" && m.allocation != m.consumed) ||
			m.appends != int64(l.appends) || m.raised != l.doorRaised || len(m.raises) != len(l.raises) {
			d.fatalf("lease %s holds %+v, and the walk did %+v", l.ref.LeaseID, m, *l)
		}
		for record, raise := range l.raises {
			if got, ok := m.raises[record]; !ok || got != raise {
				d.fatalf("lease %s's drain row %s raised %d, and the append %d", l.ref.LeaseID, record, got, raise)
			}
		}
	}
	negative := false
	for i, h := range after {
		negative = negative || h < 0
		if h < 0 && before[i] >= 0 {
			d.seen["debt"]++
		}
		if before[i] < 0 && h > before[i] {
			d.seen["debt repaid"]++
		}
	}
	if !negative {
		return
	}
	for i := range after {
		if after[i] <= before[i] {
			continue
		}
		if before[i] >= 0 || after[i] > 0 {
			d.fatalf("shard %d rose from %d to %d while the workspace is in debt: %v to %v", i, before[i], after[i],
				before, after)
		}
		for j := range i {
			if after[j] < 0 {
				d.fatalf("shard %d rose while shard %d below it stays negative: %v to %v", i, j, before, after)
			}
		}
	}
}
