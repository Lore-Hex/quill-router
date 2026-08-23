# shellcheck shell=bash
# Shared Cloud Run revision-tag helpers. Callers own traps and probe policy.

cloud_run_probe_tag_revision() {
  local service="$1"
  local region="$2"
  local project="$3"
  local tag="$4"
  local document

  document="$(gcloud run services describe "$service" \
    --region="$region" \
    --project="$project" \
    --format=json)" || return 1
  printf '%s' "$document" | python3 -c '
import json, sys
tag = sys.argv[1]
try:
    document = json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
for entry in document.get("status", {}).get("traffic", []):
    if entry.get("tag") == tag:
        print(entry.get("revisionName", ""))
        break
' "$tag"
}

cloud_run_probe_tag_reconcile() {
  local service="$1"
  local region="$2"
  local project="$3"
  local tag="$4"
  local revision="$5"
  local resolved

  gcloud run services update-traffic "$service" \
    --region="$region" \
    --project="$project" \
    --update-tags="${tag}=${revision}" \
    --quiet || return 1
  resolved="$(cloud_run_probe_tag_revision \
    "$service" "$region" "$project" "$tag")" || return 1
  if [ "$resolved" != "$revision" ]; then
    echo "ERROR: probe tag ${tag} resolves to ${resolved:-<nothing>}, expected ${revision}" >&2
    return 1
  fi
}

cloud_run_probe_tag_remove() {
  local service="$1"
  local region="$2"
  local project="$3"
  local tag="$4"
  local attempts="${TR_PROBE_TAG_REMOVE_ATTEMPTS:-3}"
  local retry_seconds="${TR_PROBE_TAG_REMOVE_RETRY_SECONDS:-2}"
  local attempt=1
  local resolved
  case "$attempts" in
    ''|*[!0-9]*|0)
      echo "ERROR: TR_PROBE_TAG_REMOVE_ATTEMPTS must be a positive integer" >&2
      return 1
      ;;
  esac
  while [ "$attempt" -le "$attempts" ]; do
    if gcloud run services update-traffic "$service" \
        --region="$region" \
        --project="$project" \
        --remove-tags="$tag" \
        --quiet; then
      if resolved="$(cloud_run_probe_tag_revision \
          "$service" "$region" "$project" "$tag")"; then
        if [ -z "$resolved" ]; then
          return 0
        fi
        echo "WARNING: probe tag ${tag} still resolves to ${resolved} after removal attempt ${attempt}/${attempts}" >&2
      else
        echo "WARNING: could not verify probe tag ${tag} removal in ${region} on attempt ${attempt}/${attempts}" >&2
      fi
    else
      echo "WARNING: could not remove probe tag ${tag} in ${region} on attempt ${attempt}/${attempts}" >&2
    fi
    if [ "$attempt" -lt "$attempts" ]; then
      sleep "$retry_seconds"
    fi
    attempt=$((attempt + 1))
  done
  echo "CRITICAL: probe tag ${tag} may still be addressable. Run exactly: gcloud run services update-traffic ${service} --region=${region} --project=${project} --remove-tags=${tag} --quiet" >&2
  return 1
}

cloud_run_probe_tagged_base_url() {
  local service="$1"
  local region="$2"
  local project="$3"
  local tag="$4"
  local revision="$5"
  local resolved
  local service_url

  resolved="$(cloud_run_probe_tag_revision \
    "$service" "$region" "$project" "$tag")" || return 1
  [ "$resolved" = "$revision" ] || {
    echo "ERROR: probe tag ${tag} resolves to ${resolved:-<nothing>}, expected ${revision}" >&2
    return 1
  }
  service_url="$(gcloud run services describe "$service" \
    --region="$region" \
    --project="$project" \
    --format='value(status.url)')" || return 1
  case "$service_url" in
    https://*.run.app)
      printf 'https://%s---%s\n' "$tag" "${service_url#https://}"
      ;;
    *) return 1 ;;
  esac
}
