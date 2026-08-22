"""Static guards on scripts/deploy/azure_clickhouse.sh.

These are read-of-the-script tests, like the AWS deploy tests beside them. They
exist because every property below is one somebody could remove without any
test failing, and each removal produces a node that still *provisions* -- the
failure only shows up as data nobody can read, or an analytics store on the
public internet.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/deploy/azure_clickhouse.sh"


def _script() -> str:
    return SCRIPT.read_text()


def _code() -> str:
    """Executable lines only: no comments, and nothing inside a heredoc body.

    The script deliberately NAMES the things it refuses to do -- "never
    0.0.0.0/0", "az role assignment create" in the instructions it prints, "az
    role assignment list" in the comment explaining why it does not use it. A
    substring test over the whole file cannot tell a prohibition from a
    violation, and would either fail on the prose or pass on a real regression
    once somebody reworded a comment.
    """
    out, in_heredoc = [], False
    for line in _script().splitlines():
        stripped = line.strip()
        if in_heredoc:
            if stripped in {"EOF", "'EOF'"}:
                in_heredoc = False
            continue
        if "<<EOF" in line or "<<'EOF'" in line or "<<-EOF" in line:
            in_heredoc = True
            continue
        if stripped.startswith("#"):
            continue
        out.append(line)
    return "\n".join(out)


def test_the_node_gets_no_public_ip() -> None:
    """A ClickHouse with a public address is protected by a password alone.

    The whole reason the control plane sits in this VNet is so the analytics
    store does not have to be reachable from outside it.
    """
    assert '--public-ip-address ""' in _script()


def test_ingress_is_restricted_to_the_vnet() -> None:
    """8123/9000 from the VNet CIDR, never 0.0.0.0/0 or Internet."""
    script = _script()

    assert "--source-address-prefixes \"$VNET_CIDR\"" in script
    assert "--destination-port-ranges 8123 9000" in script

    code = _code()
    assert "0.0.0.0/0" not in code
    assert "--source-address-prefixes Internet" not in code
    assert "--source-address-prefixes '*'" not in code


def test_the_password_never_travels_through_cloud_init() -> None:
    """Custom data is readable from inside the VM via IMDS.

    A password written into cloud-init is a password published to anything that
    can reach 169.254.169.254, which includes every process on the box. The
    node must fetch it itself, with its own identity, at first boot.
    """
    script = _script()

    # The fetch happens on the node, against the vault, using the MI token.
    assert "identity/oauth2/token" in script
    assert "vault.azure.net/secrets/" in script

    # And the generated value is never interpolated into the cloud-init doc.
    cloud_init = script[script.index("CLOUD_INIT=") : script.index("# -- the node")]
    assert "$pw" not in cloud_init
    assert "openssl rand" not in cloud_init


def test_the_generated_password_is_never_echoed() -> None:
    """It is generated once and unset; nothing prints it."""
    script = _script()

    assert "unset pw" in script
    assert not re.search(r"echo .*\$pw", script)


def test_the_schema_is_applied_by_the_node_itself() -> None:
    """On AWS this lived in NEXT_STEPS, and that gap ran for fifteen days.

    A node that is up with no tables looks exactly like one that is working.
    """
    script = _script()

    assert "006_operational_analytics_single_node.sql" in script
    assert "009_client_events_single_node.sql" in script
    assert "--multiquery < /root/operational_schema.sql" in script
    # And it waits for the server before trying, rather than racing it.
    # \$CH_PW, not $CH_PW: the cloud-init body is a heredoc, so the expansion
    # is escaped to happen ON THE NODE rather than on the operator's machine.
    assert '--query "SELECT 1"' in script
    assert '\\$CH_PW' in script


def test_the_users_file_is_chowned_to_clickhouse() -> None:
    """A root-owned 0600 users.d file is unreadable after the server drops
    privileges, and it dies in UsersConfigAccessStorage::load without naming
    the permission -- an hour of confusion the AWS script already paid for."""
    script = _script()

    assert "chown clickhouse:clickhouse /etc/clickhouse-server/users.d/default-password.xml" in script
    assert "chmod 640 /etc/clickhouse-server/users.d/default-password.xml" in script


def test_it_refuses_to_create_the_role_assignment() -> None:
    """Creating role assignments from a deploy script is how a deploy pipeline
    quietly becomes an admin. It must check, print the command, and stop."""
    script = _script()

    assert "az role assignment create" in script  # printed in the instructions

    executed = [
        line
        for line in _code().splitlines()
        if line.strip().startswith("az role assignment create")
    ]
    assert executed == [], f"the script executes a role assignment: {executed}"


def test_the_grant_check_reads_arm_not_graph() -> None:
    """`az role assignment list --assignee` resolves the principal through
    Microsoft Graph, which this tenant makes unreliable -- measured hanging
    past 120s with a valid token. Worse, its failures are indistinguishable
    from "the grant does not exist", so an outage reads as a missing role."""
    script = _script()

    assert "role assignment list" not in _code()
    assert "Microsoft.Authorization/roleAssignments?api-version" in script
    assert "could not list role assignments" in script


def test_quota_is_checked_before_anything_is_created() -> None:
    """A VM create that fails on quota after a subnet and an NSG exist leaves
    half a deployment behind a confusing error. The family limit is one read."""
    script = _script()

    quota_at = script.index("az vm list-usage")
    for created in ("az network nsg create", "az network vnet subnet create", "az vm create"):
        assert script.index(created) > quota_at, f"{created} runs before the quota check"


def test_it_does_not_recreate_an_existing_node() -> None:
    """The data disk IS the analytics store. Recreating the VM to pick up a
    config change would silently discard everything drained into it."""
    script = _script()

    assert "already exists — not recreating" in script


def test_commands_are_not_yaml_parsed() -> None:
    """A ": " inside an unquoted YAML scalar becomes a MAPPING.

    The first version of this script put each command in a runcmd list, and one
    of them contained `-H "Authorization: Bearer $TOKEN"`. YAML read that entry
    as a dict, cloud-init refused to shellify a dict, and the ENTIRE runcmd
    block died before a single command ran. The VM provisioned cleanly with no
    ClickHouse and no schema, and nothing about "VM created" said otherwise.

    So the bootstrap lives in a write_files BLOCK SCALAR -- literal, never
    parsed -- and runcmd only executes it.
    """
    script = _script()

    assert "path: /root/bootstrap.sh" in script
    assert 'echo "  - /root/bootstrap.sh"' in script

    # The Authorization header, the reason this bug existed, must not sit in a
    # runcmd entry any more.
    body = script[script.index('echo "runcmd:"') :]
    assert "Authorization:" not in body


def test_the_generated_cloud_init_is_validated_before_azure_sees_it() -> None:
    """cloud-init reports a malformed config on the node, minutes later, in a
    log nobody is watching. The shape is checkable here, in a second.

    Shape, not just parseability: the broken file was valid YAML.
    """
    script = _script()

    assert "generated cloud-init is not usable" in script
    assert "runcmd is missing or not a list" in script
    assert "not a string" in script
