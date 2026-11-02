package keywindow

import (
	"math"
	"testing"
	"time"

	"cloud.google.com/go/spanner"
)

func i64(v int64) *int64 { return &v }

func current(at time.Time, usage int64, limits Limits) Row {
	day, week, month := Floors(at)
	r := Row{
		DayUsage:   spanner.NullInt64{Int64: usage, Valid: true},
		DayStart:   spanner.NullTime{Time: day, Valid: true},
		WeekUsage:  spanner.NullInt64{Int64: usage, Valid: true},
		WeekStart:  spanner.NullTime{Time: week, Valid: true},
		MonthUsage: spanner.NullInt64{Int64: usage, Valid: true},
		MonthStart: spanner.NullTime{Time: month, Valid: true},
	}
	mirror := func(p *int64) spanner.NullInt64 {
		if p == nil {
			return spanner.NullInt64{}
		}
		return spanner.NullInt64{Int64: *p, Valid: true}
	}
	r.DayLimit, r.WeekLimit, r.MonthLimit = mirror(limits.Daily), mirror(limits.Weekly), mirror(limits.Monthly)
	return r
}

// Python's datetime holds microseconds: a reading with nanoseconds decides
// as its microsecond, so the last nanoseconds of a day are still that day.
func TestTheInstantIsTruncatedToTheMicrosecond(t *testing.T) {
	midnight := time.Date(2026, 10, 22, 0, 0, 0, 0, time.UTC)
	day, _, _ := Floors(midnight.Add(-1))
	if !day.Equal(midnight.AddDate(0, 0, -1)) {
		t.Fatalf("floor of the last nanosecond %s", day)
	}
	l := Limits{Weekly: i64(1000)}
	noon := time.Date(2026, 10, 21, 12, 0, 0, 0, time.UTC)
	rows := []Row{current(noon, 0, l)}
	// 1.5 µs before the reset truncates to 2 µs before, and ceil gives 1 s.
	reset := ResetsAt(Weekly, noon)
	v := Evaluate(l, 1, rows, 0, reset.Add(-1500*time.Nanosecond))
	if !v.Decided || v.ResetSeconds != 1 {
		t.Fatalf("%+v", v)
	}
	// The guard is inclusive at the truncated instant: 5 s and 500 ns after
	// the floor is Python's 5 s exactly, so it passes.
	if v := Evaluate(l, 1, rows, 0, ResetsAt(Weekly, noon).AddDate(0, 0, -7).Add(BoundaryGuard+500)); v.Pass != PassBoundary {
		t.Fatalf("guard at floor + 5 s + 500 ns: %+v", v)
	}
	// A whole microsecond of nanoseconds never reaches the next second.
	if got := resetSeconds(noon.Add(time.Second), noon.Add(999*time.Nanosecond).Truncate(time.Microsecond)); got != 1 {
		t.Fatalf("reset seconds %d", got)
	}
}

// A start with nanoseconds compares as Python's truncated datetime does:
// floors are whole seconds, so truncation never crosses one.
func TestSubMicrosecondStartsCompareLikePython(t *testing.T) {
	l := Limits{Daily: i64(1000)}
	noon := time.Date(2026, 10, 21, 12, 0, 0, 0, time.UTC)
	day, _, _ := Floors(noon)
	r := current(noon, 1000, l)
	r.DayStart.Time = day.Add(-500 * time.Nanosecond) // Python: floor - 1 µs + 500 ns < floor
	if v := Evaluate(l, 1, []Row{r}, 1, noon); !v.Admit() || v.Remaining != 1000 {
		t.Fatalf("stale by half a microsecond still counted: %+v", v)
	}
	r.DayStart.Time = day.Add(500 * time.Nanosecond)
	if v := Evaluate(l, 1, []Row{r}, 1, noon); v.Admit() || v.Remaining != 0 {
		t.Fatalf("current start not counted: %+v", v)
	}
}

func TestEnforcedDropsAlertOnlyWindows(t *testing.T) {
	l := Limits{Daily: i64(1), Monthly: i64(2)}
	if got := Enforced(l, false); got != l {
		t.Fatalf("enforcing %+v", got)
	}
	if got := Enforced(l, true); !got.Empty() {
		t.Fatalf("alert-only %+v", got)
	}
}

func TestEvaluateRefusesRowsItCannotTrust(t *testing.T) {
	l := Limits{Daily: i64(1000)}
	noon := time.Date(2026, 10, 21, 12, 0, 0, 0, time.UTC)
	r0, r1 := current(noon, 0, l), current(noon, 0, l)
	r1.Shard = 1
	for name, c := range map[string]struct {
		shards int
		rows   []Row
	}{
		"none":         {1, nil},
		"short":        {2, []Row{r0}},
		"out of order": {2, []Row{r1, r0}},
		"duplicate":    {2, []Row{r0, r0}},
		"extra":        {1, []Row{r0, r1}},
		"zero shards":  {0, nil},
		"over max":     {MaxShards + 1, nil},
	} {
		if v := Evaluate(l, c.shards, c.rows, 0, noon); v.Pass != PassRows || v.Decided {
			t.Errorf("%s: %+v", name, v)
		}
	}
	if v := Evaluate(l, 2, []Row{r0, r1}, 0, noon); !v.Admit() {
		t.Fatalf("complete rows: %+v", v)
	}
}

func TestOverflowPassesAtEveryStep(t *testing.T) {
	noon := time.Date(2026, 10, 21, 12, 0, 0, 0, time.UTC)
	for name, c := range map[string]struct {
		limit int64
		usage []int64
	}{
		"sum up":       {math.MaxInt64, []int64{math.MaxInt64, 1}},
		"sum down":     {0, []int64{math.MinInt64, -1}},
		"limit-used":   {1, []int64{math.MinInt64}},
		"min limit":    {math.MinInt64, []int64{1}},
		"wrap back":    {math.MaxInt64, []int64{math.MaxInt64, 1, -2}},
		"max - (-max)": {math.MaxInt64, []int64{-math.MaxInt64}},
	} {
		l := Limits{Daily: &c.limit}
		rows := make([]Row, len(c.usage))
		for i, u := range c.usage {
			rows[i] = current(noon, u, l)
			rows[i].Shard = int64(i)
		}
		if v := Evaluate(l, len(rows), rows, 0, noon); v.Pass != PassOverflow || v.Decided {
			t.Errorf("%s: %+v", name, v)
		}
	}
	for _, c := range []struct{ a, b int64 }{{-1, math.MinInt64}, {math.MinInt64, 0}, {0, math.MinInt64 + 1}} {
		if _, ok := subInt64(c.a, c.b); !ok {
			t.Errorf("%d - %d refused", c.a, c.b)
		}
	}
	if _, ok := subInt64(0, math.MinInt64); ok {
		t.Error("0 - MinInt64 accepted")
	}
}

// Passing is the only failure mode: no verdict the edge admits is one that
// Python's arithmetic, done here in big integers, would refuse.
func TestAdmitImpliesAllowedOnRandomInputs(t *testing.T) {
	noon := time.Date(2026, 10, 21, 12, 0, 0, 0, time.UTC)
	seed := uint64(2190)
	next := func(n int64) int64 {
		seed = seed*6364136223846793005 + 1442695040888963407
		return int64(seed>>33) % n
	}
	for i := 0; i < 20000; i++ {
		var l Limits
		if next(2) == 0 {
			l.Daily = i64(next(2000) - 100)
		}
		if next(2) == 0 {
			l.Weekly = i64(next(5000) - 100)
		}
		if next(2) == 0 {
			l.Monthly = i64(next(9000) - 100)
		}
		shards := int(next(4)) + 1
		rows := make([]Row, shards)
		for s := range rows {
			rows[s] = current(noon, next(800)-50, l)
			rows[s].Shard = int64(s)
		}
		estimate := next(1500)
		v := Evaluate(l, shards, rows, estimate, noon)
		if !v.Admit() {
			continue
		}
		for _, w := range Windows {
			if p := l.Get(w); p != nil {
				var used int64
				for _, r := range rows {
					u, _, _ := r.window(w)
					used += u.Int64
				}
				if estimate > max(0, *p-used) {
					t.Fatalf("admitted past %s: %+v", w, v)
				}
			}
		}
	}
}
