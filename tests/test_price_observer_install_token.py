"""Run the real observer installer against a fake gcloud: the dispatch token.

The service passes --dispatch-stalled with a token credential, so the
installer must fail closed, before copying anything to the host, when the
token's secret cannot be read or is not one fine-grained token, and must
never print the value.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "github_pat_" + "InstallerFixtureValue_" * 4
FILES = (
    "scripts/ops/install_price_refresh_observer.sh",
    "scripts/price_refresh_alerts.py",
    "scripts/ops/price-refresh-observer.service",
    "scripts/ops/price-refresh-observer.timer",
)
# Logs every call; serves the DSN; serves the token per $FAKE_TOKEN_MODE.
FAKE_GCLOUD = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_GCLOUD_LOG"
cat > /dev/null
out=
for arg in "$@"; do
  case $arg in --out-file=*) out=${arg#--out-file=} ;; esac
done
case "$*" in
  *"--secret=trustedrouter-sentry-dsn"*)
    printf 'https://abc@o1.ingest.us.sentry.io/1' > "$out" ;;
  *"--secret=trustedrouter-price-refresh-dispatch-token"*)
    case $FAKE_TOKEN_MODE in
      missing) echo "PERMISSION_DENIED: secretmanager.versions.access" >&2; exit 1 ;;
      classic) printf 'ghp_%s' "$FAKE_TOKEN_BODY" > "$out" ;;
      *) printf '%s' "$FAKE_TOKEN" > "$out" ;;
    esac ;;
  "config get-value account") echo operator@example.invalid ;;
esac
exit 0
"""


def git(cwd: Path, *args: str) -> str:
    executable = shutil.which("git")
    assert executable is not None
    return subprocess.run(  # noqa: S603 - fixed git subcommands in a temporary repository
        [executable, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "-c", "commit.gpgsign=false", *args],
        cwd=cwd, capture_output=True, text=True, check=True,
    ).stdout.strip()


@pytest.fixture
def install(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git is required")
    repo = tmp_path / "repo"
    for name in FILES:
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, repo / name)
    git(repo, "init", "-q")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "fixture")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    sha = git(repo, "rev-parse", "HEAD")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gcloud").write_text(FAKE_GCLOUD)
    (bin_dir / "gcloud").chmod(0o755)
    log = tmp_path / "gcloud.log"

    def run(mode: str, *, stale_installer: bool = False,
            ) -> tuple[subprocess.CompletedProcess[str], str]:
        log.write_text("")
        installer = repo / FILES[0]
        committed = git(repo, "show", f"{sha}:{FILES[0]}") + "\n"
        # A stale working tree: an installer other than the one at $sha.
        installer.write_text(committed + ("# older installer\n" if stale_installer else ""))
        env = {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "HOME": str(tmp_path), "TMPDIR": str(tmp_path), "FAKE_GCLOUD_LOG": str(log),
            "FAKE_TOKEN_MODE": mode, "FAKE_TOKEN": TOKEN,
            "FAKE_TOKEN_BODY": TOKEN.removeprefix("github_pat_"),
        }
        result = subprocess.run(  # noqa: S603 - the repository's installer, fake gcloud
            ["/bin/bash", str(repo / FILES[0]), sha], cwd=repo, env=env,
            capture_output=True, text=True, timeout=60, check=False,
        )
        return result, log.read_text()

    return run


@pytest.mark.parametrize("mode,message", [
    ("missing", "Could not read Secret Manager secret trustedrouter-price-refresh-dispatch-token"),
    ("classic", "is not one fine-grained GitHub token"),
])
def test_installer_fails_closed_without_the_dispatch_token(install, mode, message):
    result, calls = install(mode)
    assert result.returncode == 1
    assert message in result.stderr
    assert "Nothing was installed." in result.stderr
    # Nothing reached the host: no copy and no remote install, only cleanup.
    assert "compute scp" not in calls
    assert "sudo bash" not in calls
    assert TOKEN.removeprefix("github_pat_") not in result.stdout + result.stderr


def test_installer_copies_the_token_and_never_prints_it(install):
    result, calls = install("valid")
    assert result.returncode == 0, result.stderr
    scp = next(line for line in calls.splitlines() if line.startswith("compute scp"))
    assert "/github-dispatch.token " in scp
    assert "sudo bash" in calls
    assert TOKEN not in result.stdout + result.stderr + calls


def test_installer_from_another_commit_refuses_before_any_gcloud(install) -> None:
    """2026-11-19: an older installer installed a newer unit without its token."""
    result, calls = install("valid", stale_installer=True)
    assert result.returncode == 2
    assert "differs from scripts/ops/install_price_refresh_observer.sh at" in result.stderr
    assert "git worktree add --detach" in result.stderr
    assert "Nothing was installed." in result.stderr
    # No secret read, copy, ssh or cleanup: gcloud was never invoked.
    assert calls == ""


def test_installer_matching_the_target_commit_proceeds(install) -> None:
    result, calls = install("valid")
    assert result.returncode == 0, result.stderr
    assert "differs from" not in result.stderr
    assert "--secret=trustedrouter-sentry-dsn" in calls
    assert "compute scp" in calls
    assert "sudo bash" in calls


def test_installer_installs_the_token_where_the_service_loads_it():
    installer = (ROOT / FILES[0]).read_text()
    remote = installer.split("<<'REMOTE'", 1)[1]
    assert 'test -s "$staging/github-dispatch.token"' in remote
    install_line = ('install -m 0600 "$staging/github-dispatch.token" '
                    "/etc/tr-price-refresh/github-dispatch.token")
    assert install_line in remote
    # Validated before the unit that loads it is installed or started.
    assert remote.index('test -s "$staging/github-dispatch.token"') < remote.index(install_line)
    assert remote.index(install_line) < remote.index("systemctl start price-refresh-observer.service")
    service = (ROOT / FILES[2]).read_text()
    assert "LoadCredential=github-token:/etc/tr-price-refresh/github-dispatch.token" in service


def test_terraform_adopts_the_existing_secret_without_new_access():
    imports = (ROOT / "infra/imports.tf").read_text()
    assert ("to = google_secret_manager_secret.price_refresh_dispatch_token\n"
            '  id = "projects/quill-cloud-proxy/secrets/trustedrouter-price-refresh-dispatch-token"'
            ) in imports
    declared = (ROOT / "infra/price_refresh_observer.tf").read_text()
    assert 'secret_id = "trustedrouter-price-refresh-dispatch-token"' in declared
    assert "auto {}" in declared  # as created; a change would replace it
    assert "prevent_destroy = true" in declared
    # Read only by the operator's identity, which already reads every secret.
    assert "google_secret_manager_secret_iam" not in declared
