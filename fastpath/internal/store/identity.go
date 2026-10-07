package store

import (
	"context"
	"fmt"

	"cloud.google.com/go/spanner"
)

// CheckIdentity reads a workspace in one snapshot and returns each way it
// breaks the design's accounting, for tests and the spike's own audit:
//
//   - section 4.7's identity, per credit shard: reserved is the remaining
//     allocation, donor by donor, of every open or draining lease (the spike
//     has no synchronous holds);
//   - each live lease's allocation and consumption are its donors' sums;
//   - no row is negative unless every row is marked, rows are marked only
//     while the signed sum is negative, and every row has the same mark.
//
// An empty result is a workspace that keeps them all.
func (s *Store) CheckIdentity(ctx context.Context, workspace string) ([]string, error) {
	ro := s.client.ReadOnlyTransaction()
	defer ro.Close()
	query := func(sql string, each func(*spanner.Row) error) error {
		return ro.QueryWithOptions(ctx, spanner.Statement{SQL: sql, Params: map[string]any{"w": workspace}},
			spanner.QueryOptions{RequestTag: tag("check-identity")}).Do(each)
	}
	var reserved, headroom []int64
	var marks []bool
	err := query(`SELECT shard, reserved, total_credits - total_usage - reserved, COALESCE(in_debt, FALSE)
	                FROM tr_credit_balance WHERE workspace_id = @w ORDER BY shard`, func(row *spanner.Row) error {
		var shard, r, h int64
		var m bool
		if err := row.Columns(&shard, &r, &h, &m); err != nil {
			return err
		}
		if shard != int64(len(reserved)) {
			return ErrCreditRowsIncomplete
		}
		reserved, headroom, marks = append(reserved, r), append(headroom, h), append(marks, m)
		return nil
	})
	if err != nil {
		return nil, err
	}
	type totals struct{ allocation, consumed int64 }
	live := map[string]totals{}
	err = query(`SELECT lease_id, allocation, consumed FROM tr_lease
	              WHERE workspace_id = @w AND state IN ('open', 'draining')`, func(row *spanner.Row) error {
		var lease string
		var t totals
		if err := row.Columns(&lease, &t.allocation, &t.consumed); err != nil {
			return err
		}
		live[lease] = t
		return nil
	})
	if err != nil {
		return nil, err
	}
	held := make([]int64, len(reserved))
	donors := map[string]totals{}
	var problems []string
	err = query(`SELECT lease_id, credit_shard, allocation, consumed FROM tr_lease_donor WHERE workspace_id = @w`,
		func(row *spanner.Row) error {
			var lease string
			var shard, allocation, consumed int64
			if err := row.Columns(&lease, &shard, &allocation, &consumed); err != nil {
				return err
			}
			if _, ok := live[lease]; !ok {
				return nil
			}
			if shard < 0 || shard >= int64(len(held)) {
				problems = append(problems, fmt.Sprintf("lease %s has a donor on shard %d, which has no row", lease, shard))
				return nil
			}
			held[shard] += allocation - consumed
			d := donors[lease]
			donors[lease] = totals{d.allocation + allocation, d.consumed + consumed}
			return nil
		})
	if err != nil {
		return nil, err
	}
	for shard := range reserved {
		if reserved[shard] != held[shard] {
			problems = append(problems, fmt.Sprintf("shard %d reserves %d, and its live leases' donors hold %d",
				shard, reserved[shard], held[shard]))
		}
	}
	for lease, t := range live {
		if d := donors[lease]; d != t {
			problems = append(problems, fmt.Sprintf("lease %s has allocation %d and consumption %d, its donors %d and %d",
				lease, t.allocation, t.consumed, d.allocation, d.consumed))
		}
	}
	var signed int64
	negative, marked, alike := false, marks[0], true
	for i, h := range headroom {
		signed += h
		negative = negative || h < 0
		alike = alike && marks[i] == marks[0]
	}
	switch {
	case !alike:
		problems = append(problems, fmt.Sprintf("the rows' marks differ: %v", marks))
	case negative && !marked:
		problems = append(problems, fmt.Sprintf("a row is negative and the rows are not marked: %v", headroom))
	case marked && signed >= 0:
		problems = append(problems, fmt.Sprintf("the rows are marked and their signed sum is %d", signed))
	}
	return problems, nil
}
