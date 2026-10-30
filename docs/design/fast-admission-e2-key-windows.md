# E2 key windows: the edge decides windowed keys

Status: design, no code. Amends [`fast-admission-e2-decide.md`](fast-admission-e2-decide.md)
§3 and §8.1, which put every key with a daily, weekly or monthly spend
window outside the cohort (`key.key_window`). This document says how the Go
edge decides those keys with Python's answer, what it reads, what Python
must change first, how parity is pinned and measured, and in what order it
ships. Nothing here changes a decision until the last step of §10, behind a
setting that is off.

Line citations are against `origin/main` at `12ddee3b3`.

## 1. Why

Over the edge's shadow logs for 2026-10-29 18:00Z to 01:00Z (all GCP
traffic, about 78,000 authorizes), 61,031 (78%) passed with `key.key_window`.
No other pass reason was above 5%. Windowed keys are the one lever for edge
coverage.

## 2. What Python does today, end to end

### 2.1 The configuration

A key's windows are three optional JSON fields on its `api_key` entity
(`limit_daily_microdollars`, `limit_weekly_microdollars`,
`limit_monthly_microdollars`, `storage_models.py:397-399`) and one flag,
`budget_alert_only` (`storage_models.py:404`, default false, set only by the
key's owner in the console or the API). `spend_windows.key_window_limits`
(`spend_windows.py:198`) reads the configured windows with `int(value)`;
`enforced_window_limits` (`spend_windows.py:210`) returns `{}` when
`budget_alert_only` is truthy, else the same map.

A config edit that touches a limit field (`TYPED_LIMIT_PATCH_FIELDS`,
`storage_key_patch.py:11-19`) writes the entity and mirrors the limits into
every `tr_key_limit` row's `day_limit_micro`, `week_limit_micro` and
`month_limit_micro` in one transaction (`storage_gcp_keys.py:311-350`,
`storage_gcp_counters.key_limit_mirror_rows`). `budget_alert_only` is not
mirrored: toggling it writes the entity only.

### 2.2 The counters

`tr_key_limit` (`scripts/deploy/migrate_typed_counters.sh:95-118`), primary
key `(key_hash, shard)`, one row per usage shard `0` to
`usage_shard_count - 1`. Per window: `<w>_usage` and `<w>_start` (`day`,
`week`, `month`). Databases that predate the window DDL added these columns
nullable (`migrate_typed_counters.sh:375-383`), so an old row can read
`NULL` usage or start.

Windows are fixed UTC calendar windows, reset lazily (`spend_windows.py:1-13`,
`window_floors` at `:104`):

- daily: UTC midnight;
- weekly: ISO week, Monday 00:00 UTC (`now.weekday()`, Monday is 0);
- monthly: the 1st, 00:00 UTC.

No time zone but UTC enters anywhere: `window_floors` converts with
`astimezone(dt.UTC)` and builds floors with `replace` and `timedelta`, so
there is no DST and no tz database.

### 2.3 Authorize

The route (`gateway.py:1797-1806`) computes `window_limits`:
`{}` for a BYOK request on a key whose `include_byok_in_limit` is false, else
`enforced_window_limits(api_key)`. So for an alert-only key the authorize
never looks at windows at all.

`authorize_gateway_typed` (`storage_gcp.py:5620-5651`), when
`window_limits` is non-empty and the key is not `budget_strict`, calls
`key_window_limit_decision` (`storage_gcp_authorize.py:362-438`) **before
and outside** the authorize transaction, on a lock-free multi-use snapshot:

1. If an idempotency scope is given and a reservation with the same
   fingerprint exists, return `None` (the transaction will replay).
2. `SELECT shard, day_usage, day_start, week_usage, week_start,
   month_usage, month_start FROM tr_key_limit WHERE key_hash=@kh AND
   shard>=0 AND shard<@shard_count ORDER BY shard`.
3. No rows: `None` (pass through). Rows that are not exactly
   `0..shard_count-1`: `RuntimeError` (a 500).
4. `now = utcnow()` (a fresh reading, not the request's `created_at`),
   floors from it, and per window the sum over shards of `int(usage or 0)`
   for rows whose start is not `NULL` and `>= floor`.
5. `decide_key_window_limits` (`spend_windows.py:126-165`): per configured
   window `remaining = max(0, limit - used)`, `allowed = estimate <=
   remaining`; the first rejecting window in daily, weekly, monthly order,
   else the allowed window with the least `remaining`.

A rejection returns `key_window_limit_exceeded:<window>`; the route answers
429 with `RateLimit-*` and `Retry-After` (`gateway.py:2023-2032`). An
acceptance carries the decision as `AuthorizeVerdict.rate_limit`, and the
response middleware adds `RateLimit-Limit`, `RateLimit-Remaining` and
`RateLimit-Reset` to the 200 (`middleware.py:168-177`).

The check is deliberately approximate (`spend_windows.py:47-53`): in-flight
holds are not counted, and the snapshot is read outside the transaction on
purpose, because a shared read of the hot `tr_key_limit` row before
`reserve_key`'s conditional UPDATE would reintroduce the read-lock-upgrade
deadlock the typed migration removed (`storage_gcp_authorize.py:373-379`,
`:520-524`).

**No key hold is taken for a windowed key without a lifetime cap.** The
route passes `skip_key_limit = limit_microdollars is None and not
budget_strict` (`gateway.py:1870`), so `authorize_atomic` takes `KEY_NO_HOLD`
and writes `key_reserved_micro = 0` (`storage_gcp_authorize.py:681-683`,
`:709`). Window enforcement writes nothing at authorize.

Strict keys (`budget_strict`) are different: one conditional UPDATE that
counts outstanding holds in each window, then a locked point read, inside
the transaction (`storage_gcp_strict_budget.py:22-78`). They stay out of
this design (§9).

### 2.4 Settle, refund, reap

Every terminal books the key's usage and lazily rolls its windows through
`release_key` (`storage_gcp_counter_dml.py:550-652`): `reserved -= hold`,
`usage += actual`, and each window's usage is replaced (if its start is
older than the settle instant's floor) or incremented. The windows are
booked at the **settle's** instant, not the authorize's.

- Today's settle (`settle_atomic`, `storage_gcp_authorize.py:968`) and the
  outbox drain release the recorded hold (0 here) and book `actual` on the
  reservation's `key_shard`.
- A refund books `actual = 0`: a no-op that may roll a stale start forward.
- The reaper (`_finalize_reaped_reservation_atomic`,
  `storage_gcp_authorize.py:1407-1646`) books the snapshot charge when it
  books one, else 0, through the same `_release_key_or_skip_deleted`
  (`:888-945`), whatever the reservation's holds.
- **The leased (pilot) finalize** (`typed_finalize_leased_result`,
  `storage_gcp.py:5163-5289`) books `actual` on success, nothing on a
  refund, through `_release_key_or_skip_deleted` with hold 0 on shard 0
  (`storage_gcp.py:5234-5272`); the drain's `_apply_leased`
  (`settle_outbox_apply.py:489`) calls the same method.

So for a windowed key with no lifetime cap the key counter is touched
identically on the leased and the unleased paths: no hold at authorize, the
actual charge at the terminal. The one difference is the shard the usage
lands on (shard 0 on the leased path, the reservation's random shard on the
other), which the window check sums away.

### 2.5 Alert-only keys

`maybe_send_budget_alerts` (`services/budget_alerts.py:32-66`) runs after a
successful settle as a background task (`gateway.py:5006-5023`): for an
alert-only key it reads `typed_key_usage`, compares each configured window,
and emails the workspace owner once per window instance (marker
`budget_alerted`, not transactional, a rare duplicate accepted by design).

**Gap found:** the leased settle (`_settle_leased_authorization`,
`gateway.py:5362-5449`) returns at `gateway.py:4599-4614`, before that block.
A leased request on an alert-only key would book its usage but never send
the alert. Today no such request exists, because Python's own pilot keeps
windowed keys out (`fastpath_frontdoor.py:430`, fed by `key_capped` at
`gateway.py:1827`). It must be fixed before any windowed key is leased
(§10, increment 1).

### 2.6 Python's leased path already runs the window check

In `authorize_gateway_typed` the window check (`storage_gcp.py:5620-5651`)
runs before the leased branch (`:5674`), and `_authorize_gateway_leased`
carries the decision through to its verdict (`:5097`, `:5101`, `:5160`). So
Python's pilot is ready for windowed keys except for one defect:
`_fastpath_admission` takes the lease hold before `authorize_gateway_typed`
(`gateway.py:1812`), and the 429 branch calls only
`release_user_model_slot_after_error()` (`gateway.py:2023-2024`), not
`release_after_error()`, so a window rejection after a lease admission
would keep the lease hold until the lease's end of life. Increment 1 fixes
it.

## 3. What the edge does today

`gwstate.ParseKey` puts any key with a window field set, alert-only or not,
outside the cohort (`fastpath/internal/gwstate/facts.go:183-186`,
`ReasonKeyWindowLimit`), and `budget_alert_only` is not read (`facts.go:209`).
`edge.keyPass` maps it to `PassKeyWindow`, `key.key_window`
(`fastpath/internal/edge/decide.go:614-615`). Because the key is ineligible,
the cache does not read its workspace's credit rows (`gwstate.Unneeded`).

For a cohort request the edge admits under the in-process front door's
lease and inserts the same two rows `_authorize_gateway_leased` writes
(e2-decide §5), in one mutation-only transaction
(`store.InsertLeasedAuthorization`, e2-decide §8.4). The edge reads nothing
from `tr_key_limit` and has no schema copy of it.

## 4. The design

### 4.1 The rule, stated once

For a request in the cohort (credits only, so `window_limits =
enforced_window_limits(key)`) on a key with no lifetime cap and not strict:

- **Alert-only key:** Python's authorize ignores the windows. The edge
  decides exactly as for an uncapped key. No read, no write.
- **Enforcing key:** the edge reads the key's `tr_key_limit` window columns
  with one strong single-use read, evaluates Python's
  `decide_key_window_limits` over them at the request's instant against the
  estimate, and admits only on `allowed`. Any rejection, any doubt, any read
  failure passes to Python, which answers (429, replay, or 200). No write.

The edge never answers a window refusal, so the 429 bytes, the
`Retry-After` and the replay pass-through (step 1 of §2.3) stay Python's:
a would-be rejection passes, and Python checks the idempotency scope first.
An allowed request whose scope is taken ends `ScopeTaken` at the insert and
passes too (e2-decide §8.4). The edge therefore needs no idempotency read.

### 4.2 What joins the admit transaction: nothing

The edge's admit transaction stays the two inserts. The window read does
**not** join it, for the reason Python keeps its own read out of its
transaction: a read-write transaction that reads the key row takes a shared
lock on the hottest row a busy key has, which every settle of that key
writes, and invites the lock-upgrade contention codex #93 removed. Python's
check is a snapshot outside its transaction, so a read outside the edge's
transaction is the same contract, not a weaker one.

No counter write is added at authorize: Python writes none for these keys
(`key_reserved_micro = 0` on both of its paths, §2.3), and the edge's
reservation row already writes 0 (e2-decide §5).

Where the read sits in the decide path (`decide_path.go`), in order:

1. `Decide` (pure): the gates, the evaluator, the compose. For an enforcing
   windowed key the `Decision` carries a `Windows` check: the cached limits,
   the shard count, the estimate (`Decision.Estimate`, the model estimate:
   Python checks `estimate`, never the reservation).
2. The in-flight slot of the lookup hash (decision 14), so the per-key
   limit also bounds concurrent window reads of one key.
3. **The window read**, on the detached context with a deadline of
   `min(arrival + DecideBudget, now + 300 ms)`. Past it the request passes
   (`key_window.unread`); a budget already spent asks nothing
   (`budget.spent`).
4. The evaluation (§4.4). Anything but `allowed` passes. Nothing has been
   admitted yet, so no refund.
5. `FrontDoor.Authorize`, `Finish`, the insert, the answer, unchanged.

The read is inside the decide budget, so decision 12's worst cases (an
answer 3.5 s after arrival, a forwarded pass no later) are unchanged and
`TestTheBudgetsAreTheContracts` keeps holding. The 200's
`data.timing.spanner_rpcs` counts the read (2 after an insert, 3 after a
read-back); the comparator ignores timing values.

### 4.3 The read

`store.ReadKeyWindows(ctx, keyHash string) ([]keywindow.Row, error)`:
one `client.Single().Read` (strong) of `tr_key_limit` over the key's whole
prefix, at most 65 rows (one past the 64 shards a key may have), not the
range the cached `usage_shard_count` names: after a key's shards grow, an
edge holding the old count reads rows it does not expect and passes
(`key_window.rows`), rather than a complete-looking subset that
undercounts. Columns `shard, day_usage, day_start,
week_usage, week_start, month_usage, month_start, day_limit_micro,
week_limit_micro, month_limit_micro`. Each `INT64` and `TIMESTAMP` column
is read as nullable (`spanner.NullInt64`, `spanner.NullTime`).

`fastpath/schema/fastpath.sql` gains a copy of `tr_key_limit`, held equal to
production's `CREATE TABLE` by `schema_test.go` as the other copies are. The
store's emulator tests also create a table with the window columns added
nullable, as `migrate_typed_counters.sh:375-383` left production, and write
`NULL` usage and starts.

### 4.4 The evaluation, exactly Python's

A new package, `fastpath/internal/keywindow`, with no imports from the edge:

```go
package keywindow

// Floors are spend_windows.window_floors(at): UTC day, ISO-week Monday,
// month start.
func Floors(at time.Time) (day, week, month time.Time)

// ResetsAt is spend_windows.window_resets_at.
func ResetsAt(w Window, at time.Time) time.Time

type Limits struct{ Daily, Weekly, Monthly *int64 } // nil: unset
type Row struct {
	Shard                              int64
	DayUsage, WeekUsage, MonthUsage    spanner.NullInt64
	DayStart, WeekStart, MonthStart    spanner.NullTime
	DayLimit, WeekLimit, MonthLimit    spanner.NullInt64 // the mirror, §4.5
}

type Verdict struct {
	Allowed      bool
	Window       Window // the governing window, Python's choice
	Limit        int64
	Remaining    int64
	ResetSeconds int64
	Pass         string // "" or a key_window.* reason
}

func Evaluate(l Limits, shards int, rows []Row, estimate int64, at time.Time) Verdict
```

The arithmetic, term for term with §2.3:

- `at` is the request's one clock reading (decision 15), truncated to the
  microsecond (Python's `datetime` holds microseconds), in UTC.
- Day floor: `time.Date(y, m, d, 0, 0, 0, 0, time.UTC)`. Week floor: the day
  floor minus `(int(at.Weekday()) + 6) % 7` days (Go's Sunday is 0,
  Python's Monday is 0). Month floor: `time.Date(y, m, 1, ...)`. Resets:
  day + 24 h, week + 7 days, `time.Date(y, m+1, 1, ...)` (Python's
  `(start + 32 days).replace(day=1)`, the same instant for any month).
- A row's window counts iff its start is not `NULL` and `!start.Before(floor)`;
  then its usage counts as `usage` or 0 when `NULL` (Python's
  `int(row[i] or 0)`). Sub-microsecond starts compare the same way on both
  sides, because floors are whole seconds and Python's nanosecond datetime
  truncates toward the floor.
- `remaining = max(0, limit - used)`, `allowed = estimate <= remaining`,
  the rejecting window first in daily, weekly, monthly order, else the
  minimum `remaining` with Python's tie rule (`min` keeps the first).
- Every sum and subtraction is overflow-checked; an overflow passes
  (`key_window.overflow`) where Python's integers would not overflow.
- `ResetSeconds` is `ceil((reset - at) / 1 s)` in integer microseconds. It
  never decides; the shadow compares it (§7).

Where Go passes rather than reproduce Python:

| Reason | When | Python would |
|---|---|---|
| `key_window.rows` | no rows, or not exactly shards `0..n-1` | admit with no check (no rows), or 500 (incomplete) |
| `key_window.unread` | the read failed or ran past its deadline | answer from its own read |
| `key_window.config_drift` | a row's mirrored limit differs from the cached entity's (§4.5) | read the entity strongly |
| `key_window.boundary` | `at` is within 5 s after a configured window's floor or before its reset | read `utcnow()` a few ms later than the edge's instant (§4.6) |
| `key_window.overflow` | an int64 overflow | compute exactly |
| `key_window.exceeded` | `allowed` is false | 429, or replay a same-fingerprint reservation |

Every row is a pass, so none of them can make the edge answer what Python
would refuse.

### 4.5 Configuration freshness

The edge reads the limits and `budget_alert_only` from its key cache (5 s
refresh, 15 s maximum age, e2-decide decision 2); Python reads the entity
strongly per request. Two measures bound the difference:

- **Limits:** the read already returns each row's mirrored
  `<w>_limit_micro`, written in the same transaction as the entity on every
  limit edit (§2.1). If any row's mirror differs from the cached limit,
  including `NULL` against a set limit, the request passes
  (`key_window.config_drift`). So a lowered or added limit takes effect at
  the edge on the next request, as at Python; only a limit edit made and
  reverted within one cache interval can be missed, and it changes nothing.
- **The alert flag:** not mirrored. A switch from alert-only to enforcing
  can be missed for up to 15 s, the bound decision 17 already accepts for a
  disabled or rescoped key. A switch the other way is harmless (the edge
  only passes more). The runbook's `fastpathctl decide off` remains the
  immediate lever.

### 4.6 The clock

Python's window instant is a fresh `utcnow()` inside
`key_window_limit_decision`, taken some milliseconds after the request
arrives; the edge uses its one reading. They can only disagree when a
window boundary falls between the two readings, so the edge passes a
request whose instant is within 5 s of any configured window's floor or
next reset (`key_window.boundary`, `keywindow.BoundaryGuard = 5 * time.Second`,
pinned in the vectors' header). That is at most 10 s a day of daily-window
passes per key. No time zone, DST rule or leap second enters: both sides
work in UTC instants, and Spanner timestamps carry no leap seconds.

### 4.7 Key facts

`gwstate.KeyFacts` gains:

```go
type KeyWindows struct {
	Limits    keywindow.Limits
	AlertOnly bool
	Shards    int // usage_shard_count, 1 to 64
}
// KeyFacts.Windows: nil for a key with no window configured.
```

`ParseKey` reads each limit as a JSON integer in int64 (anything else, a
float, a string, a bool, an integer past int64, is `key_shape:<field>`:
Python's `int()` accepts several of them, so Go passes rather than guess),
and `budget_alert_only` as a JSON bool (anything else is
`key_shape:budget_alert_only`: Python reads it by truthiness). It keeps
`usage_shard_count` (today checked and dropped, `facts.go:149-157`).

A key whose only exclusion is its windows becomes `Eligible` with
`Windows` set, so the cache reads its workspace's credit entry and the
gates after it run. `key_limit` (a lifetime cap) and `key_budget_strict`
still exclude it, in their current order. The policy's mode (§5) is
checked among the **decision gates** at the end of `Decide`, beside the
decide row (`DecideGate`), so `ForShadow` carries a complete decision for a
windowed key before the mode admits it, and the shadow compares it (§7).

The new pass reasons, in the §3 vocabulary:

- `key.key_window`: an enforcing windowed key under mode `off` or `alert`
  (today's reason, narrowed);
- `key.key_window_alert`: an alert-only windowed key under mode `off`;
- `key_window.*`: §4.4's table, after the decision.

## 5. The switch: one setting, published

A Python setting `fastpath_pilot_key_windows` (`off`, `alert`, `enforce`;
default `off`), read in two places so the cohort stays one definition
(e2-decide decision 11):

1. Python's own pilot: `pilot_reason`'s `key_capped`
   (`fastpath_frontdoor.py:430`, computed at `gateway.py:1827`) becomes
   `limit_microdollars is not None or (windows configured and not
   covered)`, where `alert` covers alert-only keys and `enforce` covers
   both.
2. The edge policy (e2-decide §4): an optional member
   `fastpath_pilot_key_windows`, absent read as `off`, the way
   `async_settle` is optional (`routingsnapshot/edgepolicy.go:62-64`). Go
   ships first, accepting it absent or present; Python publishes it after.
   A stale policy already passes everything.

## 6. Settle, refund and reap symmetry

Nothing new on the edge. An edge-decided windowed request is a leased
authorization, and Python settles it as any leased one (§2.4):

| Terminal | Python unleased, windowed key | Leased (Python pilot or edge) |
|---|---|---|
| authorize | no key write; `key_reserved_micro = 0` on a random key shard | no key write; `key_reserved_micro = 0`, key shard 0 |
| success | `release_key(hold 0, actual)` on the reservation's shard: usage and windows at the settle instant | `_release_key_or_skip_deleted(hold 0, actual)` on shard 0: the same columns at the settle instant |
| refund | `release_key(0, 0)`: no usage, may roll a start forward | nothing booked |
| reap | `_release_key_or_skip_deleted(res, snapshot charge or 0)` | the same call on the same row shape |
| drain | books the frozen charge | `_apply_leased` books the frozen charge |
| alert email | `maybe_send_budget_alerts` after a successful settle | **missing** (§2.5); increment 1 adds it |

A refund's rolled start changes no decision: a stale start and a rolled
start with zero usage both count zero. The window check sums every shard,
so the shard a booking lands on changes no decision either.

Two known differences, neither a window-parity issue: (1) the leased
finalize books every usage on shard 0, so a hot key with
`usage_shard_count > 1` concentrates its settle writes on one row. This is
already true of uncapped pilot keys; picking a random shard in
`typed_finalize_leased_result` is a Python-only follow-up. (2) Settles
that only the drain completes send no alert on either path (the drain must
not send them, `settle_outbox_apply.py:140`).

## 7. Golden vectors

One new file, held fresh like the others (e2-decide §10):
`fastpath/testdata/key_window_vectors_v1.json`, generated by
`scripts/fastpath/key_window_vectors.py` (`--print`, default write), its
staleness test `tests/test_fastpath_key_window_vectors.py`, and its Go
reader `fastpath/internal/keywindow/vectors_test.go`. Instants are integer
Unix microseconds (`_us`); no key is real.

The generator calls Python's own functions, never a re-implementation:
`window_floors`, `window_resets_at`, `key_window_limits`,
`enforced_window_limits`, `key_usage_shard_count`, and
`key_window_limit_decision` itself over a fake database whose snapshot
returns the case's rows and no reservation, with `utcnow` pinned to the
case's instant. Like `gateway_key_vectors.py`'s `ROUTE_READS`, the staleness
test extracts the route's `window_limits` expression (`gateway.py:1801-1805`)
and the `skip_key_limit` and `key_capped` expressions from the source and
fails when they change, so a Python change to the rule fails CI until the
vectors and this design follow.

Case kinds, each with a sorted `labels` list:

- `floors`: an instant, and its three floors and resets. Instants: each
  side of a UTC midnight; Sunday 23:59:59.999999 and Monday 00:00; month
  ends of 28, 29, 30 and 31 days; 2028-02-29 (leap); 2026-12-31 to
  2027-01-01; an ISO week that spans a year end (2026-12-28 to
  2027-01-03); dates of US and EU DST changes (to prove none is applied);
  and 200 seeded random instants.
- `facts`: an `api_key` body and Python's reading: the configured limits,
  the enforced limits, the shard count, or Python's error; and Go's
  expected facts or `key_shape:<field>`, with `exact` as in
  `gateway_key_vectors_v1.json` (bodies Go reads exactly must agree; the
  others Go may pass). Bodies: integers, floats (`5.0`), strings (`"5"`),
  bools, `null`, past int64, negative, zero; `budget_alert_only` as bool,
  string, number, `null`.
- `decide`: limits, `usage_shard_count`, rows (nullable usage, start and
  mirrored limit per window), estimate, instant; Python's verdict (`none`,
  `allow`, `deny`, or `error`) with window, limit, remaining and reset
  seconds; and Go's `edge` outcome (`decide` or `pass:<reason>`). Labels:
  `null_start`, `null_usage`, `stale_start`, `multi_shard`,
  `incomplete_shards`, `no_rows`, `zero_limit`, `negative_limit`,
  `exact_headroom` (estimate equal to remaining: allowed),
  `over_by_one`, `zero_estimate` (allowed even when spent), `used_over_limit`,
  `int64_edge`, `tie` (two windows with equal remaining), `boundary`,
  `config_drift`, `alert_only`.

The Go test runs `keywindow.Evaluate` and `gwstate.ParseKey` over every
case: on an `exact` case Go's verdict and governing window must equal
Python's; on any case Go may pass, never admit what Python refused.

Two existing files change with it: `gateway_key_vectors_v1.json` (the
`key_window_limit` fact becomes data, and `budget_alert_only` moves from
"not read" to read in `consulted_fields`), and `edge_policy_v1` (the
optional member).

## 8. Shadow comparison and rollout gate

### 8.1 Measurement before anything (increment 0)

The split of `key.key_window` into `key.key_window` and
`key.key_window_alert` (§4.7) costs nothing and answers the one fact this
design lacks: how much of the 78% is alert-only. If alert-only keys are a
small share, increments 2 and 4 ship together and alert mode is skipped as
a rollout stage.

### 8.2 The full shadow

With §4.7 the full shadow (`edge/shadow_full.go`) gets a `ForShadow`
decision for windowed keys whose only gate is the mode, and compares
Python's 200 byte for byte as it does now. Three additions:

1. **The window read in the shadow.** For an enforcing key the shadow makes
   the same read and evaluation off the request path, rate-limited like
   its row read-backs, and counts the verdict.
2. **Python's verdict, in Python's own words.** Python's 200 for an
   enforcing key carries `RateLimit-Limit`, `RateLimit-Remaining` and
   `RateLimit-Reset`; its 429 the same and `Retry-After`. The shadow
   records these three headers (it already reads status and body) and
   compares them with Go's `Limit`, `Remaining`, `ResetSeconds`:
   `window.agree`, or `window.differ.<limit|remaining|reset|window>`, with
   `remaining` bucketed by the size of the difference. `reset` differing by
   one second is the clock and counted apart.
3. **The classes.** Python 200 and Go `allowed`: agree or differ as today,
   plus item 2. Go `key_window.*` and Python 200: `would_pass` (benign,
   counted by reason). Python 429 `key_window_limit_exceeded` and Go
   `allowed`: `would_admit_python_refused` (the existing `AlertWouldAdmit`,
   with `window.*` fields), unless it is a **confirmed race**. A candidate
   is a 429 that names a window whose limit Go holds, with that limit, and
   a `RateLimit-Remaining` below what Go's rows leave in that window. That
   shape alone proves nothing: Python's 429 means its remaining is below the
   estimate, and Go's admit means the estimate fits Go's remaining, so
   every such disagreement has it, a defect of Go's read included. So after
   Python's answer the shadow re-reads the rows (one more strong read,
   within its window limiter and `WindowReadBudget`) and evaluates that
   window at the same instant. Usage only grows inside a window, so a
   settle that landed between the first two reads shows: the re-read's
   remaining at or below Python's is `window_race` (counted, traced on a
   line with the key hash's first 8 characters, the window, Go's first and
   re-read remaining, Python's and the gap; never paged one by one). Still
   above Python's is the would-admit, paged. No re-read that settles it
   (the limiter, a failed or late re-read, rows that are not the key's
   shards, or the window reset since, its usage another window's) is
   `window_race_unconfirmed`, which pages as the would-admit does, since
   it cannot be told from a defect. `edge_window_race` alerts, apart from
   the critical page, when the last hour's races pass 0.05% of the window
   reads (at least 3), or one key races more than 3 times in an hour.

The edge's own 200 carries no `RateLimit-*` headers (e2-decide decision 13:
only `Content-Type` and `Content-Length`). The enclave reads `Retry-After`
from an authorize only on an error (quill-cloud-proxy
`enclave-go/internal/trustedrouter/client.go:849`) and never reads
`RateLimit-*`, so this is the same accepted difference as Python's other
middleware headers.

### 8.3 The gate

A count gate, not a calendar one: decided by Joseph on 2026-10-30, the
`alert` stage is skipped and the setting goes from `off` straight to
`enforce` once the shadow has compared 20,000 enforcing requests with zero
disagreements, however long that takes. There is no 7-day minimum. Every
condition below holds over those 20,000:

- **Enforce:**
  - **same inputs, same answer:** among comparisons where Python's
    `RateLimit-Limit` and `RateLimit-Remaining` equal Go's, every verdict
    agrees; one disagreement is a parity bug and blocks;
  - confirmed `window_race` at most 0.05% of the window reads
    (`fastpathwatch edge`'s `window_race_ratio`): a settle landed between
    the two reads, the race both of Python's own instances already have
    with each other, each confirmed by the re-read of §8.2 item 3; no
    window `would_admit_python_refused`, and every `window_race_unconfirmed`
    explained;
  - `key_window.boundary` and `key_window.config_drift` counted and
    explained; `key_window.unread` under 0.1%;
  - window read latency p99 under 50 ms.

Alert-only keys need no stage of their own: `enforce` covers them too
(§5), and Python never refuses one for its windows, so any
`would_admit_python_refused` among them counts against the same zero. The
integration test that a leased settle crossing an alert-only window sends
the email once (increment 1) stays.

After the gate the setting moves (`off` to `enforce`); the decide rows are
untouched. The way back, fastest first: the setting to
`off` (the edge sees it within the policy's 60 s maximum age), then
e2-decide §9's levers.

## 9. Out of scope

- **Strict keys** (`budget_strict`): their check counts in-flight holds and
  runs inside the transaction (§2.3); the edge would need its own hold on
  the key row, which the lease does not carry. They stay
  `key.budget_strict`.
- **Lifetime caps** (`limit_microdollars`), alone or with windows: they
  take a key hold whose release the lease does not model; batched-settlement
  §4.6 keeps them synchronous. They stay `key.key_cap`.
- **BYOK**: the cohort is credits only, so the `include_byok_in_limit`
  branch of §2.3 never applies.
- **Other planes**: the edge decides only on GCP (`policy.Plane.Cloud`).

## 10. Increments

Each merges dark and deploys before the next. The recommended first
increment is 0.

| # | Where | What | Files | Size |
|---|---|---|---|---|
| 0 | Go + Python vectors | `key.key_window_alert` split (§4.7, §8.1); read `budget_alert_only` exactly; `gateway_key_vectors` update | `gwstate/facts.go`, `edge/decide.go`, `scripts/fastpath/gateway_key_vectors.py`, testdata, tests | ~80 LOC + ~120 test |
| 1 | Python, dark | 429 (and lifetime-cap 402) branches call `release_after_error()` (§2.6); leased settle schedules `maybe_send_budget_alerts` (§2.5); setting `fastpath_pilot_key_windows`; `pilot_reason`'s `key_capped` (§5); edge policy publishes the optional member | `routes/internal/gateway.py`, `services/fastpath_frontdoor.py`, `config.py`, `edge_policy.py`, tests | ~150 LOC + ~350 test |
| 2 | Go | edge policy optional member (ships before 1's publish); `KeyWindows` facts; the mode as a decision gate; alert-only keys decided under `alert`; shadow classes | `routingsnapshot/edgepolicy.go`, `gwstate/facts.go`, `edge/decide.go`, `edge/shadow_full.go`, tests | ~250 LOC + ~450 test |
| 3 | Go + Python | `keywindow` package; `key_window_vectors.py`, its staleness test and Go vector test | `fastpath/internal/keywindow/*`, `scripts/fastpath/key_window_vectors.py`, `tests/test_fastpath_key_window_vectors.py`, testdata | ~200 Go + ~350 Python + ~250 test |
| 4 | Go | `store.ReadKeyWindows`; `tr_key_limit` schema copy; the decide-path read step (§4.2); shadow read and header comparison (§8.2); alert and runbook rows | `store/keywindows.go`, `schema/fastpath.sql`, `edge/decide_path.go`, `edge/shadow_full.go`, `docs/runbooks/*`, tests | ~300 LOC + ~500 test |

Then the rollout of §8.3: `off` straight to `enforce` at the count gate
(Joseph's decision of 2026-10-30; `alert` stays a valid setting, unused by
the rollout). In all, five code PRs
after this one, about 25 files, roughly 1,000 lines of production code and
1,700 of tests and generators, the vector JSON aside.

Increment 2 still ships whole (the gate and facts are needed by 4); only
the `alert` stage of the rollout is gone.

## 11. Spanner cost per decided request

Counting the authorize path's Spanner calls that differ; Python's strong
reads of the key, workspace and boot entities on every request, which the
edge serves from its caches, come on top of Python's column.

| Path | Window check | Hold and rows | Calls | Locks |
|---|---|---|---|---|
| Python today, enforcing key (unleased) | one multi-use snapshot: scope read, `tr_key_limit` read (`storage_gcp_authorize.py:395-412`) | transaction: scope read, one batch DML of credit reserve plus two inserts, commit (`:614-756`) | about 5 | exclusive on a `tr_credit_balance` row until commit |
| Python today, alert-only key (unleased) | none | the same transaction | about 3 | the same |
| Python pilot, windowed key, after increment 1 | the same snapshot | replay snapshot read, transaction of two inserts, commit (`storage_gcp.py:5089-5135`) | about 5 | none on counters |
| Edge, enforcing key | one strong single-use read, at most `usage_shard_count` rows | the two-insert commit (e2-decide §8.4) | 2 | none |
| Edge, alert-only key | none | the two-insert commit | 1 | none |

Settle is unchanged by this design: a leased settle costs what it costs
today for an uncapped pilot key.

At the measured rate (61,031 windowed authorizes in 7 hours, about
2.4 a second over all GCP regions) the edge adds about 2.4 strong
single-row-range reads a second and removes about 12 Python calls a second
and every credit-row lock those requests take today. The read is a strong
read of one key's rows: lock-free, though it waits for a settle of the same
key that is mid-commit, as Python's snapshot read does. Increment 0's shadow
line can log Python's `data.timing.spanner_rpcs` for these requests to
replace the estimates above with Python's own count.

## 12. Parity risks this design does not remove

1. **The read instant.** Go and Python read `tr_key_limit` at different
   moments, so a settle landing between them can flip a request at the
   limit. Both are within the approximation Python documents (in-flight
   holds are not counted at all); §8.3 measures the rate and blocks on any
   disagreement with equal inputs.
2. **The alert flag's 15 s.** §4.5: a switch to enforcing can be missed
   for one cache interval. Mirroring `budget_alert_only` into
   `tr_key_limit` would close it and is a Python change of its own.
3. **The 200's `RateLimit-*` headers.** Absent on the edge's 200 and
   unread by today's enclave (§8.2). An enclave that starts reading them
   would need the edge to emit them; `keywindow.Verdict` already has the
   values.
4. **Alert-only share unknown.** Increment 0 measures it.
5. **Shard-0 concentration** of leased usage bookings for hot sharded keys
   (§6), already present for uncapped pilot keys.
