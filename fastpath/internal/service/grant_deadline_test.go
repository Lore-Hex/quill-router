package service

import (
	"testing"

	"github.com/Lore-Hex/quill-router/fastpath/internal/owner"
)

// TestAGrantsDeadlineLeavesItsNodeAHeartbeat: a grant holds its node's
// member row shared until it ends, and the node's heartbeat writes that row.
// A heartbeat held back by a grant that takes all of owner.GrantDeadline
// lands at the grant's end, or at the ring's next beat after it, so the
// deadline and one beat together must come inside the time the store calls
// a member live for: otherwise one slow grant could take its own node out
// of the ring.
func TestAGrantsDeadlineLeavesItsNodeAHeartbeat(t *testing.T) {
	cfg := Defaults()
	if owner.GrantDeadline <= 0 || owner.GrantDeadline+cfg.Ring >= cfg.Store.LiveFor {
		t.Fatalf("an ask's deadline of %v and a heartbeat every %v do not fit inside a member's %v", owner.GrantDeadline,
			cfg.Ring, cfg.Store.LiveFor)
	}
}
