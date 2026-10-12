//go:build !race

package store

import (
	"bytes"
	"context"
	"fmt"
	"maps"
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
	// One walk more, scripted where the random ones seldom go: a workspace
	// marked in debt by a fault, then a return whose last donor's room is
	// more than the debt, so the money repays it and the excess must stay
	// on that donor's shard, the second, and a store that puts it on the
	// first fails the check after the return.
	d := newDebtWalkWith(t, spikeStore(t, func(c *Config) { c.Allowance, c.Floor = 30, 0 }), seed-1, []int64{10, 12, 0})
	d.script()
	for k, n := range d.seen {
		seen[k] += n
	}
	t.Logf("operations: %v", seen)
	for _, k := range []string{"grant", "refused grant", "shortfall-write", "booking", "fault", "faults in one commit",
		"return", "drain",
		"door raise", "close", "refused close: undecided", "refused commit", "debt", "debt repaid", "a lease of two donors or more",
		"a booking past the first donor", "a return before the last donor", "a batch its order changes",
		"a return its place changes", "a pack of several winners", "refused grant: " + string(RefusedInDebt),
		"drain rows whose IDs sort before an earlier row's", "drain rows whose IDs sort after an earlier row's",
		"refused grant: " + string(RefusedAllowance), "refused grant: " + string(RefusedFloor),
		"refused grant: the floor alone", "a grant beside a listed lease", "a listed hold",
		"refused grant: the listed holds alone", "refused close: holds open", "shortfall write: the stored total again",
		"shortfall write: below the stored total", "shortfall write below a total a booking raised",
		"a release's excess kept on its shard", "a commit of two leases"} {
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
	// holds is the lease's open holds, which S's commit listed, by
	// authorization: each its row as the commit put it, until a release
	// winner ends it.
	holds map[string]HoldRow
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

// shards is the walk's own replica of the workspace's credit rows: each
// shard's credit, usage and reservation, and the mark, moved by every write
// that lands, in the store's order, under section 4.7's rules as
// proofs/CreditDebt.tla states them (Cover, Squared, Distribute, Inflow),
// written here from the spec. After every operation the rows must be it.
type shards struct {
	credit, usage, reserved []int64
	marked                  bool
	// touched is each row the store wrote since the last check, which
	// stamps updated_at; updatedAt is each row's stamp as last read.
	touched   []bool
	updatedAt []spanner.NullTime
}

// touch notes a row the store writes.
func (c *shards) touch(i int) { c.touched[i] = true }

// headroom is each shard's credit less its usage and reservation (Room).
func (c *shards) headroom() []int64 {
	out := make([]int64, len(c.credit))
	for i := range c.credit {
		out[i] = c.credit[i] - c.usage[i] - c.reserved[i]
	}
	return out
}

// squared ends a write (Squared): with the signed sum negative every shard
// is marked and nothing moves; otherwise the lowest negative shard takes
// from the lowest positive one until none is negative (Cover), credit
// moving, and the mark is cleared.
func (c *shards) squared() {
	h := c.headroom()
	var sum int64
	for _, v := range h {
		sum += v
	}
	marked := sum < 0
	if marked != c.marked {
		// The mark is written on every row when it changes.
		for i := range c.touched {
			c.touch(i)
		}
	}
	c.marked = marked
	if marked {
		return
	}
	for {
		neg := slices.IndexFunc(h, func(v int64) bool { return v < 0 })
		if neg < 0 {
			break
		}
		pos := slices.IndexFunc(h, func(v int64) bool { return v > 0 })
		x := min(-h[neg], h[pos])
		h[neg] += x
		h[pos] -= x
		c.credit[neg] += x
		c.credit[pos] -= x
		c.touch(neg)
		c.touch(pos)
	}
}

// reserve is a grant's reservation on each donor's shard.
func (c *shards) reserve(donors []donor) {
	for _, dn := range donors {
		c.reserved[dn.shard] += dn.allocation
		c.touch(int(dn.shard))
	}
}

// raiseFirst is a rise of a lease's first donor's allocation, reserved on
// its shard, squared if that leaves the shard negative (adjust). A rise of
// nothing writes nothing: a door raise of 0 leaves the row, and its stamp,
// as they are.
func (c *shards) raiseFirst(shard, rise int64) {
	if rise == 0 {
		return
	}
	c.reserved[shard] += rise
	c.touch(int(shard))
	if c.headroom()[shard] < 0 {
		c.squared()
	}
}

// settle is a commit's money: each shard's change of reservation and usage,
// shard by shard in shard order, squared once if any shard went negative,
// then each return's release in order (settleCreditRows). It counts the
// releases whose excess past the debt stayed on a shard other than the
// first, which tells where the excess goes.
func (c *shards) settle(changes map[int64]shardChange, releases []freed) (excessKept int) {
	negative := false
	for _, shard := range slices.Sorted(maps.Keys(changes)) {
		if changes[shard] == (shardChange{}) {
			continue // as settleCreditRows skips a shard with no change
		}
		c.reserved[shard] += changes[shard].reserved
		c.usage[shard] += changes[shard].usage
		c.touch(int(shard))
		negative = negative || c.headroom()[shard] < 0
	}
	if negative {
		c.squared()
	}
	for _, r := range releases {
		if c.release(r.shard, r.amount) {
			excessKept++
		}
	}
	return excessKept
}

// release frees amount of a shard's reservation. Marked, or with a shard
// negative, the money repays the negative shards lowest first, each to
// zero, what is left staying on the shard that freed it (Distribute), and
// the rows are squared (Inflow); otherwise nothing more moves. It reports
// whether the money repaid debt and left an excess on a shard other than
// the first.
func (c *shards) release(shard, amount int64) bool {
	c.reserved[shard] -= amount
	c.touch(int(shard))
	h := c.headroom()
	if !c.marked && !slices.ContainsFunc(h, func(v int64) bool { return v < 0 }) {
		return false
	}
	w := slices.Clone(h)
	w[shard] -= amount
	left := amount
	for i, v := range w {
		if left == 0 {
			break
		}
		if v < 0 {
			paid := min(left, -v)
			w[i] += paid
			left -= paid
		}
	}
	w[shard] += left
	for i := range w {
		c.credit[i] += w[i] - h[i]
		if w[i] != h[i] {
			c.touch(i)
		}
	}
	c.squared()
	return left > 0 && left < amount && shard != 0
}

// debt is what the negative shards are short, together.
func (c *shards) debt() int64 {
	var out int64
	for _, v := range c.headroom() {
		out += min(v, 0)
	}
	return -out
}

// shardChange is a commit's change to one credit shard, and freed is a
// return's money freed on a donor's shard, as the store applies them.
type shardChange struct{ reserved, usage int64 }
type freed struct{ shard, amount int64 }

// moneyEffect is what a commit of money does to the credit rows, computed
// on a copy as applyMoney computes it: a booking's rise reserved on the
// first donor's shard, each matched part moving reservation to usage on its
// donor's shard, the fault as usage on the first donor's, summed per shard;
// and each return freed on the donors' shards, last donor first, in order.
func (l *debtLease) moneyEffect(money []MoneyOp) (map[int64]shardChange, []freed) {
	c := l.copied()
	changes := map[int64]shardChange{}
	var releases []freed
	add := func(shard, reserved, usage int64) {
		x := changes[shard]
		x.reserved += reserved
		x.usage += usage
		changes[shard] = x
	}
	for _, op := range money {
		if !op.Book {
			left := op.Amount
			for i := len(c.donors) - 1; i >= 0 && left > 0; i-- {
				z := min(c.donors[i].allocation-c.donors[i].consumed, left)
				if z <= 0 {
					continue
				}
				c.donors[i].allocation -= z
				left -= z
				releases = append(releases, freed{c.donors[i].shard, z})
			}
			continue
		}
		if rise := op.ShortfallTotal - c.shortfall; rise > 0 {
			c.shortfall = op.ShortfallTotal
			c.donors[0].allocation += rise
			add(c.donors[0].shard, rise, 0)
		}
		left := op.Charge
		for i := range c.donors {
			x := min(c.donors[i].allocation-c.donors[i].consumed, left)
			if x <= 0 {
				continue
			}
			c.donors[i].consumed += x
			left -= x
			add(c.donors[i].shard, -x, x)
		}
		if left > 0 {
			add(c.donors[0].shard, 0, left)
		}
	}
	return changes, releases
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
	opStart time.Time // Spanner's time read before the step's first write
	credits int64
	shards  shards // the credit rows as the walk's writes left them
	leases  []*debtLease
	history []string
	seen    map[string]int
	stamps  stamps // the writes' commit timestamps
}

func newDebtWalk(t *testing.T, s *Store, seed int64) *debtWalk {
	t.Helper()
	rng := rand.New(rand.NewSource(seed))
	return newDebtWalkWith(t, s, seed, []int64{int64(rng.Intn(30)), int64(rng.Intn(30)), int64(rng.Intn(30))}, rng)
}

// newDebtWalkWith is a walk on a workspace seeded with the credits given,
// its own draws from rng, or from the seed when none is given.
func newDebtWalkWith(t *testing.T, s *Store, seed int64, credits []int64, rngs ...*rand.Rand) *debtWalk {
	t.Helper()
	rng := rand.New(rand.NewSource(seed))
	if len(rngs) > 0 {
		rng = rngs[0]
	}
	d := &debtWalk{t: t, s: s, ws: seedWorkspace(t, credits...), rng: rng, probes: rand.New(rand.NewSource(^seed)),
		batches: rand.New(rand.NewSource(seed ^ 0x5eed)), ids: rand.New(rand.NewSource(seed ^ 0x1d5)), seed: seed,
		seen: map[string]int{}}
	for _, c := range credits {
		d.credits += c
	}
	d.shards = shards{credit: slices.Clone(credits), usage: make([]int64, len(credits)), reserved: make([]int64, len(credits)),
		touched: make([]bool, len(credits)), updatedAt: make([]spanner.NullTime, len(credits))}
	// Benign metadata the store neither reads nor writes, set on every row
	// so a write that reset a column to its default is seen (seeded rows
	// carry defaults, which such a write would leave as they are). By DML:
	// an update mutation on the emulator resets the row's other defaulted
	// columns, the tier among them.
	_, err := shared.ReadWriteTransaction(context.Background(), func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		// Two instants an hour apart, so a write that copies one column into
		// the other shows.
		_, err := txn.Update(ctx, spanner.Statement{
			SQL: `UPDATE tr_credit_balance SET trust_computed_at = @t, pause_epoch = 17, trust_reconciled_through = @u
			       WHERE workspace_id = @w`,
			Params: map[string]any{"t": walkTick, "u": walkTick.Add(time.Hour), "w": d.ws},
		})
		return err
	})
	if err != nil {
		t.Fatalf("seeding the credit rows' metadata: %v", err)
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

// script is the scripted walk: with credits 10, 12 and 0, a lease of 6 on
// the first shard and one of 12 on the first and second (4 and 8), a booking
// of 13 on the first lease, 6 matched and a fault of 7 booked as usage on
// the first shard, which leaves it at -7 against 4 on the second, the sum
// -3, so every row is marked; then the second lease returns its whole room,
// 12, the last donor's 8 first: 7 repays the first shard and the 1 left
// stays on the second, where it was freed, and the mark is cleared.
func (d *debtWalk) script() {
	d.t.Helper()
	ctx := context.Background()
	before := headrooms(readRows(d.t, d.ws))
	step := func(what string) {
		d.t.Helper()
		after := headrooms(readRows(d.t, d.ws))
		d.check(before, after)
		d.note("after %s: %v", what, after)
		before = after
	}
	d.opStart = spannerNow(d.t)
	if !d.grantAmount(ctx, 6) || !d.grantAmount(ctx, 12) {
		d.fatalf("the script's grants were refused")
	}
	step("the grants")
	d.opStart = spannerNow(d.t)
	first, second := d.leases[0], d.leases[1]
	if !slices.Equal(headrooms(readRows(d.t, d.ws)), []int64{0, 4, 0}) || len(second.donors) != 2 ||
		second.donors[1].shard != 1 || second.donors[1].allocation != 8 {
		d.fatalf("the script's grants did not leave 0, 4, 0 with 8 of the second lease on the second shard: %+v",
			second.donors)
	}
	money := []MoneyOp{Book(13, first.shortfall)}
	d.note("book %s %v", first.ref.LeaseID, money)
	got := d.commit(ctx, first, money, false)
	_, faults := first.bookedEach(money)
	if !slices.Equal(got.Faults, faults) || !slices.Equal(faults, []int64{7}) {
		d.fatalf("the script's booking faulted %v, and the walk's ledger %v", got.Faults, faults)
	}
	d.seen["booking"]++
	d.seen["fault"]++
	step("the booking")
	if !d.shards.marked || d.shards.debt() != 7 {
		d.fatalf("the script's booking left the workspace unmarked, or a debt other than 7: %+v", d.shards)
	}
	excess := d.seen["a release's excess kept on its shard"]
	d.opStart = spannerNow(d.t)
	d.giveBack(ctx, second)
	step("the return")
	if d.seen["a release's excess kept on its shard"] != excess+1 || d.shards.marked ||
		!slices.Equal(d.shards.credit, []int64{17, 5, 0}) {
		d.fatalf("the script's return did not leave 1 of excess on the second shard: %+v", d.shards)
	}
	// Then both leases book in one commit, each past its room so each
	// faults, on the one shard both have as first donor: the store sums the
	// two leases' changes to that shard before it writes it once, and a
	// store that kept one lease's change only fails the replica's check.
	d.opStart = spannerNow(d.t)
	d.commitTwo(ctx, first, []MoneyOp{Book(2, first.shortfall)}, second, []MoneyOp{Book(3, second.shortfall)})
	step("the two leases' commit")
	d.checkAllPacks()
}

// commitTwo commits two leases' money in one transaction, as the store
// commits many leases at once, and books each as commit does; the replica
// settles both leases' changes together, summed per shard as the store sums
// them, then their releases in order.
func (d *debtWalk) commitTwo(ctx context.Context, a *debtLease, moneyA []MoneyOp, b *debtLease, moneyB []MoneyOp) {
	d.t.Helper()
	d.note("commit %s %v and %s %v together", a.ref.LeaseID, moneyA, b.ref.LeaseID, moneyB)
	request := func(l *debtLease, money []MoneyOp) CommitRequest {
		seq := l.seq
		if !l.stored {
			seq++
		}
		return CommitRequest{Ref: l.ref, ReadVersion: l.version, AppliedSeq: seq, Money: money}
	}
	reqs := []CommitRequest{request(a, moneyA), request(b, moneyB)}
	changesA, releasesA := a.moneyEffect(moneyA)
	changesB, releasesB := b.moneyEffect(moneyB)
	for shard, c := range changesB {
		x := changesA[shard]
		x.reserved += c.reserved
		x.usage += c.usage
		changesA[shard] = x
	}
	got, ts, err := d.s.Commit(ctx, detached(reqs))
	if err != nil || len(got) != 2 {
		d.fatalf("the two leases' commit: %+v %v", got, err)
	}
	d.landed("commit", ts)
	d.shards.settle(changesA, append(releasesA, releasesB...))
	for i, l := range []*debtLease{a, b} {
		money := [][]MoneyOp{moneyA, moneyB}[i]
		if got[i].Ref != l.ref || got[i].Refused != "" || got[i].State != l.state {
			d.fatalf("the two leases' commit answered %+v for lease %s, which the walk has %q", got[i], l.ref.LeaseID, l.state)
		}
		l.advance(d, "the two leases' commit", got[i].NewVersion)
		l.seq = reqs[i].AppliedSeq
		l.packs = append(l.packs, Pack{CommitVersion: l.version})
		_, faults := l.bookedEach(money)
		if !slices.Equal(got[i].Faults, faults) {
			d.fatalf("the two leases' commit faulted %v for lease %s, and the walk's ledger %v", got[i].Faults, l.ref.LeaseID,
				faults)
		}
		d.checkPacks(l)
		d.checkHolds(l)
		d.checkLeaseRow(l, "the two leases' commit")
	}
	d.seen["a commit of two leases"]++
}

func (d *debtWalk) run() {
	d.t.Helper()
	ctx := context.Background()
	before := headrooms(readRows(d.t, d.ws))
	for range debtSteps {
		d.opStart = spannerNow(d.t)
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
		case d.shards.marked && d.pastDebt(live) != nil:
			// In debt, a lease whose room is more than the debt returns it
			// all: the money repays the debt and the excess must stay on the
			// shard that freed it (giveBack), which the walks must see.
			d.giveBack(ctx, d.pastDebt(live))
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
// is open or draining with its holds not listed, and its open holds'
// estimates once they are listed, against the allowance; and the headroom
// outside leases against the floor. "" is a grant. The walk's workspace is
// never paused and keeps the tier that allows leases, which the store
// checks before the exposure.
func (d *debtWalk) grantRefusal(rows []row, amount int64) Refusal {
	var exposure, listed, headroom int64
	for _, l := range d.leases {
		switch {
		case l.state == "open" || l.state == "draining" && !l.stored:
			exposure += max(l.room(), 0)
		case l.state == "draining":
			for _, h := range l.holds {
				listed += h.Estimate
			}
		}
	}
	for _, r := range rows {
		headroom += r.headroom()
	}
	switch {
	case slices.ContainsFunc(rows, func(r row) bool { return r.marked }):
		return RefusedInDebt
	case exposure+listed > d.s.cfg.Allowance-amount:
		if exposure <= d.s.cfg.Allowance-amount {
			d.seen["refused grant: the listed holds alone"]++
		}
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
	return d.grantAmount(ctx, int64(1+rng.Intn(20)))
}

// grantAmount is a grant of amount, checked as every grant is.
func (d *debtWalk) grantAmount(ctx context.Context, amount int64) bool {
	d.t.Helper()
	req := grantOf(d.ws, amount)
	req.WorkspaceShard, req.KeyStatusVersion = int64(1+d.probes.Intn(9)), int64(10+d.probes.Intn(90))
	d.note("grant %d", req.Amount)
	rows := readRows(d.t, d.ws)
	ref := LeaseRef{d.ws, req.LeaseID}
	// Refused, the grant leaves every row of the workspace as it was, the
	// other leases' rows and holds included (everyRow with no lease).
	whole := LeaseRef{Workspace: d.ws}
	before := d.rows(whole)
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
		// written; the answer carries no grant either.
		if len(got.Donors) != 0 || !got.Expiry.IsZero() || !got.CommitTS.IsZero() {
			d.fatalf("a refused grant answered a grant: %+v", got)
		}
		d.seen["refused grant"]++
		d.seen["refused grant: "+string(got.Refused)]++
		if after := d.rows(whole); after != before {
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
	d.shards.reserve(l.donors)
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
	d.shards.raiseFirst(l.donors[0].shard, total-l.shortfall)
	l.raised(total - l.shortfall)
	l.shortfall = total
	d.probeShortfall(ctx, l)
}

// probeShortfall writes the lease's shortfall total again, and one below
// it, which the store must leave as they are: the total only rises, so
// neither rises anything, carries a commit, or moves a row.
func (d *debtWalk) probeShortfall(ctx context.Context, l *debtLease) {
	d.t.Helper()
	totals := []int64{l.shortfall}
	if l.shortfall > 0 {
		totals = append(totals, d.probes.Int63n(l.shortfall))
	}
	d.unchanged(l.ref, "shortfall write", func() {
		for _, total := range totals {
			got, err := d.s.ShortfallWrite(ctx, owner, l.ref, total)
			if err != nil || got != (ShortfallResult{}) {
				d.fatalf("a shortfall write of %d with %d stored: %+v %v", total, l.shortfall, got, err)
			}
		}
	})
	d.seen["shortfall write: the stored total again"]++
	if len(totals) > 1 {
		d.seen["shortfall write: below the stored total"]++
	}
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
		// Two closes in three list a hold or two still open, each a listed
		// hold with an estimate and no snapshot yet, drawn from the probes'
		// source so the walk's own draws are as they were.
		if d.probes.Intn(3) > 0 {
			l.holds = map[string]HoldRow{}
			for i := range 1 + d.probes.Intn(2) {
				auth := fmt.Sprintf("%s-h%d", l.ref.LeaseID, i)
				l.holds[auth] = HoldRow{AuthorizationID: auth, Estimate: int64(1 + d.probes.Intn(20)), Deadline: walkTick,
					Listed: true}
				req.PutHolds = append(req.PutHolds, l.holds[auth])
				d.seen["a listed hold"]++
			}
		}
	}
	// The store gets a copy (walkCommit), so what the walk expects, from
	// money and req, is what it generated, whatever the call does with its
	// request.
	changes, releases := l.moneyEffect(money)
	result, ts, err := walkCommit(d.s, req, "")
	if err != nil {
		d.fatalf("the commit %+v: %v", req, err)
	}
	d.landed("commit", ts)
	d.seen["a release's excess kept on its shard"] += d.shards.settle(changes, releases)
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
	d.checkHolds(l)
	return got[0]
}

// checkHolds holds the lease's hold rows, every column, and the holds Load
// decodes, to the walk's open holds in authorization order: one row per
// hold, none once they are released, no row more or less.
func (d *debtWalk) checkHolds(l *debtLease) {
	d.t.Helper()
	ctx := context.Background()
	var rows []HoldRow
	err := shared.Single().Query(ctx, spanner.Statement{
		SQL: `SELECT ` + strings.Join(holdColumns[2:], ", ") + ` FROM tr_lease_hold
		       WHERE workspace_id = @w AND lease_id = @l ORDER BY authorization_id`,
		Params: map[string]any{"w": l.ref.Workspace, "l": l.ref.LeaseID},
	}).Do(func(r *spanner.Row) error {
		var h HoldRow
		if err := r.Columns(&h.AuthorizationID, &h.Estimate, &h.Deadline, &h.Listed, &h.SnapshotSeq, &h.SnapshotHash,
			&h.SnapshotUsage, &h.RunningCharge, &h.SnapshotOwnerSeq, &h.ReapBasis); err != nil {
			return err
		}
		rows = append(rows, h)
		return nil
	})
	if err != nil {
		d.fatalf("reading lease %s's holds: %v", l.ref.LeaseID, err)
	}
	want := make([]HoldRow, 0, len(l.holds))
	for _, auth := range slices.Sorted(maps.Keys(l.holds)) {
		want = append(want, l.holds[auth])
	}
	if why := sameHolds(rows, want); why != "" {
		d.fatalf("lease %s's hold rows: %s", l.ref.LeaseID, why)
	}
	loaded, err := d.s.Load(ctx, l.ref)
	if err != nil {
		d.fatalf("lease %s's load: %v", l.ref.LeaseID, err)
	}
	if why := sameHolds(loaded.Holds, want); why != "" {
		d.fatalf("lease %s's holds as Load decodes them: %s", l.ref.LeaseID, why)
	}
}

// sameHolds says how the holds read differ from the walk's, if they do:
// their number and order, and each row field by field, its deadline as an
// instant.
func sameHolds(got, want []HoldRow) string {
	if len(got) != len(want) {
		return fmt.Sprintf("%d holds read, and the walk has %d open", len(got), len(want))
	}
	for i, g := range got {
		w := want[i]
		if !g.Deadline.Equal(w.Deadline) {
			return fmt.Sprintf("hold %s's deadline is %v, and the walk's is %v", g.AuthorizationID, g.Deadline, w.Deadline)
		}
		g.Deadline, w.Deadline = time.Time{}, time.Time{}
		if !reflect.DeepEqual(g, w) {
			return fmt.Sprintf("hold %d is %+v, and the walk's is %+v", i, g, w)
		}
	}
	return ""
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
	stored := l.shortfall
	got := d.commit(ctx, l, money, false)
	pastFirst, faults := l.bookedEach(money)
	if pastFirst {
		d.seen["a booking past the first donor"]++
	}
	d.seen["booking"]++
	if l.shortfall > stored {
		// A total a booking raised is the stored one too: the owner's write
		// of its own, lower one must not take it back down.
		d.probeShortfall(ctx, l)
		d.seen["shortfall write below a total a booking raised"]++
	}
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

// pastDebt is a live lease whose room is more than the workspace's debt,
// if there is one: its last donor's shard first, so the excess a return
// leaves lands on a shard other than the first where it can.
func (d *debtWalk) pastDebt(live []*debtLease) *debtLease {
	debt := d.shards.debt()
	var found *debtLease
	for _, l := range live {
		if l.room() <= debt {
			continue
		}
		if l.donors[len(l.donors)-1].shard != 0 {
			return l
		}
		if found == nil {
			found = l
		}
	}
	return found
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
	if debt := d.shards.debt(); d.shards.marked && room > debt {
		// In debt, the whole room, more than the debt: the money repays it
		// and the excess must stay on the shard that freed it, the last
		// donor's, which the walks must see.
		amount = room
	}
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
	d.shards.raiseFirst(l.donors[0].shard, raise)
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
	// With the holds listed, the lease's exposure is its listed holds'
	// estimates, not its remaining allocation: a grant beside it is answered
	// so, and the whole workspace is checked at once after one that lands,
	// before the close could cover what it did; then the close goes on with
	// the lease.
	before := headrooms(readRows(d.t, d.ws))
	if d.grantFrom(ctx, d.probes) {
		d.seen["a grant beside a listed lease"]++
		d.check(before, headrooms(readRows(d.t, d.ws)))
	}
	lo := spannerNow(d.t)
	rows, read, err := d.s.ReadDrainSince(ctx, l.ref, time.Time{})
	hi := spannerNow(d.t)
	if err != nil {
		d.fatalf("reading the drain log: %v", err)
	}
	// The read's timestamp is Spanner's time during the call: the close takes
	// it as the member's cursor, and one ahead could pass over rows appended
	// after the read.
	if read.Before(lo) || read.After(hi) {
		d.fatalf("lease %s's drain read at %v, between reads at %v and %v", l.ref.LeaseID, read, lo, hi)
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
	if len(l.holds) > 0 {
		// A hold still open holds the close back first, whatever the drain
		// log holds, and the refusal moves nothing. The holds end with a
		// release winner each, a commit of its own, which deletes their rows.
		d.unchanged(l.ref, "close", func() {
			got, err := d.s.CloseLease(ctx, l.ref, l.version, read, now)
			if err != nil || got.Refused != RefusedHoldsOpen || got.Released != 0 || got.NewVersion != 0 ||
				!got.CommitTS.IsZero() {
				d.fatalf("a close over open holds: %+v %v", got, err)
			}
		})
		d.seen["refused close: holds open"]++
		var releases []Winner
		for _, auth := range slices.Sorted(maps.Keys(l.holds)) {
			releases = append(releases, Winner{AuthorizationID: auth, Kind: "release", RecordID: auth + "-release"})
		}
		before = headrooms(readRows(d.t, d.ws))
		result, ts, err := walkCommit(d.s, CommitRequest{Ref: l.ref, ReadVersion: l.version, AppliedSeq: l.seq,
			Winners: releases}, "")
		if err != nil {
			d.fatalf("committing the holds' releases: %v", err)
		}
		d.landed("commit", ts)
		if result.State != l.state || len(result.Faults) != 0 {
			d.fatalf("the releases' commit left lease %s %+v, and the walk has it %q", l.ref.LeaseID, result, l.state)
		}
		l.advance(d, "the holds' releases' commit", result.NewVersion)
		l.packs = append(l.packs, Pack{CommitVersion: l.version, Winners: releases})
		d.checkPacks(l)
		l.holds = nil
		d.checkHolds(l)
		d.checkLeaseRow(l, "the releases' commit")
		// The whole workspace, before the close could cover what the
		// releases' commit did to a credit row.
		d.check(before, headrooms(readRows(d.t, d.ws)))
	}
	if len(winners) > 0 {
		// Still, a row whose winner is not stored holds the close back,
		// and the refusal moves nothing, every row read at once after it.
		d.unchanged(l.ref, "close", func() {
			got, err := d.s.CloseLease(ctx, l.ref, l.version, read, now)
			if err != nil || got.Refused != RefusedUndecided || got.Released != 0 || got.NewVersion != 0 ||
				!got.CommitTS.IsZero() {
				d.fatalf("a close over undecided drain rows: %+v %v", got, err)
			}
		})
		d.seen["refused close: undecided"]++
		before := headrooms(readRows(d.t, d.ws))
		changes, releases := l.moneyEffect(money)
		result, ts, err := walkCommit(d.s, CommitRequest{Ref: l.ref, ReadVersion: l.version, AppliedSeq: l.seq,
			Money: money, Winners: winners}, "")
		if err != nil {
			d.fatalf("committing the drain log's winners: %v", err)
		}
		d.landed("commit", ts)
		d.seen["a release's excess kept on its shard"] += d.shards.settle(changes, releases)
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
	_, releases := l.moneyEffect([]MoneyOp{Return(room)})
	got, err := d.s.CloseLease(ctx, l.ref, l.version, read, now)
	if err != nil || got.Refused != "" || got.Released != room {
		d.fatalf("the close of lease %s, room %d: %+v %v", l.ref.LeaseID, room, got, err)
	}
	l.closedAt = within{d.stamps.last, got.CommitTS}
	d.landed("close", got.CommitTS)
	d.seen["a release's excess kept on its shard"] += d.shards.settle(nil, releases)
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

// checkCreditColumns holds every other column of each credit row to what
// the walk's writes leave: the seeded tier and nothing else set, and the
// stamp updated_at, which every write of a row sets from Spanner's clock
// during its statement: a row the replica says the store wrote carries a
// stamp at or after its last and at or before the walk's last commit, and a
// row it did not write keeps its stamp.
func (d *debtWalk) checkCreditColumns() {
	d.t.Helper()
	type meta struct {
		tier     spanner.NullInt64
		computed spanner.NullTime
		latched  spanner.NullTime
		override spanner.NullInt64
		causes   []spanner.NullString
		epoch    spanner.NullInt64
		through  spanner.NullTime
		source   spanner.NullTime
		updated  spanner.NullTime
	}
	var got []meta
	err := shared.Single().Query(context.Background(), spanner.Statement{
		SQL: `SELECT trust_tier, trust_computed_at, trust_latched_at, trust_override_tier, billing_pause_causes,
		             pause_epoch, trust_reconciled_through, source_updated_at, updated_at
		        FROM tr_credit_balance WHERE workspace_id = @w ORDER BY shard`,
		Params: map[string]any{"w": d.ws},
	}).Do(func(r *spanner.Row) error {
		var m meta
		if err := r.Columns(&m.tier, &m.computed, &m.latched, &m.override, &m.causes, &m.epoch, &m.through, &m.source,
			&m.updated); err != nil {
			return err
		}
		got = append(got, m)
		return nil
	})
	if err != nil {
		d.fatalf("reading the credit rows' columns: %v", err)
	}
	for i, m := range got {
		// The seeded tier and metadata (newDebtWalkWith), and nothing else; a
		// null array reads as no causes.
		if m.tier != (spanner.NullInt64{Int64: 3, Valid: true}) || !m.computed.Valid || !m.computed.Time.Equal(walkTick) ||
			m.latched.Valid || m.override.Valid || len(m.causes) != 0 || m.epoch != (spanner.NullInt64{Int64: 17, Valid: true}) ||
			!m.through.Valid || !m.through.Time.Equal(walkTick.Add(time.Hour)) || m.source.Valid {
			d.fatalf("shard %d's other columns are %+v, and the walk seeded tier 3, epoch 17, computed at %v and reconciled an hour on",
				i, m, walkTick)
		}
		prev := d.shards.updatedAt[i]
		switch {
		case d.shards.touched[i]:
			// Stamped by this step's write: at or after Spanner's time read
			// before the step, at or before the walk's last commit.
			if !m.updated.Valid || m.updated.Time.Before(d.opStart) || m.updated.Time.After(d.stamps.last) {
				d.fatalf("shard %d was written, and its updated_at is %v, not between the step's start %v and the walk's last commit %v",
					i, m.updated, d.opStart, d.stamps.last)
			}
		case m.updated != prev:
			d.fatalf("shard %d was not written, and its updated_at moved from %v to %v", i, prev, m.updated)
		}
		d.shards.updatedAt[i] = m.updated
		d.shards.touched[i] = false
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
	// Every seeded shard is still a row, no more and no fewer, before any
	// value is compared: a store that dropped an emptied row would otherwise
	// pass the comparison of the rows left.
	if err := creditShards(d.ws, len(d.shards.credit)); err != nil {
		d.fatalf("%v", err)
	}
	rows := readRows(d.t, d.ws)
	var credits int64
	for _, r := range rows {
		credits += r.credits
	}
	if credits != d.credits {
		d.fatalf("credit %d, and %d was seeded", credits, d.credits)
	}
	// Each credit row, every column, is the walk's replica of it: credit
	// moved only as section 4.7 moves it, repaying and covering, and left
	// where a release freed it otherwise.
	for i, r := range rows {
		if r.credits != d.shards.credit[i] || r.usage != d.shards.usage[i] || r.reserved != d.shards.reserved[i] ||
			r.marked != d.shards.marked {
			d.fatalf("shard %d is %+v, and the walk's replica has credit %d, usage %d, reserved %d, marked %v", i, r,
				d.shards.credit[i], d.shards.usage[i], d.shards.reserved[i], d.shards.marked)
		}
	}
	d.checkCreditColumns()
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
		d.checkHolds(l)
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
