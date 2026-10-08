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
// raises and closes. After each, the rows keep CreditDebt's claims
// (proofs/CreditDebt.tla) on what the store wrote:
//
//   - ShardIdentity, AllocationAccounted, DebtMarksEveryShard and
//     MarkMeansDebt, through CheckIdentity and the lease row's checks;
//   - UsageIsBooked: each shard's usage is what was booked against the
//     leases' donors on it, and their faults on the first donor's shard;
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
		"door raise", "close", "refused close: undecided", "debt", "debt repaid"} {
		if seen[k] == 0 {
			t.Errorf("no walk made a %s", k)
		}
	}
}

// debtLease is a lease the walk holds, with the money its row must show:
// granted, the shortfall total, the front doors' raises, what was returned,
// what was booked matched against the allocation and what as a fault; each
// drain row's raise by record ID, and the rows whose winners are not yet
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
	raises     map[string]int64
	undecided  []Winner
}

// allocation is what the lease's row must hold as its allocation.
func (l *debtLease) allocation() int64 { return l.granted + l.shortfall + l.doorRaised - l.returned }

// booked books a charge as the store does (§4.2): after the shortfall total
// rises to the record's, matched up to the lease's room, the rest a fault.
func (l *debtLease) booked(charge, total int64) {
	l.shortfall = max(l.shortfall, total)
	matched := min(charge, max(l.allocation()-l.consumed, 0))
	l.consumed += matched
	l.fault += charge - matched
}

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
	d.seen["grant"]++
	d.leases = append(d.leases, &debtLease{ref: LeaseRef{d.ws, req.LeaseID}, state: "open", granted: req.Amount,
		raises: map[string]int64{}})
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
	l.booked(charge, total)
	d.seen["booking"]++
	if len(got.Faults) > 0 {
		d.seen["fault"]++
	}
}

// giveBack returns part of what the lease has not booked.
func (d *debtWalk) giveBack(ctx context.Context, l *debtLease) {
	d.t.Helper()
	row := readLease(d.t, d.s, l.ref)
	room := row.Allocation - row.Consumed
	if room <= 0 {
		d.book(ctx, l)
		return
	}
	amount := 1 + d.rng.Int63n(room)
	d.note("return %s %d", l.ref.LeaseID, amount)
	d.commit(ctx, l, []MoneyOp{Return(amount)}, false)
	l.returned += amount
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
	got, err := d.s.Append(ctx, DrainTerminal{Ref: l.ref, AuthorizationID: "a" + record, RecordID: record, Kind: "settle",
		Charge: 2 + raise, Estimate: 2, Money: []byte(`{}`), Cause: "debt walk"})
	if err != nil || got.Refused != "" || got.Raise != raise {
		d.fatalf("the append: %+v %v", got, err)
	}
	l.doorRaised += raise
	l.raises[record] = raise
	l.undecided = append(l.undecided, Winner{AuthorizationID: "a" + record, Kind: "settle", Charge: 2 + raise,
		FromDrain: true, RecordID: record})
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
	_, read, err := d.s.ReadDrainSince(ctx, l.ref, time.Time{})
	if err != nil {
		d.fatalf("reading the drain log: %v", err)
	}
	if len(l.undecided) > 0 {
		// The holds are listed (S's commit listed them), and still a row
		// whose winner is not stored holds the close back.
		got, err := d.s.CloseLease(ctx, l.ref, l.version, read, time.Now().Add(time.Hour))
		if err != nil || got.Refused != RefusedUndecided {
			d.fatalf("a close over undecided drain rows: %+v %v", got, err)
		}
		d.seen["refused close: undecided"]++
		var money []MoneyOp
		for _, w := range l.undecided {
			money = append(money, Book(w.Charge, l.shortfall))
		}
		got2, _, err := d.s.Commit(ctx, []CommitRequest{{Ref: l.ref, ReadVersion: l.version, AppliedSeq: l.seq,
			Money: money, Winners: l.undecided}})
		if err != nil || len(got2) != 1 || got2[0].Refused != "" {
			d.fatalf("committing the drain log's winners: %+v %v", got2, err)
		}
		l.version = got2[0].NewVersion
		for _, w := range l.undecided {
			l.booked(w.Charge, l.shortfall)
		}
		l.undecided = nil
	}
	row := readLease(d.t, d.s, l.ref)
	got, err := d.s.CloseLease(ctx, l.ref, l.version, read, time.Now().Add(time.Hour))
	if err != nil || got.Refused != "" || got.Released != row.Allocation-row.Consumed {
		d.fatalf("the close of %+v: %+v %v", row, got, err)
	}
	l.state, l.version = "closed", got.NewVersion
	l.returned += got.Released
	d.seen["close"]++
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
	booked := make([]int64, len(rows))
	first := map[string]int64{}
	err := shared.Single().Query(ctx, spanner.Statement{
		SQL: `SELECT lease_id, credit_shard, consumed FROM tr_lease_donor WHERE workspace_id = @w
		       ORDER BY lease_id, credit_shard`,
		Params: map[string]any{"w": d.ws},
	}).Do(func(r *spanner.Row) error {
		var lease string
		var shard, consumed int64
		if err := r.Columns(&lease, &shard, &consumed); err != nil {
			return err
		}
		if _, ok := first[lease]; !ok {
			first[lease] = shard
		}
		booked[shard] += consumed
		return nil
	})
	if err == nil {
		err = shared.Single().Query(ctx, spanner.Statement{
			SQL:    `SELECT lease_id, fault_usage FROM tr_lease WHERE workspace_id = @w`,
			Params: map[string]any{"w": d.ws},
		}).Do(func(r *spanner.Row) error {
			var lease string
			var fault int64
			if err := r.Columns(&lease, &fault); err != nil {
				return err
			}
			booked[first[lease]] += fault
			return nil
		})
	}
	if err != nil {
		d.fatalf("reading the bookings: %v", err)
	}
	for shard, r := range rows {
		if r.usage != booked[shard] {
			d.fatalf("shard %d's usage is %d, and %d was booked on it", shard, r.usage, booked[shard])
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
		err = shared.Single().Query(ctx, spanner.Statement{
			SQL:    `SELECT lease_id, record_id, door_raise FROM tr_lease_drain WHERE workspace_id = @w`,
			Params: map[string]any{"w": d.ws},
		}).Do(func(r *spanner.Row) error {
			var lease, record string
			var raised int64
			if err := r.Columns(&lease, &record, &raised); err != nil {
				return err
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
