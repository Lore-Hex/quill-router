"""Read the workflow tiers of infra/gcp_wif.tf and the tier accounts of
infra/ci_identities.tf, for the tests that hold workflows to them.

Nothing here runs Terraform. The lists are read as written, and the two CEL
expressions are rendered from the same lines Terraform renders them from, so
a test fails on the file's own text, not on a copy of it kept in Python. Each
reader accepts only the forms those files use today and fails on any other:
a new form must be taught here before a test can pass on it.
"""

from __future__ import annotations

import re
from pathlib import Path

REPOSITORIES = {"quill_router": "quill-router", "quill_cloud_proxy": "quill-cloud-proxy"}
DEPLOY_TIER = "deploy"
# Google's limits on a workload identity pool provider
# (iam.googleapis.com projects.locations.workloadIdentityPools.providers).
ATTRIBUTE_CONDITION_LIMIT = 4096
MAPPING_EXPRESSION_LIMIT = 2048

_STRING_LIST = re.compile(r"^  (\w+)\s*=\s*\[\n(.*?)^  \]", re.MULTILINE | re.DOTALL)
_STRING_ITEM = re.compile(r'\s*"([^"\n]+)",?\s*(?:#.*)?')
_UNION = re.compile(r"^  (\w+)\s*=\s*sort\(concat\(\n((?:\s*local\.\w+,\n)+)\s*\)\)$", re.MULTILINE)
_JOINED = re.compile(r'^  (\w+)\s*=\s*join\(" ", \[\n(.*?)^  \]\)$', re.MULTILINE | re.DOTALL)
_STRING_MAP = re.compile(r"^  (\w+)\s*=\s*\{\n((?:\s*\w+\s*=\s*\"[^\"\n]+\"\n)+)\s*\}$", re.MULTILINE)
_CONSTANT = re.compile(r'^\s*(\w+)\s*=\s*"([^"\n]*)"\s*$', re.MULTILINE)
_QUOTED_LIST = """${join(", ", formatlist("'%s'", local."""


def string_lists(path: Path) -> dict[str, list[str]]:
    """Every two-space-indented `name = [ "..." ]` list of string literals."""
    lists: dict[str, list[str]] = {}
    for name, body in _STRING_LIST.findall(path.read_text()):
        items = []
        for line in body.splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            item = _STRING_ITEM.fullmatch(line)
            assert item is not None, f"{path.name}: {name} has an entry this reader does not understand: {line!r}"
            items.append(item[1])
        assert name not in lists, f"{path.name}: {name} is defined twice"
        lists[name] = items
    return lists


def tier_refs(root: Path) -> dict[str, dict[str, list[str]]]:
    """{repository: {tier: [workflow refs as gcp_wif.tf writes them]}}."""
    tiers: dict[str, dict[str, list[str]]] = {repository: {} for repository in REPOSITORIES.values()}
    for name, refs in string_lists(root / "infra/gcp_wif.tf").items():
        match = re.fullmatch(rf"({'|'.join(REPOSITORIES)})_(\w+)_workflow_refs", name)
        if match:
            tiers[REPOSITORIES[match[1]]][match[2]] = refs
    return tiers


def workflow_path(ref: str, repository: str) -> str:
    """`.github/workflows/x.yml` of a ref written for this repository's main."""
    prefix, suffix = f"${{local.github_owner}}/{repository}/", "@refs/heads/main"
    assert ref.startswith(prefix) and ref.endswith(suffix), f"Unexpected workflow ref: {ref!r}"
    return ref[len(prefix) : -len(suffix)]


def tier_of_workflows(root: Path, repository: str) -> dict[str, list[str]]:
    """{workflow path: [every tier whose list names it]}; one tier each when the lists are sound."""
    tiers: dict[str, list[str]] = {}
    for tier, refs in tier_refs(root)[repository].items():
        for ref in refs:
            tiers.setdefault(workflow_path(ref, repository), []).append(tier)
    return tiers


def admitted_refs(root: Path, repository: str) -> list[str]:
    """The refs the attribute condition admits for a repository, as written.

    The condition reads `<repository>_workflow_refs`, which must be the sorted
    concatenation of that repository's tier lists and of nothing else.
    """
    text = (root / "infra/gcp_wif.tf").read_text()
    key = next(key for key, name in REPOSITORIES.items() if name == repository)
    unions = dict(_UNION.findall(text))
    assert f"{key}_workflow_refs" in unions, (
        f"infra/gcp_wif.tf must define {key}_workflow_refs as sort(concat(...)) of its tier lists"
    )
    parts = re.findall(r"local\.(\w+)", unions[f"{key}_workflow_refs"])
    lists = string_lists(root / "infra/gcp_wif.tf")
    tier_lists = {f"{key}_{tier}_workflow_refs" for tier in tier_refs(root)[repository]}
    assert sorted(parts) == sorted(tier_lists), (
        f"{key}_workflow_refs must concatenate exactly the tier lists {sorted(tier_lists)}, not {sorted(parts)}"
    )
    return sorted(ref for part in parts for ref in lists[part])


def constants(root: Path) -> dict[str, str]:
    """The string constants of infra/variables.tf."""
    return dict(_CONSTANT.findall((root / "infra/variables.tf").read_text()))


def rendered(root: Path, name: str) -> str:
    """A `join(" ", [...])` local of gcp_wif.tf, as Terraform renders it."""
    text = (root / "infra/gcp_wif.tf").read_text()
    joined = dict(_JOINED.findall(text))
    assert name in joined, f"infra/gcp_wif.tf has no join(\" \", [...]) local named {name}"
    values = constants(root)
    lists = string_lists(root / "infra/gcp_wif.tf")
    unions = {key for key, _ in _UNION.findall(text)}

    def scalar(match: re.Match[str]) -> str:
        return values[match[1]]

    def render_list(local: str) -> str:
        if local in unions:
            repository = REPOSITORIES[local.removesuffix("_workflow_refs")]
            refs = admitted_refs(root, repository)
        else:
            refs = lists[local]
        return ", ".join("'" + re.sub(r"\$\{local\.(\w+)\}", scalar, ref) + "'" for ref in refs)

    pieces = []
    for line in joined[name].splitlines():
        line = line.strip()
        assert line.startswith('"') and line.endswith('",') and "\\" not in line, (
            f"{name} has a line this reader does not understand: {line!r}"
        )
        piece = line[1:-2]
        while _QUOTED_LIST in piece:
            start = piece.index(_QUOTED_LIST)
            end = piece.index("))}", start)
            piece = piece[:start] + render_list(piece[start + len(_QUOTED_LIST) : end]) + piece[end + 3 :]
        piece = re.sub(r"\$\{local\.(\w+)\}", scalar, piece)
        assert "${" not in piece, f"{name} has an interpolation this reader does not understand: {line!r}"
        pieces.append(piece)
    return " ".join(pieces)


def tier_of_ref(mapping: str, ref: str) -> str:
    """Evaluate the rendered attribute.tier expression for one workflow ref.

    The expression is a chain of `assertion.workflow_ref in [...] ? 'tier' :`
    tests closed by a default, which is the only form accepted here.
    """
    tests = re.findall(r"assertion\.workflow_ref in \[([^\]]*)\] \? '(\w+)' :", mapping)
    remainder = re.sub(r"\(?assertion\.workflow_ref in \[[^\]]*\] \? '\w+' : ", "", mapping)
    default = re.fullmatch(r"'(\w+)'\)*", remainder)
    assert tests and default is not None, f"Unexpected attribute.tier expression: {mapping!r}"
    assert mapping.count("(") == mapping.count(")") == len(tests) - 1, "Unbalanced attribute.tier expression"
    for members, tier in tests:
        if ref in re.findall(r"'([^']*)'", members):
            return tier
    return default[1]


def tier_accounts(root: Path) -> dict[str, str]:
    """{tier: service account email}, from ci_tier_accounts in ci_identities.tf."""
    maps = dict(_STRING_MAP.findall((root / "infra/ci_identities.tf").read_text()))
    assert "ci_tier_accounts" in maps, "infra/ci_identities.tf must define the ci_tier_accounts map"
    project = constants(root)["gcp_project_id"]
    return {
        tier: f"{account}@{project}.iam.gserviceaccount.com"
        for tier, account in re.findall(r'(\w+)\s*=\s*"([^"\n]+)"', maps["ci_tier_accounts"])
    }
