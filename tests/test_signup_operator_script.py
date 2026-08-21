from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _write_harness(
    tmp_path: Path,
    *,
    bad_region: str = "",
    bad_candidate_region: str = "",
    traffic_failure_region: str = "",
) -> tuple[Path, Path]:
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    script = deploy / "set_new_signups.sh"
    shutil.copy2(ROOT / "scripts/deploy/set_new_signups.sh", script)
    (deploy / "_lib.sh").write_text(
        """set -euo pipefail
PROJECT_ID=harness-project
SERVICE=trusted-router
CONSOLE_SERVICE="${TR_CONSOLE_SERVICE:-trusted-router-console}"
LEGACY_CONSOLE_SERVICE="${TR_LEGACY_CONSOLE_SERVICE:-trusted-router}"
TR_CONTROL_PLANE_REGIONS=region-a,region-b
log() { printf '%s\\n' "$*" >&2; }
gc() { gcloud "$@"; }
""",
        encoding="utf-8",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake = fake_bin / "gcloud"
    fake.write_text(
        r'''#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"$FAKE_GCLOUD_LOG"
region=""
for arg in "$@"; do
  case "$arg" in --region=*) region="${arg#--region=}" ;; esac
done
state="$FAKE_GCLOUD_STATE/$region"
mkdir -p "$state"
if [ "$1 $2 $3" = "run services describe" ]; then
  [ "$4" = trusted-router-console ] || exit 92
  revision="$(cat "$state/serving" 2>/dev/null || printf 'trusted-router-console-old')"
  printf '{"status":{"traffic":[{"percent":100,"revisionName":"%s"}]}}\n' "$revision"
elif [ "$1 $2 $3" = "run revisions describe" ]; then
  revision="$4"
  surface=console
  if [ "$region" = "${FAKE_BAD_REGION:-}" ] && [ "$revision" = trusted-router-console-old ]; then
    surface=public
  fi
  if [ "$region" = "${FAKE_BAD_CANDIDATE_REGION:-}" ] && [ "$revision" != trusted-router-console-old ]; then
    surface=public
  fi
  gate="$(cat "$state/gate" 2>/dev/null || printf true)"
  printf '{"spec":{"containers":[{"env":[{"name":"TR_SERVICE_SURFACE","value":"%s"},{"name":"TR_NEW_SIGNUPS_ENABLED","value":"%s"}]}]}}\n' "$surface" "$gate"
elif [ "$1 $2 $3" = "run services update" ]; then
  [ "$4" = trusted-router-console ] || exit 92
  suffix="" desired=""
  for arg in "$@"; do
    case "$arg" in
      --revision-suffix=*) suffix="${arg#--revision-suffix=}" ;;
      --update-env-vars=TR_NEW_SIGNUPS_ENABLED=*) desired="${arg##*=}" ;;
    esac
  done
  revision="trusted-router-console-${suffix}"
  printf '%s\n' "$desired" >"$state/gate"
  printf '%s\n' "$revision"
elif [ "$1 $2 $3" = "run services update-traffic" ]; then
  [ "$4" = trusted-router-console ] || exit 92
  for arg in "$@"; do
    case "$arg" in --to-revisions=*) revision="${arg#--to-revisions=}"; revision="${revision%=100}" ;; esac
  done
  if [ "$region" = "${FAKE_TRAFFIC_FAILURE_REGION:-}" ] \
      && [ "$revision" != trusted-router-console-old ]; then
    printf '%s\n' "$revision" >"$state/serving"
    exit 73
  fi
  printf '%s\n' "$revision" >"$state/serving"
else
  printf 'unexpected fake gcloud call: %s\n' "$*" >&2
  exit 91
fi
''',
        encoding="utf-8",
    )
    fake.chmod(0o755)
    jq = fake_bin / "jq"
    jq.write_text(
        r'''#!/usr/bin/python3
import json
import sys

payload = json.load(sys.stdin)
if "--arg" in sys.argv:
    index = sys.argv.index("--arg")
    name = sys.argv[index + 2]
    env = payload["spec"]["containers"][0].get("env", [])
    print(next((item.get("value", "") for item in env if item.get("name") == name), ""))
else:
    revisions = {
        item["revisionName"]
        for item in payload.get("status", {}).get("traffic", [])
        if item.get("percent", 0) == 100
    }
    if len(revisions) != 1:
        raise SystemExit(5)
    print(next(iter(revisions)))
''',
        encoding="utf-8",
    )
    jq.chmod(0o755)
    log = tmp_path / "gcloud.log"
    state = tmp_path / "state"
    state.mkdir()
    env = tmp_path / "env.json"
    env.write_text(
        json.dumps(
            {
                "PATH": f"{fake_bin}:/bin:/usr/bin",
                "FAKE_GCLOUD_LOG": str(log),
                "FAKE_GCLOUD_STATE": str(state),
                "FAKE_BAD_REGION": bad_region,
                "FAKE_BAD_CANDIDATE_REGION": bad_candidate_region,
                "FAKE_TRAFFIC_FAILURE_REGION": traffic_failure_region,
            }
        ),
        encoding="utf-8",
    )
    return script, env


def _run(script: Path, env_file: Path, action: str) -> subprocess.CompletedProcess[str]:
    env = json.loads(env_file.read_text(encoding="utf-8"))
    return subprocess.run(  # noqa: S603 - copied repo script under a fake CLI PATH.
        ["/bin/bash", str(script), action],
        text=True,
        capture_output=True,
        env={**os.environ, **env},
        check=False,
    )


def test_signup_operator_moves_every_console_region_and_verifies_serving_revision(
    tmp_path: Path,
) -> None:
    script, env_file = _write_harness(tmp_path)

    result = _run(script, env_file, "disable")

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "gcloud.log").read_text(encoding="utf-8").splitlines()
    updates = [call for call in calls if call.startswith("run services update ")]
    traffic = [call for call in calls if call.startswith("run services update-traffic ")]
    assert len(updates) == 2
    assert len(traffic) == 2
    assert all("--no-traffic" in call for call in updates)
    assert all("latestCreatedRevisionName" in call for call in updates)
    assert all("TR_NEW_SIGNUPS_ENABLED=false" in call for call in updates)
    assert all("trusted-router-console" in call for call in updates + traffic)
    assert not any("run services update trusted-router " in call for call in calls)
    assert not any("run services update-traffic trusted-router " in call for call in calls)
    assert all("=100" in call for call in traffic)
    assert "new account creation is off" in result.stderr
    first_traffic = min(index for index, call in enumerate(calls) if call.startswith("run services update-traffic "))
    assert all(
        any(
            call.startswith("run revisions describe") and f"--region={region}" in call
            for call in calls[:first_traffic]
        )
        for region in ("region-a", "region-b")
    )
    assert not any(
        call.startswith("run services describe") and "latestCreatedRevisionName" in call
        for call in calls
    )


def test_signup_operator_preflights_the_whole_fleet_before_any_mutation(
    tmp_path: Path,
) -> None:
    script, env_file = _write_harness(tmp_path, bad_region="region-b")

    result = _run(script, env_file, "disable")

    assert result.returncode != 0
    assert "expected console" in result.stderr
    calls = (tmp_path / "gcloud.log").read_text(encoding="utf-8").splitlines()
    assert not any(call.startswith("run services update ") for call in calls)
    assert not any(call.startswith("run services update-traffic ") for call in calls)


def test_signup_operator_rejects_unknown_action_without_cloud_calls(tmp_path: Path) -> None:
    script, env_file = _write_harness(tmp_path)

    result = _run(script, env_file, "toggle")

    assert result.returncode == 2
    assert "usage:" in result.stderr
    assert not (tmp_path / "gcloud.log").exists()


def test_signup_operator_rejects_legacy_monolith_alias_without_cloud_calls(
    tmp_path: Path,
) -> None:
    script, env_file = _write_harness(tmp_path)
    env = json.loads(env_file.read_text(encoding="utf-8"))
    env["TR_CONSOLE_SERVICE"] = "trusted-router"
    env_file.write_text(json.dumps(env), encoding="utf-8")

    result = _run(script, env_file, "disable")

    assert result.returncode != 0
    assert "aliases the legacy combined monolith" in result.stderr
    assert not (tmp_path / "gcloud.log").exists()


def test_signup_operator_validates_every_staged_candidate_before_traffic(
    tmp_path: Path,
) -> None:
    script, env_file = _write_harness(tmp_path, bad_candidate_region="region-b")

    result = _run(script, env_file, "disable")

    assert result.returncode != 0
    assert "staged" in result.stderr
    calls = (tmp_path / "gcloud.log").read_text(encoding="utf-8").splitlines()
    assert len([call for call in calls if call.startswith("run services update ")]) == 2
    assert not any(call.startswith("run services update-traffic ") for call in calls)


def test_signup_operator_rolls_back_prior_regions_when_promotion_fails(
    tmp_path: Path,
) -> None:
    script, env_file = _write_harness(tmp_path, traffic_failure_region="region-b")

    result = _run(script, env_file, "disable")

    assert result.returncode != 0
    assert "rolling back" in result.stderr
    state = tmp_path / "state"
    assert (state / "region-a" / "serving").read_text(encoding="utf-8").strip() == (
        "trusted-router-console-old"
    )
