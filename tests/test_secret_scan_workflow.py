"""Guard the gitleaks secret scan: pinned binary, fail-closed, redacted, narrow allowlists."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/secret-scan.yml"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def _steps() -> list[dict]:
    return _workflow()["jobs"]["gitleaks"]["steps"]


def _step(name_prefix: str) -> dict:
    return next(step for step in _steps() if step.get("name", "").startswith(name_prefix))


def test_runs_on_every_pr_and_push_to_main_with_read_only_token() -> None:
    workflow = _workflow()
    triggers = workflow[True]  # YAML 1.1 parses the GitHub `on` key as True.
    assert triggers == {
        "pull_request": {"branches": ["main"]},
        "push": {"branches": ["main"]},
        "workflow_dispatch": {},
    }
    assert workflow["permissions"] == {"contents": "read"}
    assert set(workflow["jobs"]) == {"gitleaks"}
    job = workflow["jobs"]["gitleaks"]
    assert "permissions" not in job
    assert "if" not in job  # Forks and PRs from forks are scanned too.
    assert "secrets." not in WORKFLOW.read_text()


def test_fails_closed() -> None:
    job = _workflow()["jobs"]["gitleaks"]
    assert "continue-on-error" not in job
    assert all("continue-on-error" not in step for step in job["steps"])
    for step in job["steps"]:
        if "run" in step:
            assert step["run"].lstrip().startswith("set -euo pipefail"), step.get("name")


def test_checkout_is_sha_pinned_with_full_history_and_no_credentials() -> None:
    checkout = _steps()[0]
    assert re.fullmatch(r"actions/checkout@[0-9a-f]{40}", checkout["uses"])
    assert checkout["with"] == {"fetch-depth": 0, "persist-credentials": False}
    # Only the checkout is an action; gitleaks itself is a verified binary.
    assert [step["uses"] for step in _steps() if "uses" in step] == [checkout["uses"]]


def test_gitleaks_binary_is_version_pinned_and_checksum_verified() -> None:
    env = _workflow()["env"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", env["GITLEAKS_VERSION"])
    assert re.fullmatch(r"[0-9a-f]{64}", env["GITLEAKS_SHA256"])
    run = _step("Install gitleaks")["run"]
    assert "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}" in run
    assert 'echo "${GITLEAKS_SHA256}  ${tarball}" | sha256sum -c -' in run
    assert 'grep -qxF "${GITLEAKS_SHA256}  ${tarball}" "$checksums"' in run
    # Verification happens before the archive is unpacked or run.
    assert run.index("sha256sum -c") < run.index("tar -xzf") < run.index("./gitleaks version")


def test_scan_is_redacted_and_limited_to_the_added_commits() -> None:
    step = _step("Scan added commits")
    assert step["env"] == {
        "EVENT": "${{ github.event_name }}",
        "PR_BASE": "${{ github.event.pull_request.base.sha }}",
        "PR_HEAD": "${{ github.event.pull_request.head.sha }}",
        "PUSH_BEFORE": "${{ github.event.before }}",
        "PUSH_AFTER": "${{ github.event.after }}",
    }
    run = step["run"]
    assert "${{" not in run  # Event data reaches the shell only through env.
    assert 'range="${PR_BASE}..${PR_HEAD}"' in run
    assert 'range="${PUSH_BEFORE}..${PUSH_AFTER}"' in run
    command = re.search(r"gitleaks git .*?\.$", run, re.S)
    assert command is not None
    flags = command.group(0).split()
    for flag in ("--redact", "--exit-code", "--config", '--log-opts="--diff-merges=first-parent', '$range"'):
        assert flag in flags, flag
    assert flags[flags.index("--exit-code") + 1] == "1"
    assert flags[flags.index("--config") + 1] == ".gitleaks.toml"
    assert not any(flag.startswith("--redact=") for flag in flags)  # Full redaction only.
    # Both ends must resolve before the scan, and the count is its own command.
    assert 'git rev-parse --verify --quiet "${end}^{commit}"' in run
    assert 'count=$(git rev-list --count "$range")' in run


def test_allowlists_extend_the_default_rules_and_stay_rule_scoped() -> None:
    config = tomllib.loads((ROOT / ".gitleaks.toml").read_text())
    assert config["extend"] == {"useDefault": True}
    # Rules may only be added: none may reuse a default rule's id.
    default_ids = {"generic-api-key", "jwt", "github-pat", "aws-access-token", "gcp-api-key"}
    for rule in config.get("rules", []):
        assert rule["id"].startswith("trustedrouter-"), rule["id"]
        assert rule["id"] not in default_ids
    assert "allowlist" not in config  # The legacy unscoped form.
    assert config["allowlists"]
    for allowlist in config["allowlists"]:
        assert set(allowlist) == {"description", "targetRules", "paths"}, allowlist["description"]
        assert allowlist["targetRules"], allowlist["description"]
        for path in allowlist["paths"]:
            assert path.startswith("^"), path
            re.compile(path)


def test_gitleaksignore_holds_only_commented_fingerprints() -> None:
    lines = (ROOT / ".gitleaksignore").read_text().splitlines()
    fingerprint = re.compile(r"[0-9a-f]{40}:[^:\s]+:[a-z0-9-]+:\d+")
    previous = ""
    for line in lines:
        if line and not line.startswith("#"):
            assert fingerprint.fullmatch(line), line
            # Every group of fingerprints sits under a comment saying why.
            assert previous.startswith("#") or fingerprint.fullmatch(previous), line
        previous = line


def test_trustedrouter_key_formats_have_rules() -> None:
    rules = {r["id"]: re.compile(r["regex"]) for r in tomllib.loads((ROOT / ".gitleaks.toml").read_text())["rules"]}
    api_key = "sk-tr-v1-" + "A" * 43
    session = "trsess-v1-" + "b" * 43
    assert rules["trustedrouter-api-key"].search(f'TR = "{api_key}"')
    assert rules["trustedrouter-api-key"].search(f'{{"Authorization": "Bearer {api_key}"}}')
    assert rules["trustedrouter-session-token"].search(f"cookie={session}")
    assert not rules["trustedrouter-api-key"].search("sk-tr-v1-short")
