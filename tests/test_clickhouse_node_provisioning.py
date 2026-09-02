from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_clickhouse_node_preserves_disk_and_blocks_accidental_vm_deletion() -> None:
    script = (ROOT / "scripts/deploy/clickhouse_node.sh").read_text()

    assert "--no-boot-disk-auto-delete" in script
    assert "set-disk-auto-delete" in script
    assert "--no-auto-delete" in script
    assert script.count("--deletion-protection") >= 2
    assert 'DISK_GB="${DISK_GB:-500}"' in script


def test_clickhouse_password_never_enters_instance_metadata() -> None:
    node = (ROOT / "scripts/deploy/clickhouse_node.sh").read_text()
    startup = (ROOT / "scripts/deploy/clickhouse_startup.sh").read_text()

    assert "--metadata ch-password=" not in node
    assert "clickhouse-password-secret" in node
    assert "secretmanager.googleapis.com" in startup
    assert "instance/attributes/ch-password" not in startup


def test_live_deploy_installs_archive_and_rollup_timers() -> None:
    script = (ROOT / "scripts/deploy/clickhouse_live_ingestion.sh").read_text()

    assert "002_provider_analytics_rollups.sql" in script
    assert "tr-clickhouse-archive.timer" in script
    assert "tr-clickhouse-archive-restore.timer" in script
    assert "tr-clickhouse-rollup-hourly.timer" in script
    assert "tr-clickhouse-rollup-daily.timer" in script


def test_cluster_migration_is_parity_gated_and_keeps_local_backup() -> None:
    script = (ROOT / "scripts/deploy/clickhouse_cluster.sh").read_text()

    assert "tr-clickhouse-2" in script
    assert "tr-clickhouse-3" in script
    assert "us-central1-b" in script
    assert "us-central1-c" in script
    assert "<keeper_server>" in script
    assert "pid_two" in script
    assert "pid_three" in script
    assert "SYSTEM SYNC REPLICA" in script
    assert "source and replicated fingerprints differ" in script
    assert "timedelta(minutes=5)" in script
    assert "service account did not become visible" in script
    assert "bigtable instances add-iam-policy-binding" in script
    assert '"$BIGTABLE_INSTANCE_ID"' in script
    assert "roles/bigtable.reader" in script
    assert "could not grant Bigtable read access" in script
    assert "roles/spanner.databaseUser" in script
    assert "spanner databases add-iam-policy-binding" in script
    assert "provider_benchmark_samples_local_backup" in script
    assert "RENAME TABLE provider_benchmark_samples TO" in script
    assert "DROP TABLE provider_benchmark_samples_local_backup" not in script


def test_cluster_load_balancer_is_private_global_access_and_three_backend() -> None:
    script = (ROOT / "scripts/deploy/clickhouse_cluster.sh").read_text()

    assert "--load-balancing-scheme=INTERNAL" in script
    assert "--allow-global-access" in script
    assert "--ports=8123" in script
    assert "tr-clickhouse-health-check" in script
    assert "35.191.0.0/16,130.211.0.0/22" in script
    assert "http://${ip}:8123/ping" in script


def test_rollout_prefers_private_clickhouse_load_balancer() -> None:
    script = (ROOT / "scripts/deploy/rollout.sh").read_text()

    assert "compute addresses describe tr-clickhouse-ilb" in script
    assert "TR_PROVIDER_ANALYTICS_CLICKHOUSE_URL=${PROVIDER_ANALYTICS_CLICKHOUSE_URL}" in script


def test_operational_deploy_moves_benchmark_code_schema_and_replay_together() -> None:
    script = (ROOT / "scripts/deploy/clickhouse_operational_analytics.sh").read_text()

    upload = script.index("sudo tar -xzf - -C /opt/tr-clickhouse")
    stop = script.index("systemctl stop tr-clickhouse-operational-ingest.service")
    migration = script.index('log "adding workspace attribution to benchmark samples"')
    restart = script.index(
        "systemctl start tr-clickhouse-operational-ingest.service", migration
    )
    replay = script.index("clickhouse.backfill_benchmark_samples")

    assert upload < stop < migration < restart < replay
    assert "007_benchmark_samples_workspace_id.sql" in script
    assert "TR_CLICKHOUSE_BENCHMARK_WORKSPACE_BACKFILL_LIMIT" in script


# --------------------------------------------------------------------------
# The Stockholm node: the AWS cloud's SECOND durable copy
# --------------------------------------------------------------------------

STOCKHOLM = ROOT / "scripts/deploy/aws_eu_north_clickhouse.sh"


def _statements() -> str:
    """The Stockholm script with comment-only lines removed.

    Several assertions below are of the form "this script does NOT do X", and
    the script's header explains at length why X is wrong. Matching against the
    raw text would therefore fail on the very prose that warns against the
    thing being guarded, so negative assertions read executable lines only.
    """
    return "\n".join(
        line for line in STOCKHOLM.read_text().splitlines() if not line.lstrip().startswith("#")
    )


def test_stockholm_node_is_its_own_vpc_in_its_own_region() -> None:
    """A second node inside the Paris VPC shares its route tables, its NAT and
    its region -- the failure domain the second copy exists to escape."""
    script = STOCKHOLM.read_text()

    assert 'REGION="${REGION:-eu-north-1}"' in script
    assert 'PARIS_REGION="${PARIS_REGION:-eu-west-3}"' in script
    assert "aws ec2 create-vpc" in script
    assert "aws ec2 create-subnet" in script
    # Refuses to build the "second" copy in the first one's region.
    assert 'if [ "$REGION" = "$PARIS_REGION" ]' in script


def test_stockholm_refuses_a_cidr_that_overlaps_paris() -> None:
    """Overlapping CIDRs cannot be routed across a Transit Gateway, and the
    symptom is a silent blackhole rather than an error at creation time."""
    script = STOCKHOLM.read_text()

    assert "import ipaddress" in script
    assert ".overlaps(" in script
    assert 'aws ec2 describe-vpcs --region "$PARIS_REGION"' in script


def test_stockholm_security_group_admits_only_the_paris_vpc() -> None:
    """Same rule as the Paris node's VPC-internal-only group, generalised to
    the one remote network that runs the drain."""
    script = STOCKHOLM.read_text()

    assert '--protocol tcp --port "$PORT" --cidr "$PARIS_VPC_CIDR"' in script
    assert "for PORT in 8123 9000" in script
    # No public ingress on the analytics ports, and no SSH at all.
    statements = _statements()
    assert "--port 22" not in statements
    assert "--cidr 0.0.0.0/0" not in statements


def test_stockholm_repeats_the_hard_won_details_from_the_paris_node() -> None:
    """These cost debugging cycles on the Paris node; they are repeated rather
    than rediscovered."""
    script = STOCKHOLM.read_text()

    # The users.d ownership fix: the server drops privileges, so a root-owned
    # file is unreadable to it and it dies without naming the permission.
    assert (
        "chown clickhouse:clickhouse /etc/clickhouse-server/users.d/default-password.xml" in script
    )
    # IMDSv2 required, not optional.
    assert "HttpTokens=required" in script
    assert "X-aws-ec2-metadata-token-ttl-seconds" in script
    # Private listen address. The instance has a public IP for apt egress, so
    # binding 0.0.0.0 would put the analytics store on the internet.
    assert "<listen_host>\\${PRIVATE_IP}</listen_host>" in script
    assert "<listen_host>0.0.0.0</listen_host>" not in _statements()


def test_stockholm_volume_survives_terminating_the_instance() -> None:
    """The node's entire job is to still have the data. Mirrors the GCP node's
    --no-boot-disk-auto-delete + --deletion-protection."""
    script = STOCKHOLM.read_text()

    assert "DeleteOnTermination=false" in script
    assert "--disable-api-termination" in script


def test_stockholm_applies_the_single_node_schema_not_the_replicated_one() -> None:
    """Two regions cannot form a Keeper quorum, so these nodes do not
    replicate; the drain writes both.

    This used to assert the script contained the literal string
    "006_operational_analytics_single_node.sql". That pinned the HARDCODING
    rather than the coverage: the script applied exactly 006 and 009, 010
    through 013 landed, and this test went on passing while every node it built
    was missing the workspace_id column the drain inserts.

    Newer contract: the script names no migration at all. It applies the set
    derived from clickhouse/*_single_node.sql by
    scripts/deploy/_clickhouse_single_node_schema.sh, and that naming is what
    excludes the replicated ones -- so the "not the replicated one" half of this
    test is now structural rather than a denylist of one filename. The
    derivation itself is pinned in tests/test_single_node_schema_set.py.
    """
    script = STOCKHOLM.read_text()

    assert "single_node_migrations" in script
    assert "_clickhouse_single_node_schema.sh" in script

    # No replicated migration reaches this node -- checked against ALL of them,
    # not just 004, since the set is derived and a new one could appear.
    statements = _statements()
    replicated = [
        path.name
        for path in sorted((ROOT / "clickhouse").glob("*.sql"))
        if not path.name.endswith("_single_node.sql")
    ]
    assert replicated, "no non-single-node migrations found; the check would be vacuous"
    for name in replicated:
        assert name not in statements, f"{name} is a replicated migration"

    # Applied from user-data, so the node is complete when it finishes booting
    # rather than depending on a manual step someone forgets.
    assert "--multiquery < /root/operational_schema.sql" in script


def test_stockholm_never_prints_the_clickhouse_password() -> None:
    """The operator gets a Secrets Manager fetch, not a secret on a terminal."""
    script = STOCKHOLM.read_text()

    statements = _statements()
    assert 'echo "CH_PASSWORD' not in statements
    assert 'echo "$CH_PASSWORD"' not in statements
    assert "secretsmanager get-secret-value" in script
    # Its OWN secret, in its OWN region: Secrets Manager is regional, and
    # reusing the Paris secret would put an eu-west-3 dependency inside the
    # thing built to not depend on eu-west-3.
    assert 'SECRET_ID="${SECRET_ID:-quill/tr-eu-north-clickhouse-password}"' in script


def test_stockholm_route_creation_failures_are_fatal_not_swallowed() -> None:
    """`create-route ... || true` turns "the route was never installed" into a
    silent blackhole -- the exact failure this network is most likely to hit."""
    script = STOCKHOLM.read_text()

    assert "route_exists_or_created" in script
    assert "tgw_route_exists_or_created" in script
    # "Already there" is the one tolerated outcome, and it is matched on the
    # error, not shrugged at with `|| true`.
    assert "RouteAlreadyExists" in script

    # Every route-creating call goes through a helper that re-raises anything
    # other than RouteAlreadyExists. Concretely: no executable line both
    # creates a route and discards the result.
    swallowed = [
        line
        for line in _statements().splitlines()
        if ("aws ec2 create-route" in line or "aws ec2 create-transit-gateway-route" in line)
        and "|| true" in line
    ]
    assert swallowed == []

    # And the raw calls appear exactly once each -- inside their helper.
    assert _statements().count("aws ec2 create-route") == 1
    assert _statements().count("aws ec2 create-transit-gateway-route") == 1


def test_stockholm_waits_for_transit_gateway_state_without_a_nonexistent_waiter() -> None:
    """There is no `aws ec2 wait transit-gateway-available`; checked against
    the CLI's own waiter model. Calling it would abort the script."""
    script = STOCKHOLM.read_text()

    assert "aws ec2 wait transit-gateway-available" not in _statements()
    assert "await_state" in script
    # Peering attachments are NOT propagated into a TGW route table, unlike VPC
    # attachments, so the static routes are mandatory.
    assert "aws ec2 create-transit-gateway-route" in script
    assert "AssociationDefaultRouteTableId" in script


def test_stockholm_builds_the_path_against_the_subnet_the_drain_runs_in() -> None:
    """REGRESSION. `PARIS_SUBNET_ID` defaulted to an arbitrary subnet.

    `describe-subnets --query 'Subnets[0].SubnetId'` has no defined ordering and
    a VPC has one subnet per AZ. That single value picks BOTH the TGW VPC
    attachment and the route table the return route is installed into, so the
    wrong pick gives two independent silent blackholes -- a TGW attachment
    carries traffic only for AZs where it has an ENI, and the route lands in a
    table nothing uses. The script printed "inter-region path ready" for both.
    """
    script = STOCKHOLM.read_text()
    statements = _statements()

    # Derived from the instance that actually runs the drain, by tag.
    assert 'PARIS_NODE_NAME="${PARIS_NODE_NAME:-tr-eu-clickhouse-1}"' in script
    assert "Name=tag:Name,Values=$PARIS_NODE_NAME" in script
    assert "Reservations[0].Instances[0].SubnetId" in script
    # And never from an arbitrary subnet of the VPC. The one surviving
    # `Subnets[0].SubnetId` is the Stockholm lookup, which is deterministic
    # because it filters on this script's own Name tag rather than taking
    # whatever the API returns first.
    assert statements.count("Subnets[0].SubnetId") == 1
    assert "Name=tag:Name,Values=$VPC_NAME-a" in statements
    assert 'describe-subnets --region "$PARIS_REGION" \\\n      --filters' not in statements
    # A subnet that is not in the Paris VPC is refused rather than used.
    assert "$PARIS_SUBNET_VPC" in script


def test_stockholm_env_block_contains_no_shell_substitution() -> None:
    """REGRESSION. The printed block told the operator to write `$(aws ...)`
    into a systemd EnvironmentFile, which performs no command substitution.

    The literal string `$(aws secretsmanager ...)` would become the password. It
    is non-empty, so the startup check that exists precisely to catch a missing
    credential passes; the drain logs `copies=2`; and every Stockholm insert
    then fails authentication forever while nothing is ever deleted. Fail-safe
    for the data, but the feature never works once while reporting as
    configured -- reached by following the script's own instructions verbatim.
    """
    lines = STOCKHOLM.read_text().splitlines()
    env_lines = [
        line for line in lines if "CH_REPLICA_PASSWORD=" in line or "_CLICKHOUSE_REPLICA_" in line
    ]
    assert env_lines, "the runbook must still print the replica env block"
    for line in env_lines:
        assert "$(" not in line, f"substitution in an EnvironmentFile line: {line}"

    script = STOCKHOLM.read_text()
    # The password is added by RUNNING a command that appends the resolved
    # value, not by pasting shell syntax into the file.
    assert "printf 'CH_REPLICA_PASSWORD=%s" in script
    assert ">> /etc/tr-clickhouse-ingest-postgres.env" in script
    assert "performs NO command or" in script


def test_stockholm_backfill_runs_in_the_direction_that_is_actually_open() -> None:
    """REGRESSION. The documented backfill could not connect.

    It told the operator to run `remote('<paris-ip>' ...)` FROM Stockholm, so
    the connection originates at 10.60.1.x. Paris refuses that at two
    independent layers: its security group admits only the Paris VPC CIDR
    (aws_eu_clickhouse.sh), and its ClickHouse `default` user is pinned to
    <networks> Paris-VPC + 127.0.0.1. This is the FIRST thing an operator does
    after provisioning and the only stated remedy for the permanent-hole
    limitation, so it has to run in the direction that is open: Paris pushes.
    """
    script = STOCKHOLM.read_text()

    assert "INSERT INTO FUNCTION remote(" in script
    assert "ON THE PARIS NODE" in script
    # The pull direction, which is the one that is blocked, is not offered.
    assert "SELECT * FROM\n    remote(" not in script
    assert "refused at both layers" in script


def test_stockholm_states_which_tables_the_second_copy_covers() -> None:
    """The drain names every ingested table and excludes derived products."""
    from clickhouse.ingest_operational_outbox import EVENT_TABLES

    script = STOCKHOLM.read_text()

    assert "COVERAGE:" in script
    tables = {
        table
        for value in EVENT_TABLES.values()
        for table in ((value,) if isinstance(value, str) else value)
    }
    for table in tables:
        assert table in script
    assert "does NOT write" in script
    assert "synthetic_status_rollups" in script
    assert "client_availability_rollups" in script
    assert "public_analytics_snapshots" in script


def test_the_drain_unit_refuses_to_restart_a_misconfigured_drain() -> None:
    """A config error cannot be fixed by running again.

    With a bare restart policy, a typo in the replica block crash-loops every
    RestartSec while the outbox grows at full rate -- and the backlog alarm that
    is supposed to bound that growth is emitted BY the process that is not
    running. The exit status the drain uses for configuration errors must be
    the one the unit refuses to restart, so the two cannot drift apart.
    """
    from clickhouse.ingest_operational_outbox_postgres import CONFIG_EXIT_CODE

    unit = (ROOT / "clickhouse/tr-clickhouse-operational-ingest-postgres.service").read_text()

    assert f"RestartPreventExitStatus={CONFIG_EXIT_CODE}" in unit
    assert "Restart=on-failure" in unit
    assert "RestartSec=5" in unit


def test_stockholm_documents_that_it_starts_empty_and_is_not_backfilled() -> None:
    """The real limitation: a node added later holds only rows drained after
    it was wired in. Saying so in the script's output is the difference between
    a known gap and a surprise."""
    script = STOCKHOLM.read_text()

    assert "starts EMPTY" in script
    assert "remote(" in script
    assert "ingest_version" in script


# --------------------------------------------------------------------------
# Analytics nodes must not vend the enclave hosts' credentials
# --------------------------------------------------------------------------
#
# Until 2026-08-17 both AWS ClickHouse scripts launched their node with
# quill-enclave-instance-profile. That role grants secretsmanager on quill/*
# -- roughly forty provider API keys, the Cloudflare token and the cross-cloud
# SA key -- plus kms:Decrypt on every key in the account, the CloudTrail CMK
# included. An analytics box was therefore one RCE away from the entire
# provider credential set.
#
# The split was first applied by hand to the running node, which fixed exactly
# nothing durable: the next run of the deploy script would have handed the
# profile straight back, and a new region would never have had the narrow role
# at all. These tests exist so the fix lives in the bring-up rather than in a
# shell history.

PARIS = ROOT / "scripts/deploy/aws_eu_clickhouse.sh"


def _executable_lines(path: Path) -> str:
    """Script text with comment-only lines removed.

    The negative assertions below guard against a profile name that both
    scripts legitimately DISCUSS in their comments, so matching raw text would
    fail on the prose explaining why the thing is wrong.
    """
    return "\n".join(
        line for line in path.read_text().splitlines() if not line.lstrip().startswith("#")
    )


def test_analytics_nodes_never_launch_with_the_enclave_instance_profile() -> None:
    for path in (PARIS, STOCKHOLM):
        assert "quill-enclave-instance-profile" not in _executable_lines(path), (
            f"{path.name} would give an analytics node the enclave hosts' credentials"
        )


def test_analytics_nodes_create_the_role_they_launch_with() -> None:
    """Referencing a role someone made by hand is not provisioning it.

    A control that exists only in live cloud state dies with the resource, and
    cannot be evidenced as operating across a SOC 2 observation period for
    anything rebuilt during it.
    """
    for path in (PARIS, STOCKHOLM):
        script = path.read_text()
        assert "iam create-role" in script
        assert "iam put-role-policy" in script
        assert "iam create-instance-profile" in script
        assert "iam add-role-to-instance-profile" in script


def test_analytics_role_is_scoped_to_one_secret_and_one_key() -> None:
    for path in (PARIS, STOCKHOLM):
        script = path.read_text()
        # Its own password secret, not the whole quill/ namespace.
        assert "secret:$SECRET_ID-*" in script
        assert "secret:quill/*" not in script
        # The resolved secretsmanager key, never every key in the account.
        assert "alias/aws/secretsmanager" in script
        assert "kms:*:$ACCOUNT:key/*" not in script


def test_analytics_role_trust_policy_uses_if_exists_for_ec2() -> None:
    """EC2 instance-profile assumption does not populate aws:SourceAccount.

    A hard StringEquals here fails CLOSED, and not at deploy time -- it fails
    when cached IMDS credentials next expire, which presents as an unrelated
    outage hours later. Every other role in the account uses hard StringEquals
    precisely because every other one is a service principal that does populate
    the key.
    """
    for path in (PARIS, STOCKHOLM):
        assert "StringEqualsIfExists" in _executable_lines(path)


def test_paris_converges_the_profile_on_an_existing_node() -> None:
    """Fixing only the launch path leaves every node built before the fix
    holding the enclave profile forever."""
    script = PARIS.read_text()
    assert "replace-iam-instance-profile-association" in script
