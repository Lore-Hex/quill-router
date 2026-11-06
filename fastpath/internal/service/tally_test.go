package service

import (
	"testing"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/owner"
)

// TestTheOwnerLineStatesTheTally: the owner's period line states each of
// the tally's figures, its waits in milliseconds and the acknowledgements'
// mean among them, and an empty period's mean as zero.
func TestTheOwnerLineStatesTheTally(t *testing.T) {
	got := ownerTallyLine(owner.Tally{Acks: 4, AcksSlow: 1, AckTotal: 2 * time.Second, AckMax: 1200 * time.Millisecond,
		DepthMax: 7, PublishFailures: 2, Heartbeats: 9, HeartbeatsSlow: 3, HeartbeatMax: 1400 * time.Millisecond,
		Terminals: 5, TerminalsSlow: 1, TerminalMax: 1100 * time.Millisecond})
	want := "fastpath owner: acks=4 ack_avg_ms=500 ack_max_ms=1200 acks_slow=1 depth_max=7 publish_failures=2 " +
		"heartbeats=9 heartbeat_max_ms=1400 heartbeats_slow=3 terminals=5 terminal_max_ms=1100 terminals_slow=1"
	if got != want {
		t.Fatalf("the line:\n%s\nwant\n%s", got, want)
	}
	if got := ownerTallyLine(owner.Tally{}); got != "fastpath owner: acks=0 ack_avg_ms=0 ack_max_ms=0 acks_slow=0 "+
		"depth_max=0 publish_failures=0 heartbeats=0 heartbeat_max_ms=0 heartbeats_slow=0 terminals=0 "+
		"terminal_max_ms=0 terminals_slow=0" {
		t.Fatalf("an empty period's line: %s", got)
	}
}
