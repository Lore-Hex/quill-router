"""The workflow tiers (infra/gcp_wif.tf) and their accounts (infra/ci_identities.tf).

Every workflow that reaches Google Cloud exchanges its GitHub token at one
workload identity provider. The provider admits a fixed list of workflows and
sorts each into a tier, and a service account can be impersonated by one tier
only. What a compromised or mistaken workflow can do is then decided by four
things that live in different files and can drift apart without any of them
looking wrong: the list that admits it, the expression that names its tier,
the account its jobs ask for, and what that account was granted. These tests
hold them to each other.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from tests import wif_tiers
from tests.test_spanner_schema_audit_workflow import _gcp_wif_identities

ROOT = Path(__file__).resolve().parents[1]
GCP_WIF = ROOT / "infra/gcp_wif.tf"
CI_IDENTITIES = ROOT / "infra/ci_identities.tf"
GATEWAY_ROLLOUT_ROLE = ROOT / "infra/aws_gateway_rollout_role.tf"
REFRESH_WORKFLOW = ROOT / ".github/workflows/refresh-prices.yml"
# The attribute condition with every list of workflow refs emptied. Apart from
# the lists, this one string is everything that decides who may exchange a
# token here at all: the owner and both repositories by name and by numeric id
# (a renamed or re-registered name keeps the name, not the id), main only, and
# nothing joined by "or" beside the two repository clauses.
CONDITION_SHAPE = (
    "assertion.repository_owner == 'Lore-Hex' "
    "&& assertion.repository_owner_id == '279148885' "
    "&& assertion.ref == 'refs/heads/main' "
    "&& ( "
    "(assertion.repository == 'Lore-Hex/quill-router' "
    "&& assertion.repository_id == '1227431177' "
    "&& assertion.workflow_ref in []) "
    "|| "
    "(assertion.repository == 'Lore-Hex/quill-cloud-proxy' "
    "&& assertion.repository_id == '1223401116' "
    "&& assertion.workflow_ref in []) "
    ")"
)
# What the provider maps from a GitHub token. google.subject is what the
# batch release account's binding names; attribute.tier is what every other
# account's binding names.
ATTRIBUTE_MAPPING = {
    "google.subject": '"assertion.sub"',
    "attribute.actor": '"assertion.actor"',
    "attribute.ref": '"assertion.ref"',
    "attribute.repository": '"assertion.repository"',
    "attribute.repository_owner": '"assertion.repository_owner"',
    "attribute.tier": "local.github_tier_mapping",
}
# The verbs of a permission that reads configuration and nothing else. Not a
# prefix test: iam.serviceAccounts.getAccessToken also begins with "get".
READ_VERBS = {"get", "list", "getIamPolicy"}
# Permissions with a reading verb that return content, not configuration.
CONTENT_PERMISSION_PREFIXES = (
    "logging.logEntries.",
    "logging.privateLogEntries.",
    "secretmanager.versions.",
    "spanner.databases.",
    "storage.objects.",
)


def _render_ref(ref: str) -> str:
    return ref.replace("${local.github_owner}", wif_tiers.constants(ROOT)["github_owner"])


def _resource_blocks(path: Path, kind: str) -> dict[str, str]:
    """{name: body} of every top-level resource of one type in a Terraform file."""
    return dict(re.findall(rf'^resource "{kind}" "(\w+)" \{{\n(.*?)^\}}', path.read_text(), re.MULTILINE | re.DOTALL))


def test_condition_has_the_reviewed_shape_and_the_provider_is_wired_to_it() -> None:
    """The lists are held by the tests below. This holds everything around
    them: a condition of "true", one more "or" clause, or a missing branch
    check would each admit workflows that no list names."""
    condition = wif_tiers.rendered(ROOT, "github_attribute_condition")
    assert re.sub(r"\[[^\]]*\]", "[]", condition) == CONDITION_SHAPE
    # One provider in this root, and it uses that condition and the tier
    # expression as written. A second provider in the same pool would mint
    # the same tiers under a condition of its own.
    providers = {
        (path.name, name): body
        for path in sorted((ROOT / "infra").glob("*.tf"))
        for name, body in _resource_blocks(path, "google_iam_workload_identity_pool_provider").items()
    }
    assert list(providers) == [("gcp_wif.tf", "github")]
    provider = providers[("gcp_wif.tf", "github")]
    assert re.findall(r"^  attribute_condition\s*=\s*(.+)$", provider, re.MULTILINE) == ["local.github_attribute_condition"]
    mapping = re.search(r"^  attribute_mapping = \{\n(.*?)^  \}$", provider, re.MULTILINE | re.DOTALL)
    assert mapping is not None
    assert dict(re.findall(r'^\s*"([\w.]+)"\s*=\s*(.+)$', mapping[1], re.MULTILINE)) == ATTRIBUTE_MAPPING
    assert len(mapping[1].strip().splitlines()) == len(ATTRIBUTE_MAPPING)
    assert 'workload_identity_pool_id          = "github-actions"' in provider
    assert 'issuer_uri = "https://token.actions.githubusercontent.com"' in provider


def test_condition_admits_exactly_the_union_of_the_tier_lists() -> None:
    condition = wif_tiers.rendered(ROOT, "github_attribute_condition")
    clauses = dict(
        re.findall(
            r"\(assertion\.repository == '[^']+/([\w-]+)' && assertion\.repository_id == '\d+' "
            r"&& assertion\.workflow_ref in \[([^\]]*)\]\)",
            condition,
        )
    )
    assert set(clauses) == set(wif_tiers.REPOSITORIES.values())
    admitted_anywhere = re.findall(r"'([^']+@refs/heads/main)'", condition)
    for repository, tiers in wif_tiers.tier_refs(ROOT).items():
        union = [_render_ref(ref) for refs in tiers.values() for ref in refs]
        admitted = re.findall(r"'([^']*)'", clauses[repository])
        # admitted_refs also fails unless the condition's list is written as
        # the concatenation of this repository's tier lists and nothing else.
        assert admitted == [_render_ref(ref) for ref in wif_tiers.admitted_refs(ROOT, repository)]
        assert sorted(admitted) == sorted(union), repository
        assert all(ref.startswith(f"Lore-Hex/{repository}/.github/workflows/") for ref in admitted)
    # No ref is admitted outside the two repository clauses.
    assert len(admitted_anywhere) == sum(len(re.findall(r"'([^']*)'", refs)) for refs in clauses.values())


def test_no_workflow_is_in_two_tiers() -> None:
    for repository in wif_tiers.REPOSITORIES.values():
        tiers = wif_tiers.tier_of_workflows(ROOT, repository)
        assert tiers, repository
        overlapping = {path: found for path, found in tiers.items() if len(found) != 1}
        assert not overlapping, f"{repository}: a workflow must be in exactly one tier list: {overlapping}"


def test_every_listed_workflow_of_this_repository_exists() -> None:
    # A misspelt ref admits nothing: the workflow it meant fails to authenticate.
    for path in wif_tiers.tier_of_workflows(ROOT, "quill-router"):
        assert (ROOT / path).is_file(), f"infra/gcp_wif.tf lists {path}, which does not exist"


def test_every_tier_has_one_account_and_one_principal_set() -> None:
    accounts = wif_tiers.tier_accounts(ROOT)
    tiers = {tier for by_tier in wif_tiers.tier_refs(ROOT).values() for tier in by_tier}
    assert tiers == set(accounts)
    assert len(set(accounts.values())) == len(accounts), "Two tiers must not share an account"
    terraform = (ROOT / "infra/gcp_wif.tf").read_text()
    principal_tiers = re.search(r"for tier in \[([^\]]*)\] :\n\s*tier => \"principalSet://[^\"]*/attribute\.tier/\$\{tier\}\"", terraform)
    assert principal_tiers is not None
    assert set(re.findall(r'"(\w+)"', principal_tiers[1])) == tiers
    # One additive member per tier account, and no other way in.
    identities = CI_IDENTITIES.read_text()
    assert "for_each = local.ci_tier_accounts" in identities
    assert "member             = local.github_tier_principal_sets[each.key]" in identities
    assert identities.count("principalSet://") == 0 and identities.count('"roles/iam.workloadIdentityUser"') == 1


def test_tier_mapping_gives_every_admitted_workflow_its_tier() -> None:
    mapping = wif_tiers.rendered(ROOT, "github_tier_mapping")
    named = set(re.findall(r"\? '(\w+)' :", mapping))
    tiers = wif_tiers.tier_refs(ROOT)
    small = {tier for by_tier in tiers.values() for tier in by_tier} - {wif_tiers.DEPLOY_TIER}
    # The expression names each small tier once and no deploy workflow at all:
    # a workflow is deploy by being admitted and in no small list.
    assert named == small and mapping.count("? '") == len(small)
    for by_tier in tiers.values():
        for tier, refs in by_tier.items():
            assert refs, tier
            for ref in refs:
                assert wif_tiers.tier_of_ref(mapping, _render_ref(ref)) == tier, ref
                assert (_render_ref(ref) in mapping) == (tier != wif_tiers.DEPLOY_TIER), ref
    # The default. It is safe only because the condition refuses whatever is
    # in no list, which test_condition_admits_exactly_the_union_of_the_tier_lists holds.
    unlisted = "Lore-Hex/quill-router/.github/workflows/unlisted.yml@refs/heads/main"
    assert wif_tiers.tier_of_ref(mapping, unlisted) == wif_tiers.DEPLOY_TIER
    assert unlisted not in wif_tiers.rendered(ROOT, "github_attribute_condition")


def test_rendered_expressions_fit_the_provider_limits() -> None:
    condition = wif_tiers.rendered(ROOT, "github_attribute_condition")
    mapping = wif_tiers.rendered(ROOT, "github_tier_mapping")
    assert len(condition) <= wif_tiers.ATTRIBUTE_CONDITION_LIMIT, len(condition)
    assert len(mapping) <= wif_tiers.MAPPING_EXPRESSION_LIMIT, len(mapping)


def test_every_authenticating_workflow_is_in_a_tier_and_names_an_account_it_may_use() -> None:
    """A workflow runs as its tier's account. One that has not moved to its
    tier's account yet runs as the deploy identity, and as nothing else: it
    never names another tier's account, which the provider would refuse."""
    accounts = wif_tiers.tier_accounts(ROOT)
    tiers = wif_tiers.tier_of_workflows(ROOT, "quill-router")
    identities = _gcp_wif_identities(ROOT)
    assert identities
    for path, used in sorted(identities.items()):
        assert path in tiers, f"{path} authenticates to Google Cloud and is in no tier list of infra/gcp_wif.tf"
        (tier,) = tiers[path]
        allowed = {accounts[tier], accounts[wif_tiers.DEPLOY_TIER]}
        assert used <= allowed, f"{path} is in the {tier} tier and names {sorted(map(str, used - allowed))}"


def _workflow_secret_reads() -> tuple[list[str], list[str]]:
    """(every secret refresh-prices.yml reads, those it describes first)."""
    text = REFRESH_WORKFLOW.read_text()
    workflow = yaml.safe_load(text)
    read: list[str] = []
    described: list[str] = []
    commands = 0
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            script = step.get("run", "")
            if "gcloud secrets" not in script:
                continue
            commands += script.count("gcloud secrets")
            names = re.findall(r"\btrustedrouter-[a-z0-9-]+\b", script)
            # A secret is named literally, or through ${secret}, which the
            # script sets from one of the names above. Any other form could
            # read a secret this function does not see.
            for argument in re.findall(r"--secret=(\S+)", script):
                assert argument in {*names, '"${secret}"'}, f"Unrecognized --secret argument in {step['name']}: {argument}"
            if "gcloud secrets describe" in script:
                assert re.findall(r'gcloud secrets describe (\S+)', script) == ['"${secret}"'], step["name"]
                described += re.findall(r"^\s*secret=(trustedrouter-[a-z0-9-]+)$", script, re.MULTILINE)
            read += names
    # Every use of Secret Manager in the file is in a script scanned above.
    assert commands == text.count("gcloud secrets") > 0
    return read, described


def test_prices_secret_list_matches_the_workflow() -> None:
    lists = wif_tiers.string_lists(CI_IDENTITIES)
    granted, granted_described = lists["ci_prices_secret_ids"], lists["ci_prices_described_secret_ids"]
    read, described = _workflow_secret_reads()
    assert len(granted) == len(set(granted)) and len(read) == len(set(read))
    assert set(granted) == set(read), (
        "infra/ci_identities.tf ci_prices_secret_ids and the secrets refresh-prices.yml reads differ: "
        f"only granted {sorted(set(granted) - set(read))}, only read {sorted(set(read) - set(granted))}"
    )
    assert described and sorted(granted_described) == sorted(described)
    assert set(granted_described) <= set(granted)
    identities = CI_IDENTITIES.read_text()
    assert "for_each = toset(local.ci_prices_secret_ids)" in identities
    assert "for_each = toset(local.ci_prices_described_secret_ids)" in identities


def test_probe_project_role_reads_configuration_only() -> None:
    permissions = wif_tiers.string_lists(CI_IDENTITIES)["ci_probe_project_permissions"]
    assert permissions == sorted(set(permissions))
    for permission in permissions:
        assert permission.rsplit(".", 1)[1] in READ_VERBS, f"{permission} is not a read of configuration"
        assert not permission.startswith(CONTENT_PERMISSION_PREFIXES), f"{permission} reads content"


# Each gcloud read that cloud-security-baseline.yml makes as tr-ci-probe,
# keyed by its leading command words, and the project permission it needs.
# The job runs scripts/deploy/gcp_security_baseline_check.sh, which runs
# scripts/deploy/public_surface_edge.sh check, which sources _lib.sh and
# _active_revision.sh.
PROBE_JOB_READS = {
    "alpha monitoring channels list": "monitoring.notificationChannels.list",
    "alpha monitoring policies list": "monitoring.alertPolicies.list",
    "compute backend-services describe": "compute.backendServices.get",
    "compute firewall-rules describe": "compute.firewalls.get",
    "compute firewall-rules list": "compute.firewalls.list",
    "essential-contacts compute": "essentialcontacts.contacts.list",
    "iam service-accounts keys list": "iam.serviceAccountKeys.list",
    "logging metrics describe": "logging.logMetrics.get",
    "projects describe": "resourcemanager.projects.get",
    "projects get-iam-policy": "resourcemanager.projects.getIamPolicy",
    "run revisions describe": "run.revisions.get",
    "run services describe": "run.services.get",
    # Section 8 of the baseline: every service's ingress, every region.
    "run services list": "run.services.list",
}
# Reads the project role does not need to cover, each with its reason.
PROBE_JOB_EXEMPT_READS = {
    # The ACME cache and archive buckets: tr-ci-probe's access to them is
    # granted on each bucket, not by the project role.
    "storage buckets describe",
}
_SHELL_FUNCTION = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\(\)\s*\{", re.MULTILINE)
# Where a shell command can start, so that a word inside a message
# ("gcloud returned invalid JSON", "before cutover") is not read as a call.
_COMMAND_POSITION = r"(?:^|[;|&{`]|\$\(|\b(?:if|then|do|else|elif|while|until)\b|!)[ \t]*"
_GCLOUD_CALL = re.compile(_COMMAND_POSITION + r"(?:gcloud|gc|g)[ \t]+([^\n]*)", re.MULTILINE)
_READ_PROJECT_JSON_CALL = re.compile(
    _COMMAND_POSITION + r'read_project_json[ \t]+\w+[ \t]+"[^"]*"[ \t]+([^\n]*)', re.MULTILINE
)
_COMMAND_WORD = re.compile(r"[a-z][a-z0-9-]*")


def _shell_code(path: Path) -> str:
    """The script with `\\` continuations joined and comment lines dropped."""
    joined = re.sub(r"\\\n[ \t]*", " ", path.read_text())
    return "\n".join(line for line in joined.splitlines() if not line.lstrip().startswith("#"))


def _shell_functions(code: str) -> tuple[dict[str, str], str]:
    """Each top-level function's body, and the code outside every function."""
    functions: dict[str, str] = {}
    outside: list[str] = []
    lines = code.splitlines()
    index = 0
    while index < len(lines):
        match = _SHELL_FUNCTION.match(lines[index])
        if not match:
            outside.append(lines[index])
            index += 1
            continue
        if lines[index].rstrip().endswith("}"):
            functions[match.group(1)] = lines[index]
            index += 1
            continue
        end = lines.index("}", index)
        functions[match.group(1)] = "\n".join(lines[index:end + 1])
        index = end + 1
    return functions, "\n".join(outside)


def _reachable_code(functions: dict[str, str], roots: str) -> str:
    """The roots and every function they reach, by name, transitively."""
    reached: list[str] = [roots]
    seen: set[str] = set()
    pending = [roots]
    while pending:
        text = pending.pop()
        for name, body in functions.items():
            if name in seen:
                continue
            if re.search(
                _COMMAND_POSITION + re.escape(name) + r"(?=[\s;|&)]|$)", text, re.MULTILINE
            ):
                seen.add(name)
                reached.append(body)
                pending.append(body)
    return "\n".join(reached)


def _gcloud_reads(code: str) -> list[str]:
    """The leading command words of every gcloud call in the code. A call the
    scan cannot read is a failure, never skipped, except the wrappers that
    pass "$@" through."""
    reads: list[str] = []
    rests = [m.group(1) for m in _GCLOUD_CALL.finditer(code)]
    rests += [m.group(1) for m in _READ_PROJECT_JSON_CALL.finditer(code)]
    for rest in rests:
        tokens = rest.split()
        while tokens and tokens[0].startswith("-"):
            flag = tokens.pop(0)
            if "=" not in flag and tokens:
                tokens.pop(0)
        words: list[str] = []
        for token in tokens:
            # `list; then` and `list)"` end the command at the punctuation.
            word = token.rstrip(';|&)"')
            if not _COMMAND_WORD.fullmatch(word):
                break
            words.append(word)
            if word != token:
                break
        if words:
            reads.append(" ".join(words))
        else:
            assert tokens and tokens[0].startswith('"$@"'), f"cannot read the gcloud call: {rest!r}"
    return reads


def _probe_job_reads() -> list[str]:
    deploy = ROOT / "scripts/deploy"
    baseline = _shell_code(deploy / "gcp_security_baseline_check.sh")
    assert 'public_surface_edge.sh" check' in baseline
    # public_surface_edge.sh check: its top level (which sources _lib.sh and,
    # through it, _active_revision.sh) and check_client_ip_trust, never the
    # prepare, cutover or rollback branches of its two command dispatches.
    functions: dict[str, str] = {}
    top_level: list[str] = []
    for name in ("public_surface_edge.sh", "_lib.sh", "_active_revision.sh"):
        bodies, outside = _shell_functions(_shell_code(deploy / name))
        functions.update(bodies)
        top_level.append(re.sub(r'case "\$COMMAND" in\n.*?\nesac', "", outside, flags=re.DOTALL))
    edge = _reachable_code(functions, "\n".join([*top_level, "check_client_ip_trust"]))
    return _gcloud_reads(baseline) + _gcloud_reads(edge)


def _covering_key(read: str, keys: set[str]) -> str | None:
    words = read.split()
    return next((key for key in keys if words[: len(key.split())] == key.split()), None)


def test_probe_project_role_covers_every_read_of_the_security_baseline_job() -> None:
    """cloud-security-baseline.yml runs as tr-ci-probe. A read its role lacks
    prints PERMISSION_DENIED every day: the check fails closed, but sees
    nothing. Every gcloud read the job's scripts make must be in the table with
    its permission granted, or on the exempt list, and every table entry must
    still be read, so neither side can drift from the other."""
    permissions = set(wif_tiers.string_lists(CI_IDENTITIES)["ci_probe_project_permissions"])
    reads = _probe_job_reads()
    used: set[str] = set()
    for read in reads:
        key = _covering_key(read, set(PROBE_JOB_READS) | PROBE_JOB_EXEMPT_READS)
        assert key is not None, f"`{read}` is neither in PROBE_JOB_READS nor exempt"
        used.add(key)
        if key in PROBE_JOB_READS:
            permission = PROBE_JOB_READS[key]
            assert permission in permissions, f"tr-ci-probe cannot `{read}`: add {permission}"
    unused = (set(PROBE_JOB_READS) | PROBE_JOB_EXEMPT_READS) - used
    assert not unused, f"no longer read by the job: {sorted(unused)}"


def test_only_the_audit_account_reads_the_database() -> None:
    """typed-audit.yml reads every table. It has a tier and an account to
    itself so that no other moved workflow can."""
    grants = _resource_blocks(CI_IDENTITIES, "google_spanner_database_iam_member")
    assert list(grants) == ["ci_audit_database_reader"]
    grant = grants["ci_audit_database_reader"]
    assert 'role     = "roles/spanner.databaseReader"' in grant
    assert 'member   = google_service_account.ci_tier["audit"].member' in grant
    identities = CI_IDENTITIES.read_text()
    assert not re.search(r'^resource "google_spanner_(?!database_iam_member")', identities, re.MULTILINE)
    # The audit account holds that one grant and nothing else.
    assert identities.count('google_service_account.ci_tier["audit"].member') == 1
    audit = wif_tiers.tier_refs(ROOT)["quill-router"]["audit"]
    assert [wif_tiers.workflow_path(ref, "quill-router") for ref in audit] == [".github/workflows/typed-audit.yml"]


def test_prices_project_role_only_calls_publisher_models() -> None:
    assert wif_tiers.string_lists(CI_IDENTITIES)["ci_prices_project_permissions"] == ["aiplatform.endpoints.predict"]


def test_owner_only_resources_are_exactly_what_the_targeted_apply_creates() -> None:
    """infra-apply cannot write project IAM or create a custom role. Whatever
    in this file needs an owner must be reached by the one command in its
    header, or the merge's apply fails on the resource that was left out."""
    identities = CI_IDENTITIES.read_text()
    resources = set(re.findall(r'^resource "(\w+)" "(\w+)"', identities, re.MULTILINE))
    project_level = {resource for resource in resources if resource[0].startswith(("google_project_", "google_organization_", "google_folder_"))}
    assert project_level == {("google_project_iam_custom_role", "ci_tier"), ("google_project_iam_member", "ci_tier")}
    assert "#   terraform -chdir=infra apply -target=google_project_iam_member.ci_tier\n" in identities
    # The targeted binding reaches the roles and the accounts as dependencies.
    binding = identities[identities.index('resource "google_project_iam_member" "ci_tier"') :].split("\n}\n", 1)[0]
    assert "for_each = google_project_iam_custom_role.ci_tier" in binding
    assert "member  = google_service_account.ci_tier[each.key].member" in binding
    # Every other grant is on one resource and additive: an authoritative
    # binding would remove the production readers of the same secret.
    iam = {kind for kind, _ in resources if "_iam_" in kind}
    assert all(kind.endswith("_iam_member") or kind == "google_project_iam_custom_role" for kind in iam), iam
    code = "\n".join(line for line in identities.splitlines() if not line.lstrip().startswith("#"))
    assert not re.search(r'"roles/(viewer|editor|owner)"', code)


def test_gateway_rollout_role_owner_apply_reaches_everything_ci_cannot() -> None:
    """infra-apply's AWS role cannot make an IAM role, and its plan cannot even
    refresh one that its own policy does not list. The one command in the
    role file's header must therefore reach the role, its inline policy and
    the deploy role's policy that carries the read; a target left out fails
    the merge's plan after the owner apply, not before it."""
    rollout = GATEWAY_ROLLOUT_ROLE.read_text()
    resources = set(re.findall(r'^resource "(\w+)" "(\w+)"', rollout, re.MULTILINE))
    assert resources == {
        ("aws_iam_role", "trustedrouter_github_gateway_rollout"),
        ("aws_iam_role_policy", "trustedrouter_github_gateway_rollout"),
    }
    header = rollout[: rollout.index("\nlocals {")]
    assert all(line.startswith("#") for line in header.splitlines())
    targets = re.findall(r"-target=(\w+)\.(\w+)", header)
    assert sorted(targets) == sorted([*resources, ("aws_iam_role_policy", "tr_eu_role_writes")])
    assert len(targets) == len(set(targets))

    deploy = (ROOT / "infra/aws_deploy_role.tf").read_text()
    policy = deploy[deploy.index('resource "aws_iam_role_policy" "tr_eu_role_writes"') :]
    read = policy[policy.index('Sid    = "ReadGitHubOidcRoles"') :].split("\n      },\n", 1)[0]
    assert re.findall(r'"(iam:\w+)"', read) == [
        "iam:GetRole",
        "iam:GetRolePolicy",
        "iam:ListRolePolicies",
        "iam:ListAttachedRolePolicies",
    ]
    assert '"arn:aws:iam::${local.aws_account_id}:role/trustedrouter-github-gateway-rollout"' in read


def test_gateway_rollout_role_is_assumable_by_its_one_workflow_only() -> None:
    rollout = GATEWAY_ROLLOUT_ROLE.read_text()
    ref = '"${local.github_owner}/quill-cloud-proxy/.github/workflows/rollout-aws-gateway.yml@refs/heads/main"'
    assert f"gateway_rollout_workflow_ref = {ref}" in rollout
    trust = rollout[rollout.index("assume_role_policy = jsonencode(") :].split("\n  })\n", 1)[0]
    assert trust.count("Effect") == 1 and "StringLike" not in trust and "*" not in trust
    assert 'Action    = "sts:AssumeRoleWithWebIdentity"' in trust
    assert (
        'StringEquals = merge(local.github_oidc_trust_conditions["quill-cloud-proxy"], {\n'
        '          "token.actions.githubusercontent.com:job_workflow_ref" = local.gateway_rollout_workflow_ref\n'
        "        })"
    ) in trust
    # The same workflow is the only AWS gateway entry the Google provider admits.
    deploy_tier = wif_tiers.tier_refs(ROOT)["quill-cloud-proxy"]["deploy"]
    assert [entry for entry in deploy_tier if "rollout-aws-gateway" in entry] == [ref.strip('"')]


def test_gateway_rollout_role_has_no_key_secret_or_identity_write() -> None:
    """The statements that would let the rollout admit its own measurement,
    read a bundle or widen itself are absent, and the session data channel is
    bound to sessions the same principal started."""
    rollout = GATEWAY_ROLLOUT_ROLE.read_text()
    code = "\n".join(line for line in rollout.splitlines() if not line.lstrip().startswith("#"))
    policy = code.split("\n  policy = jsonencode(", 1)[1]
    lists = re.findall(r"^\s+Action\s*=\s*\[(.*?)\]", policy, re.MULTILINE | re.DOTALL)
    assert len(lists) == policy.count("Effect ") == policy.count("Sid ")
    actions = {action for body in lists for action in re.findall(r'"([^"]+)"', body)}
    assert {action for action in actions if action.startswith("kms:")} == {"kms:DescribeKey", "kms:GetKeyPolicy"}
    assert {action for action in actions if action.startswith("iam:")} == {"iam:PassRole", "iam:GetInstanceProfile"}
    assert not {action for action in actions if action.startswith(("secretsmanager:", "route53:", "sts:"))}
    assert not {action for action in actions if "*" in action}
    assert "autoscaling:UpdateAutoScalingGroup" not in actions and "ec2:ModifyLaunchTemplate" not in actions
    channel = code[code.index('Sid      = "OpenOwnSessionDataChannel"') :].split("\n      },\n", 1)[0]
    assert 'Action   = ["ssmmessages:OpenDataChannel"]' in channel
    assert ':session/*"]' in channel
    assert 'StringEquals = { "ssm:resourceTag/aws:ssmmessages:session-id" = "$${aws:userid}" }' in channel
