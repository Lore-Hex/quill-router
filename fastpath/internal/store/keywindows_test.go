package store

import (
	"context"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
	"time"

	"cloud.google.com/go/spanner"

	"github.com/Lore-Hex/quill-router/fastpath/internal/keywindow"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store/storetest"
	"github.com/Lore-Hex/quill-router/fastpath/schema"
)

// keyLimitRow is a tr_key_limit row's mutation: the key's shard with the
// window columns given (any of them, a spanner.Null* for NULL).
func keyLimitRow(keyHash string, shard int64, columns map[string]any) *spanner.Mutation {
	row := map[string]any{"key_hash": keyHash, "shard": shard}
	for k, v := range columns {
		row[k] = v
	}
	return spanner.InsertMap("tr_key_limit", row)
}

// TestReadKeyWindowsReadsTheKeysShards (docs/design/
// fast-admission-e2-key-windows.md section 4.3): one strong read returns the
// key's rows of shards 0 to n-1 in shard order, each window's usage, start
// and mirrored limit as stored, NULL as not valid; not a shard past n-1, not
// another key's; a key with no rows reads none; and a key hash or shard
// count no key has is refused before any read.
func TestReadKeyWindowsReadsTheKeysShards(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	key, other := storetest.UniqueID("kh"), storetest.UniqueID("kh")
	day := time.Date(2026, 10, 21, 0, 0, 0, 0, time.UTC)
	week := time.Date(2026, 10, 19, 0, 0, 0, 0, time.UTC)
	if _, err := shared.Apply(ctx, []*spanner.Mutation{
		keyLimitRow(key, 0, map[string]any{"day_usage": int64(700), "day_start": day, "week_usage": int64(900),
			"week_start": week, "day_limit_micro": int64(1000), "week_limit_micro": int64(5000)}),
		keyLimitRow(key, 1, map[string]any{"day_usage": int64(5), "day_start": day, "day_limit_micro": int64(1000)}),
		keyLimitRow(key, 2, map[string]any{}),
		keyLimitRow(key, 3, map[string]any{"day_usage": int64(99), "day_start": day}),
		keyLimitRow(other, 0, map[string]any{"day_usage": int64(1), "day_start": day}),
	}); err != nil {
		t.Fatal(err)
	}
	rows, err := s.ReadKeyWindows(ctx, key, 3)
	if err != nil {
		t.Fatal(err)
	}
	null := spanner.NullInt64{}
	want := []keywindow.Row{
		{Shard: 0, DayUsage: spanner.NullInt64{Int64: 700, Valid: true}, DayStart: spanner.NullTime{Time: day, Valid: true},
			WeekUsage: spanner.NullInt64{Int64: 900, Valid: true}, WeekStart: spanner.NullTime{Time: week, Valid: true},
			MonthUsage: spanner.NullInt64{Valid: true}, DayLimit: spanner.NullInt64{Int64: 1000, Valid: true},
			WeekLimit: spanner.NullInt64{Int64: 5000, Valid: true}, MonthLimit: null},
		{Shard: 1, DayUsage: spanner.NullInt64{Int64: 5, Valid: true}, DayStart: spanner.NullTime{Time: day, Valid: true},
			WeekUsage: spanner.NullInt64{Valid: true}, MonthUsage: spanner.NullInt64{Valid: true},
			DayLimit: spanner.NullInt64{Int64: 1000, Valid: true}},
		{Shard: 2, DayUsage: spanner.NullInt64{Valid: true}, WeekUsage: spanner.NullInt64{Valid: true},
			MonthUsage: spanner.NullInt64{Valid: true}},
	}
	if len(rows) != len(want) {
		t.Fatalf("read %d rows: %+v", len(rows), rows)
	}
	for i := range want {
		if !sameRow(rows[i], want[i]) {
			t.Errorf("row %d: %+v, want %+v", i, rows[i], want[i])
		}
	}
	// A shard count of one reads shard 0 alone; one past the rows reads
	// what there is (Evaluate refuses the set).
	if rows, err := s.ReadKeyWindows(ctx, key, 1); err != nil || len(rows) != 1 || rows[0].Shard != 0 {
		t.Errorf("one shard: %+v, %v", rows, err)
	}
	if rows, err := s.ReadKeyWindows(ctx, key, 64); err != nil || len(rows) != 4 {
		t.Errorf("64 shards: %d rows, %v", len(rows), err)
	}
	if rows, err := s.ReadKeyWindows(ctx, storetest.UniqueID("kh"), 4); err != nil || len(rows) != 0 {
		t.Errorf("a key with no rows: %+v, %v", rows, err)
	}
	for _, bad := range []struct {
		key    string
		shards int
	}{{"", 1}, {key, 0}, {key, 65}, {key, -1}} {
		if _, err := s.ReadKeyWindows(ctx, bad.key, bad.shards); err == nil {
			t.Errorf("key %q with %d shards read", bad.key, bad.shards)
		}
	}
}

// sameRow compares two rows, their instants as instants.
func sameRow(a, b keywindow.Row) bool {
	sameTime := func(x, y spanner.NullTime) bool { return x.Valid == y.Valid && (!x.Valid || x.Time.Equal(y.Time)) }
	return a.Shard == b.Shard && a.DayUsage == b.DayUsage && a.WeekUsage == b.WeekUsage &&
		a.MonthUsage == b.MonthUsage && a.DayLimit == b.DayLimit && a.WeekLimit == b.WeekLimit &&
		a.MonthLimit == b.MonthLimit && sameTime(a.DayStart, b.DayStart) && sameTime(a.WeekStart, b.WeekStart) &&
		sameTime(a.MonthStart, b.MonthStart)
}

// olderKeyLimit is fastpath.sql's statements with tr_key_limit's window
// columns as scripts/deploy/migrate_typed_counters.sh adds them to a
// database that predates them (ensure_column): the usage columns nullable.
// Production's older databases hold rows so.
func olderKeyLimit(t *testing.T) []string {
	t.Helper()
	statements, err := schema.Statements()
	if err != nil {
		t.Fatal(err)
	}
	script, err := os.ReadFile(filepath.Join("..", "..", "..", "scripts", "deploy", "migrate_typed_counters.sh"))
	if err != nil {
		t.Fatal(err)
	}
	added := regexp.MustCompile(`(?m)^ensure_column tr_key_limit (\w+)\s+"([^"]+)"$`).FindAllSubmatch(script, -1)
	if len(added) != 9 {
		t.Fatalf("the script adds %d window columns to tr_key_limit, not 9", len(added))
	}
	found := 0
	for i, s := range statements {
		if !strings.Contains(s, " tr_key_limit (") {
			continue
		}
		found++
		for _, m := range added {
			column := regexp.MustCompile(`(?m)^(\s*)` + string(m[1]) + ` [^,\n]*,`)
			if len(column.FindAllString(s, -1)) != 1 {
				t.Fatalf("tr_key_limit has no single column %s", m[1])
			}
			s = column.ReplaceAllString(s, "${1}"+string(m[1])+" "+string(m[2])+",")
		}
		statements[i] = s
	}
	if found != 1 {
		t.Fatalf("fastpath.sql makes tr_key_limit %d times", found)
	}
	return statements
}

// TestReadKeyWindowsReadsAnOlderDatabasesNulls: on a database whose window
// columns were added nullable, a row written before them reads NULL usage
// and starts (not valid, not an error), and the window check counts such a
// row's windows as zero spend, as Python's int(usage or 0) for a NULL start.
func TestReadKeyWindowsReadsAnOlderDatabasesNulls(t *testing.T) {
	if emulator == nil {
		t.Skip(skipped)
	}
	ctx := context.Background()
	db, err := emulator.Database(ctx, storetest.UniqueID("keywin"), olderKeyLimit(t))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(db.Close)
	s, err := New(db, testConfig())
	if err != nil {
		t.Fatal(err)
	}
	key := storetest.UniqueID("kh")
	at := time.Date(2026, 10, 21, 12, 0, 0, 0, time.UTC)
	day, _, _ := keywindow.Floors(at)
	if _, err := db.Apply(ctx, []*spanner.Mutation{
		keyLimitRow(key, 0, map[string]any{"day_usage": spanner.NullInt64{}, "day_start": spanner.NullTime{},
			"week_usage": spanner.NullInt64{}, "month_usage": spanner.NullInt64{}, "day_limit_micro": int64(1000)}),
		keyLimitRow(key, 1, map[string]any{"day_usage": spanner.NullInt64{}, "day_start": day,
			"week_usage": spanner.NullInt64{}, "month_usage": spanner.NullInt64{}, "day_limit_micro": int64(1000)}),
	}); err != nil {
		t.Fatal(err)
	}
	rows, err := s.ReadKeyWindows(ctx, key, 2)
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 2 || rows[0].DayUsage.Valid || rows[0].DayStart.Valid || rows[0].WeekUsage.Valid ||
		rows[0].MonthUsage.Valid || rows[1].DayUsage.Valid || !rows[1].DayStart.Valid || !rows[1].DayLimit.Valid {
		t.Fatalf("rows %+v", rows)
	}
	limit := int64(1000)
	v := keywindow.Evaluate(keywindow.Limits{Daily: &limit}, 2, rows, 1000, at)
	if !v.Admit() || v.Remaining != 1000 {
		t.Fatalf("the window check over NULLs: %+v", v)
	}
	// The positive control: the fresh schema refuses a NULL usage.
	if _, err := shared.Apply(ctx, []*spanner.Mutation{keyLimitRow(storetest.UniqueID("kh"), 0,
		map[string]any{"day_usage": spanner.NullInt64{}})}); err == nil {
		t.Fatal("the fresh tr_key_limit took a NULL day_usage")
	}
}
