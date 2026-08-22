# shellcheck shell=bash
# Shared Cloud Run revision-tag helpers. Callers own traps and probe policy.

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
  resolved="$(gcloud run services describe "$service" \
    --region="$region" \
    --project="$project" \
    --format="value(status.traffic[?tag='${tag}'].revisionName)")" || return 1
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
  gcloud run services update-traffic "$service" \
    --region="$region" \
    --project="$project" \
    --remove-tags="$tag" \
    --quiet
}

cloud_run_probe_tagged_base_url() {
  local service="$1"
  local region="$2"
  local project="$3"
  local tag="$4"
  local revision="$5"
  local resolved
  local service_url

  resolved="$(gcloud run services describe "$service" \
    --region="$region" \
    --project="$project" \
    --format="value(status.traffic[?tag='${tag}'].revisionName)")" || return 1
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
