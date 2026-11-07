#!/bin/bash
# The pilot's watch for one workspace (docs/design/fast-admission-pilot.md):
# audit, the fast path's switch on, the watch alone (no -run; past any
# ceiling it turns the workspace off), audit. Usage: WS FUNDED MAX_SPEND
set -u
DB=projects/quill-cloud-proxy/instances/trusted-router-nam6/databases/trusted-router
WS=$1; FUNDED=$2; MAX_SPEND=$3
date -u; echo "workspace $WS funded $FUNDED max-spend $MAX_SPEND"
/opt/fastpath/fastpathctl -database $DB -funded $FUNDED audit $WS; rc=$?; echo "audit-before exit $rc"
if [ $rc -ne 0 ]; then echo PILOT_WATCH_ABORTED; exit 1; fi
/opt/fastpath/fastpathctl -database $DB enable $WS; echo "enable exit $?"
# -fresh 15m: a Monitoring series up to 15 minutes late is not yet a lost
# view (a 6-minute gap in one Spanner CPU series stopped the first watch,
# 2026-10-16 02:00Z). -misses 10: Cloud Monitoring answers Internal in bursts
# over a minute long (three trips 2026-10-29 01:33Z-02:22Z); -window 25m
# covers 10 misses of 30 s plus 15 minutes.
/opt/fastpath/fastpathwatch -database $DB -project quill-cloud-proxy -instance trusted-router-nam6 -subscriptions auditor,stager -workspace $WS -max-cpu 0.35 -max-undelivered 1000 -max-oldest 2m -overdue 5m -max-overdue 0 -max-spend $MAX_SPEND -fresh 15m -window 25m -misses 10
echo "watch exit $?"; date -u
/opt/fastpath/fastpathctl -database $DB -funded $FUNDED audit $WS; echo "audit-after exit $?"
echo PILOT_WATCH_DONE
