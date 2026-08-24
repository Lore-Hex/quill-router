"""The AWS-EU archive provisioning script.

Every assertion here corresponds to a way this could go wrong *silently* --
where the archive would appear to work and something else would be quietly
false: the EU residency claim, the enclave's blast radius, or the pointer
overwrite the archive depends on.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts/deploy/aws_eu_clickhouse_archive.sh"
SCRIPT = SCRIPT_PATH.read_text()


def test_script_is_executable_and_fails_fast() -> None:
    assert SCRIPT_PATH.stat().st_mode & 0o111, "deploy scripts must be executable"
    assert "set -euo pipefail" in SCRIPT


def test_defaults_to_dry_run() -> None:
    """A provisioning script whose default mutates is one bad tab-complete from
    creating real infrastructure."""

    assert "APPLY=0" in SCRIPT
    assert '--apply) APPLY=1' in SCRIPT
    assert "dry run only; nothing was changed" in SCRIPT


# --------------------------------------------------------------------------
# Residency. The archive is in a second region for one reason.
# --------------------------------------------------------------------------


def test_archive_region_is_the_nodes_eu_region_not_the_accounts_default() -> None:
    """Every other bucket in this account is us-east-1. Following that habit
    would move EU operational history to Virginia and quietly falsify the EU
    audit claim, while the archive itself kept working."""

    region_line = [line for line in SCRIPT.splitlines() if line.startswith("REGION=")]
    assert len(region_line) == 1
    assert "eu-west-3" in region_line[0]
    assert "us-east-1" not in region_line[0]


def test_script_refuses_to_create_the_eu_archive_in_us_east_1() -> None:
    assert 'if [ "$REGION" = "us-east-1" ]' in SCRIPT
    assert "refusing to create the EU archive in us-east-1" in SCRIPT


def test_script_verifies_the_node_is_actually_in_that_region() -> None:
    """Provisioning an archive in eu-west-3 for a node that is not there would
    produce a bucket that never receives an object -- and a freshness check
    pointed at it would file a daily issue forever."""

    assert "require_node_region" in SCRIPT
    assert "refusing to provision an archive" in SCRIPT


def test_existing_bucket_in_the_wrong_region_is_refused_not_reused() -> None:
    assert "get-bucket-location" in SCRIPT
    assert "refusing to use it" in SCRIPT


# --------------------------------------------------------------------------
# The pointer overwrite. This is the subtle one.
# --------------------------------------------------------------------------


def test_object_lock_is_never_enabled() -> None:
    """The archive's immutability comes from conditional writes, NOT bucket
    policy, because put_json_pointer must overwrite _latest.json on every new
    revision. Object Lock would block that and wedge the archive after its
    first day -- and on S3, object-lock-enabled cannot be turned off."""

    lowered = SCRIPT.lower()
    assert "object-lock" not in lowered
    assert "objectlock" not in lowered
    assert "governance" not in lowered
    assert "compliance_mode" not in lowered


def test_versioning_is_the_recoverability_mechanism_instead() -> None:
    assert "put-bucket-versioning" in SCRIPT
    assert "Status=Enabled" in SCRIPT
    assert "NoncurrentVersionExpiration" in SCRIPT


def test_bucket_is_private_and_encrypted_with_sse_s3_not_kms() -> None:
    """SSE-KMS PutObject needs kms:GenerateDataKey, which the node's role does
    not have and should not need. SSE-KMS would fail every archive write."""

    assert "put-public-access-block" in SCRIPT
    assert "BlockPublicAcls=true" in SCRIPT
    assert "RestrictPublicBuckets=true" in SCRIPT
    assert "AES256" in SCRIPT
    assert "aws:kms" not in SCRIPT.split("put-bucket-encryption")[1].split("\n\n")[0]


# --------------------------------------------------------------------------
# Identity separation and least privilege.
# --------------------------------------------------------------------------


def test_a_dedicated_role_is_created_rather_than_reusing_the_enclave_role() -> None:
    """tr-eu-clickhouse-1 shares quill-enclave-instance-profile with five
    attested-gateway instances. Granting the archive bucket to that role would
    hand s3:PutObject to the enclave."""

    assert "tr-eu-clickhouse-role" in SCRIPT
    assert "tr-eu-clickhouse-instance-profile" in SCRIPT
    # The enclave role must never be the grant target.
    grant_section = SCRIPT.split("write-clickhouse-archive")[1]
    assert "quill-enclave-role" not in grant_section


def test_the_archive_grant_is_scoped_to_one_bucket_and_omits_list() -> None:
    """S3ArchiveStore only calls put_object/get_object/head_object, and
    head_object is authorized by s3:GetObject. ListBucket is not needed, and a
    wildcard resource would put every bucket in the account in scope."""

    policy = _inline_policy("write-clickhouse-archive")
    statement = policy["Statement"][0]
    assert set(statement["Action"]) == {"s3:PutObject", "s3:GetObject"}
    assert statement["Resource"].startswith("arn:aws:s3:::")
    assert statement["Resource"].endswith("/*")
    assert "s3:ListBucket" not in json.dumps(policy)
    assert "s3:*" not in json.dumps(policy)
    assert '"Resource":"*"' not in json.dumps(policy).replace(" ", "")


def test_the_dedicated_role_still_carries_what_the_drain_needs() -> None:
    """The swap's real risk: the shared role granted dsql:DbConnect. A
    replacement role without it stops analytics delivery, and a stalled drain
    is indistinguishable from an idle one from outside the node."""

    dsql = _inline_policy("dsql-connect-drain")
    assert dsql["Statement"][0]["Action"] == ["dsql:DbConnect"]
    assert "cluster/" in dsql["Statement"][0]["Resource"][0]

    secrets = _inline_policy("read-clickhouse-password")
    actions = {a for s in secrets["Statement"] for a in _as_list(s["Action"])}
    assert "secretsmanager:GetSecretValue" in actions
    # SSM is how code reaches this node at all.
    assert "AmazonSSMManagedInstanceCore" in SCRIPT


def test_the_secret_grant_is_narrower_than_the_shared_roles_wildcard() -> None:
    """The shared role allowed quill/*. This node reads exactly one secret."""

    # The ARN is built from ${SECRET_ID}, so the narrowing lives in two places:
    # the default value, and the fact that the policy interpolates it rather
    # than hardcoding a prefix wildcard.
    secret_default = [line for line in SCRIPT.splitlines() if line.startswith("SECRET_ID=")]
    assert len(secret_default) == 1
    assert "quill/tr-eu-clickhouse-password" in secret_default[0]

    resources = [
        s["Resource"]
        for s in _inline_policy("read-clickhouse-password")["Statement"]
        if "secretsmanager" in json.dumps(s["Action"])
    ]
    assert resources
    for resource in resources:
        assert ":secret:" in resource
        # quill/* would restore the shared role's breadth.
        assert "quill/*" not in resource
    # And the raw script must interpolate the variable, not a wildcard prefix.
    assert "secret:${SECRET_ID}" in SCRIPT
    assert "secret:quill/*" not in SCRIPT


def test_the_enclave_keeps_no_new_permissions() -> None:
    """Nothing in this script may modify the enclave's role or profile."""

    for forbidden in (
        "--role-name quill-enclave-role",
        "--instance-profile-name quill-enclave-instance-profile",
    ):
        assert forbidden not in SCRIPT
    assert "put-role-policy --role-name quill-enclave-role" not in SCRIPT


# --------------------------------------------------------------------------
# The profile swap is the one step that can break a working system.
# --------------------------------------------------------------------------


def test_the_profile_swap_is_opt_in_and_not_part_of_the_default_run() -> None:
    assert "ATTACH_PROFILE=0" in SCRIPT
    assert "--attach-profile) ATTACH_PROFILE=1" in SCRIPT
    assert "instance profile NOT attached" in SCRIPT


def test_the_swap_preflights_the_replacement_role_before_replacing() -> None:
    """Ordering matters: the check must run before the association changes, or
    it is a post-mortem rather than a pre-flight."""

    assert "preflight_profile_swap" in SCRIPT
    body = SCRIPT.split("attach_profile() {")[1]
    preflight_at = body.index("preflight_profile_swap")
    replace_at = body.index("replace-iam-instance-profile-association")
    assert preflight_at < replace_at, "the pre-flight must precede the swap"
    assert "refusing to swap the instance profile" in SCRIPT


def test_the_swap_prints_a_revert_command_and_says_verification_is_required() -> None:
    """Instance credentials refresh on a delay, so 'it did not break instantly'
    is not evidence. The operator must be told what to check."""

    assert "To revert:" in SCRIPT
    assert "replace-iam-instance-profile-association" in SCRIPT
    assert "VERIFY BEFORE WALKING AWAY" in SCRIPT
    assert "lag stops growing" in SCRIPT


def _as_list(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    assert isinstance(value, list)
    return [str(item) for item in value]


def _inline_policy(name: str) -> dict:
    """Pull an inline policy document out of the script and parse it.

    Parsing rather than substring-matching means a policy that is malformed
    JSON, or that quietly grows an extra action, fails a test instead of
    failing at apply time against real IAM.
    """

    marker = f"--policy-name {name} \\"
    assert marker in SCRIPT, f"no inline policy named {name}"
    tail = SCRIPT.split(marker, 1)[1]
    match = re.search(r'--policy-document "(\{.*?\n\s*\]\})"', tail, re.DOTALL)
    assert match, f"could not extract the policy document for {name}"
    raw = match.group(1)
    # The script embeds JSON in a double-quoted shell string, so quotes are
    # backslash-escaped and shell variables are interpolated at run time.
    raw = raw.replace('\\"', '"')
    raw = re.sub(r"\$\{[A-Z_]+\}", "PLACEHOLDER", raw)
    return json.loads(raw)
