"""When is a cloud *done*? The stages, and the table that cannot be skipped.

On 2026-08-17 the AWS-EU cloud had been serving production traffic since
2026-08-02 with no analytics pipeline at all: the drain that moves rows from
``tr_operational_analytics_outbox`` into ClickHouse had never been installed,
470,897 rows had piled up in DSQL, and ``activity_generations`` on the Paris
node was empty. Nothing reported it for fifteen days, because the only backlog
alarm is emitted BY the drain that was missing.

:mod:`clickhouse.check_aws_analytics_freshness` and the fleet freshness check
are the *detectors*: they tell you afterwards, on someone else's schedule. This
module is about the ROLLOUT. The proximate cause was not a missing monitor — it
was a bring-up script that ended by PRINTING next steps and exiting 0
(``scripts/deploy/aws_eu_clickhouse.sh``: ``echo "Next: apply clickhouse/*.sql,
then redeploy tr-eu with ..."``). A human ran it, read the echoes, and stopped.
"The script finished" and "the cloud works" were different things, and nothing
anywhere treated "cloud exists but has no drain" as an incomplete rollout.

So this module answers one question, executably, for one cloud:

    a. is the cloud in the fleet freshness registry — i.e. does ANYONE check it?
    b. does its public ``/status.json`` carry the ``analytics`` section?
    c. is ``analytics.available`` true (or is the absence explicitly recorded)?
    d. is ``drain_lag_seconds`` under the bound the drain itself alarms on?
    e. does the control plane that feeds the outbox have the outbox ENABLED?

The order is deliberate: each stage is meaningless until the one before it
holds, and each returns a message naming the fix rather than a boolean. The
shell entry point is ``scripts/deploy/verify_cloud_complete.sh``, which does the
HTTPS fetch and calls back into the subcommands here; every judgement lives in
Python so it can be unit-tested without a network.

Two rules this module exists to enforce, in code rather than in prose:

* **The cloud list is never re-typed.** :func:`declared_clouds` reads the
  deployment-declaring tables (:func:`byok_v1_attestations.clouds_that_must_attest`
  and :data:`regions.MULTICLOUD_REGION_GEO`), so a fourth cloud added to either
  one shows up here whether or not anybody remembered this file.
  :func:`registry_gaps` then FAILS for a declared cloud that has no entry —
  which is the CI binding in ``tests/test_cloud_rollout_completeness.py``.

* **Absence must be signed for.** A cloud whose analytics genuinely cannot be
  checked yet is allowed through only by an entry in :data:`ROLLOUT_REGISTRY`
  carrying ``analytics_absent_reason``, which is a code change and therefore a
  review. Silence is not an exemption; today no cloud has one.

Nothing here does IO or touches a cloud API. The only file it reads is a deploy
script already in this repository, and it reads it as text.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trusted_router import regions
from trusted_router.byok_v1_attestations import clouds_that_must_attest
from trusted_router.config import Settings
from trusted_router.operational_analytics_freshness import (
    ANALYTICS_STATUS_KEY,
    AVAILABLE_FIELD,
    DEFAULT_MAX_DRAIN_LAG_SECONDS,
    DRAIN_LAG_FIELD,
    GENERATED_AT_FIELD,
    OLDEST_ENQUEUED_AT_FIELD,
    OUTBOX_DEPTH_FIELD,
    REASON_FIELD,
)

#: Repository root, so the outbox stage can read the deploy script that is the
#: source of truth for a cloud's control-plane environment.
REPO_ROOT = Path(__file__).resolve().parents[2]

#: The environment variable that decides whether settle enqueues operational
#: rows at all. Config-level truth: with this false the outbox stays empty, the
#: published ``drain_lag_seconds`` is 0.0 forever, and every stage above reads
#: green while ZERO rows move. Stages (b)-(d) cannot tell "fully drained" from
#: "never enqueued" — see the caveat in :func:`drain_lag_blockers`.
OUTBOX_ENABLED_ENV = "TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED"

#: Values of :data:`OUTBOX_ENABLED_ENV` that mean "off". Anything else — a
#: literal ``true`` or a shell expansion the script computes — is accepted at
#: this stage, because the runtime truth is what stages (b)-(d) measure.
_OUTBOX_DISABLED_LITERALS = frozenset({"", "false", "0", "no", "off", "none"})

#: How a deploy script DECLARES the variable, as opposed to merely mentioning
#: it. Precision on purpose, and learned the hard way: a first version matched
#: the name anywhere in the file, so the paragraph in
#: ``azure_control_plane.sh`` that TELLS an operator to set
#: ``TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED=true`` counted as having set it —
#: the check read its own instructions back as compliance and passed the one
#: cloud it was written to fail.
#:
#: All three control-plane scripts declare env in one of these shapes: an App
#: Runner JSON map, a quoted ``KEY=VALUE`` array element, or an export. Prose
#: is none of them. A future script that assigns the variable some other way
#: fails this stage rather than passing it silently, which is the safe
#: direction: the message says exactly which file and which variable.
_OUTBOX_DECLARATION_PATTERNS = (
    rf'"{OUTBOX_ENABLED_ENV}"\s*:\s*"([^"]*)"',
    rf'"{OUTBOX_ENABLED_ENV}=([^"]*)"',
    rf"^[ \t]*export[ \t]+{OUTBOX_ENABLED_ENV}=(\S*)",
)

#: How stale the published section may be before it is a frozen control plane
#: rather than a healthy one. Mirrors the default in
#: :mod:`clickhouse.check_aws_analytics_freshness`.
DEFAULT_MAX_SECTION_AGE_SECONDS = 3_600.0


@dataclass(frozen=True)
class CloudRollout:
    """What this repository knows about finishing ONE cloud's rollout.

    Deliberately small. The status URL is not stored here: it comes from the
    fleet registry (see :func:`freshness_registry`), so this table cannot drift
    into a second, disagreeing list of clouds and their endpoints.
    """

    #: Cloud id as the deployment-declaring tables spell it ("aws", "azure", "gcp").
    cloud: str
    #: Repo-relative deploy script that is the SOURCE OF TRUTH for that cloud's
    #: control-plane environment — the file whose header says so.
    control_plane_script: str
    #: The command an operator runs to install/refresh this cloud's drain.
    #: Printed by the stage that fails, so the message names the fix.
    drain_install_command: str
    #: Set ONLY to record a reviewed, deliberate absence of the analytics
    #: pipeline on this cloud. A non-empty string downgrades stages (c)-(e)
    #: from failures to loud warnings. Empty for every cloud today, on purpose:
    #: the AWS-EU outage was fifteen days of exactly this exemption granted by
    #: nobody, in silence.
    analytics_absent_reason: str | None = None


#: Every cloud that must be finishable. Keys are checked against
#: :func:`declared_clouds` by :func:`registry_gaps`, so adding a cloud to
#: `STANDALONE_CLOUDS` or `MULTICLOUD_REGION_GEO` without adding it here is a
#: CI failure rather than a cloud nobody ever verifies.
ROLLOUT_REGISTRY: dict[str, CloudRollout] = {
    "gcp": CloudRollout(
        cloud="gcp",
        control_plane_script="scripts/deploy/rollout.sh",
        drain_install_command=(
            "bash scripts/deploy/clickhouse_operational_analytics.sh  "
            "# GCP drain: systemd units on the ClickHouse cluster"
        ),
    ),
    "aws": CloudRollout(
        cloud="aws",
        control_plane_script="scripts/deploy/aws_eu_control_plane.sh",
        drain_install_command="bash scripts/deploy/aws_eu_clickhouse_drain_install.sh",
    ),
    "azure": CloudRollout(
        cloud="azure",
        control_plane_script="scripts/deploy/azure_control_plane.sh",
        # No such script exists yet: Azure has no operational-analytics outbox
        # at all, which is the point. The stage that fails says so and names
        # what has to be built, rather than pointing at a file that is not there.
        drain_install_command=(
            "write it — Azure has no operational-analytics drain yet; the outbox "
            "must be enabled in scripts/deploy/azure_control_plane.sh and a drain "
            "installed against its ClickHouse, mirroring "
            "scripts/deploy/aws_eu_clickhouse_drain_install.sh"
        ),
    ),
}


# ---------------------------------------------------------------------------
# (a) The registry. Who is checked, and by whom.
# ---------------------------------------------------------------------------


def declared_clouds() -> tuple[str, ...]:
    """Every cloud this repository DECLARES it deploys, from the real tables.

    The union of the two places a deployment is announced today:

    * :func:`byok_v1_attestations.clouds_that_must_attest` — the standalone
      deployments plus the enclave failover topology;
    * :data:`regions.MULTICLOUD_REGION_GEO` — the regions the marketing map and
      ``/v1/regions`` advertise, each tagged with its cloud.

    Neither is re-typed here. A cloud added to either table is a cloud this
    module immediately expects to be finishable, which is the whole mechanism:
    the list that grows when someone adds a cloud is the same list the
    completeness check reads.
    """
    found: list[str] = []
    for cloud in clouds_that_must_attest():
        if cloud not in found:
            found.append(cloud)
    for geo in regions.MULTICLOUD_REGION_GEO.values():
        if geo.cloud not in found:
            found.append(geo.cloud)
    return tuple(found)


def _fleet_registry_from_pr_module() -> dict[str, str] | None:
    """The fleet freshness registry from PR #643, if that module has landed.

    ``trusted_router.operational_analytics_fleet`` does not exist on ``main`` as
    of this commit — it arrives with the branch that publishes each cloud's
    ``drain_lag_seconds`` in ``/status.json`` and binds a fleet registry so a
    cloud with no freshness endpoint fails CI. This module must not edit that
    file and must not duplicate it, so it imports defensively: if the module is
    importable and exposes a ``cloud -> status/base URL`` mapping under any of
    the obvious names, that is the registry. Otherwise we fall back to the
    registry that DOES exist on main, ``Settings.synthetic_fleet_peers``.

    Returning ``None`` (rather than raising) is deliberate: the fallback is a
    real registry with the same three clouds in it, so a laptop running this
    before #643 merges gets the same verdicts.
    """
    try:  # pragma: no cover - exercised only once #643 lands
        import importlib

        module = importlib.import_module("trusted_router.operational_analytics_fleet")
    except Exception:
        return None
    for name in ("freshness_registry", "fleet_registry", "status_urls", "registry", "CLOUDS"):
        candidate = getattr(module, name, None)
        if callable(candidate):
            try:
                candidate = candidate()
            except Exception:  # noqa: S112 - a wrong-shaped symbol is not a signal
                candidate = None
        if isinstance(candidate, dict) and candidate:
            urls = {
                str(cloud): _status_url(str(getattr(value, "status_url", value) or ""))
                for cloud, value in candidate.items()
            }
            if all(url.startswith("https://") for url in urls.values()):
                return urls
    return None


def _status_url(base_or_url: str) -> str:
    url = base_or_url.rstrip("/")
    return url if url.endswith("/status.json") else f"{url}/status.json"


def _fleet_peers_setting() -> str:
    """``synthetic_fleet_peers`` from live settings, else its config-as-code default.

    Constructing ``Settings`` can fail on a machine with a partially-populated
    key file, and this verifier has to run from a laptop. The class default is
    the config-as-code source of truth every cloud rolls out with, so falling
    back to it changes nothing about which clouds are checked.
    """
    try:
        return Settings().synthetic_fleet_peers or ""
    except Exception:
        default = Settings.model_fields["synthetic_fleet_peers"].default
        return default if isinstance(default, str) else ""


def freshness_registry() -> dict[str, str]:
    """Cloud -> public ``/status.json`` URL, from the fleet registry.

    Prefers PR #643's module when present; otherwise reads the fleet peer list
    every deployment already watches (``gcp=...,aws=...,azure=...``). Either
    way the clouds come from a registry in ``src/``, never from a list retyped
    in this file or in the shell script.
    """
    from_pr = _fleet_registry_from_pr_module()
    if from_pr:
        return from_pr
    peers: dict[str, str] = {}
    for entry in _fleet_peers_setting().split(","):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        name, _, base = entry.partition("=")
        name = name.strip()
        base = base.strip()
        if name and base.startswith("https://"):
            peers[name] = _status_url(base)
    return peers


def registry_gaps() -> list[str]:
    """Declared clouds that no completeness check can reach. Empty means bound.

    This is the function CI asserts on. Two ways to be missing, both fatal:
    nobody publishes a freshness endpoint for the cloud, or this module has no
    rollout entry for it and therefore cannot say what "done" would mean.
    """
    gaps: list[str] = []
    registry = freshness_registry()
    for cloud in declared_clouds():
        if cloud not in registry:
            gaps.append(
                f"{cloud}: declared as a deployment (byok_v1_attestations.STANDALONE_CLOUDS "
                "or regions.MULTICLOUD_REGION_GEO) but absent from the fleet freshness "
                "registry, so no one ever reads its analytics freshness. Fix: add "
                f"'{cloud}=https://<its public base url>' to Settings.synthetic_fleet_peers "
                "in src/trusted_router/config.py (or to the fleet registry module)."
            )
        if cloud not in ROLLOUT_REGISTRY:
            gaps.append(
                f"{cloud}: declared as a deployment but absent from ROLLOUT_REGISTRY in "
                "src/trusted_router/cloud_rollout_completeness.py, so "
                "scripts/deploy/verify_cloud_complete.sh cannot check it and the cloud "
                "can be called done with no analytics pipeline at all. Fix: add a "
                "CloudRollout entry naming its control-plane deploy script and its drain "
                "install command."
            )
    return gaps


def registry_blockers(cloud: str) -> list[str]:
    """Stage (a) for ONE cloud."""
    blockers: list[str] = []
    if cloud not in ROLLOUT_REGISTRY:
        known = ", ".join(sorted(ROLLOUT_REGISTRY)) or "(none)"
        blockers.append(
            f"{cloud}: no ROLLOUT_REGISTRY entry in "
            "src/trusted_router/cloud_rollout_completeness.py (known: "
            f"{known}). Fix: add a CloudRollout for it before calling the cloud done."
        )
    registry = freshness_registry()
    if cloud not in registry:
        blockers.append(
            f"{cloud}: not in the fleet freshness registry, so nothing on any schedule "
            "reads its drain lag. Fix: add "
            f"'{cloud}=https://<its public base url>' to Settings.synthetic_fleet_peers "
            "in src/trusted_router/config.py."
        )
    return blockers


def status_url_for(cloud: str) -> str:
    """The public status URL for a cloud. Raises if stage (a) does not hold."""
    blockers = registry_blockers(cloud)
    if blockers:
        raise KeyError("; ".join(blockers))
    return freshness_registry()[cloud]


def exemption(cloud: str) -> str | None:
    """The recorded reason this cloud is allowed to have no analytics, if any."""
    entry = ROLLOUT_REGISTRY.get(cloud)
    reason = entry.analytics_absent_reason if entry else None
    return reason or None


def apply_exemption(cloud: str, blockers: list[str]) -> tuple[list[str], str | None]:
    """Downgrade analytics blockers to a warning when the absence is signed for.

    The single escape hatch, and it is narrow by construction: only a
    ``analytics_absent_reason`` written into :data:`ROLLOUT_REGISTRY` — a code
    change, therefore a review — can turn stages (c)-(e) from failures into a
    printed warning, and the warning still carries the original blocker so the
    exemption cannot hide what it is exempting. Stages (a) and (b) are never
    exempt: a cloud nobody checks and a status page that answers nothing are
    failures no reason makes acceptable.
    """
    reason = exemption(cloud)
    if not blockers or reason is None:
        return blockers, None
    detail = " | ".join(blockers)
    return [], (
        f"ACCEPTED-ABSENT ({cloud}): {reason} — suppressing: {detail}. Remove "
        "analytics_absent_reason from ROLLOUT_REGISTRY when this is fixed."
    )


# ---------------------------------------------------------------------------
# (b)-(d) What the cloud publishes about itself.
# ---------------------------------------------------------------------------


def unwrap_status_payload(payload: Any) -> dict[str, Any]:
    """``/status.json`` serves ``{"data": {...}}``; accept either shape."""
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        payload = payload["data"]
    if not isinstance(payload, dict):
        raise ValueError("status.json is not a JSON object")
    return payload


def section_blockers(cloud: str, payload: dict[str, Any]) -> list[str]:
    """Stage (b): the section exists at all."""
    section = payload.get(ANALYTICS_STATUS_KEY)
    if isinstance(section, dict):
        return []
    return [
        f"{cloud}: /status.json publishes no '{ANALYTICS_STATUS_KEY}' section, so this "
        "cloud's drain lag is unobservable from outside. Fix: deploy a control plane "
        "built from a commit whose status snapshot calls "
        "trusted_router.operational_analytics_freshness.analytics_status_section() — "
        "an older image serves a status page that simply omits the question."
    ]


def available_blockers(cloud: str, payload: dict[str, Any]) -> list[str]:
    """Stage (c): the section says the outbox could actually be read."""
    section = payload.get(ANALYTICS_STATUS_KEY)
    if not isinstance(section, dict):
        return section_blockers(cloud, payload)
    if section.get(AVAILABLE_FIELD):
        return []
    reason = section.get(REASON_FIELD)
    entry = ROLLOUT_REGISTRY.get(cloud)
    install = entry.drain_install_command if entry else "install this cloud's drain"
    return [
        f"{cloud}: {ANALYTICS_STATUS_KEY}.{AVAILABLE_FIELD} is false "
        f"({REASON_FIELD}={reason!r}) — the control plane could not read its own "
        "operational-analytics outbox, which is not the same as an empty one. Fix: "
        f"{install}. To accept the absence instead, record it as "
        "analytics_absent_reason on the cloud's CloudRollout in "
        "src/trusted_router/cloud_rollout_completeness.py; that is a reviewed code "
        "change on purpose."
    ]


def drain_lag_blockers(
    cloud: str,
    payload: dict[str, Any],
    *,
    now: dt.datetime,
    max_drain_lag_seconds: float = DEFAULT_MAX_DRAIN_LAG_SECONDS,
    max_section_age_seconds: float = DEFAULT_MAX_SECTION_AGE_SECONDS,
) -> list[str]:
    """Stage (d): rows are leaving the outbox, and the number is not stale.

    Both halves matter. ``drain_lag_seconds`` is the age of the oldest
    undelivered row, and rows are deleted only after ClickHouse accepts them, so
    a lag under the bound is an end-to-end statement about the pipeline. But a
    control plane that froze would serve a healthy lag forever, so the section's
    own ``generated_at`` is checked too.

    Known limitation, stated here because it is the reason stage (e) exists: a
    lag of 0 with an empty outbox is indistinguishable from a cloud that never
    enqueues anything. "Drained" and "disabled" look identical from outside.
    """
    section = payload.get(ANALYTICS_STATUS_KEY)
    if not isinstance(section, dict):
        return section_blockers(cloud, payload)
    entry = ROLLOUT_REGISTRY.get(cloud)
    install = entry.drain_install_command if entry else "install this cloud's drain"

    blockers: list[str] = []
    lag = _as_float(section.get(DRAIN_LAG_FIELD))
    if lag is None:
        blockers.append(
            f"{cloud}: {ANALYTICS_STATUS_KEY}.{DRAIN_LAG_FIELD} is missing or "
            "unparseable, so the pipeline reports nothing measurable. Fix: publish it "
            "from the outbox head (operational_analytics_freshness.analytics_status_section)."
        )
    elif lag > max_drain_lag_seconds:
        blockers.append(
            f"{cloud}: oldest undelivered outbox row is {lag:.0f}s old "
            f"(> {max_drain_lag_seconds:.0f}s) — the drain is stopped or behind. This is "
            "the AWS-EU failure shape exactly: rows enqueued, nothing draining them. "
            f"Fix: {install}"
        )

    generated_at = _parse_time(section.get(GENERATED_AT_FIELD))
    if generated_at is None:
        blockers.append(
            f"{cloud}: {ANALYTICS_STATUS_KEY}.{GENERATED_AT_FIELD} is missing or "
            "unparseable, so a frozen control plane would republish a healthy lag "
            "forever. Fix: publish generated_at with the section."
        )
    else:
        age = (now - generated_at).total_seconds()
        if age > max_section_age_seconds:
            blockers.append(
                f"{cloud}: the {ANALYTICS_STATUS_KEY} section is {age:.0f}s old "
                f"(> {max_section_age_seconds:.0f}s) — the number is stale, so it says "
                "nothing about now. Fix: check the control plane is serving and its "
                "status snapshot is refreshing."
            )
    return blockers


def drain_lag_caveat(payload: dict[str, Any]) -> str | None:
    """The sentence to print alongside a PASSING stage (d), when it applies.

    An empty outbox is the healthiest state there is AND the state a cloud with
    the outbox switched off is permanently in. Saying so out loud is what keeps
    a green run from being read as "rows are moving".
    """
    section = payload.get(ANALYTICS_STATUS_KEY)
    if not isinstance(section, dict):
        return None
    depth = _as_int(section.get(OUTBOX_DEPTH_FIELD))
    oldest = section.get(OLDEST_ENQUEUED_AT_FIELD)
    if oldest or (depth is not None and depth > 0):
        return None
    return (
        "outbox is empty: lag 0 proves nothing is STUCK, not that anything is moving. "
        "Rows observed moving is the bar — see stage (e) and, in-cloud, "
        "SELECT count() FROM activity_generations."
    )


# ---------------------------------------------------------------------------
# (e) The producer side: is anything enqueued in the first place?
# ---------------------------------------------------------------------------


def declared_outbox_value(cloud: str, root: Path | None = None) -> str | None:
    """The value the cloud's control-plane deploy script declares for the outbox.

    ``None`` means the script never DECLARES it — mentioning the name in a
    comment or in operator instructions does not count, see
    :data:`_OUTBOX_DECLARATION_PATTERNS`. These scripts say in their own headers
    that they are THE SOURCE OF TRUTH for the service environment, so the
    absence of the variable is the absence of the setting — which is exactly
    Azure's state: ``azure_control_plane.sh`` sets no outbox variable at all,
    and Azure therefore enqueues nothing to drain.

    Read as text on purpose. This must run from a laptop with no cloud
    credentials, and a check that needs ``az``/``aws`` to run is a check that
    does not run.
    """
    entry = ROLLOUT_REGISTRY.get(cloud)
    if entry is None:
        return None
    path = (root or REPO_ROOT) / entry.control_plane_script
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    for pattern in _OUTBOX_DECLARATION_PATTERNS:
        match = re.search(pattern, text, re.MULTILINE)
        if match is not None:
            return match.group(1).strip().strip('"').strip("'")
    return None


def outbox_enabled_blockers(cloud: str, root: Path | None = None) -> list[str]:
    """Stage (e): the control plane that owns the outbox has it switched on."""
    entry = ROLLOUT_REGISTRY.get(cloud)
    if entry is None:
        return registry_blockers(cloud)
    value = declared_outbox_value(cloud, root=root)
    script = entry.control_plane_script
    if value is None:
        return [
            f"{cloud}: {script} — the source of truth for this cloud's control-plane "
            f"environment — never sets {OUTBOX_ENABLED_ENV}, so settle enqueues NOTHING "
            "and the pipeline is empty by construction. A drain over an empty outbox "
            f"publishes drain_lag_seconds=0.0 and looks perfectly healthy. Fix: set "
            f"{OUTBOX_ENABLED_ENV}=true in {script} alongside the ClickHouse URL/"
            f"password wiring, then {entry.drain_install_command}"
        ]
    if value.casefold() in _OUTBOX_DISABLED_LITERALS:
        return [
            f"{cloud}: {script} sets {OUTBOX_ENABLED_ENV}={value!r} — the outbox is "
            "switched OFF, so no operational row is ever enqueued and every freshness "
            f"signal above is vacuously green. Fix: set {OUTBOX_ENABLED_ENV}=true in "
            f"{script} and redeploy."
        ]
    return []


def outbox_note(cloud: str, root: Path | None = None) -> str | None:
    """Caveat for a stage (e) that passes on a COMPUTED value.

    ``aws_eu_control_plane.sh`` writes ``"${OUTBOX_ENABLED}"``, which it sets to
    ``false`` when no ClickHouse secret exists in the region. Statically that is
    "enabled"; at runtime it may not be. Say so rather than imply more than was
    measured.
    """
    value = declared_outbox_value(cloud, root=root)
    if value is None or not value.startswith("$"):
        return None
    entry = ROLLOUT_REGISTRY[cloud]
    return (
        f"{OUTBOX_ENABLED_ENV} is computed at deploy time ({value}) in "
        f"{entry.control_plane_script}; this stage proves the script CAN enable it, not "
        "that the running service did. The runtime evidence is stages (b)-(d)."
    )


# ---------------------------------------------------------------------------
# Small parsing helpers. Duplicated from the freshness checker on purpose: this
# module must import nothing that does IO, so an operator can run it offline.
# ---------------------------------------------------------------------------


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


# ---------------------------------------------------------------------------
# CLI. One subcommand per stage; the shell script owns the ordering and the
# single HTTPS fetch, this owns every judgement.
# ---------------------------------------------------------------------------


def _load(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        return unwrap_status_payload(json.load(handle))


def _report(blockers: list[str], *, note: str | None = None) -> int:
    for blocker in blockers:
        print(blocker)
    if blockers:
        return 1
    if note:
        print(f"note: {note}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Is this cloud's rollout complete?")
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("registry", "status-url", "outbox"):
        child = sub.add_parser(name)
        child.add_argument("--cloud", required=True)
    for name in ("section", "available"):
        child = sub.add_parser(name)
        child.add_argument("--cloud", required=True)
        child.add_argument("--status-file", required=True)
    lag_parser = sub.add_parser("lag")
    lag_parser.add_argument("--cloud", required=True)
    lag_parser.add_argument("--status-file", required=True)
    lag_parser.add_argument("--max-lag-seconds", type=float, default=DEFAULT_MAX_DRAIN_LAG_SECONDS)
    lag_parser.add_argument(
        "--max-section-age-seconds", type=float, default=DEFAULT_MAX_SECTION_AGE_SECONDS
    )
    sub.add_parser("audit")
    sub.add_parser("clouds")

    args = parser.parse_args(argv)

    if args.command == "audit":
        return _report(registry_gaps())
    if args.command == "clouds":
        for cloud in declared_clouds():
            print(cloud)
        return 0
    if args.command == "registry":
        return _report(registry_blockers(args.cloud))
    if args.command == "status-url":
        blockers = registry_blockers(args.cloud)
        if blockers:
            return _report(blockers)
        print(freshness_registry()[args.cloud])
        return 0
    if args.command == "outbox":
        blockers, waived = apply_exemption(args.cloud, outbox_enabled_blockers(args.cloud))
        return _report(blockers, note=waived or outbox_note(args.cloud))

    payload = _load(args.status_file)
    if args.command == "section":
        # Never exempt: a status page that answers nothing about analytics is a
        # cloud you cannot check at all, whatever the reason for the absence.
        return _report(section_blockers(args.cloud, payload))
    if args.command == "available":
        blockers, waived = apply_exemption(args.cloud, available_blockers(args.cloud, payload))
        return _report(blockers, note=waived)
    blockers, waived = apply_exemption(
        args.cloud,
        drain_lag_blockers(
            args.cloud,
            payload,
            now=dt.datetime.now(dt.UTC),
            max_drain_lag_seconds=args.max_lag_seconds,
            max_section_age_seconds=args.max_section_age_seconds,
        ),
    )
    return _report(blockers, note=waived or drain_lag_caveat(payload))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
