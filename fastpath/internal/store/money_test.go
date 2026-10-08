package store

import (
	"errors"
	"math"
	"math/rand"
	"reflect"
	"testing"
)

func lease(donors ...donorMoney) leaseMoney {
	var m leaseMoney
	for _, d := range donors {
		m.Allocation += d.Allocation
		m.Consumed += d.Consumed
	}
	m.Donors = donors
	return m
}

func TestApplyMoneyBooksFirstDonorFirst(t *testing.T) {
	cases := []struct {
		name     string
		before   leaseMoney
		ops      []MoneyOp
		donors   []donorMoney
		shards   map[int64]shardMoney
		releases []creditRelease
		faults   []int64
		total    int64
	}{
		{"within the first donor", lease(donorMoney{0, 50, 0}, donorMoney{1, 20, 0}), []MoneyOp{Book(30, 0)},
			[]donorMoney{{0, 50, 30}, {1, 20, 0}}, map[int64]shardMoney{0: {-30, 30}}, nil, nil, 0},
		{"across donors", lease(donorMoney{0, 50, 40}, donorMoney{1, 20, 0}), []MoneyOp{Book(25, 0)},
			[]donorMoney{{0, 50, 50}, {1, 20, 15}}, map[int64]shardMoney{0: {-10, 10}, 1: {-15, 15}}, nil, nil, 0},
		{"the shortfall raised first", lease(donorMoney{0, 50, 50}, donorMoney{1, 20, 20}), []MoneyOp{Book(8, 8)},
			[]donorMoney{{0, 58, 58}, {1, 20, 20}}, map[int64]shardMoney{0: {0, 8}}, nil, nil, 8},
		{"a fault beyond every donor", lease(donorMoney{0, 10, 10}, donorMoney{1, 5, 3}), []MoneyOp{Book(6, 0)},
			[]donorMoney{{0, 10, 10}, {1, 5, 5}}, map[int64]shardMoney{0: {0, 4}, 1: {-2, 2}}, nil, []int64{4}, 0},
		{"a return from the last donor first", lease(donorMoney{0, 50, 10}, donorMoney{1, 20, 5}), []MoneyOp{Return(25)},
			[]donorMoney{{0, 40, 10}, {1, 5, 5}}, map[int64]shardMoney{}, []creditRelease{{1, 15}, {0, 10}}, nil, 0},
		{"a booking after a return", lease(donorMoney{0, 30, 0}), []MoneyOp{Return(20), Book(15, 5)},
			[]donorMoney{{0, 15, 15}}, map[int64]shardMoney{0: {5 - 15, 15}}, []creditRelease{{0, 20}}, nil, 5},
		{"a lower total raises nothing", leaseMoney{Allocation: 30, ShortfallTotal: 7, Donors: []donorMoney{{0, 30, 0}}},
			[]MoneyOp{Book(5, 3)}, []donorMoney{{0, 30, 5}}, map[int64]shardMoney{0: {-5, 5}}, nil, nil, 7},
	}
	for _, c := range cases {
		got, err := applyMoney(c.before, c.ops)
		if err != nil {
			t.Errorf("%s: %v", c.name, err)
			continue
		}
		if !reflect.DeepEqual(got.After.Donors, c.donors) || !reflect.DeepEqual(got.Shards, c.shards) ||
			!reflect.DeepEqual(got.Releases, c.releases) || !reflect.DeepEqual(got.Faults, c.faults) ||
			got.After.ShortfallTotal != c.total {
			t.Errorf("%s: got %+v", c.name, got)
		}
		// The lease's totals are its donors' sums, and its accounting holds.
		var allocation, consumed int64
		for _, d := range got.After.Donors {
			allocation += d.Allocation
			consumed += d.Consumed
		}
		a := got.After
		if a.Allocation != allocation || a.Consumed != consumed ||
			a.Allocation-c.before.Allocation != (a.ShortfallTotal-c.before.ShortfallTotal)-(a.Returned-c.before.Returned) {
			t.Errorf("%s: totals %+v, donors' %d and %d", c.name, a, allocation, consumed)
		}
	}
}

func TestApplyMoneyRefusesWhatIsNoRecord(t *testing.T) {
	for name, ops := range map[string][]MoneyOp{
		"a negative charge":            {Book(-1, 0)},
		"a negative total":             {Book(1, -1)},
		"a negative return":            {Return(-1)},
		"more returned than is held":   {Return(41)},
		"a return of what is consumed": {Book(30, 0), Return(11)},
	} {
		if _, err := applyMoney(lease(donorMoney{0, 40, 0}), ops); err == nil {
			t.Errorf("%s is applied", name)
		}
	}
	if _, err := applyMoney(leaseMoney{}, []MoneyOp{Book(1, 0)}); err == nil {
		t.Error("a lease without donors is booked")
	}
}

// TestApplyMoneyRefusesSumsPastInt64: a sum that would pass int64's range
// is an error, never a figure that wrapped.
func TestApplyMoneyRefusesSumsPastInt64(t *testing.T) {
	for name, c := range map[string]struct {
		before leaseMoney
		ops    []MoneyOp
	}{
		"a shard's usage":               {lease(donorMoney{0, 30, 0}), []MoneyOp{Book(math.MaxInt64, 0), Book(1, 0)}},
		"the lease's faults":            {lease(donorMoney{0, 30, 0}), []MoneyOp{Book(math.MaxInt64, 0), Book(200, 0)}},
		"a rise":                        {lease(donorMoney{0, 30, 0}), []MoneyOp{Book(0, math.MaxInt64)}},
		"what is returned":              {leaseMoney{Allocation: 30, Returned: math.MaxInt64, Donors: []donorMoney{{0, 30, 0}}}, []MoneyOp{Return(1)}},
		"a rise past a full allocation": {lease(donorMoney{0, math.MaxInt64, 0}), []MoneyOp{Book(math.MaxInt64, 0), Book(0, math.MaxInt64)}},
	} {
		if got, err := applyMoney(c.before, c.ops); !errors.Is(err, errMoneyRange) {
			t.Errorf("%s: %+v %v", name, got, err)
		}
	}
}

// TestApplyMoneyKeepsItsAccountsOnRandomRecords: on random leases and
// records, every donor keeps 0 <= consumed <= allocation, the lease's totals
// are its donors' sums, the allocation moves only by shortfall rises and
// returns, and the credit shards' changes add up to the lease's.
func TestApplyMoneyKeepsItsAccountsOnRandomRecords(t *testing.T) {
	rng := rand.New(rand.NewSource(1587))
	for range 5000 {
		var before leaseMoney
		for shard := range 1 + rng.Intn(3) {
			a := rng.Int63n(50)
			d := donorMoney{Shard: int64(shard), Allocation: a, Consumed: rng.Int63n(a + 1)}
			before.Donors = append(before.Donors, d)
			before.Allocation += d.Allocation
			before.Consumed += d.Consumed
		}
		before.ShortfallTotal = rng.Int63n(10)
		var ops []MoneyOp
		for range rng.Intn(6) {
			if rng.Intn(3) == 0 {
				ops = append(ops, Return(rng.Int63n(5)))
			} else {
				ops = append(ops, Book(rng.Int63n(30), rng.Int63n(20)))
			}
		}
		eff, err := applyMoney(before, ops)
		if err != nil {
			continue // a return of more than is held
		}
		a := eff.After
		var allocation, consumed, reservedDelta, usageDelta, released, faults int64
		for _, d := range a.Donors {
			if d.Consumed < 0 || d.Consumed > d.Allocation {
				t.Fatalf("donor %+v after %v on %+v", d, ops, before)
			}
			allocation += d.Allocation
			consumed += d.Consumed
		}
		for _, sm := range eff.Shards {
			reservedDelta += sm.Reserved
			usageDelta += sm.Usage
		}
		for _, r := range eff.Releases {
			released += r.Amount
		}
		for _, f := range eff.Faults {
			faults += f
		}
		rises := a.ShortfallTotal - before.ShortfallTotal
		booked := a.Consumed - before.Consumed
		if a.Allocation != allocation || a.Consumed != consumed ||
			a.Allocation-before.Allocation != rises-(a.Returned-before.Returned) ||
			reservedDelta != rises-booked || usageDelta != booked+faults || released != a.Returned-before.Returned ||
			a.FaultUsage-before.FaultUsage != faults {
			t.Fatalf("the accounts after %v on %+v: %+v", ops, before, eff)
		}
	}
}
