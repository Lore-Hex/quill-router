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
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode/utf8"
)

// maxLine bounds an event's line, which Read takes whole.
const maxLine = 1 << 20

// ID is an event's identity.
type ID struct {
	Node  string `json:"node"`
	Epoch int64  `json:"epoch"`
	Seq   int64  `json:"seq"`
}

// String is the identity as a message carries it: node, epoch and sequence,
// split by slashes; a node's name has none.
func (id ID) String() string {
	return id.Node + "/" + strconv.FormatInt(id.Epoch, 10) + "/" + strconv.FormatInt(id.Seq, 10)
}

// ParseID reads an identity as String writes it, and nothing else.
func ParseID(s string) (ID, error) {
	if parts := strings.Split(s, "/"); len(parts) == 3 {
		epoch, err1 := strconv.ParseInt(parts[1], 10, 64)
		seq, err2 := strconv.ParseInt(parts[2], 10, 64)
		id := ID{Node: parts[0], Epoch: epoch, Seq: seq}
		if err1 == nil && err2 == nil && id.valid() && id.String() == s {
			return id, nil
		}
	}
	return ID{}, fmt.Errorf("trace: %q is no event's identity", s)
}

// valid: an identity a recorder gives, which JSON and an HTTP header carry
// as it is: its node's name printable ASCII with no space and no slash, its
// epoch and sequence number at least 1.
func (id ID) valid() bool {
	if id.Node == "" || id.Epoch < 1 || id.Seq < 1 {
		return false
	}
	for i := range len(id.Node) {
		if c := id.Node[i]; c <= ' ' || c > '~' || c == '/' {
			return false
		}
	}
	return true
}

// Facts are what an event states that the specs' mappings read. A zero
// time states none; OwnerSeq is set, even to zero, only when the event
// states one.
type Facts struct {
	Lease    string    `json:"lease,omitempty"`
	Auth     string    `json:"auth,omitempty"`
	OwnerSeq *int64    `json:"owner_seq,omitempty"`
	Commit   time.Time `json:"commit,omitzero"`
	Read     time.Time `json:"read,omitzero"`
	Message  string    `json:"message,omitempty"`
	Key      string    `json:"key,omitempty"`
	Outcome  string    `json:"outcome,omitempty"`
	Detail   string    `json:"detail,omitempty"`
}

// Seq is an owner sequence number a fact states.
func Seq(n int64) *int64 { return &n }

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
// code that records runs without one. wall reads the wall clock, and
// elapsed the monotonic time since the recorder began, which the wall
// clock's steps do not move.
type Recorder struct {
	node    string
	epoch   int64
	wall    func() time.Time
	elapsed func() time.Duration

	mu   sync.Mutex
	seq  int64
	w    io.Writer
	err  error
	done bool
}

// New is a process's recorder, writing to w, for the node and the epoch its
// start took.
func New(w io.Writer, node string, epoch int64) (*Recorder, error) {
	if w == nil || !(ID{Node: node, Epoch: epoch, Seq: 1}).valid() {
		return nil, errors.New("trace: a recorder needs somewhere to write, a node whose name is printable " +
			"ASCII with no space or slash, and an epoch")
	}
	start := time.Now()
	return &Recorder{node: node, epoch: epoch, wall: time.Now, elapsed: func() time.Duration { return time.Since(start) },
		w: w}, nil
}

// Record records an event of kind, caused by cause if it has one, and
// returns its identity, which a message it sends carries. Each event is
// written whole, in one write, before Record returns, so a process killed
// after loses none. A recorder whose writer failed, or that was asked for
// an event Read could not take back as it was, such as one with no kind or
// with text that is not UTF-8, keeps numbering events and reports the
// failure at Close.
func (r *Recorder) Record(kind string, cause *ID, f Facts) ID {
	if r == nil {
		return ID{}
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	// The clocks are read under the lock, so a later number is never an
	// earlier monotonic time.
	wall, elapsed := r.wall(), r.elapsed()
	r.seq++
	f.Commit, f.Read = utc(f.Commit), utc(f.Read)
	e := Event{ID: ID{Node: r.node, Epoch: r.epoch, Seq: r.seq}, Mono: elapsed.Nanoseconds(), Wall: utc(wall),
		Kind: kind, Cause: cause, Facts: f}
	if r.done || r.err != nil {
		return e.ID
	}
	if kind == "" || (cause != nil && !cause.valid()) || !text(kind, f.Lease, f.Auth, f.Message, f.Key, f.Outcome, f.Detail) {
		r.err = fmt.Errorf("trace: event %d has no kind, a cause no recorder gives, or text that is not UTF-8", r.seq)
		return e.ID
	}
	line, err := json.Marshal(e)
	switch {
	case err != nil:
		r.err = err
	case len(line) >= maxLine:
		r.err = fmt.Errorf("trace: event %d is %d bytes, past %d", r.seq, len(line), maxLine-1)
	default:
		if _, err := r.w.Write(append(line, '\n')); err != nil {
			r.err = err
		}
	}
	return e.ID
}

// text: each string is UTF-8, which JSON keeps exactly.
func text(ss ...string) bool {
	for _, s := range ss {
		if !utf8.ValidString(s) {
			return false
		}
	}
	return true
}

// utc is a time as an event states it: in UTC, as JSON keeps it exactly.
func utc(t time.Time) time.Time {
	if t.IsZero() {
		return t
	}
	return t.Round(0).UTC()
}

// Close records nothing more; it reports the first event that failed.
func (r *Recorder) Close() error {
	if r == nil {
		return nil
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	r.done = true
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

// Read reads a process's events, in the order recorded: each line one JSON
// object, an event as a recorder writes it, with its identity, its clocks
// and its kind, and its keys as a recorder writes them (strict). It returns
// the events before the first line that is not one, as a process killed as
// it wrote leaves its last, with that line's fault.
func Read(rd io.Reader) ([]Event, error) {
	var out []Event
	sc := bufio.NewScanner(rd)
	sc.Buffer(make([]byte, 0, 64<<10), maxLine)
	for sc.Scan() {
		e, err := readEvent(sc.Bytes())
		if err != nil {
			return out, fmt.Errorf("trace: line %d: %w", len(out)+1, err)
		}
		out = append(out, e)
	}
	if err := sc.Err(); err != nil {
		return out, fmt.Errorf("trace: line %d: %w", len(out)+1, err)
	}
	return out, nil
}

// readEvent reads one line as an event.
func readEvent(line []byte) (Event, error) {
	if err := strict(line); err != nil {
		return Event{}, err
	}
	var e Event
	if err := json.Unmarshal(line, &e); err != nil {
		return Event{}, err
	}
	if !e.ID.valid() || (e.Cause != nil && !e.Cause.valid()) || e.Kind == "" || e.Wall.IsZero() || e.Mono < 0 {
		return Event{}, errors.New("no event a recorder writes")
	}
	return e, nil
}

// eventKeys are an event's keys as a recorder writes them, true for those
// every event has; id and cause are identities, with idKeys, all of which
// each has.
var (
	eventKeys = map[string]bool{"id": true, "mono": true, "wall": true, "kind": true, "cause": false,
		"lease": false, "auth": false, "owner_seq": false, "commit": false, "read": false, "message": false,
		"key": false, "outcome": false, "detail": false}
	idKeys = map[string]bool{"node": true, "epoch": true, "seq": true}
)

// strict reads line's first JSON object, whose keys must be as a recorder
// writes them: each known, in its case, at most once, and none null, which
// JSON's decoding into a struct would otherwise take, merging duplicates,
// matching keys in any case and reading a null as a zero. The decoding
// that follows takes nothing after the object.
func strict(line []byte) error {
	return object(json.NewDecoder(bytes.NewReader(line)), eventKeys)
}

// object reads one JSON object from d with the keys of keys.
func object(d *json.Decoder, keys map[string]bool) error {
	if t, err := d.Token(); err != nil || t != json.Delim('{') {
		return errors.New("not a JSON object")
	}
	seen := map[string]bool{}
	for d.More() {
		t, err := d.Token()
		if err != nil {
			return err
		}
		k, _ := t.(string)
		if _, known := keys[k]; !known || seen[k] {
			return fmt.Errorf("the key %q, unknown or twice", k)
		}
		seen[k] = true
		if k == "id" || k == "cause" {
			if err := object(d, idKeys); err != nil {
				return err
			}
			continue
		}
		v, err := d.Token()
		if err != nil {
			return err
		}
		if _, nested := v.(json.Delim); v == nil || nested {
			return fmt.Errorf("%q is not a string or a number", k)
		}
	}
	if _, err := d.Token(); err != nil {
		return err
	}
	for k, every := range keys {
		if every && !seen[k] {
			return fmt.Errorf("no %q", k)
		}
	}
	return nil
}
