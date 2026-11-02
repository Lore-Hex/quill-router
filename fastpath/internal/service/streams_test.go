package service

import (
	"testing"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/owner"
)

// TestTheDefaultsCarryTheStreamReapFloor: a node's owner reaps a stream's
// hold no sooner than the control plane's figures for the same stream, the
// reservation's 2 h and a heartbeat's 300 s renewal, plus the store's skew
// allowance (docs/design/fast-admission-streams.md section 4.6 item 4), and
// from increment 2b writes the floor into its records, so the auditor that
// drains a lease honours it too. This release is pinned only once every
// member runs increment 2's, which reads the field
// (docs/runbooks/fastpath-deploy.md, "A record field the auditors must read
// first").
func TestTheDefaultsCarryTheStreamReapFloor(t *testing.T) {
	c := Defaults()
	want := owner.StreamReap{AfterAdmission: 2 * time.Hour, AfterHeartbeat: 300 * time.Second}
	if c.Owner.StreamReap != want {
		t.Fatalf("the defaults' floor %+v, want %+v", c.Owner.StreamReap, want)
	}
	if !c.Owner.RecordReapFloor {
		t.Fatal("increment 2b's defaults do not write the reap floor into the records")
	}
	if c.Store.Skew <= 0 {
		t.Fatalf("the store's skew allowance %v, which the floor carries", c.Store.Skew)
	}
}

// TestTheOwnerTakesTheStoresSkewAllowance: the owner the service builds
// reads the store's skew allowance, the one its reap floor adds, as it
// reads the grace the floor's cap uses.
func TestTheOwnerTakesTheStoresSkewAllowance(t *testing.T) {
	c := Defaults()
	c.Store.Skew = 7 * time.Second
	c.Store.Grace = 3 * time.Minute
	c = c.agreed()
	if c.Owner.Skew != c.Store.Skew || c.Owner.Grace != c.Store.Grace {
		t.Fatalf("the owner's skew %v and grace %v, the store's %v and %v", c.Owner.Skew, c.Owner.Grace,
			c.Store.Skew, c.Store.Grace)
	}
}
