package store

import (
	"context"
	"errors"

	"cloud.google.com/go/spanner"

	"github.com/Lore-Hex/quill-router/fastpath/internal/keywindow"
)

// keyWindowColumns are the tr_key_limit columns the edge's window check reads
// (docs/design/fast-admission-e2-key-windows.md section 4.3): the shard,
// each window's usage and start, as storage_gcp_authorize.
// key_window_limit_decision selects them, and each window's mirrored limit
// (section 4.5).
var keyWindowColumns = []string{"shard", "day_usage", "day_start", "week_usage", "week_start", "month_usage",
	"month_start", "day_limit_micro", "week_limit_micro", "month_limit_micro"}

// ReadKeyWindows reads every tr_key_limit row of a key, in shard order, in
// one strong single-use read of the key's prefix, for the edge's window check
// of an enforcing windowed key; at most keywindow.MaxShards+1 rows, more than
// a key may have. It reads the whole prefix, not the shards the edge's cached
// usage_shard_count names: after the key's shards grow, an edge holding the
// old count must see rows it does not expect (keywindow.Evaluate refuses any
// set but exactly 0..n-1), not a complete-looking subset that undercounts.
// It is outside any transaction on purpose, as Python's own check reads a
// snapshot outside its: a read-write transaction that read these rows would
// take a shared lock on the hottest row a busy key has, which every settle
// of the key writes. Every column is read nullable: the window columns were
// added to older databases as nullable ones, so an old row reads NULL usage
// or start.
func (s *Store) ReadKeyWindows(ctx context.Context, keyHash string) ([]keywindow.Row, error) {
	if keyHash == "" {
		return nil, errors.New("store: no key hash")
	}
	// Strong: a stale read would miss a settle Python's own read sees.
	ro := s.client.Single()
	defer ro.Close()
	var rows []keywindow.Row
	err := ro.ReadWithOptions(ctx, "tr_key_limit", spanner.Key{keyHash}.AsPrefix(), keyWindowColumns,
		&spanner.ReadOptions{RequestTag: tag("read-key-windows"), Limit: keywindow.MaxShards + 1}).Do(
		func(r *spanner.Row) error {
			var row keywindow.Row
			if err := r.Columns(&row.Shard, &row.DayUsage, &row.DayStart, &row.WeekUsage, &row.WeekStart,
				&row.MonthUsage, &row.MonthStart, &row.DayLimit, &row.WeekLimit, &row.MonthLimit); err != nil {
				return err
			}
			rows = append(rows, row)
			return nil
		})
	if err != nil {
		return nil, err
	}
	return rows, nil
}
