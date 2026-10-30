package service

import (
	"testing"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/owner"
)

// TestTheDefaultsCarryTheStreamReapFloor: a node's owner reaps a stream's
// hold no sooner than the control plane's figures for the same stream, the
// reservation's 2 h and a heartbeat's 300 s renewal
// (docs/design/fast-admission-streams.md section 4.6 item 4), and does not
// yet write the floor into its records: the release that reads the field
// leaves it off (docs/runbooks/fastpath-deploy.md, "A record field the
// auditors must read first").
func TestTheDefaultsCarryTheStreamReapFloor(t *testing.T) {
	c := Defaults()
	want := owner.StreamReap{AfterAdmission: 2 * time.Hour, AfterHeartbeat: 300 * time.Second}
	if c.Owner.StreamReap != want {
		t.Fatalf("the defaults' floor %+v, want %+v", c.Owner.StreamReap, want)
	}
	if c.Owner.RecordReapFloor {
		t.Fatal("the release that reads the reap floor's field writes it")
	}
}
