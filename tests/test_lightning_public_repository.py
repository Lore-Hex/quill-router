"""Publication cannot pick up local credentials, databases or mutable source."""

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from scripts.lightning.export_repository import APP, TEMPLATES, export


def run(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)  # noqa: S603, S607 -- disposable test repository


@pytest.fixture
def source(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    root.mkdir()
    run(root, "init", "-b", "main")
    run(root, "config", "user.name", "Publication test")
    run(root, "config", "user.email", "test@example.invalid")
    for name, content in {
        f"{APP}/LICENSE": "BSL fixture", f"{APP}/THIRD_PARTY_NOTICES": "notice",
        f"{APP}/web/index.html": "committed source", f"{TEMPLATES}/README.md": "README",
        f"{TEMPLATES}/.github/ISSUE_TEMPLATE/config.yml": "blank_issues_enabled: false",
        "scripts/lightning/node.sh": "#!/bin/sh\nexit 0\n",
        "docs/lightning-funding-production.md": "runbook",
        "unrelated.txt": "not public",
    }.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (root / "scripts/lightning/node.sh").chmod(0o755)
    run(root, "add", ".")
    run(root, "commit", "-m", "fixture")
    return root


def test_export_only_committed_allowlisted_files(source: Path, tmp_path: Path) -> None:
    (source / APP / "web/index.html").write_text("dirty content")
    (source / APP / ".env").write_text("local secret")
    output = tmp_path / "export"
    hashes = export(source, "HEAD", output)
    assert (output / APP / "web/index.html").read_text() == "committed source"
    assert not (output / APP / ".env").exists()
    assert not (output / "unrelated.txt").exists()
    assert (output / "LICENSE").read_text() == "BSL fixture"
    assert (output / "README.md").read_text() == "README"
    assert not (output / TEMPLATES).exists()
    assert (output / "scripts/lightning/node.sh").stat().st_mode & 0o111
    manifest = json.loads((output / "SOURCE.json").read_text())
    assert manifest["files_sha256"] == hashes
    assert len(manifest["source_commit"]) == 40
    for name, digest in hashes.items():
        assert digest == hashlib.sha256((output / name).read_bytes()).hexdigest()
    second = tmp_path / "second"
    assert export(source, "HEAD", second) == hashes
    assert (output / "SOURCE.json").read_bytes() == (second / "SOURCE.json").read_bytes()


@pytest.mark.parametrize("name", [".env", "test.db", "wallet.macaroon", "node_modules/token.js"])
def test_rejects_accidentally_committed_private_paths(source: Path, tmp_path: Path, name: str) -> None:
    path = source / APP / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("must not publish")
    run(source, "add", ".")
    run(source, "commit", "-m", "unsafe fixture")
    output = tmp_path / "export"
    with pytest.raises(ValueError, match="unsafe public source"):
        export(source, "HEAD", output)
    assert not output.exists()


def test_rejects_symlinks_and_existing_destination(source: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must not exist"):
        export(source, "HEAD", source)
    (source / APP / "secret.txt").symlink_to("/etc/passwd")
    run(source, "add", ".")
    run(source, "commit", "-m", "unsafe symlink fixture")
    with pytest.raises(ValueError, match="unsafe public source"):
        export(source, "HEAD", tmp_path / "export")
