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
// fast-admission-e2-key-windows.md section 4.3): one read returns every row
// of the key, in shard order, each window's usage, start and mirrored limit
// as stored, NULL as not valid; not another key's (one whose hash the key's
// begins with included); a key with no rows reads none; and an empty key
// hash is refused before any read. It reads every shard the key has, not the
// ones a cached shard count names: after the key's shards grew from 3 to 4,
// an edge holding 3 reads 4 rows, which the window check refuses
// (key_window.rows), rather than three that undercount.
func TestReadKeyWindowsReadsTheKeysShards(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	key, other := storetest.UniqueID("kh"), storetest.UniqueID("kh")
	longer := key + "0"
	day := time.Date(2026, 10, 21, 0, 0, 0, 0, time.UTC)
	week := time.Date(2026, 10, 19, 0, 0, 0, 0, time.UTC)
	if _, err := shared.Apply(ctx, []*spanner.Mutation{
		keyLimitRow(key, 0, map[string]any{"day_usage": int64(700), "day_start": day, "week_usage": int64(900),
			"week_start": week, "day_limit_micro": int64(1000), "week_limit_micro": int64(5000)}),
		keyLimitRow(key, 1, map[string]any{"day_usage": int64(5), "day_start": day, "day_limit_micro": int64(1000)}),
		keyLimitRow(key, 2, map[string]any{}),
		keyLimitRow(key, 3, map[string]any{"day_usage": int64(99), "day_start": day, "day_limit_micro": int64(1000)}),
		keyLimitRow(other, 0, map[string]any{"day_usage": int64(1), "day_start": day}),
		keyLimitRow(longer, 0, map[string]any{"day_usage": int64(2), "day_start": day}),
	}); err != nil {
		t.Fatal(err)
	}
	rows, err := s.ReadKeyWindows(ctx, key)
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
		{Shard: 3, DayUsage: spanner.NullInt64{Int64: 99, Valid: true}, DayStart: spanner.NullTime{Time: day, Valid: true},
			WeekUsage: spanner.NullInt64{Valid: true}, MonthUsage: spanner.NullInt64{Valid: true},
			DayLimit: spanner.NullInt64{Int64: 1000, Valid: true}},
	}
	if len(rows) != len(want) {
		t.Fatalf("read %d rows: %+v", len(rows), rows)
	}
	for i := range want {
		if !sameRow(rows[i], want[i]) {
			t.Errorf("row %d: %+v, want %+v", i, rows[i], want[i])
		}
	}
	// The key's shards grew to 4 while an edge holds 3: the check refuses
	// the set; with the count it should hold, it decides.
	limit := int64(1000)
	at := day.Add(12 * time.Hour)
	if v := keywindow.Evaluate(keywindow.Limits{Daily: &limit}, 3, rows, 1, at); v.Pass != keywindow.PassRows {
		t.Errorf("a stale shard count of 3 over 4 rows: %+v", v)
	}
	if v := keywindow.Evaluate(keywindow.Limits{Daily: &limit}, 4, rows, 1, at); v.Pass != keywindow.PassConfigDrift {
		// Shard 2 mirrors no daily limit: drift, which the check passes
		// too, but only once it has read the rows as a whole.
		t.Errorf("the key's own count over its rows: %+v", v)
	}
	if rows, err := s.ReadKeyWindows(ctx, storetest.UniqueID("kh")); err != nil || len(rows) != 0 {
		t.Errorf("a key with no rows: %+v, %v", rows, err)
	}
	if _, err := s.ReadKeyWindows(ctx, ""); err == nil {
		t.Error("an empty key hash read")
	}
}

// TestReadKeyWindowsIsAStrongRead: the read sees a write committed just
// before it, and then the write after that, as Python's own read of the rows
// would; a read at a staleness bound would miss a settle that Python sees. It
// is held so twice: by what the emulator answers, and in keywindows.go's
// source, which must take Single() with no timestamp bound.
func TestReadKeyWindowsIsAStrongRead(t *testing.T) {
	src, err := os.ReadFile("keywindows.go")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(src), "ro := s.client.Single()\n") || strings.Contains(string(src), "TimestampBound") ||
		strings.Contains(string(src), "Staleness") {
		t.Fatal("ReadKeyWindows must read with a plain s.client.Single(): a strong read")
	}
	s := spikeStore(t)
	ctx := context.Background()
	key := storetest.UniqueID("kh")
	day := time.Date(2026, 10, 21, 0, 0, 0, 0, time.UTC)
	for i, usage := range []int64{100, 250, 900} {
		m := keyLimitRow(key, 0, map[string]any{"day_usage": usage, "day_start": day})
		if i > 0 {
			m = spanner.UpdateMap("tr_key_limit", map[string]any{"key_hash": key, "shard": int64(0), "day_usage": usage})
		}
		if _, err := shared.Apply(ctx, []*spanner.Mutation{m}); err != nil {
			t.Fatal(err)
		}
		rows, err := s.ReadKeyWindows(ctx, key)
		if err != nil || len(rows) != 1 || rows[0].DayUsage.Int64 != usage {
			t.Fatalf("after writing %d: %+v, %v", usage, rows, err)
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
	rows, err := s.ReadKeyWindows(ctx, key)
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

// TestReadKeyWindowsReadsOneRowPastAKeysShards: the read stops one row past
// the most shards a key may have, so a key holding more rows than that is
// read as more (65), which the window check refuses (key_window.rows) even
// under the largest count, 64; a read that stopped at 64 would look like a
// complete key of 64 shards and decide.
func TestReadKeyWindowsReadsOneRowPastAKeysShards(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	key := storetest.UniqueID("kh")
	day := time.Date(2026, 10, 21, 0, 0, 0, 0, time.UTC)
	var rows []*spanner.Mutation
	for shard := range int64(keywindow.MaxShards + 2) {
		rows = append(rows, keyLimitRow(key, shard, map[string]any{"day_start": day, "day_limit_micro": int64(1000)}))
	}
	if _, err := shared.Apply(ctx, rows); err != nil {
		t.Fatal(err)
	}
	read, err := s.ReadKeyWindows(ctx, key)
	if err != nil || len(read) != keywindow.MaxShards+1 || read[keywindow.MaxShards].Shard != keywindow.MaxShards {
		t.Fatalf("read %d rows, %v", len(read), err)
	}
	limit := int64(1000)
	at := day.Add(12 * time.Hour)
	if v := keywindow.Evaluate(keywindow.Limits{Daily: &limit}, keywindow.MaxShards, read, 1, at); v.Pass != keywindow.PassRows {
		t.Fatalf("64 shards over %d rows: %+v", len(read), v)
	}
	// The positive control: the first 64 rows alone are a complete key.
	if v := keywindow.Evaluate(keywindow.Limits{Daily: &limit}, keywindow.MaxShards, read[:keywindow.MaxShards], 1,
		at); !v.Admit() {
		t.Fatalf("64 shards over 64 rows: %+v", v)
	}
}
