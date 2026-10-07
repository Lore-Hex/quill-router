package store

import (
	"errors"
	"fmt"
)

// errMoneyRange refuses a commit whose money would pass int64's range. No
// lease's real figures come near it, and a sum that wrapped would read as
// credit (§4.7), so the whole commit is refused instead.
var errMoneyRange = errors.New("store: a commit's money passes int64's range")

// plus is a + b, or errMoneyRange where that passes int64's range.
func plus(a, b int64) (int64, error) {
	c := a + b
	if (c > a) != (b > 0) {
		return 0, errMoneyRange
	}
	return c, nil
}

// MoneyOp is one money record an auditor's commit applies, in the lease's
// log order: a terminal's booking, or a return of allocation.
type MoneyOp struct {
	// Book is a booking of Charge, after the stored shortfall total is
	// raised to ShortfallTotal, the total the terminal's record carries.
	Book           bool
	Charge         int64
	ShortfallTotal int64
	// Amount is a return's.
	Amount int64
}

// Book is a terminal's booking: its charge, and the shortfall total its
// record carries.
func Book(charge, shortfallTotal int64) MoneyOp {
	return MoneyOp{Book: true, Charge: charge, ShortfallTotal: shortfallTotal}
}

// Return is a return of amount of a lease's allocation.
func Return(amount int64) MoneyOp {
	return MoneyOp{Amount: amount}
}

// donorMoney is a donor's allocation and what is booked against it.
type donorMoney struct {
	Shard      int64
	Allocation int64
	Consumed   int64
}

// leaseMoney is a lease's money as a commit reads it: the row's totals, and
// its donors in ascending shard order.
type leaseMoney struct {
	Allocation     int64
	Consumed       int64
	ShortfallTotal int64
	Returned       int64
	FaultUsage     int64
	Donors         []donorMoney
}

// shardMoney is a change to a credit row: of its reservation and its usage.
type shardMoney struct {
	Reserved int64
	Usage    int64
}

// creditRelease is a return's money, freed on a donor's credit shard.
type creditRelease struct {
	Shard  int64
	Amount int64
}

// moneyEffect is what a commit's money records do: the lease after, each
// credit shard's change, the returns to release, in order, and each fault,
// the part of a booking beyond the allocation.
type moneyEffect struct {
	After    leaseMoney
	Shards   map[int64]shardMoney
	Releases []creditRelease
	Faults   []int64
}

// applyMoney runs a commit's money records against the lease as the commit
// read it, in log order (§4.2, §4.7):
//
//   - A booking first raises the stored shortfall total to its record's, the
//     larger of the two, and adds what it rose by to the allocation and to
//     the first donor's, which reserves it on that donor's shard. It then
//     books the charge first donor first, each donor up to its room, as a
//     matched booking: the shard's reservation goes down and its usage up by
//     the same amount. What no donor has room for is a fault, booked as usage
//     on the first donor's shard.
//   - A return takes allocation from the last donor first, never below what
//     that donor has booked, and frees it on the donor's shard.
func applyMoney(before leaseMoney, ops []MoneyOp) (moneyEffect, error) {
	if len(before.Donors) == 0 {
		return moneyEffect{}, errors.New("store: a lease has at least one donor")
	}
	m := before
	m.Donors = append([]donorMoney(nil), before.Donors...)
	eff := moneyEffect{Shards: map[int64]shardMoney{}}
	// Every sum is checked: the first to pass int64's range is the error.
	var err error
	add := func(to *int64, v int64) {
		if err == nil {
			*to, err = plus(*to, v)
		}
	}
	addShard := func(shard, reserved, usage int64) {
		s := eff.Shards[shard]
		add(&s.Reserved, reserved)
		add(&s.Usage, usage)
		eff.Shards[shard] = s
	}
	for _, op := range ops {
		if !op.Book {
			if op.Amount < 0 {
				return moneyEffect{}, fmt.Errorf("store: a return of %d", op.Amount)
			}
			left := op.Amount
			for i := len(m.Donors) - 1; i >= 0 && left > 0; i-- {
				z := min(m.Donors[i].Allocation-m.Donors[i].Consumed, left)
				if z <= 0 {
					continue
				}
				m.Donors[i].Allocation -= z
				m.Allocation -= z
				add(&m.Returned, z)
				eff.Releases = append(eff.Releases, creditRelease{m.Donors[i].Shard, z})
				left -= z
			}
			if err != nil {
				return moneyEffect{}, err
			}
			if left > 0 {
				return moneyEffect{}, fmt.Errorf("store: a return of %d is %d more than the lease holds", op.Amount, left)
			}
			continue
		}
		if op.Charge < 0 || op.ShortfallTotal < 0 {
			return moneyEffect{}, fmt.Errorf("store: a booking of %d with a shortfall total of %d", op.Charge, op.ShortfallTotal)
		}
		if rise := op.ShortfallTotal - m.ShortfallTotal; rise > 0 {
			m.ShortfallTotal = op.ShortfallTotal
			add(&m.Allocation, rise)
			add(&m.Donors[0].Allocation, rise)
			addShard(m.Donors[0].Shard, rise, 0)
		}
		left := op.Charge
		for i := range m.Donors {
			x := min(m.Donors[i].Allocation-m.Donors[i].Consumed, left)
			if x <= 0 {
				continue
			}
			m.Donors[i].Consumed += x
			add(&m.Consumed, x)
			addShard(m.Donors[i].Shard, -x, x)
			left -= x
		}
		if left > 0 {
			add(&m.FaultUsage, left)
			addShard(m.Donors[0].Shard, 0, left)
			eff.Faults = append(eff.Faults, left)
		}
		if err != nil {
			return moneyEffect{}, err
		}
	}
	eff.After = m
	return eff, nil
}
