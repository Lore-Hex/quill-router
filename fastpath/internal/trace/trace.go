// Package trace is the spike's recorder (spike plan §6). Each process
// records its events to a local file, one JSON object a line. An event has
// an identity: its process's node, epoch and a sequence number of the
// process's own, which orders the process's events, with the process's
// monotonic and wall clocks. It names its cause, the event, in this process
// or another, whose message caused it: a request between nodes carries the
// sender's event identity, and the receiver records it. And it carries the
// facts the specs' mappings read: the lease and the authorization, an owner
// sequence number, a Spanner write's commit timestamp or a read's read
// timestamp, a Pub/Sub message's ID and ordering key.
package trace

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strconv"
	"strings"
	"sync"
	"time"
)

// ID is an event's identity.
type ID struct {
	Node  string `json:"node"`
	Epoch int64  `json:"epoch"`
	Seq   int64  `json:"seq"`
}

// String is the identity as a message carries it: node, epoch and sequence,
// split by slashes; a node's address has none.
func (id ID) String() string {
	return id.Node + "/" + strconv.FormatInt(id.Epoch, 10) + "/" + strconv.FormatInt(id.Seq, 10)
}

// ParseID reads an identity as String writes it.
func ParseID(s string) (ID, error) {
	parts := strings.Split(s, "/")
	if len(parts) != 3 || parts[0] == "" {
		return ID{}, fmt.Errorf("trace: %q is no event's identity", s)
	}
	epoch, err := strconv.ParseInt(parts[1], 10, 64)
	if err != nil || epoch < 1 {
		return ID{}, fmt.Errorf("trace: %q is no event's identity", s)
	}
	seq, err := strconv.ParseInt(parts[2], 10, 64)
	if err != nil || seq < 1 {
		return ID{}, fmt.Errorf("trace: %q is no event's identity", s)
	}
	return ID{Node: parts[0], Epoch: epoch, Seq: seq}, nil
}

// Facts are what an event states that the specs' mappings read.
type Facts struct {
	Lease    string    `json:"lease,omitempty"`
	Auth     string    `json:"auth,omitempty"`
	OwnerSeq int64     `json:"owner_seq,omitempty"`
	Commit   time.Time `json:"commit,omitzero"`
	Read     time.Time `json:"read,omitzero"`
	Message  string    `json:"message,omitempty"`
	Key      string    `json:"key,omitempty"`
	Outcome  string    `json:"outcome,omitempty"`
	Detail   string    `json:"detail,omitempty"`
}

// Event is one recorded event.
type Event struct {
	ID ID `json:"id"`
	// Mono is the process's monotonic clock, nanoseconds since its recorder
	// began; Wall its wall clock.
	Mono  int64     `json:"mono"`
	Wall  time.Time `json:"wall"`
	Kind  string    `json:"kind"`
	Cause *ID       `json:"cause,omitempty"`
	Facts
}

// Recorder records a process's events. A nil Recorder records nothing, so
// code that records runs without one.
type Recorder struct {
	node  string
	epoch int64
	start time.Time
	clock func() time.Time

	mu   sync.Mutex
	seq  int64
	w    *bufio.Writer
	err  error
	done bool
}

// New is a process's recorder, writing to w, for the node and the epoch its
// start took.
func New(w io.Writer, node string, epoch int64) (*Recorder, error) {
	if w == nil || node == "" || strings.Contains(node, "/") || epoch < 1 {
		return nil, errors.New("trace: a recorder needs somewhere to write, a node with no slash and an epoch")
	}
	return &Recorder{node: node, epoch: epoch, start: time.Now(), clock: time.Now, w: bufio.NewWriter(w)}, nil
}

// Record records an event of kind, caused by cause if it has one, and
// returns its identity, which a message it sends carries. A recorder whose
// writer failed keeps numbering events and reports the failure at Close.
func (r *Recorder) Record(kind string, cause *ID, f Facts) ID {
	if r == nil {
		return ID{}
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	// The clock is read under the lock, so a later number is never an
	// earlier time.
	now := r.clock()
	r.seq++
	e := Event{ID: ID{Node: r.node, Epoch: r.epoch, Seq: r.seq}, Mono: now.Sub(r.start).Nanoseconds(),
		Wall: now.Round(0).UTC(), Kind: kind, Cause: cause, Facts: f}
	if r.done || r.err != nil {
		return e.ID
	}
	line, err := json.Marshal(e)
	if err == nil {
		_, err = r.w.Write(append(line, '\n'))
	}
	if err != nil {
		r.err = err
	}
	return e.ID
}

// Close writes what is buffered and records nothing more; it reports the
// first write that failed.
func (r *Recorder) Close() error {
	if r == nil {
		return nil
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if !r.done {
		r.done = true
		if err := r.w.Flush(); err != nil && r.err == nil {
			r.err = err
		}
	}
	return r.err
}

type causeKey struct{}

// WithCause is ctx carrying the event that causes what is done under it.
func WithCause(ctx context.Context, id ID) context.Context {
	return context.WithValue(ctx, causeKey{}, id)
}

// Cause is the event ctx carries, if any.
func Cause(ctx context.Context) *ID {
	if id, ok := ctx.Value(causeKey{}).(ID); ok {
		return &id
	}
	return nil
}

// Header is the HTTP header a request between nodes carries its cause in.
const Header = "Fastpath-Cause"

// Read reads a process's events, in the order recorded.
func Read(rd io.Reader) ([]Event, error) {
	var out []Event
	d := json.NewDecoder(rd)
	d.DisallowUnknownFields()
	for {
		var e Event
		if err := d.Decode(&e); errors.Is(err, io.EOF) {
			return out, nil
		} else if err != nil {
			return out, fmt.Errorf("trace: event %d: %w", len(out)+1, err)
		}
		out = append(out, e)
	}
}
