package store

import (
	"context"
	"errors"
	"fmt"

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

// ReadKeyWindows reads a key's tr_key_limit rows of shards 0 to shards-1 in
// one strong single-use read of the key range, in shard order, for the
// edge's window check of an enforcing windowed key. It is outside any
// transaction on purpose, as Python's own check reads a snapshot outside
// its: a read-write transaction that read these rows would take a shared
// lock on the hottest row a busy key has, which every settle of the key
// writes. Every column is read nullable: the window columns were added to
// older databases as nullable ones, so an old row reads NULL usage or start.
// It returns whatever rows the range holds, none or some missing included;
// keywindow.Evaluate refuses any set but exactly 0..shards-1.
func (s *Store) ReadKeyWindows(ctx context.Context, keyHash string, shards int) ([]keywindow.Row, error) {
	if keyHash == "" {
		return nil, errors.New("store: no key hash")
	}
	if shards < 1 || shards > keywindow.MaxShards {
		return nil, fmt.Errorf("store: a key has 1 to %d usage shards, not %d", keywindow.MaxShards, shards)
	}
	ro := s.client.Single()
	defer ro.Close()
	keys := spanner.KeyRange{Start: spanner.Key{keyHash, int64(0)}, End: spanner.Key{keyHash, int64(shards)},
		Kind: spanner.ClosedOpen}
	var rows []keywindow.Row
	err := ro.ReadWithOptions(ctx, "tr_key_limit", keys, keyWindowColumns,
		&spanner.ReadOptions{RequestTag: tag("read-key-windows")}).Do(func(r *spanner.Row) error {
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
