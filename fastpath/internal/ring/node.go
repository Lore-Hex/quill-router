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

// ErrStopped means the node was stopped, so it writes nothing more.
var ErrStopped = errors.New("ring: the node is stopped")

// Node keeps a node's row (spike plan, §4): it joins with the address's next
// epoch, which its leases carry as their owner_epoch, then writes a
// heartbeat with its state every interval. Leaving is for good: once a node
// says it is leaving it stays leaving until it starts again, as the store
// holds. Withdrawn and serving go back and forth, for a front door that
// cannot reach owners (§4.3). A heartbeat the row refuses means the node is
// lost: it stops, and Lost is closed. A heartbeat that fails is tried again
// at the next interval, with the state the node last meant: a write that
// failed may still have landed, so a node that meant to leave keeps saying
// so.
type Node struct {
	m        Membership
	address  string
	epoch    int64
	interval time.Duration

	// ctx ends when Stop begins, and with it every write under way.
	ctx    context.Context
	cancel context.CancelFunc

	mu sync.Mutex
	// meant is the state the node last set; written is the one its last
	// confirmed heartbeat carried.
	meant, written string

	lost     chan struct{}
	lostOnce sync.Once
	done     chan struct{}
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
	n := &Node{m: m, address: address, epoch: epoch, interval: interval, meant: store.Serving,
		written: store.Serving, lost: make(chan struct{}), done: make(chan struct{})}
	n.ctx, n.cancel = context.WithCancel(context.Background())
	go n.run()
	return n, nil
}

// Address and Epoch are the node's.
func (n *Node) Address() string { return n.address }
func (n *Node) Epoch() int64    { return n.epoch }

// State is the state the node's last confirmed heartbeat carried.
func (n *Node) State() string {
	n.mu.Lock()
	defer n.mu.Unlock()
	return n.written
}

// Lost is closed once the row refuses the node's heartbeat.
func (n *Node) Lost() <-chan struct{} { return n.lost }

// SetState writes the node's new state at once, in a heartbeat, and the
// node goes on writing it. A node that meant to leave cannot serve or
// withdraw again. ErrLost means the row refused it, ErrStopped that the
// node was stopped; any other error leaves the state meant, for the next
// heartbeat.
func (n *Node) SetState(ctx context.Context, state string) error {
	n.mu.Lock()
	defer n.mu.Unlock()
	if n.ctx.Err() != nil {
		return ErrStopped
	}
	if n.meant == store.Leaving && state != store.Leaving {
		return fmt.Errorf("ring: a leaving node cannot be %s until it starts again", state)
	}
	n.meant = state
	// The write ends when the caller's context does, or when Stop begins.
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	defer context.AfterFunc(n.ctx, cancel)()
	return n.beat(ctx)
}

// Stop stops the heartbeats, ending any write under way, and waits for the
// node's last write to end. The row stays, and ages out of liveness.
func (n *Node) Stop() {
	n.cancel()
	<-n.done
	// A SetState under way when Stop began has ended once the lock is free.
	n.mu.Lock()
	defer n.mu.Unlock()
}

func (n *Node) run() {
	defer close(n.done)
	ticker := time.NewTicker(n.interval)
	defer ticker.Stop()
	for {
		select {
		case <-n.ctx.Done():
			return
		case <-n.lost:
			return
		case <-ticker.C:
		}
		n.mu.Lock()
		// A heartbeat takes at most an interval, so a slow one holds
		// SetState back for little longer, and one interval's failure is
		// retried.
		ctx, cancel := context.WithTimeout(n.ctx, n.interval)
		_ = n.beat(ctx)
		cancel()
		n.mu.Unlock()
	}
}

// beat writes one heartbeat with the state meant, with n.mu held.
func (n *Node) beat(ctx context.Context) error {
	select {
	case <-n.lost:
		return ErrLost
	default:
	}
	if n.ctx.Err() != nil {
		return ErrStopped
	}
	written, _, err := n.m.Heartbeat(ctx, n.address, n.epoch, n.meant)
	if err != nil {
		return err
	}
	if !written {
		n.lostOnce.Do(func() { close(n.lost) })
		return ErrLost
	}
	n.written = n.meant
	return nil
}
