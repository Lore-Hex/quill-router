//go:build !race

package store

import (
	"context"
	"fmt"
	"math/rand"
	"os"
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
// 6) from FASTPATH_WALK_SEED (default a fixed seed).
func TestTheStoreKeepsCreditDebtsClaims(t *testing.T) {
	s := spikeStore(t, func(c *Config) { c.Allowance = 60 })
	walks, seed := 6, int64(20261008)
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
		d := newDebtWalk(t, s, seed+int64(i))
		d.run()
		for k, n := range d.seen {
			seen[k] += n
		}
	}
	t.Logf("operations: %v", seen)
	for _, k := range []string{"grant", "refused grant", "shortfall-write", "booking", "fault", "return", "drain",
		"door raise", "close", "refused close: undecided", "debt", "debt repaid", "a lease of two donors or more",
		"a booking past the first donor", "a return before the last donor"} {
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
	committed  map[string]bool
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
// a donor after the first took part.
func (l *debtLease) booked(charge, total int64) (pastFirst bool) {
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
	return pastFirst
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

type debtWalk struct {
	t       *testing.T
	s       *Store
	ws      string
	rng     *rand.Rand
	seed    int64
	credits int64
	leases  []*debtLease
	history []string
	seen    map[string]int
}

func newDebtWalk(t *testing.T, s *Store, seed int64) *debtWalk {
	t.Helper()
	rng := rand.New(rand.NewSource(seed))
	credits := []int64{int64(rng.Intn(30)), int64(rng.Intn(30)), int64(rng.Intn(30))}
	d := &debtWalk{t: t, s: s, ws: seedWorkspace(t, credits...), rng: rng, seed: seed, seen: map[string]int{}}
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
		case op == 7:
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
}

func (d *debtWalk) note(format string, args ...any) {
	d.history = append(d.history, fmt.Sprintf(format, args...))
}

// grant reports whether the workspace granted a lease.
func (d *debtWalk) grant(ctx context.Context) bool {
	d.t.Helper()
	req := grantOf(d.ws, int64(1+d.rng.Intn(20)))
	d.note("grant %d", req.Amount)
	rows := readRows(d.t, d.ws)
	got, err := d.s.Grant(ctx, req)
	if err != nil {
		d.fatalf("the grant: %v", err)
	}
	if got.Refused != "" {
		d.seen["refused grant"]++
		if after := readRows(d.t, d.ws); !slices.Equal(after, rows) {
			d.fatalf("a refused grant changed the credit rows: %+v, then %+v", rows, after)
		}
		return false
	}
	// A grant reserves L on donor shards in ascending order, each up to its
	// headroom.
	l := &debtLease{ref: LeaseRef{d.ws, req.LeaseID}, state: "open", granted: req.Amount,
		rows: map[string]DrainTerminal{}, raises: map[string]int64{}, committed: map[string]bool{}}
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
	got, _, err := d.s.Commit(ctx, []CommitRequest{req})
	if err != nil || len(got) != 1 || got[0].Refused != "" {
		d.fatalf("the commit %+v: %+v %v", req, got, err)
	}
	l.version, l.seq = got[0].NewVersion, seq
	l.stored = l.stored || boundary
	return got[0]
}

// book books a charge, carrying a shortfall total that sometimes raises the
// stored one; a charge past the allocation is a fault.
func (d *debtWalk) book(ctx context.Context, l *debtLease) {
	d.t.Helper()
	charge, total := int64(d.rng.Intn(16)), l.shortfall
	if d.rng.Intn(3) == 0 {
		total += int64(d.rng.Intn(6))
	}
	d.note("book %s %d at %d", l.ref.LeaseID, charge, total)
	got := d.commit(ctx, l, []MoneyOp{Book(charge, total)}, false)
	if l.booked(charge, total) {
		d.seen["a booking past the first donor"]++
	}
	d.seen["booking"]++
	if len(got.Faults) > 0 {
		d.seen["fault"]++
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
	if ok, _, err := d.s.OwnerMarkDraining(ctx, owner, l.ref); err != nil || !ok {
		d.fatalf("the draining write: %v %v", ok, err)
	}
	l.state = "draining"
	d.seen["drain"]++
}

// doorRaise appends a front door's settle at or past its estimate, which
// raises the lease by the difference, none when there is none.
func (d *debtWalk) doorRaise(ctx context.Context, l *debtLease) {
	d.t.Helper()
	l.appends++
	raise := int64(d.rng.Intn(7))
	record := fmt.Sprintf("d%d", l.appends)
	d.note("door %s +%d", l.ref.LeaseID, raise)
	t := DrainTerminal{Ref: l.ref, AuthorizationID: "a" + record, RecordID: record, Kind: "settle", Charge: 2 + raise,
		Estimate: 2, Money: []byte(`{}`), Cause: "debt walk"}
	got, err := d.s.Append(ctx, t)
	if err != nil || got.Refused != "" || got.Raise != raise {
		d.fatalf("the append: %+v %v", got, err)
	}
	l.doorRaised += raise
	l.raised(raise)
	l.rows[record], l.raises[record] = t, raise
	d.seen["door raise"]++
}

// close stores S if it is not, reads the drain log to the end, commits the
// winners of its rows, which a close before then must refuse, and closes the
// lease, which returns its remainder.
func (d *debtWalk) close(ctx context.Context, l *debtLease) {
	d.t.Helper()
	d.note("close %s", l.ref.LeaseID)
	if !l.stored {
		d.commit(ctx, l, nil, true)
	}
	rows, read, err := d.s.ReadDrainSince(ctx, l.ref, time.Time{})
	if err != nil {
		d.fatalf("reading the drain log: %v", err)
	}
	// The winners are the rows as read back, each its authorization's first
	// terminal, held to what the walk appended.
	var winners []Winner
	var money []MoneyOp
	for _, r := range rows {
		d.checkRow(l, r.RecordID, r.AuthorizationID, r.Kind, r.Charge, r.Estimate, r.DoorRaise)
		if !l.committed[r.RecordID] {
			winners = append(winners, Winner{AuthorizationID: r.AuthorizationID, Kind: r.Kind, Charge: r.Charge,
				FromDrain: true, RecordID: r.RecordID})
			money = append(money, Book(r.Charge, l.shortfall))
		}
	}
	if len(rows) != len(l.rows) {
		d.fatalf("lease %s's drain log reads %d rows, and the walk appended %d", l.ref.LeaseID, len(rows), len(l.rows))
	}
	if len(winners) > 0 {
		// The holds are listed (S's commit listed them), and still a row
		// whose winner is not stored holds the close back.
		got, err := d.s.CloseLease(ctx, l.ref, l.version, read, time.Now().Add(time.Hour))
		if err != nil || got.Refused != RefusedUndecided {
			d.fatalf("a close over undecided drain rows: %+v %v", got, err)
		}
		d.seen["refused close: undecided"]++
		got2, _, err := d.s.Commit(ctx, []CommitRequest{{Ref: l.ref, ReadVersion: l.version, AppliedSeq: l.seq,
			Money: money, Winners: winners}})
		if err != nil || len(got2) != 1 || got2[0].Refused != "" {
			d.fatalf("committing the drain log's winners: %+v %v", got2, err)
		}
		l.version = got2[0].NewVersion
		for _, w := range winners {
			if l.booked(w.Charge, l.shortfall) {
				d.seen["a booking past the first donor"]++
			}
			l.committed[w.RecordID] = true
		}
	}
	room := l.room()
	got, err := d.s.CloseLease(ctx, l.ref, l.version, read, time.Now().Add(time.Hour))
	if err != nil || got.Refused != "" || got.Released != room {
		d.fatalf("the close of lease %s, room %d: %+v %v", l.ref.LeaseID, room, got, err)
	}
	l.state, l.version = "closed", got.NewVersion
	if l.gaveBack(got.Released) {
		d.seen["a return before the last donor"]++
	}
	d.seen["close"]++
}

// checkRow holds a drain row to what the walk appended.
func (d *debtWalk) checkRow(l *debtLease, record, auth, kind string, charge, estimate, raise int64) {
	d.t.Helper()
	t, ok := l.rows[record]
	if !ok || auth != t.AuthorizationID || kind != t.Kind || charge != t.Charge || estimate != t.Estimate ||
		raise != l.raises[record] {
		d.fatalf("lease %s's drain row %s is %s %s of %d on %d raising %d, and the walk appended %+v raising %d",
			l.ref.LeaseID, record, auth, kind, charge, estimate, raise, t, l.raises[record])
	}
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
			SQL: `SELECT lease_id, record_id, authorization_id, kind, charge, estimate, door_raise FROM tr_lease_drain
			       WHERE workspace_id = @w`,
			Params: map[string]any{"w": d.ws},
		}).Do(func(r *spanner.Row) error {
			var lease, record, auth, kind string
			var charge, estimate, raised int64
			if err := r.Columns(&lease, &record, &auth, &kind, &charge, &estimate, &raised); err != nil {
				return err
			}
			if l := leases[lease]; l != nil {
				d.checkRow(l, record, auth, kind, charge, estimate, raised)
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
