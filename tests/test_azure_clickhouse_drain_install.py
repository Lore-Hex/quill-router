"""Static guards on scripts/deploy/azure_clickhouse_drain_install.sh.

The drain is the process the AWS-EU outage was missing for fifteen days, and
every property below is one whose removal still produces an installer that
"succeeds" while the drain delivers nothing.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/deploy/azure_clickhouse_drain_install.sh"


def _script() -> str:
    return SCRIPT.read_text()


def test_secrets_are_fetched_on_the_node_not_passed_in() -> None:
    """systemd's EnvironmentFile performs NO command substitution.

    A literal $(curl ...) written into it BECOMES the password: non-empty, so
    every startup check passes, and then authentication fails forever while the
    outbox grows. So the fetch and the write happen in one step ON the node,
    and the values never pass through this script's output or arguments.
    """
    script = _script()

    assert "identity/oauth2/token" in script
    assert "vault.azure.net/secrets/" in script
    # And the installer refuses an env file that captured a command instead --
    # for BOTH secrets, since either one landing as an unexpanded $(...) is
    # non-empty, passes every startup check, and then fails auth forever.
    assert "CH_PASSWORD is a literal command; refusing" in script
    assert "PGPASSWORD is a literal command; refusing" in script


def test_the_postgres_password_is_not_put_in_the_dsn() -> None:
    """The drain refuses a DSN carrying one, and says why:

        DSN must not contain a password; set PGPASSWORD instead so the secret
        does not appear in argv

    The DSN is handed to libpq, where it can surface in a process listing. The
    first version of this installer wrote it there and the drain rejected every
    connection -- failed_shards=32 -- while looking perfectly healthy from the
    outside: the unit was active and the outbox simply never drained.
    """
    script = _script()

    assert "PGPASSWORD=%s" in script
    assert "the DSN carries a password; the drain refuses that" in script


def test_it_proves_the_secret_landed_without_printing_it() -> None:
    """Length and shape only. A drain install that echoes a provider password
    into a terminal has leaked it to the scrollback and the CI log."""
    script = _script()

    assert 'print \\"CH_PASSWORD length=\\" length(\\$2)' in script
    assert "cut -d= -f1" in script  # names only


def test_the_payload_is_chunked_and_checksummed() -> None:
    """`az vm run-command` truncates around 256KB SILENTLY.

    A truncated tarball extracts into a partial tree that imports far enough to
    look installed. The sha256 is checked on the node before extraction.
    """
    script = _script()

    assert "CHUNK_BYTES" in script
    assert "split -b" in script
    assert "sha256sum" in script
    assert "sha256 mismatch" in script


def test_static_is_excluded_from_the_payload() -> None:
    """9.5MB of images the drain never imports, which would turn 19 chunks into
    130 and the install into an hour."""
    assert "--exclude='static'" in _script()


def test_appledouble_sidecars_are_removed_and_then_asserted_gone() -> None:
    """COPYFILE_DISABLE stops macOS writing ._* into the tar; the delete and
    the assertion catch the case where it did anyway. They land beside every
    file and break `python -m` imports."""
    script = _script()

    assert "COPYFILE_DISABLE=1" in script
    assert "-name '._*' -delete" in script
    assert "AppleDouble sidecars survived" in script


def test_the_code_is_import_tested_before_it_replaces_what_runs() -> None:
    """A staging dir that cannot import is a staging dir. The same tree moved
    into place first is an outage."""
    script = _script()

    smoke = script.index("import smoke test")
    swap = script.index("swap into place")
    assert smoke < swap, "the smoke test must run before the swap"


def test_the_unit_is_restarted_not_just_enabled() -> None:
    """`systemctl enable --now` does NOT restart an already-running unit, so a
    reinstall leaves the OLD process running the OLD code -- observed on
    AWS-EU, where the PID never changed across a reinstall."""
    script = _script()

    assert "systemctl restart" in script


def test_it_refuses_to_run_without_the_scoped_role() -> None:
    """The drain connects as tr_drain, which has SELECT and DELETE on exactly
    one table. Installing before that role exists produces a unit that starts,
    fails every connection, and delivers nothing."""
    script = _script()

    assert "tr-azure-pg-drain-password" in script
    assert "create-drain-role.sh first" in script


def test_run_command_failures_are_not_read_as_success() -> None:
    """`az vm run-command invoke` exits 0 even when the script inside failed.

    Without an explicit marker every remote step "succeeds" and the installer
    reports a drain it never installed.
    """
    script = _script()

    assert "__TR_STEP_OK__" in script
    assert "the remote script failed" in script


def test_the_closing_note_refuses_to_call_active_evidence() -> None:
    """"The unit is active" is the claim the AWS-EU outage would also have
    passed, once the unit existed. Rows moving is the bar."""
    script = _script()

    assert "The unit being active is NOT the evidence" in script
    assert "SELECT count() FROM activity_generations" in script
