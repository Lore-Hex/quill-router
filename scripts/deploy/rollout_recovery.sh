#!/usr/bin/env bash
# Publish/recover the private manifest-bound rollout bundle and active authority.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_TOOL="${SCRIPT_DIR}/rollout_state.py"
JOURNAL_TOOL="${SCRIPT_DIR}/rollout_journal.py"

usage() {
  cat >&2 <<'EOF'
Usage:
  rollout_recovery.sh publish MANIFEST
  rollout_recovery.sh recover gs://BUCKET/DEDICATED_PREFIX DESTINATION_DIRECTORY
  rollout_recovery.sh close MANIFEST

publish/close require TR_ROLLOUT_BUNDLE_GCS_URI and
TR_ROLLOUT_AUTHORITY_GCS_URI beneath TR_ROLLOUT_RECOVERY_GCS_PREFIX. The bundle
URI is a unique release prefix; the authority URI is the environment-wide CAS
record that supersedes older manifests. No object contents are printed.
EOF
  exit 2
}

[ "$#" -ge 2 ] || usage
COMMAND="$1"
TARGET="$2"
case "$COMMAND" in publish|recover|close) ;; *) usage ;; esac
if [ "$COMMAND" = recover ]; then
  [ "$#" -eq 3 ] || usage
  RECOVER_DESTINATION="$3"
else
  [ "$#" -eq 2 ] || usage
  RECOVER_DESTINATION=""
fi

for binary in gcloud python3; do
  command -v "$binary" >/dev/null || {
    echo "ERROR: required command is missing: ${binary}" >&2
    exit 1
  }
done

verify_recovery_uri_contract() {
  python3 - "$1" "$2" "$3" <<'PY'
import re
import sys

prefix, bundle, authority = sys.argv[1:]
match = re.fullmatch(
    r"gs://([a-z0-9][a-z0-9._-]{1,220}[a-z0-9])/([^\x00-\x1f\x7f]+)",
    prefix,
)
if (
    not match
    or prefix.endswith("/")
    or "//" in match.group(2)
    or any(part in {"", ".", ".."} for part in match.group(2).split("/"))
):
    raise SystemExit("TR_ROLLOUT_RECOVERY_GCS_PREFIX is not canonical")
bucket, object_prefix = match.groups()
if authority != f"gs://{bucket}/{object_prefix}/authority.json":
    raise SystemExit("rollout authority URI is outside the recovery prefix")
release_prefix = f"gs://{bucket}/{object_prefix}/releases/"
if not bundle.startswith(release_prefix):
    raise SystemExit("rollout bundle URI is outside the recovery releases prefix")
epoch = bundle[len(release_prefix):]
if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{7,159}", epoch):
    raise SystemExit("rollout bundle must use one unique canonical manifest epoch")
print(bucket)
PY
}

object_generation() {
  local uri="$1" metadata
  if metadata="$(gcloud storage objects describe "$uri" --format=json 2>&1)"; then
    python3 - "$metadata" <<'PY'
import json
import sys

generation = (json.loads(sys.argv[1]) or {}).get("generation")
if not str(generation).isdigit() or int(generation) <= 0:
    raise SystemExit("GCS object generation is invalid")
print(generation)
PY
    return
  fi
  case "$metadata" in
    *NOT_FOUND*|*"not found"*|*"No URLs matched"*) echo 0 ;;
    *) echo "ERROR: cannot inspect private rollout object" >&2; return 1 ;;
  esac
}

verify_bucket_contract() {
  local bucket="$1" bucket_json
  bucket_json="$(gcloud storage buckets describe "gs://${bucket}" --format=json)" || return 1
  python3 - "$bucket_json" <<'PY'
import json
import sys

bucket = json.loads(sys.argv[1])
iam = bucket.get("iamConfiguration") or {}
if (iam.get("uniformBucketLevelAccess") or {}).get("enabled") is not True:
    raise SystemExit("rollout recovery bucket must use uniform bucket IAM")
if str(iam.get("publicAccessPrevention", "")).lower() != "enforced":
    raise SystemExit("rollout recovery bucket must enforce public access prevention")
if (bucket.get("versioning") or {}).get("enabled") is not True:
    raise SystemExit("rollout recovery bucket must enable object versioning")
retention = bucket.get("retentionPolicy") or {}
try:
    seconds = int(retention.get("retentionPeriod") or 0)
except (TypeError, ValueError):
    seconds = 0
if seconds < 604800:
    raise SystemExit("rollout recovery bucket retention must be at least seven days")
PY
}

upload_immutable() {
  local source="$1" uri="$2" generation remote
  generation="$(object_generation "$uri")" || return 1
  if [ "$generation" = 0 ]; then
    gcloud storage cp "$source" "$uri" --if-generation-match=0 --quiet >/dev/null || return 1
  else
    remote="$(mktemp "${TMPDIR:-/tmp}/tr-recovery-existing-XXXXXX")"
    chmod 600 "$remote"
    gcloud storage cp "$uri" "$remote" --quiet >/dev/null || { rm -f "$remote"; return 1; }
    cmp -s "$source" "$remote" || {
      rm -f "$remote"
      echo "ERROR: immutable rollout recovery object already exists with different bytes" >&2
      return 1
    }
    rm -f "$remote"
  fi
}

cas_authority() {
  local manifest="$1" state_value="$2" authority="$3"
  local generation candidate observed current remote upload_failed=0
  generation="$(object_generation "$authority")" || return 1
  if [ "$generation" -gt 0 ]; then
    current="$(mktemp "${TMPDIR:-/tmp}/tr-authority-current-XXXXXX")"
    chmod 600 "$current"
    gcloud storage cp "$authority" "$current" --quiet >/dev/null || {
      rm -f "$current"
      return 1
    }
    python3 "$JOURNAL_TOOL" authority-validate \
      "$current" "$manifest" active "$BUNDLE_URI" || {
      rm -f "$current"
      echo "ERROR: refusing to overwrite another or closed rollout authority" >&2
      return 1
    }
    rm -f "$current"
    [ "$state_value" = closed ] || return 0
  elif [ "$state_value" = closed ]; then
    echo "ERROR: rollout authority does not exist" >&2
    return 1
  fi
  candidate="$(mktemp "${TMPDIR:-/tmp}/tr-authority-candidate-XXXXXX")"
  rm -f "$candidate"
  python3 "$JOURNAL_TOOL" authority-write "$manifest" "$candidate" \
    "$state_value" "$BUNDLE_URI" || return 1
  gcloud storage cp "$candidate" "$authority" \
    --if-generation-match="$generation" --quiet >/dev/null || upload_failed=1
  observed="$(object_generation "$authority")" || { rm -f "$candidate"; return 1; }
  [ "$observed" -gt "$generation" ] || {
    rm -f "$candidate"
    echo "ERROR: rollout authority generation did not advance" >&2
    return 1
  }
  remote="$(mktemp "${TMPDIR:-/tmp}/tr-authority-observed-XXXXXX")"
  chmod 600 "$remote"
  gcloud storage cp "$authority" "$remote" --quiet >/dev/null || {
    rm -f "$candidate" "$remote"
    return 1
  }
  cmp -s "$candidate" "$remote" || {
    rm -f "$candidate" "$remote"
    echo "ERROR: rollout authority CAS lost a concurrent update" >&2
    return 1
  }
  rm -f "$remote"
  if [ "$upload_failed" = 1 ]; then
    echo "rollout authority upload exited non-zero after applying; exact CAS verified" >&2
  fi
  rm -f "$candidate"
}

if [ "$COMMAND" = recover ]; then
  BUNDLE_URI="$TARGET"
  DESTINATION="$RECOVER_DESTINATION"
  AUTHORITY_URI="${TR_ROLLOUT_AUTHORITY_GCS_URI:-}"
  RECOVERY_PREFIX="${TR_ROLLOUT_RECOVERY_GCS_PREFIX:-}"
  [ -n "$AUTHORITY_URI" ] && [ -n "$RECOVERY_PREFIX" ] || {
    echo "ERROR: recovery requires authority and recovery-prefix GCS URIs" >&2
    exit 1
  }
  BUNDLE_BUCKET="$(verify_recovery_uri_contract \
    "$RECOVERY_PREFIX" "$BUNDLE_URI" "$AUTHORITY_URI")" || exit 1
  verify_bucket_contract "$BUNDLE_BUCKET"
  [ -n "$DESTINATION" ] || usage
  mkdir -p "$DESTINATION"
  DESTINATION="$(cd "$DESTINATION" && pwd)"
  descriptor="${DESTINATION}/bundle.json"
  gcloud storage cp "${BUNDLE_URI}/bundle.json" "$descriptor" --quiet >/dev/null
  chmod 600 "$descriptor"
  names="$(python3 - "$descriptor" <<'PY'
import json
import sys
from pathlib import Path

value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for item in value.get("files") or []:
    name = item.get("name")
    if not isinstance(name, str) or Path(name).name != name:
        raise SystemExit("unsafe recovery bundle filename")
    print(name)
PY
)" || exit 1
  while IFS= read -r name; do
    [ -n "$name" ] || continue
    gcloud storage cp "${BUNDLE_URI}/${name}" "${DESTINATION}/${name}" --quiet >/dev/null
    chmod 600 "${DESTINATION}/${name}"
  done <<<"$names"
  manifest_path="$(python3 "$JOURNAL_TOOL" bundle-validate "$descriptor" "$DESTINATION")"
  python3 "$STATE_TOOL" validate-manifest "$manifest_path"
  state_name="$(python3 - "$descriptor" <<'PY'
import json
import sys
from pathlib import Path

print(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))["promotion_state"])
PY
)" || exit 1
  state_uri="${BUNDLE_URI}/${state_name}"
  state_generation="$(object_generation "$state_uri")" || exit 1
  [ "$state_generation" -gt 0 ] || {
    echo "ERROR: recovery bundle has no durable promotion journal" >&2
    exit 1
  }
  gcloud storage cp "$state_uri" "${DESTINATION}/${state_name}" --quiet >/dev/null
  chmod 600 "${DESTINATION}/${state_name}"
  printf '%s\n' "$state_generation" >"${DESTINATION}/promotion-state.generation"
  chmod 600 "${DESTINATION}/promotion-state.generation"
  python3 "$JOURNAL_TOOL" validate "${DESTINATION}/${state_name}" "$manifest_path"
  gcloud storage cp "$AUTHORITY_URI" "${DESTINATION}/authority.json" --quiet >/dev/null
  chmod 600 "${DESTINATION}/authority.json"
  python3 "$JOURNAL_TOOL" authority-validate \
    "${DESTINATION}/authority.json" "$manifest_path" active "$BUNDLE_URI"
  printf '%s\n' "$manifest_path"
  exit 0
fi

MANIFEST="$TARGET"
python3 "$STATE_TOOL" validate-manifest "$MANIFEST"
MANIFEST_DIR="$(cd "$(dirname "$MANIFEST")" && pwd)"
MANIFEST="${MANIFEST_DIR}/$(basename "$MANIFEST")"
BUNDLE_URI="${TR_ROLLOUT_BUNDLE_GCS_URI:-}"
AUTHORITY_URI="${TR_ROLLOUT_AUTHORITY_GCS_URI:-}"
RECOVERY_PREFIX="${TR_ROLLOUT_RECOVERY_GCS_PREFIX:-}"
[ -n "$BUNDLE_URI" ] && [ -n "$AUTHORITY_URI" ] && \
  [ -n "$RECOVERY_PREFIX" ] || {
  echo "ERROR: rollout recovery requires prefix, bundle, and authority GCS URIs" >&2
  exit 1
}
BUNDLE_BUCKET="$(verify_recovery_uri_contract \
  "$RECOVERY_PREFIX" "$BUNDLE_URI" "$AUTHORITY_URI")" || exit 1
verify_bucket_contract "$BUNDLE_BUCKET"

if [ "$COMMAND" = publish ]; then
  names=""
  descriptor="${MANIFEST_DIR}/bundle.json"
  [ ! -e "$descriptor" ] || {
    echo "ERROR: refusing to overwrite local recovery bundle descriptor" >&2
    exit 1
  }
  python3 "$JOURNAL_TOOL" bundle-write "$MANIFEST" "$descriptor"
  names="$(python3 - "$descriptor" <<'PY'
import json
import sys
from pathlib import Path

for item in json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))["files"]:
    print(item["name"])
PY
)" || exit 1
  while IFS= read -r name; do
    [ -n "$name" ] || continue
    upload_immutable "${MANIFEST_DIR}/${name}" "${BUNDLE_URI}/${name}"
  done <<<"$names"
  upload_immutable "$descriptor" "${BUNDLE_URI}/bundle.json"
  state_name="$(jq -er '.promotion_state' "$MANIFEST")" || exit 1
  state_path="${MANIFEST_DIR}/${state_name}"
  python3 "$JOURNAL_TOOL" init "$state_path" "$MANIFEST"
  python3 - "$state_path" <<'PY'
import json
import sys
from pathlib import Path

state = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if state.get("lease") is not None or state.get("attempts") != []:
    raise SystemExit("initial recovery bundle journal is not idle and empty")
PY
  upload_immutable "$state_path" "${BUNDLE_URI}/${state_name}"
  cas_authority "$MANIFEST" active "$AUTHORITY_URI"
  exit 0
fi

authority_local="$(mktemp "${TMPDIR:-/tmp}/tr-authority-current-XXXXXX")"
chmod 600 "$authority_local"
gcloud storage cp "$AUTHORITY_URI" "$authority_local" --quiet >/dev/null
python3 "$JOURNAL_TOOL" authority-validate \
  "$authority_local" "$MANIFEST" active "$BUNDLE_URI"
rm -f "$authority_local"
cas_authority "$MANIFEST" closed "$AUTHORITY_URI"
