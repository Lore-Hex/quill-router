# shellcheck shell=bash
# Build a deterministic ClickHouse worker bundle from committed source only.

build_clickhouse_bundle() {
  local root="$1"
  local archive="$2"
  local dirty
  local stage

  dirty="$(git -C "$root" status --porcelain --untracked-files=all -- \
    clickhouse src/trusted_router)"
  if [ -n "$dirty" ]; then
    echo "refusing ClickHouse deployment from modified worker source:" >&2
    echo "$dirty" >&2
    return 1
  fi

  git -C "$root" archive \
    --format=tar.gz \
    --output="$archive" \
    HEAD \
    clickhouse \
    src/trusted_router

  stage="$(mktemp -d "${TMPDIR:-/tmp}/tr-clickhouse-bundle.XXXXXX")"
  if ! tar -xzf "$archive" -C "$stage"; then
    rm -rf "$stage"
    return 1
  fi
  if ! python3 - "$stage/src/trusted_router/data" <<'PY'
import json
from pathlib import Path
import sys

root = Path(sys.argv[1])
for path in sorted(root.rglob("*.json")):
    try:
        json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"provider bundle contains invalid JSON: {path}: {exc}")
PY
  then
    rm -rf "$stage"
    return 1
  fi
  rm -rf "$stage"
}
