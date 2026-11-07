#!/usr/bin/env bash
# The fast path's workspace lists, in one file so every deploy that reads
# them reads the same values (docs/design/fast-admission-widening.md section
# 5.7). rollout.sh sources it and renders each variable into its ENV_VARS;
# the enabler job's script (widening PR 6) will source it too, with its
# include list beside this one. tests/test_fastpath_lists.py holds every
# script that sources this file to these variables.
#
# TR_FASTPATH_PILOT_EXCLUDE: all mode's exclude list (Settings
# .fastpath_pilot_exclude, at most 64, comma-separated): the workspaces the
# pilot leaves out while TR_FASTPATH_PILOT_ALL is true. It is published in
# the edge policy and read by the pilot only in all mode, so with all mode
# off it changes nothing. Before all mode turns on (WS2) it must name every
# async pilot workspace outside the overlap list (the settings refuse to
# start otherwise) and the probes' workspaces (section 4(c)). Workspace ids
# only; never a key.
TR_FASTPATH_PILOT_EXCLUDE=""
