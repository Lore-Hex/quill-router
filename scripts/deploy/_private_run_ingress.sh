# shellcheck shell=bash
# Private run.app connectivity for Cloud Run jobs that must address one
# regional service directly while the destination only permits internal/LB
# ingress. Functions only; callers provide gc() and log().

PRIVATE_RUN_APP_JOB_NETWORK_ARGS=()

_upsert_private_run_app_record() {
  local zone="$1"
  local dns_name="$2"
  local record_type="$3"
  local rrdata="$4"

  if gc dns record-sets describe "$dns_name" \
      --zone="$zone" --type="$record_type" >/dev/null 2>&1; then
    gc dns record-sets update "$dns_name" \
      --zone="$zone" \
      --type="$record_type" \
      --ttl=300 \
      --rrdatas="$rrdata" >/dev/null
  else
    gc dns record-sets create "$dns_name" \
      --zone="$zone" \
      --type="$record_type" \
      --ttl=300 \
      --rrdatas="$rrdata" >/dev/null
  fi
}

ensure_private_run_app_access() {
  local region="$1"
  local network="${TR_SYNTHETIC_NETWORK:-default}"
  local subnet="${TR_SYNTHETIC_SUBNET:-default}"
  local zone="${TR_PRIVATE_RUN_APP_DNS_ZONE:-trusted-router-private-run-app}"
  local zone_json=""

  [ -n "$region" ] || {
    echo "ERROR: ensure_private_run_app_access requires a region" >&2
    return 2
  }
  case "$network:$subnet:$zone" in
    *[!a-zA-Z0-9_.:-]*)
      echo "ERROR: invalid private run.app network/subnet/zone name" >&2
      return 2
      ;;
  esac

  gc services enable dns.googleapis.com >/dev/null
  # Private Google Access plus the private VIP DNS records is the documented
  # path that makes another Cloud Run job's request count as internal without
  # forcing all of the job's provider traffic through a new Cloud NAT.
  gc compute networks subnets update "$subnet" \
    --region="$region" \
    --enable-private-ip-google-access \
    --quiet >/dev/null

  if ! gc dns managed-zones describe "$zone" >/dev/null 2>&1; then
    log "creating private run.app DNS zone ${zone} on ${network}"
    gc dns managed-zones create "$zone" \
      --dns-name=run.app. \
      --description="Private Google Access for regional Cloud Run monitor calls" \
      --visibility=private \
      --networks="$network" >/dev/null
  fi
  zone_json="$(gc dns managed-zones describe "$zone" --format=json)"
  if ! printf '%s' "$zone_json" | python3 -c '
import json
import sys

data = json.load(sys.stdin)
expected_network = sys.argv[1]
if data.get("dnsName") != "run.app.":
    raise SystemExit("private DNS zone does not own run.app.")
if data.get("visibility") != "private":
    raise SystemExit("run.app DNS zone is not private")
networks = data.get("privateVisibilityConfig", {}).get("networks", [])
urls = [str(item.get("networkUrl", "")) for item in networks]
if not any(url.rstrip("/").endswith("/networks/" + expected_network) for url in urls):
    raise SystemExit(f"run.app DNS zone is not attached to {expected_network}: {urls!r}")
' "$network"; then
    echo "ERROR: private run.app DNS zone ${zone} has unsafe drift" >&2
    return 1
  fi

  _upsert_private_run_app_record \
    "$zone" \
    run.app. \
    A \
    199.36.153.8,199.36.153.9,199.36.153.10,199.36.153.11
  _upsert_private_run_app_record "$zone" '*.run.app.' CNAME run.app.

  TR_SYNTHETIC_NETWORK="$network"
  TR_SYNTHETIC_SUBNET="$subnet"
  PRIVATE_RUN_APP_JOB_NETWORK_ARGS=(
    --network "$TR_SYNTHETIC_NETWORK"
    --subnet "$TR_SYNTHETIC_SUBNET"
    --vpc-egress private-ranges-only
  )
}
