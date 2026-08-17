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

Three rules this module exists to enforce, in code rather than in prose:

* **The cloud list is never re-typed.** :func:`declared_clouds` reads the
  deployment-declaring tables (:func:`byok_v1_attestations.clouds_that_must_attest`,
  :data:`regions.MULTICLOUD_REGION_GEO` and the fleet freshness registry), so a
  fourth cloud added to any one of them shows up here whether or not anybody
  remembered this file. :func:`registry_gaps` then FAILS for a declared cloud
  that has no entry — which is the CI binding in
  ``tests/test_cloud_rollout_completeness.py``.

* **The scripts are never re-typed either.** Which deploy scripts must END in
  ``verify_cloud_complete.sh`` is DATA on the :class:`CloudRollout`
  (``deploy_scripts``), not a list in a test — a hand-written list is a fourth
  copy of the fleet and copies are what drift. :func:`script_binding_gaps`
  fails when a registered cloud names no script, when a named script does not
  invoke the verifier, or when a script invokes it for a cloud that never
  claimed it. A script deliberately NOT bound (GCP's ``rollout.sh``, which runs
  inside ``.github/workflows/deploy.yml``) must say so in
  ``exempt_deploy_scripts`` WITH ITS REASON; silence fails CI.

* **Absence must be signed for.** A cloud whose analytics genuinely cannot be
  checked yet is allowed through only by an entry in :data:`ROLLOUT_REGISTRY`
  carrying ``analytics_absent_reason``, which is a code change and therefore a
  review. Silence is not an exemption; today no cloud has one. The waiver is
  reported by the shell as NOT VERIFIED, never as COMPLETE.

Nothing this module decides comes from the environment. That is not an
accident: the bound in stage (d) and the URL in stage (a) are what an attacker
of the *process* — a tired operator with an ``export`` in their shell profile —
would reach for, and a deploy script inherits every variable its caller had.
The bound is a constant here, the URL comes from the registry, and even
``Settings.synthetic_fleet_peers`` is read from its config-as-code default
rather than from a live ``Settings()`` (which would honour
``TR_SYNTHETIC_FLEET_PEERS`` from the same inherited environment). Overrides
exist, but only as explicit command-line flags on
``scripts/deploy/verify_cloud_complete.sh``, and a run that uses one is a
DIAGNOSTIC run that may not print the COMPLETE banner.

Nothing here does IO or touches a cloud API. The only files it reads are deploy
scripts already in this repository, and it reads them as text.
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
#:
#: Matched against the WHOLE file, heredocs included — AWS declares its map
#: inside ``CONFIG=$(cat <<JSON``, so stripping heredocs here would blind the
#: stage to the one cloud that passes it.
_OUTBOX_DECLARATION_PATTERNS = (
    rf'"{OUTBOX_ENABLED_ENV}"\s*:\s*"([^"]*)"',
    rf'"{OUTBOX_ENABLED_ENV}=([^"]*)"',
    rf"^[ \t]*export[ \t]+{OUTBOX_ENABLED_ENV}=(\S*)",
)

#: The fourth shape, and the reason it needs its own pass: a plain
#: ``TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED=true`` line is EXACTLY what the
#: failure message tells an operator to write, and the patterns above rejected
#: it — a gate that refuses the fix it prescribes teaches people the gate is
#: wrong. It cannot be matched against the whole file, because the same text
#: appears inside the heredoc where ``azure_control_plane.sh`` prints that
#: advice; it is matched against :func:`_executable_text`, which is the script
#: with heredoc bodies and whole-line comments removed. Instructions stay
#: instructions, and an assignment counts.
_OUTBOX_BARE_ASSIGNMENT_PATTERN = rf"^[ \t]*{OUTBOX_ENABLED_ENV}=(\S*)"

#: Repo-relative path of the shell entry point every bound deploy script ends in.
VERIFIER_SCRIPT = "scripts/deploy/verify_cloud_complete.sh"

#: Where deploy scripts live, scanned by :func:`script_binding_gaps` for
#: invocations of the verifier that no registry entry claims.
DEPLOY_SCRIPT_DIR = "scripts/deploy"

#: How many trailing lines count as "ends in". The check is that the verifier is
#: the LAST thing a bring-up script does, not merely that it appears somewhere:
#: a call in the middle followed by twenty more steps is a check of a cloud that
#: does not exist yet.
VERIFIER_TAIL_LINES = 45

#: How stale the published section may be before it is a frozen control plane
#: rather than a healthy one. Mirrors the default in
#: :mod:`clickhouse.check_aws_analytics_freshness`.
DEFAULT_MAX_SECTION_AGE_SECONDS = 3_600.0


@dataclass(frozen=True)
class ScriptExemption:
    """A deploy script that deliberately does NOT end in the verifier.

    Both fields are required, which is the entire design: the failure mode this
    replaces was a cloud being absent from a hand-written list, and absence
    carries no reason. An exemption is a sentence somebody wrote and a reviewer
    read.
    """

    #: Repo-relative path, so the test can assert the file exists.
    script: str
    #: Why binding this one would be wrong. Printed by ``audit``.
    reason: str


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
    #: Every deploy script for this cloud that must END in
    #: ``verify_cloud_complete.sh <cloud>``. THIS is the script -> verifier
    #: binding: :func:`script_binding_gaps` reads it, so adding a cloud whose
    #: bring-up script prints "Next: ..." and exits 0 fails CI with a message
    #: naming the file. Empty is legal only when every one of the cloud's
    #: scripts is in :attr:`exempt_deploy_scripts`.
    deploy_scripts: tuple[str, ...] = ()
    #: Deploy scripts deliberately left unbound, each with its reason. Named in
    #: code so that "this cloud's script does not run the check" is a claim
    #: somebody made and a reviewer saw, rather than a row missing from a list.
    exempt_deploy_scripts: tuple[ScriptExemption, ...] = ()
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
        exempt_deploy_scripts=(
            ScriptExemption(
                script="scripts/deploy/rollout.sh",
                reason=(
                    "GCP has no bring-up script a human runs: rollout.sh is a step of the "
                    "deploy JOB (.github/workflows/deploy.yml), which runs on every merge to "
                    "main. Ending it in this verifier would put a public HTTPS fetch of "
                    "trustedrouter.com/status.json on the deploy path of the cloud that "
                    "SERVES trustedrouter.com — so the deploy that repairs an outage would "
                    "fail because of the outage it repairs, and the primary cloud would have "
                    "the gate exactly when it could not satisfy it. GCP is checked instead "
                    "by running 'bash scripts/deploy/verify_cloud_complete.sh gcp' out of "
                    "band (docs/runbook.md, 'Adding a cloud') and by the scheduled analytics "
                    "freshness workflow, neither of which can deadlock a deploy."
                ),
            ),
        ),
    ),
    "aws": CloudRollout(
        cloud="aws",
        control_plane_script="scripts/deploy/aws_eu_control_plane.sh",
        drain_install_command="bash scripts/deploy/aws_eu_clickhouse_drain_install.sh",
        deploy_scripts=(
            "scripts/deploy/aws_eu_control_plane.sh",
            "scripts/deploy/aws_eu_clickhouse.sh",
            "scripts/deploy/aws_eu_north_clickhouse.sh",
            "scripts/deploy/aws_eu_clickhouse_drain_install.sh",
        ),
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
        deploy_scripts=("scripts/deploy/azure_control_plane.sh",),
    ),
}


# ---------------------------------------------------------------------------
# (a) The registry. Who is checked, and by whom.
# ---------------------------------------------------------------------------


def declared_clouds() -> tuple[str, ...]:
    """Every cloud this repository DECLARES it deploys, from the real tables.

    The union of the three places a deployment announces itself today:

    * :func:`byok_v1_attestations.clouds_that_must_attest` — the standalone
      deployments plus the enclave failover topology;
    * :data:`regions.MULTICLOUD_REGION_GEO` — the regions the marketing map and
      ``/v1/regions`` advertise, each tagged with its cloud;
    * :func:`freshness_registry` — the fleet peers every deployment watches, so
      that wiring a cloud's status URL first (the likelier half-finished order)
      is enough to make it visible here.

    None of them is re-typed here. A cloud added to any table is a cloud this
    module immediately expects to be finishable, which is the whole mechanism:
    the list that grows when someone adds a cloud is the same list the
    completeness check reads.

    The residual gap, stated because the docs must not overstate this: a cloud
    that enters NO table — provisioned by hand, serving traffic, named nowhere
    in ``src/`` — is invisible to every check here, exactly as a cloud that
    nobody declares is invisible to the marketing map and to ``/v1/regions``.
    Three tables is not "any conceivable cloud"; it is every way this repository
    currently learns that a cloud exists.
    """
    found: list[str] = []
    for cloud in clouds_that_must_attest():
        if cloud not in found:
            found.append(cloud)
    for geo in regions.MULTICLOUD_REGION_GEO.values():
        if geo.cloud not in found:
            found.append(geo.cloud)
    for cloud in freshness_registry():
        if cloud not in found:
            found.append(cloud)
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
    """``synthetic_fleet_peers`` as CODE: the class default, not a live ``Settings()``.

    The order here is the point, and it is the opposite of the obvious one.
    ``Settings`` is a ``BaseSettings`` with ``env_prefix="TR_"``, so
    ``Settings().synthetic_fleet_peers`` honours ``TR_SYNTHETIC_FLEET_PEERS``
    from the environment — and this module is called by deploy scripts, which
    inherit whatever the operator's shell exported. Reading live settings first
    would therefore leave the gate with exactly the hole that
    ``TR_STATUS_URL`` used to be: one exported variable and every cloud's
    status page becomes whichever page answers the way you want.

    The class default is the config-as-code source of truth every cloud rolls
    out with, it is a code change to edit, and it lists the same clouds. So the
    gate reads it, and only falls back to a live ``Settings()`` if the default
    were ever emptied — which would itself be a reviewed change.
    """
    default = Settings.model_fields["synthetic_fleet_peers"].default
    if isinstance(default, str) and default.strip():
        return default
    try:
        return Settings().synthetic_fleet_peers or ""
    except Exception:
        return ""


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
                f"{cloud}: declared as a deployment (byok_v1_attestations.STANDALONE_CLOUDS, "
                "regions.MULTICLOUD_REGION_GEO or the fleet peer list) but absent from the "
                "fleet freshness registry, so no one ever reads its analytics freshness. Fix: add "
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


# ---------------------------------------------------------------------------
# The other binding: which SCRIPTS must end in the verifier.
#
# A cloud can be perfectly registered here and still ship a bring-up script
# that prints "Next: ..." and exits 0 — which is not a hypothetical, it is what
# happened. Until this function existed the script -> verifier binding was a
# hand-written list of five (script, cloud) pairs in the test file: a fourth
# copy of the fleet, in the file whose docstring says copies are the enemy.
# Now the pairs are fields on the registry entry, and this is what CI asserts.
# ---------------------------------------------------------------------------


def _invokes_verifier(text: str, cloud: str) -> bool:
    """Does this script text run the verifier FOR THIS CLOUD, at the end?

    Two conditions, because either alone is satisfiable without meaning it:
    the invocation must name this cloud (a script that verifies a different
    cloud proves nothing about its own), and it must be within the last
    :data:`VERIFIER_TAIL_LINES` lines (a check in the middle is a check of a
    cloud that does not exist yet, followed by more steps that can fail).
    """
    pattern = re.compile(rf"verify_cloud_complete\.sh\"?\s+{re.escape(cloud)}\b")
    if not pattern.search(text):
        return False
    tail = "\n".join(text.rstrip().splitlines()[-VERIFIER_TAIL_LINES:])
    return bool(pattern.search(tail))


def _deploy_scripts_invoking_the_verifier(root: Path) -> list[str]:
    """Every script under ``scripts/deploy`` that mentions the verifier at all."""
    directory = root / DEPLOY_SCRIPT_DIR
    if not directory.is_dir():
        return []
    found: list[str] = []
    for path in sorted(directory.glob("*.sh")):
        if path.name == Path(VERIFIER_SCRIPT).name:
            continue
        if "verify_cloud_complete.sh" in path.read_text(encoding="utf-8"):
            found.append(f"{DEPLOY_SCRIPT_DIR}/{path.name}")
    return found


def script_binding_gaps(root: Path | None = None) -> list[str]:
    """Deploy scripts that are not bound to the verifier. Empty means bound.

    Four ways to be wrong, all of them things a new cloud does by accident:

    1. the cloud names no script at all and claims no exemption — the shape the
       old hand-written list allowed silently, because a cloud that is simply
       absent from a list looks the same as a cloud with nothing to bind;
    2. a named script does not exist, or does not end in the verifier;
    3. the cloud's own ``control_plane_script`` — the file that IS the source
       of truth for its environment — is neither bound nor exempt;
    4. some script invokes the verifier for a cloud that never named it, so the
       wiring exists but no registry entry would notice it disappearing.

    Every message names the file to edit, because the fix is a code change and
    a CI failure that does not say where to go teaches nobody.
    """
    root = root or REPO_ROOT
    gaps: list[str] = []
    here = "src/trusted_router/cloud_rollout_completeness.py"
    claimed: dict[str, str] = {}

    for cloud, entry in sorted(ROLLOUT_REGISTRY.items()):
        exempt = {item.script: item.reason for item in entry.exempt_deploy_scripts}
        for script in entry.deploy_scripts:
            claimed[script] = cloud
        for script, reason in exempt.items():
            claimed.setdefault(script, cloud)
            if not reason.strip():
                gaps.append(
                    f"{cloud}: {script} is exempt from ending in {VERIFIER_SCRIPT} with an "
                    f"EMPTY reason. An exemption with no reason is the missing row it "
                    f"replaced. Fix: write why in ScriptExemption.reason in {here}."
                )
            if not (root / script).is_file():
                gaps.append(
                    f"{cloud}: exempt script {script} does not exist. Fix: correct or drop "
                    f"the ScriptExemption in {here}."
                )

        if not entry.deploy_scripts and not exempt:
            gaps.append(
                f"{cloud}: ROLLOUT_REGISTRY names no deploy script for this cloud, so "
                f"nothing binds its bring-up to {VERIFIER_SCRIPT} and it can ship a script "
                'that prints "Next: ..." and exits 0 with CI green — the AWS-EU outage '
                f"exactly. Fix: in {here}, add every deploy script for {cloud} to "
                "deploy_scripts=(...) on its CloudRollout, and end each of those scripts "
                f'with: bash "${{SCRIPT_DIR}}/verify_cloud_complete.sh" {cloud}. If one of '
                "them must NOT be bound, say so in exempt_deploy_scripts=(ScriptExemption("
                "script=..., reason=...),)."
            )

        if entry.control_plane_script not in entry.deploy_scripts and (
            entry.control_plane_script not in exempt
        ):
            gaps.append(
                f"{cloud}: {entry.control_plane_script} is this cloud's control-plane "
                "script — the source of truth for its service environment — but it is "
                "neither in deploy_scripts nor exempt, so deploying this cloud never asks "
                f"whether the cloud works. Fix: add it to deploy_scripts in {here} and end "
                f'it with: bash "${{SCRIPT_DIR}}/verify_cloud_complete.sh" {cloud}'
            )

        for script in entry.deploy_scripts:
            path = root / script
            if not path.is_file():
                gaps.append(
                    f"{cloud}: deploy_scripts names {script}, which does not exist. Fix: "
                    f"correct the path in {here}."
                )
                continue
            if not _invokes_verifier(path.read_text(encoding="utf-8"), cloud):
                gaps.append(
                    f"{cloud}: {script} does not END in "
                    f'`bash "${{SCRIPT_DIR}}/verify_cloud_complete.sh" {cloud}`, so running '
                    "it to completion says nothing about whether the cloud works. Fix: add "
                    f"that invocation as the last step of {script} (within its final "
                    f"{VERIFIER_TAIL_LINES} lines) and let its exit code stand."
                )

    for script in _deploy_scripts_invoking_the_verifier(root):
        if script not in claimed:
            gaps.append(
                f"{script} runs {VERIFIER_SCRIPT} but no CloudRollout claims it, so nothing "
                "would notice if that call were deleted. Fix: add it to deploy_scripts (or "
                f"exempt_deploy_scripts) of the cloud it belongs to in {here}."
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


def _executable_text(text: str) -> str:
    """The script with heredoc BODIES and whole-line comments removed.

    Just enough shell awareness to tell an assignment from a paragraph that
    contains one. ``cat <<'NEXT' ... NEXT`` is how every one of these scripts
    prints operator instructions, and those instructions quote the very
    assignment the operator is being asked to add; a bare-assignment pattern
    over the raw text would read that advice back as compliance, which is the
    bug :data:`_OUTBOX_DECLARATION_PATTERNS` documents having already made once.

    The line that OPENS a heredoc is kept — it is code — and ``<<<`` here-strings
    are not heredocs and are left alone.
    """
    kept: list[str] = []
    terminator: str | None = None
    opener = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
    for line in text.splitlines():
        if terminator is not None:
            if line.strip() == terminator:
                terminator = None
            continue
        match = opener.search(line)
        if match is not None:
            terminator = match.group(2)
            kept.append(line)
            continue
        if line.lstrip().startswith("#"):
            continue
        kept.append(line)
    return "\n".join(kept)


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
    bare = re.search(_OUTBOX_BARE_ASSIGNMENT_PATTERN, _executable_text(text), re.MULTILINE)
    if bare is not None:
        return bare.group(1).strip().strip('"').strip("'")
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


def outbox_fact(cloud: str, root: Path | None = None) -> str:
    """What a PASSING stage (e) actually established, said precisely.

    Not a caveat — a caveat means "weaker than it sounds" and changes the
    verdict banner. This is the plain description of the measurement, and it
    exists because "control-plane outbox is enabled" overstates it in a way
    worth one clause: this is a static read of a file in the WORKING TREE, not
    of the revision that is deployed. A local edit that has not shipped reads as
    enabled here; so does a script that shipped six months ago. What the running
    service does with the variable is stages (b)-(d)'s business.
    """
    entry = ROLLOUT_REGISTRY.get(cloud)
    script = entry.control_plane_script if entry else "its control-plane script"
    value = declared_outbox_value(cloud, root=root)
    return (
        f"{OUTBOX_ENABLED_ENV}={value!r} in the working tree's {script} "
        "(static read of this checkout, not of the deployed revision)"
    )


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


#: Prefixes on a PASSING stage's one line of output. The shell reads the first
#: word and nothing else, and the three mean genuinely different things, which
#: is why they are not all "note:":
#:
#:   ``waived:`` this stage was NOT measured; an exemption in code suppressed
#:               it. The run may not print COMPLETE.
#:   ``caveat:`` measured, but the evidence is weaker than the stage's headline
#:               claim (an empty outbox, a value computed at deploy time).
#:   ``fact:``   measured; here is precisely what was measured.
WAIVED_PREFIX = "waived: "
CAVEAT_PREFIX = "caveat: "
FACT_PREFIX = "fact: "


def _report(
    blockers: list[str],
    *,
    waived: str | None = None,
    caveat: str | None = None,
    fact: str | None = None,
) -> int:
    for blocker in blockers:
        print(blocker)
    if blockers:
        return 1
    if waived:
        print(f"{WAIVED_PREFIX}{waived}")
    elif caveat:
        print(f"{CAVEAT_PREFIX}{caveat}")
    elif fact:
        print(f"{FACT_PREFIX}{fact}")
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
        return _report(registry_gaps() + script_binding_gaps())
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
        return _report(
            blockers,
            waived=waived,
            caveat=outbox_note(args.cloud),
            fact=outbox_fact(args.cloud),
        )

    payload = _load(args.status_file)
    if args.command == "section":
        # Never exempt: a status page that answers nothing about analytics is a
        # cloud you cannot check at all, whatever the reason for the absence.
        return _report(section_blockers(args.cloud, payload))
    if args.command == "available":
        blockers, waived = apply_exemption(args.cloud, available_blockers(args.cloud, payload))
        return _report(blockers, waived=waived)
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
    return _report(blockers, waived=waived, caveat=drain_lag_caveat(payload))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
