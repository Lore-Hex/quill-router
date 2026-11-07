#!/bin/bash
# The pilot's hourly audit (docs/design/fast-admission-pilot.md, "What it
# checks"): each pilot workspace, funded as its credit rows say at the time
# (a top-up changes the funding). A problem, or an audit that cannot run,
# is written to the journal in the service's alert form, kind "other", which
# the "TR Fast path: other" policy notifies on. A funding read that fails
# is only logged, and that workspace waits for the next hour, so read the
# log for "funding unreadable". Results go to /var/tmp/pilot-audit.log,
# which survives a reboot.
set -u
DB=projects/quill-cloud-proxy/instances/trusted-router-nam6/databases/trusted-router
LOG=/var/tmp/pilot-audit.log
WORKSPACES="45819281-0ce9-4811-a0cd-c660ab3a116d 84cbb802-e054-486a-9226-e3cda4a799fb 8b682b96-d8ef-4631-b0ee-bbf41816f1f8"
while true; do
  for ws in $WORKSPACES; do
    funded=$(gcloud spanner databases execute-sql trusted-router --instance=trusted-router-nam6 \
      --project=quill-cloud-proxy \
      --sql="SELECT SUM(total_credits) FROM tr_credit_balance WHERE workspace_id = '$ws'" 2>/dev/null | tail -1)
    case "$funded" in
      ''|*[!0-9]*)
        # The funding could not be read: a watcher's failure, not a money
        # problem, so it is logged and the audit waits for the next hour.
        printf '%s %s funding unreadable: %s\n' "$(date -u +%FT%TZ)" "$ws" "$funded" >> $LOG
        continue;;
    esac
    out=$(/opt/fastpath/fastpathctl -database $DB -funded "$funded" audit $ws 2>&1); rc=$?
    problems=$(printf '%s' "$out" | python3 -c 'import json,sys
try: print(json.dumps(json.load(sys.stdin).get("problems")))
except Exception as e: print("unreadable: %s" % e)' 2>/dev/null)
    printf '%s %s funded=%s exit=%s problems=%s\n' "$(date -u +%FT%TZ)" "$ws" "$funded" "$rc" "$problems" >> $LOG
    if [ "$rc" != 0 ] && [ -z "${DRY:-}" ]; then
      printf '<3>{"message":"fastpath alert: the pilot audit of %s exited %s: %s","subject":"%s","logging.googleapis.com/labels":{"fastpath_alert":"other"}}\n' \
        "$ws" "$rc" "$(printf '%s' "$problems" | tr -d '"\\' | cut -c1-300)" "$ws" | systemd-cat -t fastpath-pilot-audit
    fi
  done
  [ -n "${DRY:-}" ] && break
  sleep 3600
done
