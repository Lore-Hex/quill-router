package store

import (
	"context"
	"testing"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/store/storetest"
)

func member(t *testing.T, s *Store, address string) Member {
	t.Helper()
	members, _, err := s.Members(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	for _, m := range members {
		if m.Address == address {
			return m
		}
	}
	t.Fatalf("no member %s", address)
	return Member{}
}

func TestJoinTakesTheNextEpoch(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	address := storetest.UniqueID("node")
	first, firstAt, err := s.Join(ctx, address, []string{"owner"})
	if err != nil || first != 1 {
		t.Fatalf("a new node's epoch is %d, %v", first, err)
	}
	second, secondAt, err := s.Join(ctx, address, []string{"owner", "auditor"})
	if err != nil || second != 2 || !secondAt.After(firstAt) {
		t.Fatalf("a node's second start has epoch %d at %v after %v, %v", second, secondAt, firstAt, err)
	}
	m := member(t, s, address)
	if m.Epoch != 2 || m.State != Serving || len(m.Roles) != 2 || !m.StartedAt.Equal(secondAt) || !m.HeartbeatAt.Equal(secondAt) {
		t.Fatalf("the row after the second start: %+v", m)
	}
}

func TestHeartbeatNeedsTheNodesEpoch(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	address := storetest.UniqueID("node")
	if _, _, err := s.Join(ctx, address, []string{"owner"}); err != nil {
		t.Fatal(err)
	}
	_, restarted, err := s.Join(ctx, address, []string{"owner"})
	if err != nil {
		t.Fatal(err)
	}
	if written, _, err := s.Heartbeat(ctx, address, 1, Serving); err != nil || written {
		t.Fatalf("the old process's heartbeat: written %v, %v", written, err)
	}
	if m := member(t, s, address); !m.HeartbeatAt.Equal(restarted) {
		t.Fatalf("a refused heartbeat moved the row's to %v", m.HeartbeatAt)
	}
	written, at, err := s.Heartbeat(ctx, address, 2, Serving)
	if err != nil || !written {
		t.Fatalf("the node's own heartbeat: written %v, %v", written, err)
	}
	if m := member(t, s, address); !m.HeartbeatAt.Equal(at) || !at.After(restarted) {
		t.Fatalf("the heartbeat at %v left the row at %v", at, m.HeartbeatAt)
	}
}

func TestALeavingMemberStaysLeaving(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	address := storetest.UniqueID("node")
	epoch, _, err := s.Join(ctx, address, []string{"owner"})
	if err != nil {
		t.Fatal(err)
	}
	steps := []struct {
		state   string
		written bool
	}{
		{Withdrawn, true}, {Serving, true}, {Leaving, true}, {Serving, false}, {Withdrawn, false}, {Leaving, true},
	}
	for _, step := range steps {
		written, _, err := s.Heartbeat(ctx, address, epoch, step.state)
		if err != nil || written != step.written {
			t.Fatalf("a heartbeat %s: written %v, want %v, %v", step.state, written, step.written, err)
		}
	}
	if m := member(t, s, address); m.State != Leaving {
		t.Fatalf("the member is %s", m.State)
	}
	again, _, err := s.Join(ctx, address, []string{"owner"})
	if err != nil || again != epoch+1 || member(t, s, address).State != Serving {
		t.Fatalf("a leaving node that starts again: epoch %d, %v", again, err)
	}
}

func TestHeartbeatRefusesAStateThatIsNone(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	address := storetest.UniqueID("node")
	epoch, joined, err := s.Join(ctx, address, []string{"owner"})
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := s.Heartbeat(ctx, address, epoch, "gone"); err == nil {
		t.Fatal("a heartbeat with state gone is taken")
	}
	if m := member(t, s, address); m.State != Serving || !m.HeartbeatAt.Equal(joined) {
		t.Fatalf("the refused heartbeat wrote %+v", m)
	}
}

func TestMembersAreLiveBySpannersTime(t *testing.T) {
	ctx := context.Background()
	s := spikeStore(t)
	address := storetest.UniqueID("node")
	_, joined, err := s.Join(ctx, address, []string{"owner"})
	if err != nil {
		t.Fatal(err)
	}
	if m := member(t, s, address); !m.Live {
		t.Fatalf("a member that joined at %v is not live within an hour", joined)
	}
	cfg := testConfig()
	cfg.LiveFor = 50 * time.Millisecond
	brief, err := New(shared, cfg)
	if err != nil {
		t.Fatal(err)
	}
	time.Sleep(100 * time.Millisecond)
	members, read, err := brief.Members(ctx)
	if err != nil {
		t.Fatal(err)
	}
	for _, m := range members {
		if m.Address == address && (m.Live || read.Sub(m.HeartbeatAt) < 50*time.Millisecond) {
			t.Fatalf("a heartbeat %v before the read is live within 50 ms", read.Sub(m.HeartbeatAt))
		}
	}
}
