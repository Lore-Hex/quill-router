// Package creditdebt holds the debt rules of the fast-admission design's
// section 4.7: what a write to a workspace's credit rows must end with. They
// are proofs/CreditDebt.tla's Cover, Squared, Distribute and Inflow, as
// src/trusted_router/credit_debt.py computes them for production, and the
// tests hold this package to that module's results
// (fastpath/testdata/credit_debt_vectors.json).
//
// A workspace's credit is spread over shard rows of tr_credit_balance. A
// row's headroom is its credit less its usage and its reservations, and the
// workspace's signed sum is the sum of its rows' headroom.
//
//   - No row is negative unless every row is marked, and the rows are marked
//     only while the signed sum is negative. A marked row refuses
//     reservations.
//   - Money coming in repays the negative rows first, lowest shard first,
//     each at most to zero.
//   - A negative row is covered from the others: the lowest negative row from
//     the lowest positive row first.
//
// These functions only compute: the store reads the rows, calls them and
// writes what they return. Only credit moves, so a row's change of headroom
// is a change of its total_credits. Unlike Python's integers, int64 has
// bounds, but the rules need only the sign of the rows' sum, which is taken
// exactly, and covering and repaying move each row toward zero, so every
// row stays in range. A row at the lowest int64, whose negation does not, is
// refused.
package creditdebt

import (
	"errors"
	"math"
	"math/big"
)

// Squared is what the rows end with: each row's headroom, in shard order,
// and the mark.
type Squared struct {
	Headroom []int64
	Marked   bool
}

// Inflow is money coming in, after it has repaid what it can and the rows
// are squared. Left is the part that repaid nothing, and is not in Headroom;
// it is 0 when the rows end marked. The caller spreads what is left of new
// money over the rows, or leaves what is left of a return on the row that
// freed it.
type Inflow struct {
	Headroom []int64
	Marked   bool
	Left     int64
}

var errNoRows = errors.New("creditdebt: a workspace has at least one credit row")

// rows copies headroom and returns the sign of its sum, taken exactly.
func rows(headroom []int64) ([]int64, int, error) {
	if len(headroom) == 0 {
		return nil, 0, errNoRows
	}
	out := append([]int64(nil), headroom...)
	sum := new(big.Int)
	for _, v := range out {
		if v == math.MinInt64 {
			return nil, 0, errors.New("creditdebt: a row at the lowest int64")
		}
		sum.Add(sum, big.NewInt(v))
	}
	return out, sum.Sign(), nil
}

// Cover moves headroom from positive rows to negative ones until none is
// negative: the lowest negative row takes from the lowest positive row
// first, as Cover does. The signed sum must not be negative.
func Cover(headroom []int64) ([]int64, error) {
	r, sign, err := rows(headroom)
	if err != nil {
		return nil, err
	}
	if sign < 0 {
		return nil, errors.New("creditdebt: rows whose signed sum is negative are marked, not covered")
	}
	return cover(r), nil
}

// cover covers r in place. Its sum is not negative, so while a row is
// negative another is positive.
func cover(r []int64) []int64 {
	for {
		negative := -1
		for i, v := range r {
			if v < 0 {
				negative = i
				break
			}
		}
		if negative < 0 {
			return r
		}
		positive := -1
		for i, v := range r {
			if v > 0 {
				positive = i
				break
			}
		}
		moved := min(-r[negative], r[positive])
		r[negative] += moved
		r[positive] -= moved
	}
}

// Square is what any write to the rows ends with (Squared): if the signed
// sum is negative, every row is marked and nothing moves; otherwise every
// negative row is covered and no row is marked.
func Square(headroom []int64) (Squared, error) {
	r, sign, err := rows(headroom)
	if err != nil {
		return Squared{}, err
	}
	if sign < 0 {
		return Squared{Headroom: r, Marked: true}, nil
	}
	return Squared{Headroom: cover(r)}, nil
}

// Repay spends amount on the negative rows, lowest first, each at most to
// zero, and returns the rows after and what is left of the amount
// (Distribute, before it puts what is left on a row).
func Repay(headroom []int64, amount int64) ([]int64, int64, error) {
	r, _, err := rows(headroom)
	if err != nil {
		return nil, 0, err
	}
	if amount < 0 {
		return nil, 0, errors.New("creditdebt: money coming in is not negative")
	}
	left := amount
	for i, v := range r {
		if left == 0 {
			break
		}
		if v < 0 {
			paid := min(left, -v)
			r[i] += paid
			left -= paid
		}
	}
	return r, left, nil
}

// TakeInflow is money coming in (Inflow): it repays the negative rows first,
// then squares them. headroom is each row's headroom without the money; for
// a return, that is its own row's headroom before the release freed it.
func TakeInflow(headroom []int64, amount int64) (Inflow, error) {
	repaid, left, err := Repay(headroom, amount)
	if err != nil {
		return Inflow{}, err
	}
	r, sign, err := rows(repaid)
	if err != nil {
		return Inflow{}, err
	}
	if sign < 0 {
		// The money ran out with a row still negative: all of it repaid.
		return Inflow{Headroom: r, Marked: true}, nil
	}
	return Inflow{Headroom: cover(r), Left: left}, nil
}

// CreditDeltas is each row's change of total_credits that takes its headroom
// from before to after.
func CreditDeltas(before, after []int64) ([]int64, error) {
	if len(before) != len(after) {
		return nil, errors.New("creditdebt: before and after are not the same rows")
	}
	out := make([]int64, len(before))
	for i := range before {
		d := after[i] - before[i]
		if (after[i] >= 0) != (before[i] >= 0) && (d >= 0) != (after[i] >= before[i]) {
			return nil, errors.New("creditdebt: a row's change is out of int64's range")
		}
		out[i] = d
	}
	return out, nil
}
