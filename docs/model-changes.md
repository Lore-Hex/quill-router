# Public model change history

After changing catalog data, routing/privacy declarations, or lifecycle schedules, run:

```sh
.venv/bin/python scripts/update_model_changes.py
.venv/bin/python scripts/update_model_changes.py --check
```

Review and include both `src/trusted_router/data/model_changes.jsonl` and
`model_changes_state.json`. CI runs the check; the hourly pricing workflow runs the
update before validation and stages both files. Neither command accesses a database
or a provider API. The package serves the committed history without rebuilding it.

The baseline stores a compact public catalog projection and its observation time.
The check reconstructs at that time, including future lifecycle projections, so CI
cannot become stale solely because a scheduled minute passed. Updating advances the
baseline through recorded cutovers before comparing the new catalog. Retirements
come from `provider_lifecycle._RETIREMENTS` (the retirement-email helper is not yet
in this checkout). Pricing and retirement boundaries come from that module's dated
`*_AT` constants. Add a dated constant when adding a new one-time lifecycle cutover.
Each forecast uses the catalog's own behavior at the cutover. Discovery freshness
stays pinned during forecasts: a missed-refresh deadline is conditional, not a
provider announcement. An actual expiry is recorded at the next observation.

Concrete model confidentiality comes from `trustedrouter.capabilities.confidential`.
Aliases without a capability declaration keep `null`; they do not acquire a made-up
availability flag. Endpoint identity, upstream id, usage type, privacy tier and
confidential capability are compared; other capabilities are outside this history's
route/privacy scope. Ordinary price refreshes are omitted. Pricing policies and
announced rates have a separate, opt-in `pricing_schedule_changed` type; recurring
peak/off-peak switches do not generate repeated entries.

The JSON and Atom endpoints use the same selection: exact `model`, inclusive
`since` on `recorded_at`, repeated `type`, `include_prices`, and optional `upcoming`.
Results have a stable ascending `(recorded_at, id)` order and no implicit limit.
Pollers retain the greatest recording timestamp and deduplicate ids. A future
change is visible as soon as recorded; its `effective_at` does not advance the
polling cursor. Cancelled forecasts stay in the committed audit file but are
replaced in the public view by `schedule_cancelled` entries referencing their ids.
An unchanged forecast keeps its id. Reinstating a cancelled forecast gets a new id.

September 2026 was backfilled by `python -m scripts.backfill_model_changes`, which
refuses to overwrite existing history. It replays first-parent catalog revisions
and scheduled boundaries from the preceding revision, in isolated source trees.
Pre-capabilities revisions call their own routing predicate; they do not apply the
current privacy rule retroactively. The September 1 baseline does not invent model
launches. Commit timestamps are observations, not proof of deployment, and historical
changes first learned after an announced date use the observation date. Each
commit-derived event includes its source commit. New recordings likewise describe
catalog state rather than production rollout completion or transient health.
