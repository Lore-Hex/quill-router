// Package ring is the spike's membership (spike plan, §4): a node keeps its
// row in tr_fastpath_member with a heartbeat, and a front door watches the
// rows and picks a workspace shard's owner by rendezvous hashing over the
// live members that serve as owners. A member's going moves only the shards
// it had, and the ring needs no virtual nodes.
//
// Correctness does not rest on it (design §4.3): two nodes that both think
// they own a shard hold two leases. The spike measures how often that
// happens.
package ring

import (
	"context"
	"crypto/sha256"
	"encoding/binary"
	"slices"
	"strconv"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// OwnerRole is the role of a member that holds leases.
const OwnerRole = "owner"

// Membership is the store's membership, which *store.Store has.
type Membership interface {
	Join(ctx context.Context, address string, roles []string) (int64, time.Time, error)
	Heartbeat(ctx context.Context, address string, epoch int64, state string) (bool, time.Time, error)
	Members(ctx context.Context) ([]store.Member, time.Time, error)
}

// View is the members as one strong read saw them. Each member's Live is as
// of the read's timestamp, ReadAt, so liveness compares Spanner's times only.
type View struct {
	Members []store.Member
	ReadAt  time.Time
}

// Owners are the members that take new leases: live, serving, and with the
// owner role. A leaving member keeps the leases it has and takes none.
func (v View) Owners() []store.Member {
	var out []store.Member
	for _, m := range v.Members {
		if m.Live && m.State == store.Serving && slices.Contains(m.Roles, OwnerRole) {
			out = append(out, m)
		}
	}
	return out
}

// Owner picks the owner of a key, ShardKey's: of Owners, the one with the
// highest Score for the key, the lower address on a tie. It reports false
// when no member takes leases.
func (v View) Owner(key string) (store.Member, bool) {
	var best store.Member
	var bestScore uint64
	found := false
	for _, m := range v.Owners() {
		s := Score(m.Address, key)
		if !found || s > bestScore || (s == bestScore && m.Address < best.Address) {
			best, bestScore, found = m, s, true
		}
	}
	return best, found
}

// ShardKey is the key of a workspace's shard (design §4.3).
func ShardKey(workspace string, shard int64) string {
	return workspace + "/" + strconv.FormatInt(shard, 10)
}

// Score is a member's weight for a key: the first eight bytes, big-endian,
// of SHA-256 over the address, a zero byte and the key. Every process
// computes the same, so front doors that see the same members pick the same
// owner, and one that restarts at its address keeps its shards.
func Score(address, key string) uint64 {
	h := sha256.New()
	h.Write([]byte(address))
	h.Write([]byte{0})
	h.Write([]byte(key))
	var sum [sha256.Size]byte
	return binary.BigEndian.Uint64(h.Sum(sum[:0])[:8])
}
