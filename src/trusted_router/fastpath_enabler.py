"""The fast path's enabler (docs/design/fast-admission-widening.md section 5).

Runs as ``python -m trusted_router.fastpath_enabler`` in a Cloud Run job every
15 minutes (scripts/deploy/fastpath_enabler_job.sh). It turns the switch,
``tr_fastpath_workspace``, on for every eligible workspace and off for every
enabled workspace that is no longer eligible, by tier, funding and plane.

``TR_FASTPATH_ENABLER_MODE``:

- ``off`` (the default) reads nothing and writes nothing;
- ``dry-run`` reads one snapshot and logs the run's report;
- ``apply`` also writes. Each write is its own read-write transaction that
  first reads the hold table's rows ``*`` (a kill), ``#pause`` and ``#armed``
  and writes nothing while ``*`` or ``#pause`` exists or ``#armed`` does not
  (section 5.6), so nothing is written before an operator's
  ``fastpathctl enabler arm`` and nothing after a kill or a pause commits.

The enabler never writes the hold table. Its enable writes the switch row
only (the fixture's ``enable`` less its hold removal, which it never needs: it
enables only a workspace with no hold, read in the same transaction), and its
disable is the fixture's ``disable_unheld`` (``disable_unheld`` here, a function
of its own, never a flag on a common one). The writes are
``fastpath/testdata/switch_statements_v1.json``, the fixture Go's
``Store.SetWorkspace`` executes, carried in the package as
``data/fastpath_switch_statements_v1.json`` (the image holds ``src`` alone);
a test holds the two copies to the same bytes.

Nothing here runs in a request: it is a job's process, so its Spanner calls
are synchronous by design.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trusted_router.security import lookup_hash_api_key
from trusted_router.storage_gcp_io import run_in_transaction_with_retry

logger = logging.getLogger("trusted_router.fastpath_enabler")

MODES = ("off", "dry-run", "apply")
# The tier a lease needs (section 4(b)); the grant refuses below it.
REQUIRED_TIER = 3
# Enables a run writes at most, so a group lands over a few runs (section 5.3).
MAX_ENABLES = 500
# The fact-change guard and the rank pace: more than this many of a run's
# disables for those causes is a reading defect sooner than a fact.
GUARD_FLOOR = 20
GUARD_FRACTION = 0.10
# The snapshot's chunk, as the tier job reads its own.
CHUNK = 1_000
LOW = {"priority": "PRIORITY_LOW"}

# The hold table's rows that are no workspace's (store/hold.go).
FLEET_HOLD = "*"
PAUSE_ROW = "#pause"
ARMED_ROW = "#armed"
GUARD_ROWS = (FLEET_HOLD, PAUSE_ROW, ARMED_ROW)

# Why a workspace is out, by how its disable is taken (section 5.3):
# uncapped, the safe direction, already refused by the grant; applied whole,
# an operator's list; guarded, a change of facts that could be a reading
# defect. Each list is in precedence order, and a workspace is counted under
# its first cause alone. A rank past the margin is paced, not guarded.
UNCAPPED = ("latched", "paused", "in_debt", "held")
WHOLE = ("excluded", "async_shadow")
GUARDED = ("credit_rows", "tier", "balance", "bypass", "deleted", "federated", "probe", "monitor_key")
PRECEDENCE = UNCAPPED + WHOLE + GUARDED
RANK = "rank"

# The snapshot's reads, each at the snapshot's timestamp and LOW priority.
# The workspaces, the overrides and the entities are read as the tier job
# reads them (trust_tier_bulk: BULK_WORKSPACE_IDS_SQL, BULK_OVERRIDES_SQL,
# BULK_ENTITIES_BY_ID_SQL; a test holds these to those).
ENABLER_WORKSPACE_IDS_SQL = "SELECT DISTINCT workspace_id FROM tr_credit_balance"
ENABLER_OVERRIDES_SQL = (
    "SELECT workspace_id, tier, identity_bypass FROM tr_trust_override WHERE workspace_id IN UNNEST(@ids)"
)
ENABLER_ENTITIES_SQL = "SELECT id, body FROM tr_entities WHERE kind=@kind AND id IN UNNEST(@ids)"
ENABLER_BALANCE_SQL = (
    "SELECT workspace_id, shard, trust_tier, trust_latched_at, trust_override_tier, billing_pause_causes, "
    "in_debt, total_credits, total_usage, reserved, trust_reconciled_through FROM tr_credit_balance "
    "WHERE workspace_id IN UNNEST(@ids)"
)
ENABLER_SWITCH_SQL = "SELECT workspace_id, enabled, changed_at FROM tr_fastpath_workspace"
ENABLER_HOLDS_SQL = "SELECT workspace_id, reason, held_at FROM tr_fastpath_workspace_hold"
ENABLER_KILLS_SQL = "SELECT kill_at FROM tr_fastpath_kill"
# A write's own reads, in its read-write transaction, by complete key.
ENABLER_GUARD_SQL = "SELECT workspace_id FROM tr_fastpath_workspace_hold WHERE workspace_id IN UNNEST(@rows)"
ENABLER_SWITCH_ROW_SQL = "SELECT enabled FROM tr_fastpath_workspace WHERE workspace_id = @workspace"
ENABLER_OVERRIDE_SQL = "SELECT tier, identity_bypass FROM tr_trust_override WHERE workspace_id = @workspace"
ENABLER_CREDIT_ROWS_SQL = (
    "SELECT shard, trust_tier, trust_latched_at, trust_override_tier, billing_pause_causes, in_debt, "
    "total_credits, total_usage, reserved, trust_reconciled_through FROM tr_credit_balance "
    "WHERE workspace_id = @workspace ORDER BY shard"
)

FIXTURE = Path(__file__).resolve().parent / "data" / "fastpath_switch_statements_v1.json"
HOLD_TABLE = "tr_fastpath_workspace_hold"
_PARAMETER = re.compile(r"@(\w+)")
_COMMIT_TIMESTAMP = "@commit_timestamp"

# The probes' keys, by the reason their workspaces are counted under. The
# job reads each from the control plane's own secret, by an environment
# variable outside the TR_ prefix (the control surface's settings may not
# hold them, config.SERVICE_SURFACE_SECRET_OWNERS), hashes it and keeps only
# the lookup hash.
PROBE_KEY_ENVIRONMENT = {
    "FASTPATH_ENABLER_MONITOR_KEY": "monitor_key",
    "FASTPATH_ENABLER_STAGE_D_PROBE_KEY": "probe",
}


class SnapshotError(RuntimeError):
    """A snapshot read failed: the run writes nothing."""


# -- the fixture ----------------------------------------------------------------


def load_switch_operations(path: Path = FIXTURE) -> dict[str, dict[str, Any]]:
    """The fixture's operations, refused as Go's switchsql.Parse refuses
    one: another version, an operation missing or unknown, a mutation that
    is not an insert_or_update of paired columns and values, a statement
    without SQL or a count's name of its own."""
    fixture = json.loads(path.read_text())
    if fixture.get("version") != 1:
        raise ValueError(f"switch fixture: version {fixture.get('version')!r}, want 1")
    operations = fixture.get("operations", {})
    if set(operations) != {"enable", "disable", "disable_unheld"}:
        raise ValueError(f"switch fixture: operations {sorted(operations)}")
    for name, op in operations.items():
        for mutation in op["mutations"]:
            if mutation["op"] != "insert_or_update" or len(mutation["columns"]) != len(mutation["values"]):
                raise ValueError(f"switch fixture: {name}: a mutation that is not a paired insert_or_update")
        counts = [statement["counts"] for statement in op["statements"]]
        if any(not statement["sql"].strip() for statement in op["statements"]) or len(set(counts)) != len(counts):
            raise ValueError(f"switch fixture: {name}: a statement without SQL or a count's name of its own")
    return operations


def _names_hold_table(text: str) -> bool:
    # Spanner identifiers are case-insensitive.
    return HOLD_TABLE in text.lower()


def _writes_hold_table(op: Mapping[str, Any]) -> bool:
    return any(_names_hold_table(m["table"]) for m in op["mutations"]) or any(
        _names_hold_table(s["sql"]) for s in op["statements"])


def enabler_operations(path: Path = FIXTURE) -> dict[str, dict[str, Any]]:
    """The enabler's two writes from the fixture: ``enable``, the fixture's
    less its statements on the hold table (its hold removal, which deletes
    nothing for a workspace the transaction found unheld), and
    ``disable_unheld`` as it is. Refused if either still writes the hold
    table, so a fixture change that would make the enabler hold or unhold
    anything fails here, not in production."""
    operations = load_switch_operations(path)
    enable = operations["enable"]
    if any(_names_hold_table(m["table"]) for m in enable["mutations"]):
        raise ValueError("switch fixture: enable writes the hold table by a mutation")
    ours = {
        "enable": {"mutations": enable["mutations"],
                   "statements": [s for s in enable["statements"] if not _names_hold_table(s["sql"])]},
        "disable_unheld": operations["disable_unheld"],
    }
    for name, op in ours.items():
        if _writes_hold_table(op):
            raise ValueError(f"switch fixture: the enabler's {name} writes the hold table")
        if [m["table"] for m in op["mutations"]] != ["tr_fastpath_workspace"]:
            raise ValueError(f"switch fixture: the enabler's {name} writes more than the switch row")
    return ours


def switch_statements(op: Mapping[str, Any], params: Mapping[str, str],
                      param_types: Any) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    """An operation's statements as the enabler executes them, each with the
    parameters its SQL names, every one a STRING (a workspace id)."""
    out = []
    for statement in op["statements"]:
        names = sorted(set(_PARAMETER.findall(statement["sql"])))
        out.append((statement["sql"], {name: params[name] for name in names},
                    {name: param_types.STRING for name in names}))
    return out


def _apply(transaction: Any, op: Mapping[str, Any], params: Mapping[str, Any], *, param_types: Any,
           commit_timestamp: Any) -> dict[str, int]:
    """Buffer the operation's mutations and execute its statements, in
    order. ``param_types`` and ``commit_timestamp`` are the Spanner client's
    (the store's ``_param_types`` and ``_spanner.COMMIT_TIMESTAMP``), passed
    in so this module imports no cloud SDK."""
    for mutation in op["mutations"]:
        values = []
        for value in mutation["values"]:
            if value == _COMMIT_TIMESTAMP:
                values.append(commit_timestamp)
            elif isinstance(value, str) and value.startswith("@"):
                values.append(params[value[1:]])
            else:
                values.append(value)
        transaction.insert_or_update(table=mutation["table"], columns=mutation["columns"], values=[values])
    counts = {}
    for (sql, bound, types), statement in zip(switch_statements(op, params, param_types), op["statements"], strict=True):
        counts[statement["counts"]] = transaction.execute_update(sql, params=bound, param_types=types)
    return counts


def enable_switch(transaction: Any, workspace_id: str, operations: Mapping[str, Any], *, param_types: Any,
                  commit_timestamp: Any) -> dict[str, int]:
    """Turn the switch on in the transaction: the row's enabled and its
    commit timestamp, nothing else."""
    return _apply(transaction, operations["enable"], {"workspace": workspace_id}, param_types=param_types,
                  commit_timestamp=commit_timestamp)


def disable_unheld(transaction: Any, workspace_id: str, operations: Mapping[str, Any], *, param_types: Any,
                   commit_timestamp: Any) -> dict[str, int]:
    """The enabler's own disable, the fixture's ``disable_unheld``: the
    switch off and the workspace's open leases revoked, with no hold, so a
    workspace that becomes eligible again joins again."""
    return _apply(transaction, operations["disable_unheld"], {"workspace": workspace_id}, param_types=param_types,
                  commit_timestamp=commit_timestamp)


# -- facts -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CreditRow:
    shard: int
    tier: int
    latched: bool
    paused: bool
    in_debt: bool
    credits: int
    usage: int
    reconciled_through: datetime | None


@dataclass(frozen=True, slots=True)
class Override:
    tier: int
    bypass: bool


@dataclass(frozen=True, slots=True)
class Entity:
    deleted: bool
    federated: bool


@dataclass(frozen=True, slots=True)
class SwitchRow:
    enabled: bool
    changed_at: datetime


@dataclass(frozen=True, slots=True)
class EnablerConfig:
    mode: str = "off"
    max_rank: int = 0
    include: frozenset[str] = frozenset()
    exclude: frozenset[str] = frozenset()
    async_shadow: frozenset[str] = frozenset()
    synthetic: frozenset[str] = frozenset()
    enable_balance: int = 100_000_000
    disable_balance: int = 40_000_000
    trust_max_age_seconds: int = 3_600

    @classmethod
    def from_settings(cls, settings: Any) -> EnablerConfig:
        return cls(
            mode=settings.fastpath_enabler_mode,
            max_rank=int(settings.fastpath_enabler_max_rank),
            include=frozenset(settings.fastpath_enabler_include_ids),
            exclude=frozenset(settings.fastpath_pilot_exclude_ids),
            async_shadow=frozenset(settings.async_settle_shadow_workspace_ids),
            synthetic=frozenset(settings.client_events_synthetic_workspace_ids),
            enable_balance=int(settings.fastpath_enabler_enable_balance_micro),
            disable_balance=int(settings.fastpath_enabler_disable_balance_micro),
            trust_max_age_seconds=int(settings.trust_reconcile_max_age_seconds),
        )


def _row(values: Sequence[Any]) -> CreditRow:
    shard, tier, latched_at, _override_tier, causes, in_debt, credits, usage, _reserved, reconciled = values
    return CreditRow(shard=int(shard), tier=int(tier or 0), latched=latched_at is not None,
                     paused=bool(causes), in_debt=bool(in_debt), credits=int(credits or 0),
                     usage=int(usage or 0), reconciled_through=reconciled)


def unspent(rows: Sequence[CreditRow]) -> int:
    return sum(row.credits - row.usage for row in rows)


def lifetime_usage(rows: Sequence[CreditRow]) -> int:
    return sum(row.usage for row in rows)


def credit_causes(rows: Sequence[CreditRow], override: Override | None, *, enabled: bool, included: bool,
                  config: EnablerConfig) -> list[str]:
    """The causes the credit rows and the override give, in precedence
    order: the grant's own refusals read as Go's rowsRefuse reads them
    (debt mark, pause, the least tier with a latched row at zero), the
    rows' completeness (shards 0..n-1, as Go's readCreditRows requires),
    the balance (the enable balance to join, the disable balance to stay),
    and a tier 3 resting on an identity bypass, unless included."""
    causes = []
    if any(row.latched for row in rows):
        causes.append("latched")
    if any(row.paused for row in rows):
        causes.append("paused")
    if any(row.in_debt for row in rows):
        causes.append("in_debt")
    if not rows or [row.shard for row in rows] != list(range(len(rows))):
        causes.append("credit_rows")
    elif min(0 if row.latched else row.tier for row in rows) < REQUIRED_TIER:
        causes.append("tier")
    balance = unspent(rows)
    if (balance < config.disable_balance) if enabled else (balance < config.enable_balance):
        causes.append("balance")
    if override is not None and override.bypass and not included:
        causes.append("bypass")
    return causes


def watermark_stale(rows: Sequence[CreditRow], override: Override | None, *, now: datetime,
                    config: EnablerConfig) -> bool:
    """Whether a new enable of the workspace is frozen (section 4(a)): its
    oldest trust_reconciled_through across its rows absent or older than
    the reconciler's maximum age. A workspace at tier 3 by an operator's
    override has nothing to reconcile and counts as fresh."""
    if override is not None and override.tier >= REQUIRED_TIER:
        return False
    marks = [row.reconciled_through for row in rows]
    if not marks or any(mark is None for mark in marks):
        return True
    oldest = min(mark for mark in marks if mark is not None)
    return (now - oldest).total_seconds() > config.trust_max_age_seconds


def _stamp(value: datetime) -> str:
    """A timestamp's exact text, nanoseconds kept where Spanner gave them,
    for comparing a changed_at with a kill's commit timestamp."""
    rfc3339 = getattr(value, "rfc3339", None)
    if callable(rfc3339):
        return str(rfc3339())
    return value.astimezone(UTC).isoformat()


# -- the snapshot ----------------------------------------------------------------


@dataclass
class Snapshot:
    """One strong read-only snapshot's rows (section 5.1)."""

    credit: dict[str, list[CreditRow]] = field(default_factory=dict)
    overrides: dict[str, Override] = field(default_factory=dict)
    entities: dict[str, Entity] = field(default_factory=dict)
    switch: dict[str, SwitchRow] = field(default_factory=dict)
    holds: dict[str, tuple[str, datetime]] = field(default_factory=dict)
    kills: list[datetime] = field(default_factory=list)
    # workspace -> the probe reason its key gives ("monitor_key" or "probe")
    probes: dict[str, str] = field(default_factory=dict)


def _entity(body: Any) -> Entity:
    try:
        data = json.loads(body)
    except (TypeError, ValueError):
        return Entity(deleted=True, federated=False)
    if not isinstance(data, dict):
        return Entity(deleted=True, federated=False)
    return Entity(deleted=bool(data.get("deleted")), federated=bool(data.get("federated_home")))


def read_snapshot(database: Any, param_types: Any, *, probe_hashes: Mapping[str, str] | None = None,
                  chunk_size: int = CHUNK) -> Snapshot:
    """Every input from one strong read-only snapshot, read in chunks at
    LOW priority. Any failed read raises SnapshotError, so a partial
    snapshot never decides. The workspace entities, the overrides and the
    probes' keys are read by complete key only, never a body scan."""
    ids_type = param_types.Array(param_types.STRING)
    entity_types = {"kind": param_types.STRING, "ids": ids_type}
    out = Snapshot()
    try:
        with database.snapshot(multi_use=True) as snap:
            for workspace, enabled, changed_at in snap.execute_sql(ENABLER_SWITCH_SQL, request_options=LOW):
                out.switch[str(workspace)] = SwitchRow(enabled=bool(enabled), changed_at=changed_at)
            for workspace, reason, held_at in snap.execute_sql(ENABLER_HOLDS_SQL, request_options=LOW):
                out.holds[str(workspace)] = (str(reason), held_at)
            out.kills = [row[0] for row in snap.execute_sql(ENABLER_KILLS_SQL, request_options=LOW)]
            credit_ids = {str(row[0]) for row in snap.execute_sql(ENABLER_WORKSPACE_IDS_SQL, request_options=LOW)}
            workspace_ids = sorted(credit_ids | set(out.switch))
            for start in range(0, len(workspace_ids), chunk_size):
                ids = workspace_ids[start:start + chunk_size]
                rows: dict[str, list[CreditRow]] = {}
                for values in snap.execute_sql(ENABLER_BALANCE_SQL, params={"ids": ids},
                                               param_types={"ids": ids_type}, request_options=LOW):
                    rows.setdefault(str(values[0]), []).append(_row(values[1:]))
                for workspace, credit_rows in rows.items():
                    out.credit[workspace] = sorted(credit_rows, key=lambda row: row.shard)
                for workspace, tier, bypass in snap.execute_sql(ENABLER_OVERRIDES_SQL, params={"ids": ids},
                                                                param_types={"ids": ids_type},
                                                                request_options=LOW):
                    out.overrides[str(workspace)] = Override(tier=int(tier), bypass=bool(bypass))
                for entity_id, body in _entities(snap, "workspace", ids, entity_types).items():
                    out.entities[entity_id] = _entity(body)
            if probe_hashes:
                out.probes = _probe_workspaces(snap, entity_types, probe_hashes)
    except Exception as exc:
        raise SnapshotError(type(exc).__name__) from exc
    return out


def _entities(snap: Any, kind: str, ids: Sequence[str], types: dict[str, Any]) -> dict[str, Any]:
    """Entities of one kind by complete key: (kind, id) for each id."""
    if not ids:
        return {}
    return {str(entity_id): body for entity_id, body in snap.execute_sql(
        ENABLER_ENTITIES_SQL, params={"kind": kind, "ids": list(ids)}, param_types=types, request_options=LOW)}


def _bodies(snap: Any, kind: str, ids: Sequence[str], types: dict[str, Any]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for entity_id, body in _entities(snap, kind, ids, types).items():
        try:
            data = json.loads(body)
        except (TypeError, ValueError):
            continue
        if isinstance(data, dict):
            found[entity_id] = data
    return found


def _probe_workspaces(snap: Any, types: dict[str, Any], probe_hashes: Mapping[str, str]) -> dict[str, str]:
    """The probes' workspaces, by their keys' lookup hashes: the lookup
    entity names the key, the key entity its workspace."""
    lookups = _bodies(snap, "api_key_lookup", sorted(probe_hashes), types)
    key_of = {h: str(body.get("key_id") or "") for h, body in lookups.items()}
    keys = _bodies(snap, "api_key", sorted({k for k in key_of.values() if k}), types)
    probes: dict[str, str] = {}
    for lookup_hash, key_id in key_of.items():
        workspace = str(keys.get(key_id, {}).get("workspace_id") or "")
        if workspace:
            reason = probe_hashes[lookup_hash]
            # The monitor's reason wins: it is the edge's monitor_key too.
            if probes.get(workspace) != "monitor_key":
                probes[workspace] = reason
    return probes


# -- the plan ----------------------------------------------------------------------


@dataclass
class Plan:
    """What a run would write, and its report's counts (section 5.4)."""

    enables: list[str] = field(default_factory=list)
    disables: list[tuple[str, str]] = field(default_factory=list)  # (workspace, cause)
    report: dict[str, Any] = field(default_factory=dict)
    lists: dict[str, list[str]] = field(default_factory=dict)
    mass_disable: bool = False
    trust_stale: bool = False


def _rank_limit(max_rank: int) -> int:
    """The rank past which an enabled workspace leaves (section 5.2): a
    margin of a tenth, at least five, and none at 0."""
    if max_rank <= 0:
        return 0
    return max_rank + max(5, max_rank // 10)


def plan(snapshot: Snapshot, config: EnablerConfig, *, now: datetime) -> Plan:
    """Decide the run from the snapshot alone (sections 5.2 and 5.3)."""
    universe = sorted(set(snapshot.credit) | set(snapshot.switch))
    enabled_set = {w for w, row in snapshot.switch.items() if row.enabled}
    causes_of: dict[str, list[str]] = {}
    bypass_included = bypass_excluded = 0
    for workspace in universe:
        enabled = workspace in enabled_set
        included = workspace in config.include
        rows = snapshot.credit.get(workspace, [])
        override = snapshot.overrides.get(workspace)
        found = set(credit_causes(rows, override, enabled=enabled, included=included, config=config))
        if override is not None and override.bypass:
            if included:
                bypass_included += 1
            else:
                bypass_excluded += 1
        if workspace in snapshot.holds:
            found.add("held")
        if workspace in config.exclude:
            found.add("excluded")
        if workspace in config.async_shadow:
            found.add("async_shadow")
        entity = snapshot.entities.get(workspace)
        if entity is None or entity.deleted:
            found.add("deleted")
        elif entity.federated:
            found.add("federated")
        probe = snapshot.probes.get(workspace)
        if probe is not None:
            found.add(probe)
        if workspace in config.synthetic:
            found.add("probe")
        causes_of[workspace] = [cause for cause in PRECEDENCE if cause in found]

    # The rank: lifetime usage, largest first, ties by id, among workspaces
    # otherwise eligible (include list too, so max_rank N is the N largest).
    ranked = sorted((w for w in universe if not causes_of[w]),
                    key=lambda w: (-lifetime_usage(snapshot.credit.get(w, [])), w))
    rank = {w: i + 1 for i, w in enumerate(ranked)}
    limit = _rank_limit(config.max_rank)

    excluded: Counter[str] = Counter()
    disables_by: dict[str, list[str]] = {"uncapped": [], "whole": [], "guarded": [], "rank": []}
    would_enable: list[tuple[str, str]] = []  # (workspace, reason)
    eligible: list[str] = []
    for workspace in universe:
        causes = causes_of[workspace]
        enabled = workspace in enabled_set
        if causes:
            excluded[causes[0]] += 1
            if enabled:
                group = ("uncapped" if causes[0] in UNCAPPED else "whole" if causes[0] in WHOLE else "guarded")
                disables_by[group].append(workspace)
            continue
        included = workspace in config.include
        if enabled:
            if included or rank[workspace] <= limit:
                eligible.append(workspace)
            else:
                disables_by["rank"].append(workspace)
        elif included or rank[workspace] <= config.max_rank:
            eligible.append(workspace)
            would_enable.append((workspace, "include" if included else "rank"))

    stale = {w for w in eligible
             if watermark_stale(snapshot.credit.get(w, []), snapshot.overrides.get(w), now=now, config=config)}
    frozen = [w for w, _ in would_enable if w in stale]
    unfrozen = [(w, reason) for w, reason in would_enable if w not in stale]
    # Include list first, then by rank, so a capped run takes the largest.
    unfrozen.sort(key=lambda item: (item[1] != "include", rank[item[0]]))
    enables = [w for w, _ in unfrozen][:MAX_ENABLES]

    cap = max(GUARD_FLOOR, int(GUARD_FRACTION * len(enabled_set)))
    guarded = disables_by["guarded"]
    mass_disable = len(guarded) > cap
    paced = sorted(disables_by["rank"], key=lambda w: -rank[w])
    rank_now, rank_later = paced[:cap], paced[cap:]

    def cause(workspace: str) -> str:
        return causes_of[workspace][0] if causes_of[workspace] else RANK

    disables = [(w, cause(w)) for w in disables_by["uncapped"] + disables_by["whole"]]
    if not mass_disable:
        disables += [(w, cause(w)) for w in guarded]
    disables += [(w, RANK) for w in rank_now]

    trust_stale = bool(eligible) and 2 * len(stale) > len(eligible)
    held_rows = {w for w in snapshot.holds if w[:1] not in ("*", "#")}
    kills = {_stamp(k) for k in snapshot.kills}
    unheld = [(w, row) for w, row in sorted(snapshot.switch.items()) if not row.enabled and w not in held_rows]
    under_kill = sum(1 for _, row in unheld if _stamp(row.changed_at) in kills)
    unheld_would_enable = [f"{w}@{_stamp(row.changed_at)}" for w, row in unheld if w in {x for x, _ in unfrozen}]
    would_disable = Counter(cause(w) for w in disables_by["uncapped"] + disables_by["whole"] + guarded)
    if disables_by["rank"]:
        would_disable[RANK] = len(disables_by["rank"])
    report = {
        "eligible": len(eligible),
        "enabled": len(enabled_set),
        "would_enable": dict(sorted(Counter(reason for _, reason in unfrozen).items())),
        "would_disable": dict(sorted(would_disable.items())),
        "excluded": dict(sorted(excluded.items())),
        "held": {
            "fleet": FLEET_HOLD in snapshot.holds,
            "paused": PAUSE_ROW in snapshot.holds,
            "armed": ARMED_ROW in snapshot.holds,
            "workspaces": len(held_rows),
        },
        "frozen": len(frozen),
        "capped": {
            "enables": max(0, len(unfrozen) - MAX_ENABLES),
            "guarded": len(guarded) if mass_disable else 0,
            "rank": len(rank_later),
            "cap": cap,
        },
        "bypass": {"included": bypass_included, "excluded": bypass_excluded},
        "enabled_outside_include": len(enabled_set - set(config.include)),
        "unheld_disabled": {"total": len(unheld), "under_kill": under_kill, "other": len(unheld) - under_kill},
        "stale": len(stale),
        "max_rank": config.max_rank,
    }
    lists = {
        "enable": enables,
        "disable": [f"{w}:{c}" for w, c in disables],
        "frozen": sorted(frozen),
        "unheld_would_enable": unheld_would_enable,
    }
    return Plan(enables=enables, disables=disables, report=report, lists=lists,
                mass_disable=mass_disable, trust_stale=trust_stale)


# -- the writes --------------------------------------------------------------------

# A write's outcomes. The first three stop the run's writes: every later
# transaction would read the same rows.
STOPS = ("fleet", "paused", "unarmed")


@dataclass
class Outcome:
    result: str
    committed: datetime | None = None
    revoked: int = 0


def _guard(transaction: Any, param_types: Any, workspace_id: str) -> set[str]:
    rows = list(transaction.execute_sql(
        ENABLER_GUARD_SQL, params={"rows": [*GUARD_ROWS, workspace_id]},
        param_types={"rows": param_types.Array(param_types.STRING)}))
    return {str(row[0]) for row in rows}


def _guard_stop(present: set[str]) -> str | None:
    if FLEET_HOLD in present:
        return "fleet"
    if PAUSE_ROW in present:
        return "paused"
    if ARMED_ROW not in present:
        return "unarmed"
    return None


def _switch_enabled(transaction: Any, param_types: Any, workspace_id: str) -> bool:
    rows = list(transaction.execute_sql(ENABLER_SWITCH_ROW_SQL, params={"workspace": workspace_id},
                                        param_types={"workspace": param_types.STRING}))
    return bool(rows and rows[0][0])


def _run_write(database: Any, func: Callable[[Any], Outcome], tag: str) -> Outcome:
    seen: list[Any] = []

    def wrapped(transaction: Any) -> Outcome:
        seen.append(transaction)
        return func(transaction)

    outcome = run_in_transaction_with_retry(database, wrapped, transaction_tag=tag)
    if outcome.result in ("enabled", "disabled") and seen:
        outcome.committed = getattr(seen[-1], "committed", None)
    return outcome


def write_enable(database: Any, param_types: Any, workspace_id: str, *, included: bool, config: EnablerConfig,
                 now: datetime, operations: Mapping[str, Any], commit_timestamp: Any) -> Outcome:
    """One enable, its own transaction: the guard rows, the workspace's own
    hold, its switch row, its credit rows and its override, each by complete
    key; the switch written only if none of them stops it (section 5.3).
    Spanner orders it against any disable or disable-all that holds, so it
    is before the kill, which then turns it off, or after, and sees it."""
    def tx(transaction: Any) -> Outcome:
        present = _guard(transaction, param_types, workspace_id)
        stop = _guard_stop(present)
        if stop is not None:
            return Outcome(stop)
        if workspace_id in present:
            return Outcome("held")
        if _switch_enabled(transaction, param_types, workspace_id):
            return Outcome("already")
        rows = sorted((_row(values) for values in transaction.execute_sql(
            ENABLER_CREDIT_ROWS_SQL, params={"workspace": workspace_id},
            param_types={"workspace": param_types.STRING})), key=lambda row: row.shard)
        found = list(transaction.execute_sql(ENABLER_OVERRIDE_SQL, params={"workspace": workspace_id},
                                             param_types={"workspace": param_types.STRING}))
        override = Override(tier=int(found[0][0]), bypass=bool(found[0][1])) if found else None
        causes = credit_causes(rows, override, enabled=False, included=included, config=config)
        if causes:
            return Outcome("ineligible:" + causes[0])
        if watermark_stale(rows, override, now=now, config=config):
            return Outcome("frozen")
        enable_switch(transaction, workspace_id, operations, param_types=param_types,
                      commit_timestamp=commit_timestamp)
        return Outcome("enabled")

    return _run_write(database, tx, "tr_fastpath_enabler_enable")


def write_disable(database: Any, param_types: Any, workspace_id: str, *,
                  operations: Mapping[str, Any], commit_timestamp: Any) -> Outcome:
    """One disable, its own transaction: the guard rows and the switch row,
    then the fixture's disable_unheld. No hold is written."""
    def tx(transaction: Any) -> Outcome:
        stop = _guard_stop(_guard(transaction, param_types, workspace_id))
        if stop is not None:
            return Outcome(stop)
        if not _switch_enabled(transaction, param_types, workspace_id):
            return Outcome("already")
        counts = disable_unheld(transaction, workspace_id, operations, param_types=param_types,
                                commit_timestamp=commit_timestamp)
        return Outcome("disabled", revoked=int(counts.get("revoked_leases", 0)))

    return _run_write(database, tx, "tr_fastpath_enabler_disable")


# -- a run -------------------------------------------------------------------------


@dataclass
class RunResult:
    report: dict[str, Any]
    plan: Plan | None = None


def run(database: Any, param_types: Any, config: EnablerConfig, *, probe_hashes: Mapping[str, str] | None = None,
        now: datetime | None = None, operations: Mapping[str, Any] | None = None,
        reader: Callable[..., Snapshot] = read_snapshot, commit_timestamp: Any = None) -> RunResult:
    """One run: off returns at once; dry-run reads and reports; apply also
    writes, disables first (the safe direction), stopping at the first
    transaction that finds the fleet hold, the pause or no arm. Apply needs
    the Spanner client's commit timestamp sentinel."""
    if config.mode not in MODES:
        raise ValueError(f"fast path enabler mode {config.mode!r}")
    if config.mode == "apply" and commit_timestamp is None:
        raise ValueError("fast path enabler apply mode needs the commit timestamp sentinel")
    if config.mode == "off":
        dormant: dict[str, Any] = {"mode": "off"}
        logger.info("fastpath_enabler.report %s", json.dumps(dormant, sort_keys=True))
        return RunResult(report=dormant)
    started = time.monotonic()
    ops = operations if operations is not None else enabler_operations()
    snapshot = reader(database, param_types, probe_hashes=probe_hashes)
    moment = now or datetime.now(UTC)
    decided = plan(snapshot, config, now=moment)
    report: dict[str, Any] = {"mode": config.mode, **decided.report}
    if decided.mass_disable:
        logger.error("fastpath_enabler_mass_disable guarded=%d cap=%d enabled=%d",
                     decided.report["capped"]["guarded"], decided.report["capped"]["cap"],
                     decided.report["enabled"])
    if decided.trust_stale:
        logger.error("fastpath_enabler_trust_stale stale=%d eligible=%d",
                     decided.report["stale"], decided.report["eligible"])
    for name, items in decided.lists.items():
        logger.info("fastpath_enabler.list %s=%s", name, ",".join(items))
    if config.mode == "apply":
        written: Counter[str] = Counter()
        commits: list[str] = []
        revoked = 0
        stopped = None
        try:
            for workspace, _cause in decided.disables:
                outcome = write_disable(database, param_types, workspace, operations=ops,
                                        commit_timestamp=commit_timestamp)
                written[outcome.result] += 1
                revoked += outcome.revoked
                if outcome.committed is not None:
                    commits.append(_stamp(outcome.committed))
                if outcome.result in STOPS:
                    stopped = outcome.result
                    break
            if stopped is None:
                for workspace in decided.enables:
                    outcome = write_enable(database, param_types, workspace, included=workspace in config.include,
                                           config=config, now=moment, operations=ops,
                                           commit_timestamp=commit_timestamp)
                    written[outcome.result] += 1
                    if outcome.committed is not None:
                        commits.append(_stamp(outcome.committed))
                    if outcome.result in STOPS:
                        stopped = outcome.result
                        break
        except Exception as exc:
            # A write that raises past its retries ends the run; the writes
            # before it are committed, so report them before failing the job.
            report["failed"] = type(exc).__name__
            raise
        finally:
            report["writes"] = dict(sorted(written.items()))
            report["revoked_leases"] = revoked
            report["stopped"] = stopped
            report["commit_times"] = commits
            if "failed" in report:
                report["elapsed_seconds"] = round(time.monotonic() - started, 3)
                logger.error("fastpath_enabler.write_failed %s", json.dumps(report, sort_keys=True))
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    logger.info("fastpath_enabler.report %s", json.dumps(report, sort_keys=True))
    return RunResult(report=report, plan=decided)


def probe_hashes_from_environment(environ: Mapping[str, str] = os.environ) -> dict[str, str]:
    """The probes' keys' lookup hashes, from the job's environment; each key
    is hashed and dropped, never logged."""
    hashes: dict[str, str] = {}
    for name, reason in PROBE_KEY_ENVIRONMENT.items():
        key = environ.get(name, "").strip()
        if key:
            lookup = lookup_hash_api_key(key)
            if hashes.get(lookup) != "monitor_key":
                hashes[lookup] = reason
    return hashes


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    from trusted_router.config import get_settings

    settings = get_settings()
    config = EnablerConfig.from_settings(settings)
    if config.mode == "off":
        # Dormant: no store, no Spanner client, no read.
        run(None, None, config)
        return 0
    if settings.storage_backend != "spanner-clickhouse":
        logger.error("fastpath_enabler.refused storage_backend=%s: GCP's typed Spanner store only",
                     settings.storage_backend)
        return 1
    from trusted_router.sentry_config import init_sentry
    from trusted_router.storage import create_store

    init_sentry(settings)
    store = create_store(settings)
    target: Any = getattr(store, "_backend", store)
    try:
        run(target._database, target._param_types, config, probe_hashes=probe_hashes_from_environment(),
            commit_timestamp=target._spanner.COMMIT_TIMESTAMP)
    except SnapshotError as exc:
        logger.error("fastpath_enabler.snapshot_failed error=%s; nothing written", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
