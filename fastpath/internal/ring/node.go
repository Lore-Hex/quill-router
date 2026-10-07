package ring

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// ErrLost means the node's row refused its heartbeat: its address started
// again elsewhere, with a newer epoch, so this process owns nothing now.
var ErrLost = errors.New("ring: the node's row has a newer epoch")

// Node keeps a node's row (spike plan, §4): it joins with the address's next
// epoch, which its leases carry as their owner_epoch, then writes a
// heartbeat with its state every interval. Leaving is for good: once a node
// says it is leaving it stays leaving until it starts again, as the store
// holds. Withdrawn and serving go back and forth, for a front door that
// cannot reach owners (§4.3). A heartbeat the row refuses means the node is
// lost: it stops, and Lost is closed. A heartbeat that fails is tried again
// at the next interval.
type Node struct {
	m        Membership
	address  string
	epoch    int64
	interval time.Duration

	mu    sync.Mutex
	state string

	lost     chan struct{}
	lostOnce sync.Once
	stop     chan struct{}
	done     chan struct{}
	stopOnce sync.Once
}

// Start joins and starts the node's heartbeats.
func Start(ctx context.Context, m Membership, address string, roles []string, interval time.Duration) (*Node, error) {
	if interval <= 0 {
		return nil, fmt.Errorf("ring: a heartbeat interval of %v", interval)
	}
	epoch, _, err := m.Join(ctx, address, roles)
	if err != nil {
		return nil, err
	}
	n := &Node{m: m, address: address, epoch: epoch, interval: interval, state: store.Serving,
		lost: make(chan struct{}), stop: make(chan struct{}), done: make(chan struct{})}
	go n.run()
	return n, nil
}

// Address and Epoch are the node's.
func (n *Node) Address() string { return n.address }
func (n *Node) Epoch() int64    { return n.epoch }

// State is the state the node last wrote.
func (n *Node) State() string {
	n.mu.Lock()
	defer n.mu.Unlock()
	return n.state
}

// Lost is closed once the row refuses the node's heartbeat.
func (n *Node) Lost() <-chan struct{} { return n.lost }

// SetState writes the node's new state at once, in a heartbeat. A leaving
// node cannot serve or withdraw again; ErrLost means the row refused it.
func (n *Node) SetState(ctx context.Context, state string) error {
	n.mu.Lock()
	defer n.mu.Unlock()
	if n.state == store.Leaving && state != store.Leaving {
		return fmt.Errorf("ring: a leaving node cannot be %s until it starts again", state)
	}
	if err := n.beat(ctx, state); err != nil {
		return err
	}
	n.state = state
	return nil
}

// Stop stops the heartbeats and waits for the last to end. The row stays,
// and ages out of liveness.
func (n *Node) Stop() {
	n.stopOnce.Do(func() { close(n.stop) })
	<-n.done
}

func (n *Node) run() {
	defer close(n.done)
	ticker := time.NewTicker(n.interval)
	defer ticker.Stop()
	for {
		select {
		case <-n.stop:
			return
		case <-n.lost:
			return
		case <-ticker.C:
		}
		n.mu.Lock()
		// A heartbeat takes at most an interval, so a slow one does not
		// hold SetState for long, and one interval's failure is retried.
		ctx, cancel := context.WithTimeout(context.Background(), n.interval)
		_ = n.beat(ctx, n.state)
		cancel()
		n.mu.Unlock()
	}
}

// beat writes one heartbeat with n.mu held.
func (n *Node) beat(ctx context.Context, state string) error {
	select {
	case <-n.lost:
		return ErrLost
	default:
	}
	written, _, err := n.m.Heartbeat(ctx, n.address, n.epoch, state)
	if err != nil {
		return err
	}
	if !written {
		n.lostOnce.Do(func() { close(n.lost) })
		return ErrLost
	}
	return nil
}
