#!/usr/bin/env bash
# The fast path's workspace lists, in one file so every deploy that reads
# them reads the same values (docs/design/fast-admission-widening.md section
# 5.7). rollout.sh and the enabler job's script (fastpath_enabler_job.sh)
# source it and render each variable they read into their environment.
# tests/test_fastpath_lists.py holds every script that sources this file to
# these variables. Each is a literal, or a reference to one assigned above it.
#
# TR_FASTPATH_PILOT_WORKSPACES: the pilot's explicit set
# (docs/design/fast-admission-pilot.md), rendered by rollout.sh. Empty since
# WS2 (2026-11-09): rollout.sh sets TR_FASTPATH_PILOT_ALL=true, the settings
# refuse to start in all mode with a workspace here, and the pilot is every
# workspace the switch enables but the exclude list below. With
# TR_FASTPATH_PILOT_ALL=false and this still empty the pilot is nobody; to
# return to an explicit set, name its ids here in that same deploy.
TR_FASTPATH_PILOT_WORKSPACES=""

# TR_FASTPATH_PILOT_EXCLUDE: all mode's exclude list (Settings
# .fastpath_pilot_exclude, at most 64, comma-separated): the workspaces the
# pilot leaves out while TR_FASTPATH_PILOT_ALL is true, published in the edge
# policy and left out by the enabler. It names every async pilot workspace
# outside the overlap list (the settings refuse to start otherwise) and the
# probes' workspaces (section 4(c)): the Stage D probe's, the synthetic
# monitor's, and the async settle pilot's own probe workspace, in that order.
# Adding a workspace here stops the pilot for it at the next deploy, whatever
# its switch row says. Workspace ids only; never a key.
TR_FASTPATH_PILOT_EXCLUDE="91d7810e-93b2-4c37-b1bd-ba9227585416,d385c399-b245-4147-a528-0a4f6f170c71,162e441a-5d13-414f-92bd-7c6cd805faa3"

# TR_FASTPATH_ENABLER_INCLUDE: the enabler's include list (Settings
# .fastpath_enabler_include, at most 32): workspaces exempt from its rank and
# from its bypass exclusion, and from nothing else (section 5.2). The pilot's
# explicit set as it stood when WS2 emptied it, now this list's own literal:
# Joseph's workspace and, on his word (2026-10-16), one customer's; from
# 2026-11-03 also P1's synthetic workspace, his own and outside async settle
# admission, so the edge answers its load tests.
TR_FASTPATH_ENABLER_INCLUDE="45819281-0ce9-4811-a0cd-c660ab3a116d,84cbb802-e054-486a-9226-e3cda4a799fb,8b682b96-d8ef-4631-b0ee-bbf41816f1f8"
