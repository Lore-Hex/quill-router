"""The AWS-EU drain: its install script, its freshness contract, and the check.

Background these tests are pinning down. On 2026-08-17 the drain for the
AWS-EU cloud was found never to have been installed: no unit on
tr-eu-clickhouse-1, no environment file, no process, `activity_generations`
empty, and 465,119 undelivered rows in the DSQL outbox going back to
2026-08-02. It was silent for fifteen days because every signal for this
pipeline is emitted by the drain itself.

So the assertions below are mostly about the two ways that recurs: an install
that puts files somewhere the unit does not look, and a monitor that reports
healthy when it is actually seeing nothing.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

import clickhouse.check_aws_analytics_freshness as aws_check
import clickhouse.check_fleet_analytics_freshness as fleet_check
from clickhouse.check_aws_analytics_freshness import evaluate
from trusted_router.operational_analytics_freshness import (
    ANALYTICS_STATUS_KEY,
    AVAILABLE_FIELD,
    DRAIN_LAG_FIELD,
    GENERATED_AT_FIELD,
    OUTBOX_DEPTH_FIELD,
    PUBLISHED_AVAILABLE_FIELDS,
    PUBLISHED_UNAVAILABLE_FIELDS,
    analytics_status_section,
    analytics_status_unavailable,
)

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "scripts/deploy/aws_eu_clickhouse_drain_install.sh"
UNIT = ROOT / "clickhouse/tr-clickhouse-operational-ingest-postgres.service"
WORKFLOW = ROOT / ".github/workflows/check-analytics-freshness.yml"

NOW = dt.datetime(2026, 8, 17, 12, 0, tzinfo=dt.UTC)


def _now_iso() -> str:
    """`main` compares against the real clock, so fixtures must be fresh."""
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# The install script must agree with the unit about where the code lives.
# ---------------------------------------------------------------------------


def test_install_targets_the_path_the_unit_actually_execs() -> None:
    """The reason /opt/drain was dead weight: the unit never looked there.

    This is the drift guard. If someone edits WorkingDirectory or ExecStart in
    the unit, the install script stops matching and this fails, rather than the
    node quietly gaining a second unused copy of the drain.
    """
    unit = UNIT.read_text()
    script = INSTALL.read_text()

    assert "WorkingDirectory=/opt/tr-clickhouse" in unit
    assert "ExecStart=/opt/tr-clickhouse/venv/bin/python -m clickhouse.ingest_operational_outbox_postgres" in unit
    assert 'REMOTE_ROOT="${REMOTE_ROOT:-/opt/tr-clickhouse}"' in script
    # The unit sets no PYTHONPATH, so `python -m` sees only WorkingDirectory:
    # the package has to be flattened out of src/ or `import trusted_router`
    # fails. That is precisely what /opt/drain got wrong.
    assert "PYTHONPATH" not in unit
    assert "mv '$STAGE_DIR/src/trusted_router' '$STAGE_DIR/trusted_router'" in script


def test_install_creates_what_the_unit_hardening_requires() -> None:
    """ProtectSystem=strict + ReadWritePaths refuses to start on a missing dir."""
    unit = UNIT.read_text()
    script = INSTALL.read_text()

    assert "ReadWritePaths=/var/lib/tr-clickhouse-ingest" in unit
    assert "User=tr-clickhouse-ingest" in unit
    assert "useradd --system" in script
    assert "install -d -o '$SERVICE_USER' -g '$SERVICE_USER'" in script


def test_install_refuses_to_proceed_without_dsql_permission() -> None:
    """The one precondition that fails silently at runtime, so it fails loudly here.

    quill-enclave-role holds no dsql action at all, so without this gate the
    script would finish "successfully" and leave a unit that connects to
    nothing.
    """
    script = INSTALL.read_text()

    assert "simulate-principal-policy" in script
    assert "dsql:DbConnectAdmin dsql:DbConnect" in script
    assert "FATAL:" in script
    # It must document the scoped alternative rather than normalising admin on a
    # cluster that also holds wallets, keys and the ledger.
    assert "GRANT SELECT, DELETE ON tr_operational_analytics_outbox" in script
    assert "AWS IAM GRANT tr_drain" in script


def test_install_never_ships_appledouble_sidecars() -> None:
    """/opt/drain/clickhouse currently holds four of these; they broke a parser once."""
    script = INSTALL.read_text()

    assert "COPYFILE_DISABLE=1 tar" in script
    assert "--exclude='._*'" in script
    assert "find '$STAGE_DIR' -name '._*' -print -delete" in script
    # Deleting is not enough: if one survives, the tarball was not built the way
    # this script claims, and that is worth failing over. (Escaped, because the
    # snippet is carried to the node inside a quoted SSM command.)
    assert r"""test -z \"\$(find '$STAGE_DIR' -name '._*')""" in script


def test_install_stages_and_verifies_before_swapping_anything_in() -> None:
    script = INSTALL.read_text()

    assert "sha256sum -c -" in script
    assert "import smoke test (staging)" in script
    # The swap happens after the smoke test, never before.
    assert script.index("import smoke test (staging)") < script.index("drain: activate")
    assert "${REMOTE_ROOT}.previous" in script


def test_install_writes_the_secret_by_running_a_command_not_by_pasting() -> None:
    """EnvironmentFile does no substitution: a pasted $(...) BECOMES the password.

    It is non-empty, so every startup check passes, and then authentication
    fails on every insert forever while the outbox grows.
    """
    script = INSTALL.read_text()

    assert "get_secret_value" in script
    assert "print('CH_PASSWORD=' + secret)" in script
    assert "chmod 600 '$ENV_FILE'" in script
    assert "CH_PASSWORD is a literal command; refusing" in script
    # Never echoed into the SSM output, which is retained and readable by
    # anyone with ssm:GetCommandInvocation. The verification prints the key
    # NAMES and the password's length, never its value.
    assert "CH_PASSWORD length=" in script
    assert "cut -d= -f1 '$ENV_FILE'" in script
    assert "cat '$ENV_FILE'" not in script


def test_env_file_carries_exactly_the_drain_contract_and_no_password_in_the_dsn() -> None:
    script = INSTALL.read_text()

    for key in (
        "TR_POSTGRES_DSN=",
        "TR_POSTGRES_IAM_AUTH=aws-dsql",
        "TR_POSTGRES_IAM_REGION=",
        "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_USER=",
        "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_DATABASE=",
    ):
        assert key in script

    # "default"/"default", not the "tr"/"tr" default: this node's schema is
    # applied unqualified, and a mismatch fails auth only AFTER a batch has
    # been read out of the outbox.
    assert 'CH_USER="${CH_USER:-default}"' in script
    assert 'CH_DATABASE="${CH_DATABASE:-default}"' in script

    # On DSQL the token is minted per connection, so a password in the DSN is a
    # configuration error the drain raises on.
    dsn_line = next(line for line in script.splitlines() if line.startswith("TR_POSTGRES_DSN="))
    assert "password=" not in dsn_line
    assert "sslmode=require" in dsn_line


def test_install_stays_single_target_until_stockholm_exists() -> None:
    """No instances in eu-north-1 as of 2026-08-17.

    Setting a _REPLICA_HOST that resolves to nothing means NOTHING is ever
    deleted from the outbox, which is the failure this whole PR is fixing.
    """
    script = INSTALL.read_text()

    assert "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_REPLICA_HOST=" not in script
    assert "CH_REPLICA_PASSWORD=" not in script
    assert "aws_eu_north_clickhouse.sh" in script


def test_install_verifies_delivery_and_not_merely_that_the_unit_started() -> None:
    """`active` proves execve succeeded. It does not prove a single row moved."""
    script = INSTALL.read_text()

    assert "outbox\\.(metrics|targets|config_invalid|backlog_alarm)" in script
    assert "SELECT count() FROM activity_generations" in script
    assert "degraded_targets" in script


# ---------------------------------------------------------------------------
# The published freshness contract.
# ---------------------------------------------------------------------------


def test_empty_outbox_is_zero_lag_and_not_missing_data() -> None:
    """A fully drained outbox is the healthiest state, not an absence of signal."""
    section = analytics_status_section(oldest_enqueued_at=None, now=NOW, outbox_depth=0)

    assert section[AVAILABLE_FIELD] is True
    assert section[DRAIN_LAG_FIELD] == 0.0
    assert section[OUTBOX_DEPTH_FIELD] == 0


def test_the_published_section_carries_exactly_the_documented_fields() -> None:
    """A public contract that grows by accident cannot be narrowed later.

    An added key is a promise to whoever started reading it. `oldest_enqueued_at`
    was published by an earlier revision of this PR and read by nothing: the
    checker uses `drain_lag_seconds`, the runbook quotes the lag, and the value
    is `generated_at` minus the lag anyway. Nothing serves the section yet, so
    dropping it costs nobody anything -- and this pin is what keeps the next
    field from arriving the same way.
    """
    available = analytics_status_section(
        oldest_enqueued_at=NOW - dt.timedelta(seconds=30), now=NOW, outbox_depth=3
    )
    unavailable = analytics_status_unavailable()

    assert set(available) == PUBLISHED_AVAILABLE_FIELDS
    assert set(unavailable) == PUBLISHED_UNAVAILABLE_FIELDS
    assert "oldest_enqueued_at" not in available


def test_unavailable_is_distinguishable_from_drained() -> None:
    """Collapsing "could not look" into "nothing queued" turns an outage green."""
    section = analytics_status_unavailable()

    assert section[AVAILABLE_FIELD] is False
    assert DRAIN_LAG_FIELD not in section


def test_lag_is_the_age_of_the_oldest_undelivered_row() -> None:
    section = analytics_status_section(
        oldest_enqueued_at=NOW - dt.timedelta(hours=2),
        now=NOW,
        outbox_depth=465_119,
    )

    assert section[DRAIN_LAG_FIELD] == 7200.0
    assert section[OUTBOX_DEPTH_FIELD] == 465_119


def test_clock_skew_cannot_publish_a_negative_age() -> None:
    """enqueued_at comes from the writer's clock, now from the reader's."""
    section = analytics_status_section(
        oldest_enqueued_at=NOW + dt.timedelta(seconds=3),
        now=NOW,
    )

    assert section[DRAIN_LAG_FIELD] == 0.0


def test_naive_timestamps_are_read_as_utc() -> None:
    section = analytics_status_section(
        oldest_enqueued_at=dt.datetime(2026, 8, 17, 11, 0),
        now=NOW,
    )

    assert section[DRAIN_LAG_FIELD] == 3600.0


# ---------------------------------------------------------------------------
# The external check.
# ---------------------------------------------------------------------------


def _payload(**overrides: object) -> dict[str, object]:
    section: dict[str, object] = dict(
        analytics_status_section(
            oldest_enqueued_at=NOW - dt.timedelta(seconds=30),
            now=NOW,
            outbox_depth=12,
        )
    )
    section.update(overrides)
    return {ANALYTICS_STATUS_KEY: section}


def test_healthy_drain_reports_no_problems() -> None:
    assert evaluate(_payload(), now=NOW) == []


def test_missing_section_fails_rather_than_skips() -> None:
    """A monitor that treats "no signal" as "no problem" is decorative.

    This was the state of every live /status.json until the control plane
    started publishing the section; a deployment running older code still
    renders this way, and must fail rather than pass.
    """
    problems = evaluate({}, now=NOW)

    assert len(problems) == 1
    assert ANALYTICS_STATUS_KEY in problems[0]


def test_unavailable_section_fails() -> None:
    problems = evaluate({ANALYTICS_STATUS_KEY: analytics_status_unavailable()}, now=NOW)

    assert len(problems) == 1
    assert "no_data" in problems[0]


def test_backlog_older_than_the_drains_own_alarm_fails() -> None:
    """3600s is DEFAULT_MAX_LAG_SECONDS; a check looser than the daemon is useless."""
    problems = evaluate(
        _payload(**{DRAIN_LAG_FIELD: 7200.0}),
        now=NOW,
    )

    assert any("7200s old" in problem for problem in problems)


def test_frozen_control_plane_is_caught_by_the_sections_own_age() -> None:
    """Otherwise a stuck publisher serves a healthy-looking lag forever."""
    problems = evaluate(
        _payload(**{GENERATED_AT_FIELD: "2026-08-16T12:00:00Z"}),
        now=NOW,
    )

    assert any("analytics section is" in problem for problem in problems)


def test_depth_bound_is_opt_in() -> None:
    """count(*) over a big backlog is the expensive question; lag answers the real one."""
    payload = _payload(**{OUTBOX_DEPTH_FIELD: 465_119})

    assert evaluate(payload, now=NOW) == []
    assert evaluate(payload, now=NOW, max_outbox_depth=1_000) != []


def test_unparseable_lag_is_a_problem_not_a_pass() -> None:
    problems = evaluate(_payload(**{DRAIN_LAG_FIELD: "soon"}), now=NOW)

    assert any(DRAIN_LAG_FIELD in problem for problem in problems)


# ---------------------------------------------------------------------------
# The workflow.
# ---------------------------------------------------------------------------


def test_workflow_needs_no_cloud_credentials() -> None:
    """The whole reason for publishing the field: the check is a plain GET."""
    workflow = WORKFLOW.read_text()

    assert "configure-aws-credentials" not in workflow
    assert "role-to-assume" not in workflow
    assert "google-github-actions/auth" not in workflow
    assert "clickhouse.check_fleet_analytics_freshness" in workflow


def test_workflow_still_does_not_page_before_the_field_is_deployed() -> None:
    """Publishing the field in this repo is not the same as serving it.

    The predecessor shipped without a `schedule:` because nothing published the
    section. That is still true of the LIVE fleet: merging main auto-deploys
    the GCP control plane only, while AWS-EU and Azure are hand-run scripts and
    are already behind. A schedule enabled in the same commit as the publisher
    would file an issue every morning about clouds nobody redeployed, and a
    check that cries wolf is a check people learn to ignore.

    The same goes for `push:`. It looked different -- one run, on the merge,
    addressed to whoever is still holding the context -- but on that merge no
    plane publishes the section yet, so the run fails and the failure step
    opens a labelled PUBLIC issue about the precondition this header already
    documents as unmet.

    The structural version of this assertion, on parsed YAML, is in
    `tests/test_analytics_freshness_registry.py`; this one keeps the
    predecessor's own guard alive where the predecessor's tests live.
    """
    workflow = WORKFLOW.read_text()

    # Matched at the two-space indent a real trigger sits at under `on:`, so
    # the enabling instructions in the header do not satisfy it.
    assert "\n  schedule:" not in workflow
    assert "\n  push:" not in workflow
    assert "\n  workflow_dispatch:" in workflow
    assert "OPERATOR STEPS BEFORE THE SCHEDULE IS ENABLED" in workflow


def test_workflow_issue_names_the_never_installed_case() -> None:
    """The failure that actually happened must be in the runbook, not inferred."""
    workflow = WORKFLOW.read_text()

    assert "aws_eu_clickhouse_drain_install.sh" in workflow
    assert "the unit was never installed" in workflow
    assert "status **78**" in workflow
    assert "aws-analytics-freshness" in workflow


# ---------------------------------------------------------------------------
# The AWS-only entrypoint still works, and is now a slice of the fleet check.
# ---------------------------------------------------------------------------


def test_aws_alias_checks_only_aws(monkeypatch) -> None:
    """Kept so an operator can ask about one cloud mid-incident.

    The SCHEDULED job must never use it: a monitor restricted to one cloud is
    green about the cloud somebody named, which is how AWS-EU stayed silent
    while GCP looked fine.
    """
    fetched: list[str] = []

    def fake_fetch(url: str) -> dict[str, object]:
        fetched.append(url)
        return _payload(**{GENERATED_AT_FIELD: _now_iso()})

    monkeypatch.setattr(fleet_check, "fetch_status", fake_fetch)

    assert aws_check.main([]) == 0
    assert fetched == [fleet_check.DEFAULT_STATUS_URL]


def test_aws_alias_still_accepts_a_bare_status_url(monkeypatch) -> None:
    """The old CLI took a URL; the fleet CLI takes CLOUD=URL. Both must work."""
    fetched: list[str] = []

    def fake_fetch(url: str) -> dict[str, object]:
        fetched.append(url)
        return _payload(**{GENERATED_AT_FIELD: _now_iso()})

    monkeypatch.setattr(fleet_check, "fetch_status", fake_fetch)

    assert aws_check.main(["--status-url", "https://tr-eu.example/status.json"]) == 0
    assert aws_check.main(["--status-url=https://tr-eu.example/status.json"]) == 0
    assert fetched == ["https://tr-eu.example/status.json"] * 2


@pytest.mark.parametrize(
    "spelling",
    [
        ["--cloud", "gcp"],
        ["--cloud=gcp"],
        # argparse accepts any unambiguous prefix. Each of these binds `--cloud`
        # downstream just as surely as the two above, and each walked past the
        # string-comparison guard that only knew the two exact spellings -- into
        # an argv where it landed AFTER this alias's own `--cloud aws`, i.e. a
        # two-cloud run from an entrypoint whose name promises one.
        ["--clo=gcp"],
        ["--clo", "gcp"],
        ["--c", "gcp"],
        ["--cl=gcp"],
    ],
)
def test_aws_alias_refuses_to_be_used_as_a_cloud_selector(spelling: list[str]) -> None:
    """`--cloud`, however it is spelled, must not widen an AWS-only entrypoint.

    The guard reads argv the way argparse will rather than comparing strings,
    which is the only version of this that covers spellings nobody listed.
    """
    with pytest.raises(SystemExit):
        aws_check.main(spelling)


def test_aws_alias_accepts_an_abbreviated_status_url_too(monkeypatch) -> None:
    """The same reading applies to the option this alias translates.

    An abbreviation used to slip past the hand-written translator and arrive at
    the fleet parser as a bare URL, which failed with "--status-url expects
    CLOUD=URL" -- an error about an argument the operator never typed.
    """
    fetched: list[str] = []

    def fake_fetch(url: str) -> dict[str, object]:
        fetched.append(url)
        return _payload(**{GENERATED_AT_FIELD: _now_iso()})

    monkeypatch.setattr(fleet_check, "fetch_status", fake_fetch)

    assert aws_check.main(["--status-u", "https://tr-eu.example/status.json"]) == 0
    assert fetched == ["https://tr-eu.example/status.json"]


def test_aws_alias_reads_cloud_prefixes_by_name_not_by_the_first_equals_sign(
    monkeypatch,
) -> None:
    """A URL containing `=` is still a URL, and a cloud prefix names a cloud.

    The translation used to be "contains `=` -> already CLOUD=URL", which turned
    any URL with an `=` in it into a cloud named `https://tr-eu.example/tenant`
    and answered with "--status-url names unknown cloud(s)" -- an error about an
    argument the operator never typed. Both forms below must reach the fetcher
    unchanged.
    """
    fetched: list[str] = []

    def fake_fetch(url: str) -> dict[str, object]:
        fetched.append(url)
        return _payload(**{GENERATED_AT_FIELD: _now_iso()})

    monkeypatch.setattr(fleet_check, "fetch_status", fake_fetch)

    assert aws_check.main(["--status-url", "https://tr-eu.example/tenant=eu/status.json"]) == 0
    assert aws_check.main(["--status-url", "aws=https://tr-eu.example/status.json"]) == 0
    assert fetched == [
        "https://tr-eu.example/tenant=eu/status.json",
        "https://tr-eu.example/status.json",
    ]


def test_aws_alias_fails_when_aws_publishes_nothing(monkeypatch) -> None:
    monkeypatch.setattr(fleet_check, "fetch_status", lambda _url: {})

    assert aws_check.main([]) == 1
