// Package keywindow is the edge's copy of Python's per-key spend-window check
// (docs/design/fast-admission-e2-key-windows.md §4.4): the fixed UTC window
// floors of spend_windows.window_floors, the resets of
// spend_windows.window_resets_at, and the allowed/denied decision that
// storage_gcp_authorize.key_window_limit_decision reaches from the summed
// tr_key_limit shard counters through spend_windows.decide_key_window_limits.
//
// It is pure: no clock, no I/O, no imports from the edge. The caller reads the
// rows and supplies the request's one clock reading. Where Go cannot be sure
// it answers what Python would, the Verdict carries a Pass reason and the
// edge hands the request to Python; a Verdict admits only when Pass is empty.
//
// fastpath/testdata/key_window_vectors_v1.json, written by
// scripts/fastpath/key_window_vectors.py from Python's own functions, holds
// this package to Python (vectors_test.go).
package keywindow

import (
	"math"
	"time"

	"cloud.google.com/go/spanner"
)

// Window names a spend window as Python does (spend_windows.WINDOWS).
type Window string

const (
	Daily   Window = "daily"
	Weekly  Window = "weekly"
	Monthly Window = "monthly"
)

// Windows is Python's enforcement order: a rejecting window earlier in it wins.
var Windows = [3]Window{Daily, Weekly, Monthly}

// BoundaryGuard is how close to a configured window's floor or next reset the
// request's instant may fall before the edge passes (key_window.boundary,
// §4.6): Python reads its own utcnow() a few milliseconds later than the
// edge's instant, so a boundary between the two readings could flip the
// answer. Pinned in the vectors' header.
const BoundaryGuard = 5 * time.Second

// MaxShards is storage_gcp_counters.MAX_KEY_USAGE_SHARDS.
const MaxShards = 64

// MinYear and MaxYear bound the instants Evaluate decides. Python's datetime
// holds years 1 to 9999, so a reset that falls in year 10000 raises
// OverflowError there; the edge passes any instant near either end.
const (
	MinYear = 2
	MaxYear = 9998
)

// Pass reasons, §4.4's table. Every one hands the request to Python.
const (
	// No enforced window. The edge never evaluates such a key (an alert-only
	// or unwindowed key is decided as an uncapped one); Evaluate passes it
	// rather than guess.
	PassUnconfigured = "key_window.unconfigured"
	// No rows, or not exactly shards 0..n-1 (or a shard count outside
	// 1..MaxShards). Python admits with no check (no rows), or raises.
	PassRows = "key_window.rows"
	// The read failed or ran past its deadline. Set by the caller, not here.
	PassUnread = "key_window.unread"
	// A row's mirrored <w>_limit_micro differs from the cached limit (§4.5).
	PassConfigDrift = "key_window.config_drift"
	// The instant is within BoundaryGuard of a configured window's floor or
	// next reset (§4.6).
	PassBoundary = "key_window.boundary"
	// A sum or difference left int64, where Python's integers would not; or
	// the instant is outside MinYear..MaxYear, where Python's datetime
	// arithmetic (a reset in year 10000) raises.
	PassOverflow = "key_window.overflow"
	// Python's decision is a rejection: Python answers the 429 (or replays).
	PassExceeded = "key_window.exceeded"
)

// Limits are a key's window limits in microdollars; nil is unset.
type Limits struct{ Daily, Weekly, Monthly *int64 }

// Get is the limit of one window, nil when unset.
func (l Limits) Get(w Window) *int64 {
	switch w {
	case Daily:
		return l.Daily
	case Weekly:
		return l.Weekly
	case Monthly:
		return l.Monthly
	}
	return nil
}

// Empty reports that no window is set.
func (l Limits) Empty() bool { return l.Daily == nil && l.Weekly == nil && l.Monthly == nil }

// Enforced is spend_windows.enforced_window_limits: the configured limits,
// or none for an alert-only key (its windows email, never block).
func Enforced(configured Limits, alertOnly bool) Limits {
	if alertOnly {
		return Limits{}
	}
	return configured
}

// Row is one tr_key_limit shard as the edge reads it. Every column is
// nullable: databases older than the window DDL added them nullable.
type Row struct {
	Shard                           int64
	DayUsage, WeekUsage, MonthUsage spanner.NullInt64
	DayStart, WeekStart, MonthStart spanner.NullTime
	DayLimit, WeekLimit, MonthLimit spanner.NullInt64 // the mirror, §4.5
}

func (r Row) window(w Window) (usage spanner.NullInt64, start spanner.NullTime, limit spanner.NullInt64) {
	switch w {
	case Daily:
		return r.DayUsage, r.DayStart, r.DayLimit
	case Weekly:
		return r.WeekUsage, r.WeekStart, r.WeekLimit
	default:
		return r.MonthUsage, r.MonthStart, r.MonthLimit
	}
}

// Verdict is the edge's reading of one window check.
type Verdict struct {
	// Decided: Go computed Python's decision, in the fields below. False when
	// the rows, the limits or an overflow kept it from doing so.
	Decided bool
	// Allowed is Python's allowed: estimate <= Remaining of every window.
	Allowed bool
	// Window is the governing window, Python's choice: the first rejecting
	// window in daily, weekly, monthly order, else the least Remaining (the
	// first on a tie).
	Window       Window
	Limit        int64
	Remaining    int64
	ResetSeconds int64
	// Pass is "" or a key_window.* reason; any reason hands the request to
	// Python.
	Pass string
}

// Admit reports that the edge may admit: Python's decision computed, allowed,
// and no reason to pass.
func (v Verdict) Admit() bool { return v.Pass == "" && v.Decided && v.Allowed }

// instant is the request's clock reading as Python's datetime holds it: UTC,
// truncated to the microsecond.
func instant(at time.Time) time.Time { return at.UTC().Truncate(time.Microsecond) }

// Floors is spend_windows.window_floors(at): the UTC day, the ISO week's
// Monday and the month's first, each at 00:00 UTC.
func Floors(at time.Time) (day, week, month time.Time) {
	at = instant(at)
	y, m, d := at.Date()
	day = time.Date(y, m, d, 0, 0, 0, 0, time.UTC)
	// Go's Sunday is 0, Python's Monday is 0.
	week = day.AddDate(0, 0, -((int(at.Weekday()) + 6) % 7))
	month = time.Date(y, m, 1, 0, 0, 0, 0, time.UTC)
	return day, week, month
}

// ResetsAt is spend_windows.window_resets_at(w, at): the start of the next
// window. Python's monthly (start + 32 days).replace(day=1) is the first of
// the next month for every month.
func ResetsAt(w Window, at time.Time) time.Time {
	day, week, month := Floors(at)
	switch w {
	case Daily:
		return day.AddDate(0, 0, 1)
	case Weekly:
		return week.AddDate(0, 0, 7)
	case Monthly:
		return time.Date(month.Year(), month.Month()+1, 1, 0, 0, 0, 0, time.UTC)
	}
	panic("keywindow: unknown window " + string(w))
}

func floor(w Window, at time.Time) time.Time {
	day, week, month := Floors(at)
	switch w {
	case Daily:
		return day
	case Weekly:
		return week
	}
	return month
}

// resetSeconds is Python's max(0, ceil((resets_at - now).total_seconds())),
// in integer microseconds (at is whole microseconds).
func resetSeconds(reset, at time.Time) int64 {
	us := int64(reset.Sub(at) / time.Microsecond)
	if us <= 0 {
		return 0
	}
	return (us + 999_999) / 1_000_000
}

// nearBoundary reports that at is within BoundaryGuard after the window's
// floor or before its next reset, both ends inclusive.
func nearBoundary(w Window, at time.Time) bool {
	return at.Sub(floor(w, at)) <= BoundaryGuard || ResetsAt(w, at).Sub(at) <= BoundaryGuard
}

func addInt64(a, b int64) (int64, bool) {
	s := a + b
	if (b > 0 && s < a) || (b < 0 && s > a) {
		return 0, false
	}
	return s, true
}

func subInt64(a, b int64) (int64, bool) {
	if b == math.MinInt64 {
		if a >= 0 {
			return 0, false
		}
		return a - b, true
	}
	return addInt64(a, -b)
}

// Evaluate is key_window_limit_decision over rows already read (strongly, in
// shard order) for a key with usage_shard_count shards, against the key's
// enforced limits, for a request of estimate microdollars at instant at. It
// computes Python's decision whenever it can (Decided), and sets Pass when
// the edge must not admit: in order unconfigured, rows, an instant out of
// range (overflow), config_drift, boundary, overflow, exceeded.
func Evaluate(l Limits, shards int, rows []Row, estimate int64, at time.Time) Verdict {
	// Python checks nothing when no window is enforced.
	if l.Empty() {
		return Verdict{Pass: PassUnconfigured}
	}
	// Python: no rows is None (admit unchecked); rows other than exactly
	// 0..n-1 raise. Neither is the edge's to answer.
	if shards < 1 || shards > MaxShards || len(rows) != shards {
		return Verdict{Pass: PassRows}
	}
	for i, r := range rows {
		if r.Shard != int64(i) {
			return Verdict{Pass: PassRows}
		}
	}
	at = instant(at)
	if y := at.Year(); y < MinYear || y > MaxYear {
		return Verdict{Pass: PassOverflow}
	}

	drift, boundary, overflow := false, false, false
	var v Verdict
	var best *Verdict // the allowed window with the least Remaining
	for _, w := range Windows {
		limit := l.Get(w)
		for _, r := range rows {
			_, _, mirror := r.window(w)
			if (limit == nil) != !mirror.Valid || (limit != nil && mirror.Int64 != *limit) {
				drift = true
			}
		}
		if limit == nil {
			continue
		}
		if nearBoundary(w, at) {
			boundary = true
		}
		if overflow {
			continue
		}
		fl := floor(w, at)
		var used int64
		ok := true
		for _, r := range rows {
			usage, start, _ := r.window(w)
			// Python: int(usage or 0) for rows whose start is not NULL
			// and >= the floor; a NULL or stale start is zero spend.
			if !start.Valid || start.Time.Before(fl) || !usage.Valid {
				continue
			}
			if used, ok = addInt64(used, usage.Int64); !ok {
				break
			}
		}
		var left int64
		if ok {
			left, ok = subInt64(*limit, used)
		}
		if !ok {
			overflow = true
			continue
		}
		d := Verdict{
			Decided:      true,
			Window:       w,
			Limit:        *limit,
			Remaining:    max(0, left),
			ResetSeconds: resetSeconds(ResetsAt(w, at), at),
		}
		d.Allowed = estimate <= d.Remaining
		switch {
		case v.Decided:
			// A rejecting window already governs.
		case !d.Allowed:
			v = d
		case best == nil || d.Remaining < best.Remaining:
			best = &d
		}
	}
	if overflow {
		v = Verdict{}
	} else if !v.Decided && best != nil {
		v = *best
	}
	switch {
	case drift:
		v.Pass = PassConfigDrift
	case boundary:
		v.Pass = PassBoundary
	case overflow:
		v.Pass = PassOverflow
	case !v.Allowed:
		v.Pass = PassExceeded
	}
	return v
}
