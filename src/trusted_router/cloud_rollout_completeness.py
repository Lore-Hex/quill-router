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

    a. is the cloud in the fleet freshness registry — i.e. is there an endpoint
       to read its drain lag from at all? (Registered, not watched: the fleet
       workflow that reads that registry ships with ``workflow_dispatch`` as its
       only trigger, deliberately, until every cloud publishes the section.)
    b. does its public ``/status.json`` carry the ``analytics`` section?
    c. is ``analytics.available`` true — the control plane could READ its outbox?
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
completion in a harness whose ``PATH`` holds a recording stub for every command
that leaves the machine plus a symlink to everything else in ``/bin`` and
``/usr/bin`` — isolation BY NAME, not a sandbox, and ``tests/deploy_script_harness.py``
says so in its own header — with a stub ``verify_cloud_complete.sh``, and two
properties are asserted: the verifier was CALLED with this cloud, and when the
verifier FAILS the script exits non-zero. A printed instruction fails both by
construction.

Every cloud is in that harness, GCP included. It was not, for one revision: the
primary cloud's only binding was a substring search over the ``run:`` blocks of
a workflow job, which a printed instruction satisfies — the exception had become
the hole. The job's body now lives in ``scripts/deploy/verify_gcp_complete.sh``,
which is bound here and executed like the rest.

:data:`ROLLOUT_REGISTRY` is therefore the list of what to EXECUTE, not the
assertion itself. Each :class:`DeployScript` says how it is proven:

* :data:`PROVEN_BY_EXECUTION` — the harness runs it end to end, both ways.
* :data:`NOT_PROVEN` — it could not be run honestly under stubs, and the reason
  is written down here. Nothing in this repository claims those scripts run the
  gate; the docs and the PR say the same thing in the same words.

The functions in this file still do static checks — that a claimed script
exists, that no unclaimed script calls the verifier, that an exemption carries a
reason — but none of them is the "does it run the gate?" assertion any more.

Two further rules this module enforces in code rather than in prose:

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

THERE IS NO WAY TO EXCUSE A STAGE
---------------------------------
Earlier revisions of this module carried an exemption: a cloud could record
``analytics_absent_reason`` and have some stages "waived" rather than measured,
with a verdict taxonomy deciding which failures a waiver was allowed to touch.
Two review rounds found bugs in that machinery, and the second set were
regressions introduced by the fixes to the first. It is gone. A cloud that
cannot be checked is simply NOT VERIFIED: the run exits non-zero and prints the
reason. Nothing in this file can turn a failure into a pass.

Nothing this module decides comes from the environment or from a flag, either.
That is not an accident: the bound in stage (d) and the URL in stage (a) are
what an attacker of the *process* — a tired operator with an ``export`` in their
shell profile — would reach for, and a deploy script inherits every variable its
caller had. The bound is a constant here; the URL comes from the fleet registry
in ``src/``; and neither this module nor the shell entry point accepts an
override for either.

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
#: "never enqueued" — see :data:`DRAIN_LAG_LIMIT_NOTE`, which says so on every
#: run that passes stage (d).
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
    job, and requires one of its steps to be an EXACT invocation of a script
    this cloud's :attr:`CloudRollout.deploy_scripts` records as
    :data:`PROVEN_BY_EXECUTION`.

    Both halves of that sentence were learned. The check used to concatenate the
    job's ``run:`` blocks and look for the substring
    ``verify_cloud_complete.sh gcp``; a reviewer replaced the whole body with an
    ``echo`` of that string plus ``exit 0`` and the suite stayed green, as it
    did for a commented-out line and for ``|| true``. All three are the shapes
    this module exists to kill, and they satisfied the only binding covering the
    primary cloud. Requiring an exact invocation kills them here, and pointing
    it at a proven script moves the interesting half — what the check does — out
    of YAML and into the behavioural harness.

    What remains a DECLARATION, because a workflow cannot be executed here: that
    the job exists, what it depends on, and whether GitHub reaches it on a merge.
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
    #: Why binding this one would be wrong. Nothing prints this: it is read by
    #: whoever opens this file and by the reviewer of the diff that adds it. CI
    #: only requires that it is not blank.
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
    #: The deploy scripts for this cloud that end in the completeness gate. This
    #: is the list the behavioural harness EXECUTES; it is not itself the
    #: assertion.
    #:
    #: What :func:`script_binding_gaps` actually enforces, stated exactly
    #: because an earlier version of this comment described a stricter rule that
    #: no code enforced and no cloud satisfied: the cloud must name at least one
    #: script here or one exemption; its :attr:`control_plane_script` must be in
    #: one of the two; and no script anywhere under ``scripts/`` may invoke the
    #: gate without appearing in one of the two. It does NOT enumerate a cloud's
    #: scripts and require each to be bound — nothing here knows which of the
    #: sixty files in ``scripts/deploy/`` belong to which cloud. A cloud can
    #: still ship a bring-up script that neither runs the gate nor is named
    #: here, and the check that catches that is a reviewer.
    deploy_scripts: tuple[DeployScript, ...] = ()
    #: Deploy scripts deliberately left unbound, each with its reason. Named in
    #: code so that "this cloud's script does not run the check" is a claim
    #: somebody made and a reviewer saw, rather than a row missing from a list.
    #:
    #: This is about which SCRIPTS end in the gate. It is not, and cannot become,
    #: permission for a cloud to skip a stage: there is no such permission.
    exempt_deploy_scripts: tuple[ScriptExemption, ...] = ()


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
        deploy_scripts=(
            DeployScript("scripts/deploy/verify_gcp_complete.sh", PROVEN_BY_EXECUTION),
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
                    "the same workflow as its own job, out of band, where a failure is a red "
                    "run and never a half-finished rollout. That job's body is "
                    "scripts/deploy/verify_gcp_complete.sh, which is bound above and proven "
                    "by execution: the exemption is now about WHICH FILE ends in the gate, "
                    "not about GCP being checked differently from anyone else."
                ),
                compensating_control=CompensatingControl(
                    workflow=".github/workflows/deploy.yml",
                    job="verify-cloud-complete",
                    description=(
                        "Runs 'bash scripts/deploy/verify_gcp_complete.sh' — nothing else — "
                        "after the deploy job has finished mutating production, retrying "
                        "while the new revision takes traffic. Out of band by construction: "
                        "it can only make the run red, never leave GCP half-deployed. Its "
                        "COVERAGE is every run in which the deploy job ran, whatever the "
                        "result; migrate-schema and sync-runtime-secrets mutate production "
                        "before deploy, and a run that skips deploy skips this. It has never "
                        "run on a merge as of this commit — it lands with this change."
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
            f"green. Fix: {install}. Nothing in this repository excuses this: a cloud "
            "that runs no analytics pipeline is not a finished cloud, and there is no "
            "flag, variable or registry field that makes this run exit 0."
        ]
    return [
        f"{cloud}: {ANALYTICS_STATUS_KEY}.{AVAILABLE_FIELD} is false "
        f"({REASON_FIELD}={reason!r}) — the control plane could not read its own "
        "operational-analytics outbox, which is not the same as an empty one and not "
        f"the same as not having one. Fix: {install}"
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


#: Printed under every PASSING stage (d), unconditionally.
#:
#: It used to be conditional on ``outbox_depth == 0`` — "say this only when the
#: outbox is empty" — and that was worse than useless: no storage backend in
#: this repository populates ``outbox_depth`` (both build ``OutboxFreshness``
#: with the field left ``None``), so the condition was never true against a real
#: cloud and the sentence was never printed where it mattered. The limitation it
#: describes does not depend on the depth anyway: a lag under the bound says
#: nothing is STUCK, whatever the depth, and an outbox nobody enqueues into
#: publishes the same number as one that is being drained perfectly.
DRAIN_LAG_LIMIT_NOTE = (
    "a lag under the bound proves nothing is STUCK; it does not prove rows are moving. "
    "An empty outbox and a switched-off one publish the same number. Rows observed "
    "moving is the bar — see stage (e) and, in-cloud, "
    "SELECT count() FROM activity_generations twice, ten minutes apart."
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
    script = entry.control_plane_script
    if not ((root or REPO_ROOT) / script).is_file():
        # Not the same finding as "the script does not set the variable", and
        # the fix is not the same either. Saying "SCRIPT never sets X" about a
        # file that is not there sends the reader to open it, and it teaches
        # them the gate is confused — which is how a gate stops being read.
        return [
            f"{cloud}: {script} — named in ROLLOUT_REGISTRY as this cloud's control-plane "
            "script, the source of truth for its service environment — DOES NOT EXIST in "
            "this checkout, so nothing here can say whether the cloud enqueues anything "
            "at all. Fix: correct control_plane_script on this cloud's CloudRollout in "
            "src/trusted_router/cloud_rollout_completeness.py, or write the script."
        ]
    value = declared_outbox_value(cloud, root=root)
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

    Printed verbatim under the outcome, and it changes nothing: the stage passed
    or it did not. It exists because "control-plane outbox is enabled"
    overstates what was done in a way worth one clause: this is a static read of
    a file in the WORKING TREE, not
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
    """The extra line a stage (e) that passes on a COMPUTED value needs.

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
# THE OUTPUT CONTRACT, which is now as small as it can be.
#
# A stage HELD if this process exits 0. That is the whole contract. There is no
# verdict word to parse, no kind, no sentinel, and nothing printed by anything
# at all — an interpreter warning, a pip notice, a library writing to stdout on
# import — can turn a non-zero exit into a pass.
#
# It used to be bigger. The shell captured this process with `2>&1` and
# classified the result by its FIRST WORD; the fix for that was a tab-separated
# sentinel line carrying one of eight `kind` values, each of which the shell
# turned into a different outcome. Two review rounds then found bugs IN THE
# TAXONOMY, including one where the exemption kind could not be produced at all
# and one where the flat green banner was reachable on evidence the module says
# can never earn it. Classification whose misclassification can upgrade a
# verdict has to earn its keep, and this one did not.
#
# So:
#
#   exit status  0 = this stage held. Anything else = it did not.
#   stderr       the human sentences: what failed and what to do about it.
#   stdout       zero or more PLAIN lines the shell prints verbatim under the
#                outcome, and one special case, `status-url`, whose single line
#                IS the answer the shell asked for.
#
# Stdout carries no authority: an extra line is an extra note under the banner,
# never a different verdict.
# ---------------------------------------------------------------------------

#: The one non-zero exit that means something other than "this cloud is broken":
#: the status page parses and carries no ``analytics`` section at all, so the
#: question cannot be asked from outside yet. Every cloud is in this state until
#: a control plane built from a commit that publishes the section is deployed to
#: it, and the run that INSTALLS a drain hits it by construction. Reporting that
#: as "your install failed" is how an operator learns to stop reading exit
#: codes. It is still a failure: a cloud nobody can see is not a finished cloud.
EXIT_NOT_OBSERVABLE = 5


def _fail(blockers: list[str]) -> int:
    """Every blocker to stderr, verbatim; the process is the verdict."""
    for line in blockers:
        print(line, file=sys.stderr)
    return 1


def _pass(notes: list[str] | None = None) -> int:
    """Notes to stdout for the shell to print under the outcome. Exit 0."""
    for note in notes or []:
        print(" ".join(note.split()))
    return 0


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
    # No --max-lag-seconds and no --status-url. The bound is
    # DEFAULT_MAX_DRAIN_LAG_SECONDS and the URL is the fleet registry's; a gate
    # with a knob for either is a gate with a way to pass a cloud that failed.
    for name in ("section", "available", "lag"):
        child = sub.add_parser(name)
        child.add_argument("--cloud", required=True)
        child.add_argument("--status-file", required=True)
    sub.add_parser("audit")
    sub.add_parser("clouds")

    args = parser.parse_args(argv)

    if args.command == "audit":
        gaps = registry_gaps() + script_binding_gaps()
        return _fail(gaps) if gaps else _pass()
    if args.command == "clouds":
        return _pass([",".join(declared_clouds())])
    if args.command == "registry":
        blockers = registry_blockers(args.cloud)
        return _fail(blockers) if blockers else _pass()
    if args.command == "status-url":
        blockers = registry_blockers(args.cloud)
        if blockers:
            return _fail(blockers)
        # The one subcommand whose stdout the shell CONSUMES rather than
        # reprints. It still carries no authority: the shell refuses anything
        # that is not an https:// URL, and a refusal is a failure.
        return _pass([freshness_registry()[args.cloud]])
    if args.command == "outbox":
        blockers = outbox_enabled_blockers(args.cloud)
        if blockers:
            return _fail(blockers)
        notes = [outbox_fact(args.cloud)]
        note = outbox_note(args.cloud)
        if note:
            notes.append(note)
        return _pass(notes)

    try:
        payload = _load(args.status_file)
    except UnreadableStatusPage as exc:
        # NOT the "publishes no analytics section" state, and deploying a newer
        # control plane does nothing for it, so it does not get that state's
        # exit code. It is a plain failure with its own sentence.
        return _fail(
            [
                f"{args.cloud}: {exc}. This is not 'the cloud publishes no analytics "
                "section' — a CDN interstitial, a captive portal or a truncated body is "
                "not an older control plane, and redeploying will not change it. Fetch "
                "the URL by hand and look at what came back."
            ]
        )

    if args.command == "section":
        blockers = section_blockers(args.cloud, payload)
        if blockers:
            for line in blockers:
                print(line, file=sys.stderr)
            return EXIT_NOT_OBSERVABLE
        return _pass()

    if args.command == "available":
        blockers = available_blockers(args.cloud, payload)
        return _fail(blockers) if blockers else _pass()

    blockers = drain_lag_blockers(args.cloud, payload, now=dt.datetime.now(dt.UTC))
    return _fail(blockers) if blockers else _pass([DRAIN_LAG_LIMIT_NOTE])


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
