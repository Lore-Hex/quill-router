# E2 strict keys: the edge decides a strict key with no limit

Status: increment 1, dark (`fastpath_pilot_strict_keys` off). Amends
[`fast-admission-e2-decide.md`](fast-admission-e2-decide.md) §3 and §8.1 and
[`fast-admission-e2-key-windows.md`](fast-admission-e2-key-windows.md) §9,
which put every `budget_strict` key outside the cohort (`key.budget_strict`).
It narrows that exclusion for one cohort: a strict key with no lifetime
limit and no spend window. Every other strict key stays on Python.

## 1. Why

Under `TR_FASTPATH_PILOT_KEY_WINDOWS=off` (production's setting), a strict
key with any window passes as `key.key_window` or `key.key_window_alert`
(the window gate overrides the reason, `TestAnUncoveredWindowedKeyKeepsItsReason`),
and one with a lifetime limit as `key.key_cap` (it comes first in the fact
order). So every `key.budget_strict` pass is a strict key with **no limit at
all**. That reason is the largest the edge's decide lines still pass to
Python.

## 2. What Python does for a strict key

- **Authorize** (`storage_gcp.py` `authorize_gateway_typed`):
  - the per-process admission slot, `strict_budget.strict_budget_slot`: one
    authorize per key and 16 keys per process, a 1 s wait inside a 5 s
    budget, else `StrictBudgetBusy`, which is answered 503 with
    `Retry-After: 1`;
  - then `authorize_atomic` with `strict_budget=True`: a credit reserve
    taken one shard at a time, then `storage_gcp_strict_budget.reserve_strict_key`;
  - that reserve is one conditional `UPDATE tr_key_limit SET reserved =
    reserved + @est` on shard 0. The update matches if `limit_micro` and
    each enforced window's `<w>_limit_micro` is NULL or leaves room for
    `reserved + est`;
  - a locked point read of the row decides: accepted with
    `key_reserved_micro = estimate`; 429 `key_window_limit_exceeded`; 402 for
    the lifetime limit or for a missing row (`KEY_MISSING`);
  - Stage D eligibility is ANDed with `not budget_strict` (`gateway.py`), so
    the 200's `stage_d.reason` is `stage_d_disabled`.
- **Settle, refund and reap** release the recorded key hold through
  `release_key`: `reserved -= hold`, `usage += actual`, and the windows are
  booked at the settle instant.
- **Python's pilot** keeps every strict key out (`pilot_reason`'s `key_cap`).

For a key whose four mirrored limits are NULL, every predicate of the
`UPDATE` is true. The reserve matches whatever the row holds and never
refuses the key for its budget. The hold it books on `reserved` is read by
no decision while the key has no limit. Its only purpose is to be counted
if a limit is added later.

## 3. What the earlier design said, and what replaces it here

[`fast-admission-and-batched-settlement.md`](fast-admission-and-batched-settlement.md)
says:

- §4.6 and §4.11 keep `budget_strict` keys synchronous at first;
- §4.6, under "Adding a cap, a window limit or `budget_strict`", sends a key
  that gains a limit through the key-status fence (§5 invariant 8: a new cap
  takes effect only after every fast hold of the key is booked).

[`fast-admission-e2-key-windows.md`](fast-admission-e2-key-windows.md) §9
leaves strict keys out because "the edge would need its own hold on the key
row, which the lease does not carry".

For the limitless cohort, a product rule replaces both the fence and the
key hold. **A strict key with no limit can never gain one**
(`storage_key_patch.StrictKeyFirstLimit`, #2902):

- a patch that would set the lifetime limit or any window on a limitless
  strict key is refused with 400;
- a strict key's limits can still be changed and removed, and removing the
  last one is permanent.

So the four limit fields, and their `tr_key_limit` mirrors (written in the
same commit), never go from all unset to any set. No limit can appear that
would need the edge's holds counted, and the edge takes none. The fence is
still needed for a key that gains a limit; this cohort cannot.

## 4. The rule

### 4.1 Facts

`gwstate.ParseKey` marks a `budget_strict` key with no lifetime limit and no
window `StrictLimitless`, and eligible:

- a strict key with a lifetime limit stays `key_limit`;
- one with any window stays `key_budget_strict`, with `Windows`;
- `PilotAt` still gives a limitless strict key `key_budget_strict`, Python's
  pilot reason, so `gateway_key_vectors_v1.json` is unchanged.

### 4.2 The counter row, read with the key and cached

Python's reserve reads the mirror columns on `tr_key_limit` shard 0, never
the entity, and answers 402 when the row is missing. So for a key whose
body says `budget_strict` true, `store.ReadKeyContext` makes a second read
after the key-context query: a strong point read of `(key_hash, 0)`, its
four mirrored limits only (`store.StrictKeyLimit`). No other key reads the
counter table. If that read fails, the row is left unread. Under enforce
the key then passes as `key.budget_strict_row.unread` until the next
refresh, and the key's other facts stay fresh.

The answer is cached with the key: a 5 s refresh and 15 s maximum age, as for
every key fact. No per-request read is needed:

- a strict key's mirrors never go from NULL to set (§3);
- a key's rows are never deleted while the key exists on GCP: key deletion
  keeps them, and reshards refuse strict keys;
- so a cached "present and limitless" can only become out of date by the
  key's deletion or disabling, which every key fact already has (decision
  17's maximum age);
- a stale "missing" or "limited" only passes more.

Cost: one point read per strict key per refresh, about 0.2 a second per
strict key in use.

### 4.3 The decision

A limitless strict key is decided as an uncapped key: every gate
unchanged (workspace eligibility, credit state, the lease, the decide row,
the in-flight slot). There are three differences:

- **Stage D** is composed with `EligibilityEnabled && !StrictLimitless`, so
  the 200 and the authorization row carry `stage_d_disabled` as Python's
  do. A strict key's stream therefore passes as
  `stream.stage_d:stage_d_disabled` and never goes on a lease.
- **No key hold.** The reservation row's `key_reserved_micro` is 0, as for
  every leased authorization. The leased settle books the actual usage on
  shard 0 (a strict key has one shard) with hold 0. Python's strict path
  holds the estimate on the key; the edge holds nothing on it.
- **No per-process slot.** Python's slot limits harm to the key's counter
  row, which the edge does not write. The edge's per-lookup-hash in-flight
  gate (decision 14) still bounds the key.

### 4.4 What stays on Python

- **Every strict key with a lifetime limit or a window** (cohort B). Its
  reserve counts in-flight holds, so the edge would need the key hold e2-key-windows §9
  describes. That is a design of its own.
- **A limitless strict key whose row is missing or mirrors a limit.**
- **Every strict key when the edge passes.** This includes `owner.busy`,
  `budget.spent`, a cold cache, and a stream. Python answers on its strict
  path, with its slot and its key hold.
- **Python's own pilot.** It keeps every strict key out under every mode:
  its leased branch reads no `tr_key_limit` row, so it could not keep the
  402 for a missing one.

## 5. The switch

A Python setting, `fastpath_pilot_strict_keys` (`off`, `shadow`, `enforce`;
default `off`; `TR_FASTPATH_PILOT_STRICT_KEYS=off` in `rollout.sh`). It is
published in the edge policy as the optional member
`fastpath_pilot_strict_keys`:

- the publisher writes the member only when the setting is not off;
- Go reads the member absent as off, and refuses `"off"`, null and anything
  else, as Python's parser does.

What the edge does in each mode:

| Mode | A limitless strict key |
|---|---|
| off (absent) | passes as `key.budget_strict` at the key's place in the fact order, today's answer, composed for no shadow |
| shadow | goes on through every gate. `key.budget_strict` is its decision gate, so the full shadow compares the answer Go would give. Any pass after the key's facts reads `key.budget_strict`, so the pass mix is unchanged |
| enforce | decided. A row Python's reserve would not match passes as `key.budget_strict_row.missing`, `.limited` or `.unread` |

Under shadow, those row passes read `key.budget_strict`.

## 6. Shadow accounting and the gate

The full shadow counts the classes of every limitless strict key's decision
in its own `strict` map on the period line. `fastpathwatch edge` reads it as
`strict_compared`, `strict_agree_full`,
`strict_would_admit_python_refused` and `strict_python_busy`.

- **Python refuses, Go would admit.** A 402 for a missing row, a 429, or any
  other refusal is `would_admit_python_refused` and pages, as for every key.
- **Python's strict-busy 503** ("Strict budget authorization is busy; retry
  with backoff.") is `strict.python_busy`: Python's slot was taken, a harm
  limit, not a refusal on the facts. It is neither an agreement nor a
  disagreement, and never pages. The same 503 to another decision stays
  `python_error.503`.

The gate is a count gate, as in e2-key-windows §8.3: 20,000 compared
limitless strict requests (`strict_compared`) with zero
`strict_would_admit_python_refused`, then `shadow` to `enforce`.

## 7. Parity vectors

The authorize goldens (`scripts/fastpath/authorize_vectors.py`) record, for
a strict key, its `tr_key_limit` shard 0 as Python's world held it
(`key_limit_row`). They also record:

- for the limitless key, Python's own strict-path 200 and rows (`shadow`);
- the same key with no row, and with a mirrored lifetime limit (both 402);
- a strict key with a lifetime limit (`key.key_cap`).

`TestTheStrictGoldensUnderEachMode` checks:

- under shadow and enforce, Go's composition staged under Python's ids is
  Python's 200 byte for byte;
- Python's reservation row holds the estimate on the key, where Go's holds
  nothing on it;
- the other keys pass under every mode.

`TestTheStrictGoldenThroughTheShadow` runs the full shadow over the same
golden (`agree_full`, rows `python_not_leased`, the strict map).

## 8. Rollout

1. Merge #2902, the product rule, and deploy the control plane.
2. Merge the edge's release with this member read and absent: its
   behaviour is unchanged, since absent is off.
3. Pin that release on both nodes (`infra/fastpath.tf`).
4. Set `TR_FASTPATH_PILOT_STRICT_KEYS=shadow` and deploy the control plane.
5. Watch the gate (§6).
6. Then `enforce`.

The way back is the setting to `off`, which the edge sees within the
policy's 60 s maximum age, then e2-decide §9's levers.

## 9. Residual differences

1. **`reserved` on a strict key's row** no longer counts the edge's in-flight
   holds. No decision reads it while the key has no limit, and none can
   appear (§3). `GET /v1/key`'s `reserved_microdollars` for such a key
   shows only Python's holds.
2. **Python's slot** no longer bounds the edge's requests for the key; the
   edge's in-flight gate does. Python's strict-busy 503s fall with it.
3. **Federated keys** never reach the edge (`key.federated`). On a peer
   plane, a federated copy of a strict key follows the home plane's record,
   where the rule holds.
4. **The public docs' latency note** ("strict authorization can be much
   slower ... bypasses local spend leases") stops being true for a limitless
   strict key under `enforce`. Correct it when the switch moves.
