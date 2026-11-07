package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// refused is the exit code of a hold command that refused, writing nothing,
// as decide delete's refusal is: the answer is printed first, so the caller
// sees what stood in the way.
const refused = 2

// resumeFirst is the refusal of a release under the pause.
var resumeFirst = errors.New("the enabler is paused: resume first (fastpathctl enabler resume)")

// heldRows and unheldRows are the rows as the answers print them.
func heldRows(rows []store.HeldRow) []map[string]any {
	out := []map[string]any{}
	for _, r := range rows {
		out = append(out, map[string]any{"workspace": r.Workspace, "enabled": r.Enabled, "changed_at": r.ChangedAt,
			"reason": r.Reason, "held_at": r.HeldAt})
	}
	return out
}

func unheldRows(rows []store.UnheldRow) []map[string]any {
	out := []map[string]any{}
	for _, r := range rows {
		out = append(out, map[string]any{"workspace": r.Workspace, "changed_at": r.ChangedAt, "under_kill": r.UnderKill})
	}
	return out
}

// holdCommand is release, release-all, enabler, release-stamped,
// hold-backfill and holds (docs/design/fast-admission-widening.md, section
// 5.6), its exit code and what went wrong; arg is release's workspace or
// enabler's verb.
func holdCommand(ctx context.Context, s *store.Store, command, arg string, stamp time.Time, yes bool,
	out io.Writer) (int, error) {
	var answer map[string]any
	code := 0
	var why error
	switch command {
	case "release":
		got, err := s.Release(ctx, arg)
		if err != nil {
			return 1, err
		}
		answer = map[string]any{"workspace": arg, "released": got.Released, "refused": got.Paused, "at": got.At}
		switch {
		case got.Paused:
			code, why = refused, resumeFirst
		case !got.Released:
			code = notDone
		}
	case "release-all":
		got, err := s.ReleaseAll(ctx)
		if err != nil {
			return 1, err
		}
		answer = map[string]any{"fleet_hold_removed": got.Fleet, "kills_removed": got.Kills, "refused": got.Paused,
			"at": got.At}
		switch {
		case got.Paused:
			code, why = refused, resumeFirst
		case !got.Fleet && got.Kills == 0:
			code = notDone
		}
	case "enabler":
		switch arg {
		case "pause":
			got, err := s.Pause(ctx)
			if err != nil {
				return 1, err
			}
			answer = map[string]any{"paused": true, "already": !got.Paused, "held_at": got.HeldAt}
		case "resume":
			got, err := s.Resume(ctx)
			if err != nil {
				return 1, err
			}
			answer = map[string]any{"resumed": got.Paused && len(got.Refused) == 0, "paused": got.Paused,
				"enabled_and_held": heldRows(got.Refused), "at": got.At}
			switch {
			case !got.Paused:
				code = notDone
			case len(got.Refused) > 0:
				code, why = refused, fmt.Errorf("%d rows are enabled and held: enable or disable each, then resume",
					len(got.Refused))
			default:
				// The window is gone with the pause, so it is printed from
				// the resume's own transaction.
				answer["window"] = map[string]any{"from": got.From, "until": got.At}
				answer["held"], answer["held_workspaces"] = len(got.Held), got.Held
				answer["under_kill"], answer["already_held"] = got.UnderKill, got.AlreadyHeld
			}
		case "arm":
			got, err := s.Arm(ctx)
			if err != nil {
				return 1, err
			}
			answer = map[string]any{"armed": got.Armed, "already": got.Already, "unheld_disabled": unheldRows(got.Unheld),
				"at": got.At}
			if !got.Armed {
				code, why = refused, fmt.Errorf("%d rows are turned off with no hold of their own: hold-backfill, "+
					"or disable each, then arm", len(got.Unheld))
			}
		}
	case "release-stamped":
		got, err := s.ReleaseStamped(ctx, stamp, yes)
		if err != nil {
			return 1, err
		}
		answer = map[string]any{"stamp": stamp, "holds": len(got.Workspaces), "workspaces": len(got.Workspaces),
			"workspace_ids": got.Workspaces, "released": got.Applied, "refused": yes && got.Paused, "at": got.At}
		switch {
		case yes && got.Paused:
			code, why = refused, resumeFirst
		case len(got.Workspaces) == 0:
			code = notDone
		}
	case "hold-backfill":
		got, err := s.HoldBackfill(ctx)
		if err != nil {
			return 1, err
		}
		answer = map[string]any{"held": len(got.Held), "workspaces": got.Held, "refused": got.Armed, "at": got.At}
		if got.Armed {
			code, why = refused, errors.New("the enabler is armed: after the arm, rows turned off with no hold "+
				"include the enabler's own, which a backfill would wrongly hold")
		}
	case "holds":
		got, err := s.Holds(ctx)
		if err != nil {
			return 1, err
		}
		answer = map[string]any{"fleet_hold": got.Fleet, "paused": got.Pause, "armed": got.Armed, "kills": got.Kills,
			"holds": got.Holds, "enabled_and_held": heldRows(got.EnabledHeld),
			"unheld_disabled": map[string]any{"under_kill": got.UnderKill, "other": got.Other,
				"rows": unheldRows(got.UnheldDisabled)},
			"read_at": got.ReadTS}
		if len(got.UnheldDisabled) > 0 {
			code = notDone
		}
	}
	// An answer not written is a failure, whatever was done: the caller
	// cannot tell what happened.
	if err := json.NewEncoder(out).Encode(answer); err != nil {
		return 1, err
	}
	return code, why
}
