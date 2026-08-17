"""When is a cloud *done*? The stages, and the binding that cannot be skipped.

On 2026-08-17 the AWS-EU cloud had been serving production traffic since
2026-08-02 with no analytics pipeline at all: the drain that moves rows from
``tr_operational_analytics_outbox`` into ClickHouse had never been installed,
470,897 rows had piled up in DSQL, and ``activity_generations`` on the Paris
node was empty. Nothing reported it for fifteen days, because the only backlog
alarm is emitted BY the drain that was missing.

:mod:`trusted_router.operational_analytics_fleet` and
``clickhouse.check_fleet_analytics_freshness`` are the *detectors*: they tell
you afterwards, on someone else's schedule. This module is about the ROLLOUT.
The proximate cause was not a missing monitor — it was a bring-up script that
ended by PRINTING next steps and exiting 0 (``scripts/deploy/aws_eu_clickhouse.sh``:
``echo "Next: apply clickhouse/*.sql, then redeploy tr-eu with ..."``). A human
ran it, read the echoes, and stopped. "The script finished" and "the cloud
works" were different things, and nothing anywhere treated "cloud exists but has
no drain" as an incomplete rollout.

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

WHAT THIS MODULE PROVES, AND WHAT IT DOES NOT
---------------------------------------------
Round 2 of review killed the previous answer to "does this deploy script run
the gate?", and it is worth keeping the corpse visible. It used to be a regex:
does the string ``verify_cloud_complete.sh <cloud>`` appear in the script's last
N lines? That is satisfied by a heredoc body, by a printed instruction, and by a
commented-out line — which is *verbatim the bug this module exists to prevent*.
Printing the step is not doing the step, and no amount of regex hardening fixes
a proof-by-text: the next careless edit always wins.

So the binding is now behavioural, and it lives in the test suite
(``tests/test_deploy_script_execution.py``): each bound script is RUN to
completion in a hermetic harness whose ``PATH`` contains nothing but recording
stubs, with a stub ``verify_cloud_complete.sh``, and two properties are
asserted — the verifier was CALLED with this cloud, and when the verifier FAILS
the script exits non-zero. A printed instruction fails both by construction.

:data:`ROLLOUT_REGISTRY` is therefore the list of what to EXECUTE, not the
assertion itself. Each :class:`DeployScript` says how it is proven:

* :data:`PROVEN_BY_EXECUTION` — the harness runs it end to end, both ways.
* :data:`NOT_PROVEN` — it could not be run honestly under stubs, and the reason
  is written down here. Nothing in this repository claims those scripts run the
  gate; the docs and the PR say the same thing in the same words.

The functions in this file still do static checks — that a claimed script
exists, that no unclaimed script calls the verifier, that an exemption carries a
reason — but none of them is the "does it run the gate?" assertion any more.

Three further rules this module enforces in code rather than in prose:

* **The cloud list is never re-typed.** :func:`declared_clouds` delegates to
  :func:`operational_analytics_fleet.deployed_clouds`, which is the union of
  every table in this repo that declares a deployment. A fourth cloud added to
  any one of them shows up here whether or not anybody remembered this file.
  :func:`registry_gaps` then FAILS for a declared cloud that has no entry.

* **The status URL is never re-typed either.** It comes from
  :data:`operational_analytics_fleet.ANALYTICS_FRESHNESS_FLEET`, which is the
  registry the scheduled fleet check reads. That matters concretely: AWS's entry
  there is the *App Runner* control plane that holds the Aurora DSQL connection
  and whose drain was missing, NOT ``aws.trustedrouter.com``, which fronts the
  Fargate plane through Global Accelerator. A gate pointed at the wrong AWS
  front end answers a question nobody asked.

* **Absence must be signed for, and never launders a measurement.** A cloud
  whose analytics genuinely cannot exist yet is allowed through only by
  ``analytics_absent_reason`` in :data:`ROLLOUT_REGISTRY` — a code change, and
  therefore a review. The waiver applies ONLY to stages whose failure is
  structural (a file that sets no variable; a control plane that publishes
  ``not_configured``, i.e. says of itself that it runs no outbox). A MEASURED
  failure — an unreadable outbox, a lag over the bound, a stale section — is
  never waivable, and an exempted run exits non-zero and prints NOT VERIFIED. An
  exemption is a decision to ship without knowing; it must never be the thing
  that makes the machine-readable signal say success.

Nothing this module decides comes from the environment. That is not an
accident: the bound in stage (d) and the URL in stage (a) are what an attacker
of the *process* — a tired operator with an ``export`` in their shell profile —
would reach for, and a deploy script inherits every variable its caller had.
The bound is a constant here and the URL comes from the fleet registry in
``src/``. Overrides exist, but only as explicit command-line flags on
``scripts/deploy/verify_cloud_complete.sh``, and a run that uses one is a
DIAGNOSTIC run that may not print the COMPLETE banner.

This module opens no socket and shells out to nothing. It reads two kinds of
file: deploy scripts in this repository (as text, for stage (e)) and the
``--status-file`` the shell has already fetched.
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

from trusted_router.operational_analytics_fleet import (
    ANALYTICS_FRESHNESS_FLEET,
    deployed_clouds,
    fleet_endpoint,
)
from trusted_router.operational_analytics_freshness import (
    ANALYTICS_STATUS_KEY,
    AVAILABLE_FIELD,
    DEFAULT_MAX_DRAIN_LAG_SECONDS,
    DRAIN_LAG_FIELD,
    GENERATED_AT_FIELD,
    OUTBOX_DEPTH_FIELD,
    REASON_FIELD,
    REASON_NOT_CONFIGURED,
)

#: Repository root, so the outbox stage can read the deploy script that is the
#: source of truth for a cloud's control-plane environment.
REPO_ROOT = Path(__file__).resolve().parents[2]

#: The environment variable that decides whether settle enqueues operational
#: rows at all. Config-level truth: with this false the outbox stays empty, the
#: published ``drain_lag_seconds`` is 0.0 forever, and every stage above reads
#: green while ZERO rows move. Stages (b)-(d) cannot tell "fully drained" from
#: "never enqueued" — see the caveat in :func:`drain_lag_caveat`.
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
#:
#: Stage (e) is the one judgement in this module that is still made by reading
#: text, and it is the weakest thing here. It is kept because the alternative
#: needs cloud credentials, and a check that needs production credentials is a
#: check that does not get run — but the docs say plainly that a determined
#: edit beats it, exactly as one beat the old script binding.
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

#: Repo-relative path of the sourced fragment that RUNS the verifier and turns
#: its exit status into an outcome every bound script reports identically.
#: Shared on purpose: exit 5 ("nobody outside can see this cloud yet") used to
#: be understood by one of five scripts, so the other four reported today's real
#: state as the wrong failure with the wrong fix.
GATE_LIBRARY = "scripts/deploy/cloud_complete_gate.sh"

#: Where deploy scripts live. Scanned RECURSIVELY by :func:`script_binding_gaps`
#: for invocations of the verifier that no registry entry claims — the previous
#: glob was ``scripts/deploy/*.sh``, so a script one directory down escaped it.
DEPLOY_SCRIPT_DIR = "scripts"

#: How stale the published section may be before it is a frozen control plane
#: rather than a healthy one. Mirrors the default in the fleet freshness check.
DEFAULT_MAX_SECTION_AGE_SECONDS = 3_600.0

#: This script is executed end to end by the behavioural harness in
#: ``tests/test_deploy_script_execution.py``, which asserts that it calls the
#: verifier for its cloud AND that a failing verifier makes it exit non-zero.
PROVEN_BY_EXECUTION = "execution"

#: This script is NOT proven. Nothing in this repository establishes that it
#: runs the gate; :attr:`DeployScript.unproven_reason` says why not, and the
#: docs repeat it. Kept in the registry so the omission is a sentence somebody
#: wrote rather than a row missing from a list.
NOT_PROVEN = "unproven"


@dataclass(frozen=True)
class CompensatingControl:
    """A CI job that checks a cloud whose deploy script is exempt from the gate.

    Exists because the first version of GCP's exemption cited "the scheduled
    analytics freshness workflow" — a workflow that ships with no ``schedule:``
    trigger, deliberately and in its own header. The primary cloud therefore had
    no automated completeness check at all, behind a sentence saying it did.

    So a claimed control is a structured reference, and
    ``tests/test_cloud_rollout_completeness.py`` opens the workflow, finds the
    job, and fails if it does not run the verifier for that cloud. That is a
    check of a DECLARATION — a YAML file cannot be executed here — and it is
    weaker than the behavioural harness. It is enough to stop an exemption
    citing a control that does not exist, which is what happened.
    """

    #: Repo-relative path of the workflow file.
    workflow: str
    #: Job id inside ``jobs:``.
    job: str
    #: What it does and when it runs, for the human reading the exemption.
    description: str


@dataclass(frozen=True)
class ScriptExemption:
    """A deploy script that deliberately does NOT run the completeness gate.

    Both text fields are required, which is the entire design: the failure mode
    this replaces was a cloud being absent from a hand-written list, and absence
    carries no reason. An exemption is a sentence somebody wrote and a reviewer
    read — and, since round 2, a control somebody can go and look at.
    """

    #: Repo-relative path, so the test can assert the file exists.
    script: str
    #: Why binding this one would be wrong. Printed by ``audit``.
    reason: str
    #: What checks the cloud instead. ``None`` is legal and means UNCHECKED —
    #: which must then be said out loud, here and in the docs, rather than
    #: implied away.
    compensating_control: CompensatingControl | None = None


@dataclass(frozen=True)
class DeployScript:
    """One deploy script that must end in the completeness gate, and its proof."""

    #: Repo-relative path.
    path: str
    #: :data:`PROVEN_BY_EXECUTION` or :data:`NOT_PROVEN`.
    proof: str = PROVEN_BY_EXECUTION
    #: Required when :attr:`proof` is :data:`NOT_PROVEN`: what stops the harness
    #: from running this one honestly. A blank reason is a CI failure.
    unproven_reason: str = ""


@dataclass(frozen=True)
class CloudRollout:
    """What this repository knows about finishing ONE cloud's rollout.

    Deliberately small. The status URL is not stored here: it comes from
    :data:`operational_analytics_fleet.ANALYTICS_FRESHNESS_FLEET`, so this table
    cannot drift into a second, disagreeing list of clouds and their endpoints.
    """

    #: Cloud id as the deployment-declaring tables spell it ("aws", "azure", "gcp").
    cloud: str
    #: Repo-relative deploy script that is the SOURCE OF TRUTH for that cloud's
    #: control-plane environment — the file whose header says so.
    control_plane_script: str
    #: The command an operator runs to install/refresh this cloud's drain.
    #: Printed by the stage that fails, so the message names the fix.
    drain_install_command: str
    #: Every deploy script for this cloud that must end in the completeness
    #: gate. This is the list the behavioural harness EXECUTES; it is not itself
    #: the assertion. Empty is legal only when every one of the cloud's scripts
    #: is in :attr:`exempt_deploy_scripts`.
    deploy_scripts: tuple[DeployScript, ...] = ()
    #: Deploy scripts deliberately left unbound, each with its reason. Named in
    #: code so that "this cloud's script does not run the check" is a claim
    #: somebody made and a reviewer saw, rather than a row missing from a list.
    exempt_deploy_scripts: tuple[ScriptExemption, ...] = ()
    #: Set ONLY to record a reviewed, deliberate absence of the analytics
    #: pipeline on this cloud. A non-empty string downgrades the STRUCTURAL
    #: blockers — never a measurement — to a NOT VERIFIED verdict that still
    #: exits non-zero. Empty for every cloud today, on purpose: the AWS-EU
    #: outage was fifteen days of exactly this exemption granted by nobody, in
    #: silence.
    analytics_absent_reason: str | None = None


#: Every cloud that must be finishable. Keys are checked against
#: :func:`declared_clouds` by :func:`registry_gaps`, so adding a cloud to any
#: deployment-declaring table without adding it here is a CI failure rather than
#: a cloud nobody ever verifies.
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
                    "main. Ending rollout.sh itself in this verifier would put a public HTTPS "
                    "fetch of trustedrouter.com/status.json in the MIDDLE of the deploy of the "
                    "cloud that SERVES trustedrouter.com — the deploy that repairs an outage "
                    "would abort partway because of the outage it repairs. So the gate runs in "
                    "the same workflow but AFTER every production mutation, as its own job, "
                    "where a failure is a red run and never a half-finished rollout."
                ),
                compensating_control=CompensatingControl(
                    workflow=".github/workflows/deploy.yml",
                    job="verify-cloud-complete",
                    description=(
                        "Runs 'bash scripts/deploy/verify_cloud_complete.sh gcp' after the "
                        "deploy job has finished mutating production, retrying while the new "
                        "revision takes traffic. Out of band by construction: it can only "
                        "make the run red, never leave GCP half-deployed. It has never run "
                        "on a merge as of this commit — it lands with this change."
                    ),
                ),
            ),
        ),
    ),
    "aws": CloudRollout(
        cloud="aws",
        control_plane_script="scripts/deploy/aws_eu_control_plane.sh",
        drain_install_command="bash scripts/deploy/aws_eu_clickhouse_drain_install.sh",
        deploy_scripts=(
            DeployScript("scripts/deploy/aws_eu_clickhouse.sh", PROVEN_BY_EXECUTION),
            DeployScript("scripts/deploy/aws_eu_control_plane.sh", PROVEN_BY_EXECUTION),
            DeployScript("scripts/deploy/aws_eu_north_clickhouse.sh", PROVEN_BY_EXECUTION),
            DeployScript(
                "scripts/deploy/aws_eu_clickhouse_drain_install.sh",
                NOT_PROVEN,
                unproven_reason=(
                    "Not runnable under stubs without lying about the thing it exists to "
                    "prove. Its steps 4-9 ship a tarball to the node in base64 chunks over "
                    "SSM and then read the drain's own journal back to establish that rows "
                    "MOVED; a stub SSM that answers Status=Success to every command turns "
                    "that verification into an assertion about the stub. The harness would be "
                    "executing a script whose middle had been replaced by the answer it wants, "
                    "which is the failure this whole change is about, one level up. Its tail "
                    "is therefore CLAIMED, not proven: it sources "
                    "scripts/deploy/cloud_complete_gate.sh (whose behaviour IS proven, both "
                    "ways, by tests/test_deploy_script_execution.py) and calls "
                    "require_cloud_complete aws. Nothing here establishes that the call is "
                    "reached on a real run."
                ),
            ),
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
        deploy_scripts=(
            DeployScript("scripts/deploy/azure_control_plane.sh", PROVEN_BY_EXECUTION),
        ),
    ),
}


def rollout_scripts(cloud: str) -> tuple[DeployScript, ...]:
    entry = ROLLOUT_REGISTRY.get(cloud)
    return entry.deploy_scripts if entry else ()


def scripts_proven_by_execution() -> tuple[tuple[str, str], ...]:
    """``(script, cloud)`` for every script the behavioural harness runs."""
    return tuple(
        (script.path, cloud)
        for cloud, entry in sorted(ROLLOUT_REGISTRY.items())
        for script in entry.deploy_scripts
        if script.proof == PROVEN_BY_EXECUTION
    )


def scripts_not_proven() -> tuple[tuple[str, str, str], ...]:
    """``(script, cloud, reason)`` for every script nothing here proves."""
    return tuple(
        (script.path, cloud, script.unproven_reason)
        for cloud, entry in sorted(ROLLOUT_REGISTRY.items())
        for script in entry.deploy_scripts
        if script.proof != PROVEN_BY_EXECUTION
    )


# ---------------------------------------------------------------------------
# (a) The registry. Who is checked, and by whom.
# ---------------------------------------------------------------------------


def declared_clouds() -> tuple[str, ...]:
    """Every cloud this repository DECLARES it deploys.

    Delegated whole to :func:`operational_analytics_fleet.deployed_clouds`,
    which is the union of every table in this repo that declares a deployment
    (the BYOK attestation tables, ``MULTICLOUD_REGION_GEO``, the
    ``external_live_regions``/``marketing_regions`` settings, and
    ``synthetic_fleet_peers``) and which names its sources in its own failure
    messages. Delegated rather than re-derived: a second union that reads three
    of those five tables is a fourth copy of the fleet, and copies drift — the
    thing this module exists to stop.

    The residual gap, stated because the docs must not overstate this: a cloud
    that enters NO table — provisioned by hand, serving traffic, named nowhere
    in ``src/`` — is invisible to every check here, exactly as a cloud that
    nobody declares is invisible to the marketing map and to ``/v1/regions``.
    """
    return deployed_clouds()


def freshness_registry() -> dict[str, str]:
    """Cloud -> public ``/status.json`` URL, from the fleet registry.

    The URLs come from :data:`ANALYTICS_FRESHNESS_FLEET` and nowhere else. That
    is load-bearing rather than tidy: AWS's entry there is the tr-eu App Runner
    control plane, the deployment that holds the Aurora DSQL connection and
    whose drain was missing for fifteen days. The obvious-looking
    ``aws.trustedrouter.com`` is a different service — the Fargate plane behind
    Global Accelerator — and a gate pointed at it would have been green
    throughout the outage.

    Entries with a ``reason`` and no URL are deliberately absent from this
    mapping: they are clouds the registry declares cannot be checked over HTTP,
    and :func:`registry_blockers` reports them with that reason rather than
    letting them fall through as "unknown cloud".
    """
    return {
        entry.cloud: entry.status_url
        for entry in ANALYTICS_FRESHNESS_FLEET
        if entry.status_url
    }


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
            endpoint = fleet_endpoint(cloud)
            if endpoint is not None and endpoint.reason:
                gaps.append(
                    f"{cloud}: the fleet registry records it as UNCHECKABLE over HTTP "
                    f"({endpoint.reason!r}), so scripts/deploy/verify_cloud_complete.sh "
                    "cannot reach it and no rollout of it can be verified from outside. "
                    "Fix: give it a public control-plane /status.json in "
                    "ANALYTICS_FRESHNESS_FLEET (src/trusted_router/"
                    "operational_analytics_fleet.py), or stop deploying it."
                )
            else:
                gaps.append(
                    f"{cloud}: declared as a deployment (see "
                    "operational_analytics_fleet.deployment_sources() for which table "
                    "says so) but absent from ANALYTICS_FRESHNESS_FLEET, so no one ever "
                    "reads its analytics freshness. Fix: add a FleetAnalyticsEndpoint for "
                    "it in src/trusted_router/operational_analytics_fleet.py."
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
# The other binding: which SCRIPTS must run the gate.
#
# What is checked HERE is structural — a claimed script exists, an exemption
# carries a reason, no unclaimed script calls the verifier. Whether a script
# ACTUALLY runs the gate is proven by executing it; see the module docstring
# and tests/test_deploy_script_execution.py.
# ---------------------------------------------------------------------------


def _scripts_invoking_the_verifier(root: Path) -> list[str]:
    """Every script under ``scripts/`` that mentions the verifier at all.

    Recursive since round 2. The previous version globbed
    ``scripts/deploy/*.sh`` and nothing else, so a bring-up script one directory
    down — ``scripts/deploy/aws/bring_up.sh`` — could call the verifier while no
    registry entry claimed it, which is the "wiring nobody would miss" shape
    this check exists to catch.
    """
    directory = root / DEPLOY_SCRIPT_DIR
    if not directory.is_dir():
        return []
    verifier_name = Path(VERIFIER_SCRIPT).name
    gate_name = Path(GATE_LIBRARY).name
    found: list[str] = []
    for path in sorted(directory.rglob("*.sh")):
        if path.name in (verifier_name, gate_name):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):  # pragma: no cover - unreadable file
            continue
        if verifier_name in text or "require_cloud_complete" in text:
            found.append(str(path.relative_to(root)))
    return found


def script_binding_gaps(root: Path | None = None) -> list[str]:
    """Structural defects in the script registry. Empty means well-formed.

    Six ways to be wrong, all of them things a new cloud does by accident:

    1. the cloud names no script at all and claims no exemption — the shape the
       old hand-written list allowed silently, because a cloud that is simply
       absent from a list looks the same as a cloud with nothing to bind;
    2. a named script does not exist;
    3. the cloud's own ``control_plane_script`` — the file that IS the source
       of truth for its environment — is neither bound nor exempt;
    4. some script under ``scripts/`` runs the verifier for a cloud that never
       named it, so the wiring exists but no registry entry would notice it
       disappearing;
    5. a script recorded as :data:`NOT_PROVEN` gives no reason, which is the
       missing row it replaced;
    6. an exemption has no reason, names a file that is not there, or cites a
       compensating control whose workflow file does not exist.

    Every message names the file to edit, because the fix is a code change and
    a CI failure that does not say where to go teaches nobody.
    """
    root = root or REPO_ROOT
    gaps: list[str] = []
    here = "src/trusted_router/cloud_rollout_completeness.py"
    claimed: dict[str, str] = {}

    for cloud, entry in sorted(ROLLOUT_REGISTRY.items()):
        exempt = {item.script: item for item in entry.exempt_deploy_scripts}
        for script in entry.deploy_scripts:
            claimed[script.path] = cloud
        for path, item in exempt.items():
            claimed.setdefault(path, cloud)
            if not item.reason.strip():
                gaps.append(
                    f"{cloud}: {path} is exempt from the completeness gate with an EMPTY "
                    f"reason. An exemption with no reason is the missing row it replaced. "
                    f"Fix: write why in ScriptExemption.reason in {here}."
                )
            if not (root / path).is_file():
                gaps.append(
                    f"{cloud}: exempt script {path} does not exist. Fix: correct or drop "
                    f"the ScriptExemption in {here}."
                )
            control = item.compensating_control
            if control is not None and not (root / control.workflow).is_file():
                gaps.append(
                    f"{cloud}: {path} is exempt and cites {control.workflow} "
                    f"(job {control.job}) as the control that checks this cloud instead, "
                    "but that workflow file does not exist. An exemption citing a control "
                    "that is not there is worse than one admitting the cloud is unchecked. "
                    f"Fix: correct or drop the CompensatingControl in {here}."
                )

        if not entry.deploy_scripts and not exempt:
            gaps.append(
                f"{cloud}: ROLLOUT_REGISTRY names no deploy script for this cloud, so "
                f"nothing binds its bring-up to {VERIFIER_SCRIPT} and it can ship a script "
                'that prints "Next: ..." and exits 0 with CI green — the AWS-EU outage '
                f"exactly. Fix: in {here}, add every deploy script for {cloud} to "
                "deploy_scripts=(...) on its CloudRollout, and end each of those scripts "
                f'with: require_cloud_complete {cloud}. If one of them must NOT be bound, '
                "say so in exempt_deploy_scripts=(ScriptExemption(script=..., reason=...),)."
            )

        bound_paths = {script.path for script in entry.deploy_scripts}
        if entry.control_plane_script not in bound_paths and (
            entry.control_plane_script not in exempt
        ):
            gaps.append(
                f"{cloud}: {entry.control_plane_script} is this cloud's control-plane "
                "script — the source of truth for its service environment — but it is "
                "neither in deploy_scripts nor exempt, so deploying this cloud never asks "
                f"whether the cloud works. Fix: add it to deploy_scripts in {here} and end "
                f"it with: require_cloud_complete {cloud}"
            )

        for script in entry.deploy_scripts:
            if not (root / script.path).is_file():
                gaps.append(
                    f"{cloud}: deploy_scripts names {script.path}, which does not exist. "
                    f"Fix: correct the path in {here}."
                )
            if script.proof not in (PROVEN_BY_EXECUTION, NOT_PROVEN):
                gaps.append(
                    f"{cloud}: {script.path} has proof={script.proof!r}, which is neither "
                    f"PROVEN_BY_EXECUTION nor NOT_PROVEN. Fix: pick one in {here}."
                )
            if script.proof == NOT_PROVEN and not script.unproven_reason.strip():
                gaps.append(
                    f"{cloud}: {script.path} is recorded as NOT_PROVEN with no reason, so "
                    "the repository says nothing establishes that it runs the gate and "
                    "also does not say why. That is the missing row again. Fix: write "
                    f"unproven_reason in {here}, and say the same thing in the docs."
                )

    for found in _scripts_invoking_the_verifier(root):
        if found not in claimed:
            gaps.append(
                f"{found} runs {VERIFIER_SCRIPT} but no CloudRollout claims it, so nothing "
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
        endpoint = fleet_endpoint(cloud)
        detail = (
            f" The fleet registry has an entry but no URL: {endpoint.reason!r}."
            if endpoint is not None and endpoint.reason
            else ""
        )
        blockers.append(
            f"{cloud}: no public status URL in ANALYTICS_FRESHNESS_FLEET, so nothing on "
            f"any schedule reads its drain lag and this gate has nothing to fetch.{detail}"
            " Fix: add a FleetAnalyticsEndpoint with the CONTROL plane's public "
            "/status.json in src/trusted_router/operational_analytics_fleet.py."
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


def apply_exemption(
    cloud: str, blockers: list[str], *, waivable: bool
) -> tuple[list[str], str | None]:
    """Downgrade STRUCTURAL blockers to a NOT VERIFIED note when signed for.

    The single escape hatch, and it is narrow in two directions since round 2.

    First, only a ``analytics_absent_reason`` written into
    :data:`ROLLOUT_REGISTRY` — a code change, therefore a review — can waive
    anything, and the note still carries the original blocker so the exemption
    cannot hide what it is exempting.

    Second, and this is the part that was wrong: ``waivable`` must be False for
    any blocker that came from a MEASUREMENT. The previous version waived stages
    (c), (d) and (e) alike, so a cloud that had been measured and had FAILED —
    an unreadable outbox, a lag over the bound — was let through by a sentence
    about a pipeline that was never built, and the run still exited 0. An
    exemption may excuse the ABSENCE of a pipeline. It may never launder a
    reading, and (see the shell) it never exits 0.
    """
    reason = exemption(cloud)
    if not blockers or reason is None or not waivable:
        return blockers, None
    detail = " | ".join(blockers)
    return [], (
        f"ACCEPTED-ABSENT ({cloud}): {reason} — suppressing: {detail}. Remove "
        "analytics_absent_reason from ROLLOUT_REGISTRY when this is fixed."
    )


# ---------------------------------------------------------------------------
# (b)-(d) What the cloud publishes about itself.
# ---------------------------------------------------------------------------


class UnreadableStatusPage(ValueError):
    """The body fetched from the status URL is not the JSON status document.

    Its own type because it is its own finding, and round 2 caught the two being
    collapsed: "this cloud publishes no analytics section" (a deployed control
    plane that predates the publisher — nobody outside can see this cloud yet)
    and "the body could not be parsed at all" (a CDN interstitial, a captive
    portal, a truncated response) were both reported as NOT YET OBSERVABLE with
    the same fix instruction. Only one of them is fixed by deploying a newer
    control plane.
    """


def unwrap_status_payload(payload: Any) -> dict[str, Any]:
    """``/status.json`` serves ``{"data": {...}}``; accept either shape."""
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        payload = payload["data"]
    if not isinstance(payload, dict):
        raise UnreadableStatusPage(
            "the status URL answered, but the body is a "
            f"{type(payload).__name__}, not the JSON object /status.json serves"
        )
    return payload


def section_blockers(cloud: str, payload: dict[str, Any]) -> list[str]:
    """Stage (b): the section exists at all."""
    section = payload.get(ANALYTICS_STATUS_KEY)
    if isinstance(section, dict):
        return []
    return [
        f"{cloud}: /status.json parsed, and publishes no '{ANALYTICS_STATUS_KEY}' section, "
        "so this cloud's drain lag is unobservable from outside. Fix: deploy a control "
        "plane built from a commit whose status snapshot calls "
        "trusted_router.operational_analytics_freshness.analytics_status_section() — "
        "an older image serves a status page that simply omits the question."
    ]


def structurally_absent(payload: dict[str, Any]) -> bool:
    """Does the cloud SAY OF ITSELF that it runs no outbox?

    ``available=false`` with ``reason=not_configured`` is a control plane
    reporting a configuration, not a reading that went wrong: the outbox flag is
    off, so there is no table to read and never was. That is the one shape of
    stage (c)/(d) failure an ``analytics_absent_reason`` may excuse. Every other
    reason — ``unreachable``, ``no_data``, anything unrecognised — is a
    measurement, and a measurement is never waivable.
    """
    section = payload.get(ANALYTICS_STATUS_KEY)
    if not isinstance(section, dict) or section.get(AVAILABLE_FIELD):
        return False
    return section.get(REASON_FIELD) == REASON_NOT_CONFIGURED


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
    if reason == REASON_NOT_CONFIGURED:
        return [
            f"{cloud}: {ANALYTICS_STATUS_KEY}.{REASON_FIELD} is "
            f"{REASON_NOT_CONFIGURED!r} — the deployed control plane reports that it "
            "runs NO operational-analytics outbox, so nothing is enqueued, nothing is "
            "drained, and every freshness number this cloud can publish is vacuously "
            f"green. Fix: {install}. To accept the absence instead, record it as "
            "analytics_absent_reason on the cloud's CloudRollout in "
            "src/trusted_router/cloud_rollout_completeness.py; that is a reviewed code "
            "change, and the run still reports NOT VERIFIED and exits non-zero."
        ]
    return [
        f"{cloud}: {ANALYTICS_STATUS_KEY}.{AVAILABLE_FIELD} is false "
        f"({REASON_FIELD}={reason!r}) — the control plane could not read its own "
        "operational-analytics outbox, which is not the same as an empty one and not "
        f"the same as not having one. Fix: {install}. This is a MEASUREMENT, so no "
        "analytics_absent_reason waives it: an exemption may excuse a pipeline that was "
        "never built, never a reading that failed."
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

    if not section.get(AVAILABLE_FIELD):
        # Nothing to measure, and inventing a "missing field" failure here would
        # report the same condition twice in different words. Defer to (c),
        # which already said whether this is a configuration or a fault.
        return [
            f"{cloud}: {ANALYTICS_STATUS_KEY}.{AVAILABLE_FIELD} is false "
            f"({REASON_FIELD}={section.get(REASON_FIELD)!r}), so there is no lag to "
            "read. Stage (c) is the one to fix; this stage cannot run until it passes."
        ]

    blockers: list[str] = []
    lag = _as_float(section.get(DRAIN_LAG_FIELD))
    if lag is None:
        blockers.append(
            f"{cloud}: {ANALYTICS_STATUS_KEY}.{DRAIN_LAG_FIELD} is missing or "
            "unparseable while the section claims to be available, so the pipeline "
            "reports nothing measurable. Fix: publish it from the outbox head "
            "(operational_analytics_freshness.analytics_status_section)."
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

    Read off ``outbox_depth`` alone. The section used to carry
    ``oldest_enqueued_at`` as well and this function read both; that field was
    dropped before anything published it, so depth is what there is.
    """
    section = payload.get(ANALYTICS_STATUS_KEY)
    if not isinstance(section, dict):
        return None
    depth = _as_int(section.get(OUTBOX_DEPTH_FIELD))
    if depth is None or depth > 0:
        return None
    return (
        "outbox is empty: lag 0 proves nothing is STUCK, not that anything is moving. "
        "Rows observed moving is the bar — see stage (e) and, in-cloud, "
        "SELECT count() FROM activity_generations."
    )


# ---------------------------------------------------------------------------
# (e) The producer side: is anything enqueued in the first place?
# ---------------------------------------------------------------------------

#: A heredoc opener, and NOT a here-string. The distinction cost a false claim
#: in the docstring below: ``<<-?\s*(['"]?)(NAME)\1`` looks like it cannot match
#: ``<<<WORD``, and it does — the engine simply starts one character later, so
#: the ``<<`` of ``<<<`` plus ``WORD`` matched and everything after it was
#: treated as a heredoc body until a line reading ``WORD`` turned up, which it
#: never does. A here-string that happened to sit above the outbox assignment
#: would have swallowed it and failed the cloud that passes.
_HEREDOC_OPENER = re.compile(r"(?<!<)<<(?!<)-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def _executable_text(text: str) -> str:
    """The script with heredoc BODIES and whole-line comments removed.

    Just enough shell awareness to tell an assignment from a paragraph that
    contains one. ``cat <<'NEXT' ... NEXT`` is how every one of these scripts
    prints operator instructions, and those instructions quote the very
    assignment the operator is being asked to add; a bare-assignment pattern
    over the raw text would read that advice back as compliance, which is the
    bug :data:`_OUTBOX_DECLARATION_PATTERNS` documents having already made once.

    The line that OPENS a heredoc is kept — it is code. ``<<<`` here-strings are
    not heredocs and are genuinely skipped now; see :data:`_HEREDOC_OPENER` for
    what "now" is doing in that sentence.
    """
    kept: list[str] = []
    terminator: str | None = None
    for line in text.splitlines():
        if terminator is not None:
            if line.strip() == terminator:
                terminator = None
            continue
        match = _HEREDOC_OPENER.search(line)
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
    does not run. It is also, therefore, the weakest judgement in this module —
    a static read of a file in the working tree, beatable by anyone who wants to
    beat it, and the docs say so.
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
#
# THE OUTPUT CONTRACT, which round 2 made structural.
#
# The shell used to capture this process with `2>&1` and classify the result by
# its FIRST WORD. One stray line on stderr — a DeprecationWarning, a pip notice,
# a urllib3 warning from some transitive import — landed at the top of the
# captured text, the first word became something else, and the whole
# waived:/caveat:/fact: distinction collapsed back into the flat green COMPLETE
# banner. That banner is the exact sentence this work exists to make
# unprintable, and an interpreter warning could restore it.
#
# So the streams are separate now and the classification is a record, not a
# prefix: every human sentence goes to STDERR, and stdout carries exactly one
# line, the last thing written, of the form
#
#     TR_VERDICT<TAB><kind><TAB><one-line summary>
#
# The shell reads the LAST line of stdout, requires the sentinel, and refuses to
# reach any verdict at all if it is not there. Anything else printed to stdout
# by anything at all is ignored, and an absent sentinel is a hard failure rather
# than a green default — a gate that cannot classify its own output must not be
# the thing that says a cloud is finished.
# ---------------------------------------------------------------------------

#: Marks the one machine-readable line on stdout.
VERDICT_SENTINEL = "TR_VERDICT"

#: ``kind`` values in that line. Every one of them means something different to
#: the shell, and none of them is inferred from prose.
KIND_OK = "ok"  # measured, passed, nothing more to say
KIND_FACT = "fact"  # measured, passed, here is precisely what was measured
KIND_CAVEAT = "caveat"  # measured, passed, weaker than the stage's headline
KIND_WAIVED = "waived"  # NOT measured; an exemption in code suppressed it
KIND_BLOCKED = "blocked"  # measured, failed
KIND_UNOBSERVABLE = "unobservable"  # parsed fine, publishes no analytics section
KIND_UNREADABLE = "unreadable"  # the body is not the status document at all
KIND_VALUE = "value"  # not a verdict: the answer to a question (a URL)


def _emit(kind: str, summary: str, detail: list[str] | None = None) -> int:
    """Write the human text to stderr and the one machine line to stdout."""
    for line in detail or []:
        print(line, file=sys.stderr)
    flat = " ".join(summary.split())
    print(f"{VERDICT_SENTINEL}\t{kind}\t{flat}")
    return 0 if kind in (KIND_OK, KIND_FACT, KIND_CAVEAT, KIND_VALUE) else 1


def _report(
    blockers: list[str],
    *,
    waived: str | None = None,
    caveat: str | None = None,
    fact: str | None = None,
) -> int:
    if blockers:
        return _emit(KIND_BLOCKED, blockers[0], detail=blockers)
    if waived:
        return _emit(KIND_WAIVED, waived, detail=[waived])
    if caveat:
        return _emit(KIND_CAVEAT, caveat, detail=[caveat])
    if fact:
        return _emit(KIND_FACT, fact, detail=[fact])
    return _emit(KIND_OK, "passed")


def _load(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        raw = handle.read()
    try:
        return unwrap_status_payload(json.loads(raw))
    except json.JSONDecodeError as exc:
        head = " ".join(raw[:160].split()) or "(empty body)"
        raise UnreadableStatusPage(
            f"the status URL answered 200 but the body is not JSON ({exc.msg} at line "
            f"{exc.lineno}). First bytes: {head!r}"
        ) from exc


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
            print(cloud, file=sys.stderr)
        return _emit(KIND_VALUE, ",".join(declared_clouds()))
    if args.command == "registry":
        return _report(registry_blockers(args.cloud))
    if args.command == "status-url":
        blockers = registry_blockers(args.cloud)
        if blockers:
            return _report(blockers)
        return _emit(KIND_VALUE, freshness_registry()[args.cloud])
    if args.command == "outbox":
        # Stage (e) reads a file in this checkout. Nothing about it is a
        # measurement of a running cloud, so it is the one stage an exemption
        # may waive outright.
        blockers, waived = apply_exemption(
            args.cloud, outbox_enabled_blockers(args.cloud), waivable=True
        )
        return _report(
            blockers,
            waived=waived,
            caveat=outbox_note(args.cloud),
            fact=outbox_fact(args.cloud),
        )

    try:
        payload = _load(args.status_file)
    except UnreadableStatusPage as exc:
        return _emit(KIND_UNREADABLE, str(exc), detail=[f"{args.cloud}: {exc}"])

    if args.command == "section":
        # Never exempt: a status page that answers nothing about analytics is a
        # cloud you cannot check at all, whatever the reason for the absence.
        blockers = section_blockers(args.cloud, payload)
        if blockers:
            return _emit(KIND_UNOBSERVABLE, blockers[0], detail=blockers)
        return _emit(KIND_OK, "the analytics section is published")

    # Stages (c) and (d) read a MEASUREMENT. The only shape of failure an
    # exemption may excuse is the cloud reporting of ITSELF that it runs no
    # outbox; a reading that went wrong is never waivable.
    waivable = structurally_absent(payload)
    if args.command == "available":
        blockers, waived = apply_exemption(
            args.cloud, available_blockers(args.cloud, payload), waivable=waivable
        )
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
        waivable=waivable,
    )
    return _report(blockers, waived=waived, caveat=drain_lag_caveat(payload))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
