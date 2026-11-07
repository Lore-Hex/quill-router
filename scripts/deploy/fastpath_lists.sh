#!/usr/bin/env bash
# The fast path's workspace lists, in one file so every deploy that reads
# them reads the same values (docs/design/fast-admission-widening.md section
# 5.7). rollout.sh and the enabler job's script (fastpath_enabler_job.sh)
# source it and render each variable they read into their environment.
# tests/test_fastpath_lists.py holds every script that sources this file to
# these variables. Each is a literal, or a reference to one assigned above it.
#
# TR_FASTPATH_PILOT_WORKSPACES: the pilot's explicit set
# (docs/design/fast-admission-pilot.md), rendered by rollout.sh: Joseph's
# workspace and, on his word (2026-10-16), one customer's, admitted by the
# front doors from the us-central1 control plane; from 2026-11-03 also P1's
# synthetic workspace, his own and outside async settle admission, so the
# edge answers its load tests. Rollback: drop an ID, or
# TR_FASTPATH_PILOT_ENABLED=false. Emptied at WS2, when all mode turns on.
TR_FASTPATH_PILOT_WORKSPACES="45819281-0ce9-4811-a0cd-c660ab3a116d,84cbb802-e054-486a-9226-e3cda4a799fb,8b682b96-d8ef-4631-b0ee-bbf41816f1f8"

# TR_FASTPATH_PILOT_EXCLUDE: all mode's exclude list (Settings
# .fastpath_pilot_exclude, at most 64, comma-separated): the workspaces the
# pilot leaves out while TR_FASTPATH_PILOT_ALL is true. It is published in
# the edge policy and read by the pilot only in all mode, so with all mode
# off it changes nothing. Before all mode turns on (WS2) it must name every
# async pilot workspace outside the overlap list (the settings refuse to
# start otherwise) and the probes' workspaces (section 4(c)). Workspace ids
# only; never a key.
TR_FASTPATH_PILOT_EXCLUDE=""

# TR_FASTPATH_ENABLER_INCLUDE: the enabler's include list (Settings
# .fastpath_enabler_include, at most 32): workspaces exempt from its rank and
# from its bypass exclusion, and from nothing else (section 5.2). Today's
# pilot set, by reference, so the two cannot differ until WS2 empties the
# explicit set; that step gives this list the set's ids as its own literal.
TR_FASTPATH_ENABLER_INCLUDE="${TR_FASTPATH_PILOT_WORKSPACES}"
