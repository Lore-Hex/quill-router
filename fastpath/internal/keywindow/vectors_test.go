package keywindow

import (
	"encoding/json"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"testing"
	"time"

	"cloud.google.com/go/spanner"
)

// The vectors are what Python's own functions compute
// (scripts/fastpath/key_window_vectors.py, held fresh by
// tests/test_fastpath_key_window_vectors.py).
type vectorFile struct {
	BoundaryGuardUS int64             `json:"boundary_guard_us"`
	MaxShards       int               `json:"max_shards"`
	Windows         []string          `json:"windows"`
	Floors          []floorsVector    `json:"floors"`
	Decide          []decideVector    `json:"decide"`
	PinnedSource    map[string]string `json:"pinned_source"`
}

type floorsVector struct {
	Name          string           `json:"name"`
	AtUS          int64            `json:"at_us"`
	OffsetSeconds int              `json:"offset_seconds"`
	FloorsUS      map[string]int64 `json:"floors_us"`
	ResetsUS      map[string]int64 `json:"resets_us"`
}

type vectorRow struct {
	Shard        int64  `json:"shard"`
	DayUsage     *int64 `json:"day_usage"`
	DayStartUS   *int64 `json:"day_start_us"`
	DayLimit     *int64 `json:"day_limit"`
	WeekUsage    *int64 `json:"week_usage"`
	WeekStartUS  *int64 `json:"week_start_us"`
	WeekLimit    *int64 `json:"week_limit"`
	MonthUsage   *int64 `json:"month_usage"`
	MonthStartUS *int64 `json:"month_start_us"`
	MonthLimit   *int64 `json:"month_limit"`
}

type decideVector struct {
	Name      string            `json:"name"`
	Labels    []string          `json:"labels"`
	Limits    map[string]*int64 `json:"limits"`
	AlertOnly bool              `json:"alert_only"`
	Shards    int               `json:"shards"`
	Rows      []vectorRow       `json:"rows"`
	Estimate  int64             `json:"estimate"`
	AtUS      int64             `json:"at_us"`
	Python    struct {
		Enforced map[string]int64 `json:"enforced"`
		Verdict  string           `json:"verdict"`
		Window   string           `json:"window"`
		// Python's integers: past int64 only where Go passes for overflow.
		Limit        json.Number `json:"limit"`
		Remaining    json.Number `json:"remaining"`
		ResetSeconds int64       `json:"reset_seconds"`
		ResetsAtUS   int64       `json:"resets_at_us"`
	} `json:"python"`
	Edge string `json:"edge"`
}

func loadVectors(t *testing.T) vectorFile {
	t.Helper()
	text, err := os.ReadFile(filepath.Join("..", "..", "testdata", "key_window_vectors_v1.json"))
	if err != nil {
		t.Fatal(err)
	}
	var v vectorFile
	if err := json.Unmarshal(text, &v); err != nil {
		t.Fatal(err)
	}
	return v
}

func nullInt(p *int64) spanner.NullInt64 {
	if p == nil {
		return spanner.NullInt64{}
	}
	return spanner.NullInt64{Int64: *p, Valid: true}
}

func nullTime(us *int64) spanner.NullTime {
	if us == nil {
		return spanner.NullTime{}
	}
	return spanner.NullTime{Time: time.UnixMicro(*us).UTC(), Valid: true}
}

func (r vectorRow) row() Row {
	return Row{
		Shard:      r.Shard,
		DayUsage:   nullInt(r.DayUsage),
		DayStart:   nullTime(r.DayStartUS),
		DayLimit:   nullInt(r.DayLimit),
		WeekUsage:  nullInt(r.WeekUsage),
		WeekStart:  nullTime(r.WeekStartUS),
		WeekLimit:  nullInt(r.WeekLimit),
		MonthUsage: nullInt(r.MonthUsage),
		MonthStart: nullTime(r.MonthStartUS),
		MonthLimit: nullInt(r.MonthLimit),
	}
}

func edgeOutcome(v Verdict) string {
	if v.Admit() {
		return "decide"
	}
	return "pass:" + v.Pass
}

func TestTheVectorHeaderIsThePackage(t *testing.T) {
	v := loadVectors(t)
	if time.Duration(v.BoundaryGuardUS)*time.Microsecond != BoundaryGuard {
		t.Fatalf("boundary_guard_us %d, BoundaryGuard %s", v.BoundaryGuardUS, BoundaryGuard)
	}
	if v.MaxShards != MaxShards {
		t.Fatalf("max_shards %d, MaxShards %d", v.MaxShards, MaxShards)
	}
	if len(v.Windows) != len(Windows) {
		t.Fatalf("windows %v", v.Windows)
	}
	for i, w := range Windows {
		if v.Windows[i] != string(w) {
			t.Fatalf("windows %v, Go %v", v.Windows, Windows)
		}
	}
	if len(v.PinnedSource) != 4 {
		t.Fatalf("pinned_source %v", v.PinnedSource)
	}
}

func TestFloorsAndResetsAreWhatPythonComputes(t *testing.T) {
	v := loadVectors(t)
	if len(v.Floors) < 200 {
		t.Fatalf("%d floors cases", len(v.Floors))
	}
	for _, c := range v.Floors {
		at := time.UnixMicro(c.AtUS)
		if c.OffsetSeconds != 0 {
			// The same instant in another zone: Floors converts to UTC first.
			at = at.In(time.FixedZone("vector", c.OffsetSeconds))
		}
		day, week, month := Floors(at)
		got := map[Window]time.Time{Daily: day, Weekly: week, Monthly: month}
		for _, w := range Windows {
			if want := c.FloorsUS[string(w)]; got[w].UnixMicro() != want || got[w].Location() != time.UTC {
				t.Errorf("%s: %s floor %s, Python %s", c.Name, w, got[w], time.UnixMicro(want).UTC())
			}
			if want, r := c.ResetsUS[string(w)], ResetsAt(w, at); r.UnixMicro() != want {
				t.Errorf("%s: %s reset %s, Python %s", c.Name, w, r, time.UnixMicro(want).UTC())
			}
		}
	}
}

func TestEvaluateIsWhatPythonDecides(t *testing.T) {
	v := loadVectors(t)
	if len(v.Decide) < 400 {
		t.Fatalf("%d decide cases", len(v.Decide))
	}
	edges := map[string]int{}
	for _, c := range v.Decide {
		configured := Limits{Daily: c.Limits["daily"], Weekly: c.Limits["weekly"], Monthly: c.Limits["monthly"]}
		enforced := Enforced(configured, c.AlertOnly)
		for _, w := range Windows {
			want, set := c.Python.Enforced[string(w)]
			got := enforced.Get(w)
			if set != (got != nil) || (got != nil && *got != want) {
				t.Errorf("%s: enforced %s %v, Python %v (set %v)", c.Name, w, got, want, set)
			}
		}
		rows := make([]Row, len(c.Rows))
		for i, r := range c.Rows {
			rows[i] = r.row()
		}
		at := time.UnixMicro(c.AtUS)
		verdict := Evaluate(enforced, c.Shards, rows, c.Estimate, at)

		// The edge's outcome.
		got := edgeOutcome(verdict)
		if enforced.Empty() {
			// The edge never evaluates such a key; Evaluate passes it anyway.
			if verdict.Pass != PassUnconfigured {
				t.Errorf("%s: no enforced window, Evaluate %+v", c.Name, verdict)
			}
			got = "no_check"
		}
		edges[got]++
		if got != c.Edge {
			t.Errorf("%s: edge %s, want %s (verdict %+v, python %+v)", c.Name, got, c.Edge, verdict, c.Python)
		}

		// Never admit what Python refused or did not decide.
		pyDecided := c.Python.Verdict == "allow" || c.Python.Verdict == "deny"
		if verdict.Admit() && c.Python.Verdict != "allow" {
			t.Errorf("%s: Go admits, Python %s", c.Name, c.Python.Verdict)
		}
		// Go computes Python's decision on every case Python decides, except
		// where int64 or the year range cannot hold the arithmetic (the
		// overflow label, whichever pass reason takes precedence).
		if verdict.Decided != (pyDecided && !slices.Contains(c.Labels, "overflow")) {
			t.Errorf("%s: Decided %v, Python %s, edge %s", c.Name, verdict.Decided, c.Python.Verdict, c.Edge)
			continue
		}
		if !verdict.Decided {
			continue
		}
		if verdict.Allowed != (c.Python.Verdict == "allow") ||
			string(verdict.Window) != c.Python.Window ||
			strconv.FormatInt(verdict.Limit, 10) != c.Python.Limit.String() ||
			strconv.FormatInt(verdict.Remaining, 10) != c.Python.Remaining.String() ||
			verdict.ResetSeconds != c.Python.ResetSeconds {
			t.Errorf("%s: verdict %+v, Python %+v", c.Name, verdict, c.Python)
		}
		if r := ResetsAt(verdict.Window, at); r.UnixMicro() != c.Python.ResetsAtUS {
			t.Errorf("%s: resets %s, Python %s", c.Name, r, time.UnixMicro(c.Python.ResetsAtUS).UTC())
		}
	}
	// Every outcome is exercised.
	for _, edge := range []string{
		"decide", "no_check", "pass:" + PassRows, "pass:" + PassConfigDrift,
		"pass:" + PassBoundary, "pass:" + PassOverflow, "pass:" + PassExceeded,
	} {
		if edges[edge] == 0 {
			t.Errorf("no vector ends %s: %v", edge, edges)
		}
	}
}
