package service

import (
	"context"
	"errors"
	"testing"
	"time"
)

// TestTheSwitchFailsClosed: a node's copy of the switch enables nothing
// before its first read and once it is three intervals old, enables a
// workspace a read has, and drops one the next read has not.
func TestTheSwitchFailsClosed(t *testing.T) {
	now := time.Date(2026, 10, 9, 12, 0, 0, 0, time.UTC)
	var got map[string]bool
	var failing bool
	w := &workspaceSwitch{every: time.Second, clock: func() time.Time { return now },
		read: func(context.Context) (map[string]bool, time.Time, error) {
			if failing {
				return nil, time.Time{}, errors.New("unreachable")
			}
			return got, now, nil
		}}
	ctx := context.Background()
	if w.Enabled("a") {
		t.Fatal("enabled before any read")
	}
	got = map[string]bool{"a": true}
	if err := w.refresh(ctx); err != nil {
		t.Fatal(err)
	}
	if !w.Enabled("a") || w.Enabled("b") {
		t.Fatalf("after reading {a}: a %v, b %v", w.Enabled("a"), w.Enabled("b"))
	}
	failing = true
	now = now.Add(3*time.Second - time.Nanosecond)
	if err := w.refresh(ctx); err == nil || !w.Enabled("a") {
		t.Fatalf("a failed read just under three intervals on: %v, a %v", err, w.Enabled("a"))
	}
	now = now.Add(time.Nanosecond)
	if w.Enabled("a") {
		t.Fatal("a copy three intervals old still enables a")
	}
	failing, got = false, map[string]bool{}
	if err := w.refresh(ctx); err != nil || w.Enabled("a") {
		t.Fatalf("a read without a: %v, a %v", err, w.Enabled("a"))
	}
	got = map[string]bool{"a": true}
	if err := w.refresh(ctx); err != nil || !w.Enabled("a") {
		t.Fatalf("a read with a again: %v, a %v", err, w.Enabled("a"))
	}
	// A read that takes three intervals answers what was true at its
	// start: the copy it makes is already as old as that, and enables
	// nothing.
	slow := w.read
	w.read = func(ctx context.Context) (map[string]bool, time.Time, error) {
		now = now.Add(3 * time.Second)
		return slow(ctx)
	}
	if err := w.refresh(ctx); err != nil || w.Enabled("a") {
		t.Fatalf("a read three intervals long: %v, a %v", err, w.Enabled("a"))
	}
}
