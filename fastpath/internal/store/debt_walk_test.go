package store

import (
	"context"
	"fmt"
	"math/rand"
	"os"
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
		"door raise", "close", "debt", "debt repaid"} {
		if seen[k] == 0 {
			t.Errorf("no walk made a %s", k)
		}
	}
}

type debtLease struct {
	ref       LeaseRef
	state     string
	version   int64
	seq       int64
	shortfall int64
	stored    bool
	appends   int
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
	got, err := d.s.Grant(ctx, req)
	if err != nil {
		d.fatalf("the grant: %v", err)
	}
	if got.Refused != "" {
		d.seen["refused grant"]++
		return false
	}
	d.seen["grant"]++
	d.leases = append(d.leases, &debtLease{ref: LeaseRef{d.ws, req.LeaseID}, state: "open"})
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
	l.shortfall = total
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

// doorRaise appends a front door's settle past its estimate, which raises
// the lease by the difference.
func (d *debtWalk) doorRaise(ctx context.Context, l *debtLease) {
	d.t.Helper()
	l.appends++
	raise := int64(1 + d.rng.Intn(6))
	record := fmt.Sprintf("d%d", l.appends)
	d.note("door %s +%d", l.ref.LeaseID, raise)
	got, err := d.s.Append(ctx, DrainTerminal{Ref: l.ref, AuthorizationID: "a" + record, RecordID: record, Kind: "settle",
		Charge: 2 + raise, Estimate: 2, Money: []byte(`{}`), Cause: "debt walk"})
	if err != nil || got.Refused != "" || got.Raise != raise {
		d.fatalf("the append: %+v %v", got, err)
	}
	d.seen["door raise"]++
}

// close stores S if it is not, reads the drain log to the end, and closes
// the lease, which returns its remainder.
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
	got, err := d.s.CloseLease(ctx, l.ref, l.version, read, time.Now().Add(time.Hour))
	if err != nil || got.Refused != "" {
		d.fatalf("the close: %+v %v", got, err)
	}
	l.state, l.version = "closed", got.NewVersion
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
