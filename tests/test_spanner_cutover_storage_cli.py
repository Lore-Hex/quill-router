from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _run(
    tmp_path: Path, *, apply: bool = True, missing_buckets: bool = False,
    manifest_result: str = "found",
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    script = deploy / "spanner_zero_downtime_cutover.sh"
    shutil.copy2(ROOT / "scripts/deploy/spanner_zero_downtime_cutover.sh", script)
    (deploy / "_lib.sh").write_text(
        'PROJECT_ID=harness-project\n'
        'log() { printf "%s\\n" "$*" >&2; }\n'
        'gc() { gcloud --project "$PROJECT_ID" "$@"; }\n',
        encoding="utf-8",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake = fake_bin / "gcloud"
    fake.write_text(
        r'''#!/bin/bash
set -euo pipefail
printf '%s\n' "$*" >>"$CALL_LOG"
[[ "$1 $2" == "--project harness-project" ]] || exit 91
shift 2
case "$1 $2 $3" in
  "spanner backups describe") printf '%s\n' '2026-10-29T00:00:00Z' ;;
  "storage buckets describe") [[ "$MISSING_BUCKETS" == 0 ]] ;;
  "storage buckets create") : ;;
  "storage objects describe") : ;;
  "spanner databases describe") : ;;
  "spanner databases execute-sql") printf '1\n' ;;
  *)
    if [[ "$1 $2" == "storage ls" ]]; then
      [[ "$MANIFEST_RESULT" != error ]] || exit 92
      [[ "$MANIFEST_RESULT" != empty ]] || exit 0
      prefix='gs://harness-project-spanner-export/migration-seed/trusted-router-trusted-router'
      printf '%s\n' "$prefix-20261029/tr_entities-manifest.json" \
        "$prefix-20261028/tr_entities-manifest.json"
    elif [[ "$1 $2" == "storage cat" ]]; then
      printf '%s\n' '{"snapshotTime":"2026-10-29T01:02:03Z"}'
    else
      printf 'Unexpected cloud command: %s\n' "$*" >&2
      exit 93
    fi ;;
esac
''',
        encoding="utf-8",
    )
    fake.chmod(0o755)
    log = tmp_path / "calls.log"
    result = subprocess.run(  # noqa: S603 - isolated script with fake cloud CLI.
        ["/bin/bash", str(script), *(["--apply"] if apply else [])],
        env={**os.environ, "PATH": f"{fake_bin}:/usr/bin:/bin", "CALL_LOG": str(log),
             "MISSING_BUCKETS": str(int(missing_buckets)), "MANIFEST_RESULT": manifest_result},
        text=True, capture_output=True, check=False, timeout=10,
    )
    return result, log.read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize("missing_buckets", [False, True])
def test_cutover_uses_project_scoped_storage_and_manifest_objects(
    tmp_path: Path, missing_buckets: bool,
) -> None:
    result, calls = _run(tmp_path, missing_buckets=missing_buckets)
    assert result.returncode == 0, result.stderr
    creates = [call for call in calls if "storage buckets create" in call]
    assert len(creates) == (2 if missing_buckets else 0)
    assert all("--uniform-bucket-level-access" in call for call in creates)
    assert all("--location=us-central1" in call for call in creates)
    assert all(call.startswith("--project harness-project ") for call in calls)
    assert any("-*/tr_entities-manifest.json" in call for call in calls)
    manifest = (
        "gs://harness-project-spanner-export/migration-seed/"
        "trusted-router-trusted-router-20261029/tr_entities-manifest.json"
    )
    assert f"--project harness-project storage cat {manifest}" in calls
    assert "2026-10-29T01:02:03Z" in result.stderr
    assert "gsutil" not in (ROOT / "scripts/deploy/spanner_zero_downtime_cutover.sh").read_text()


@pytest.mark.parametrize("manifest_result", ["empty", "error"])
def test_missing_export_manifest_stops_before_import(tmp_path: Path, manifest_result: str) -> None:
    result, calls = _run(tmp_path, manifest_result=manifest_result)
    assert result.returncode != 0
    assert not any("storage cat" in call or " dataflow jobs " in call for call in calls)
    assert not any("spanner databases" in call for call in calls)


def test_cutover_dry_run_does_not_mutate_or_access_storage(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, apply=False)
    assert result.returncode == 0, result.stderr
    assert all("spanner backups describe" in call or "spanner databases describe" in call for call in calls)
    assert "[dry-run] gcloud dataflow jobs run" in result.stderr
