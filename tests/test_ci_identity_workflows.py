"""Each workflow authenticates as the account of its tier, and as no other.

tests/test_ci_identities.py holds the tiers and their accounts to each other.
This holds the workflows to them exactly: a workflow in the probe or prices
tier that still asked for the deploy identity would keep working for as long
as the deploy identity's repository-wide bindings exist, with the reach of a
deploy, and nothing else would say so.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from tests import wif_tiers
from tests.test_spanner_schema_audit_workflow import _gcp_wif_identities

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github/workflows"


def test_every_workflow_names_exactly_the_account_of_its_tier() -> None:
    accounts = wif_tiers.tier_accounts(ROOT)
    tiers = wif_tiers.tier_of_workflows(ROOT, "quill-router")
    identities = _gcp_wif_identities(ROOT)
    for path, used in sorted(identities.items()):
        (tier,) = tiers[path]
        assert used == {accounts[tier]}, f"{path} is in the {tier} tier, whose account is {accounts[tier]}, and names {used}"
    # Every small tier is in use: a tier whose workflows all moved away should
    # lose its list, its account and its grants together.
    used_tiers = {tiers[path][0] for path in identities}
    assert {tier for found in tiers.values() for tier in found} == used_tiers


def test_only_deploy_workflows_mention_the_deploy_identity() -> None:
    """Text, not only the authentication step: a script that impersonates
    the deploy identity by flag would not show in the step's inputs."""
    deploy = wif_tiers.tier_accounts(ROOT)[wif_tiers.DEPLOY_TIER]
    tiers = wif_tiers.tier_of_workflows(ROOT, "quill-router")
    # A workflow that only other workflows call runs under its caller's ref,
    # so it is in no list itself; it may be called from the deploy tier alone.
    callers: dict[str, set[str]] = {}
    for workflow in sorted(WORKFLOWS.glob("*.y*ml")):
        for job in (yaml.safe_load(workflow.read_text()).get("jobs") or {}).values():
            uses = job.get("uses")
            if isinstance(uses, str) and uses.startswith("./.github/workflows/"):
                callers.setdefault(uses[2:], set()).add(workflow.relative_to(ROOT).as_posix())
    mentioning = [
        workflow.relative_to(ROOT).as_posix()
        for workflow in sorted(WORKFLOWS.glob("*.y*ml"))
        if deploy in workflow.read_text()
    ]
    assert mentioning
    for path in mentioning:
        listed = tiers.get(path)
        if listed is None:
            assert callers.get(path), f"{path} names the deploy identity and is in no tier list"
            listed = sorted({tier for caller in callers[path] for tier in tiers.get(caller, ["none"])})
        assert listed == [wif_tiers.DEPLOY_TIER], f"{path} names the deploy identity from the {listed} tier"
