// Package record is the settle log's wire format (design §4.2, §4.5, §4.8,
// §4.9): what an owner publishes on its lease's ordering key, and the fence
// tick the auditor publishes there. Every record carries money fields only,
// about 250 bytes; a request's full record goes to the record topic, and a
// terminal names it by its digest.
//
// A record is versioned JSON. Decode refuses a version, a kind or a field it
// does not know, and a record missing what its kind needs, so an owner and an
// auditor of two releases never read each other's records differently.
package record

import (
	"bytes"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"reflect"
	"slices"
	"time"
)

// Version is the format's.
const Version = 1

// Kind is what a record is.
type Kind string

// The owner's kinds, each with the lease's next owner sequence number, and
// the auditor's fence tick, with none.
const (
	Heartbeat  Kind = "hb"
	Settle     Kind = "settle"
	Refund     Kind = "refund"
	Reap       Kind = "reap"
	Release    Kind = "release"
	Checkpoint Kind = "ckpt"
	Handoff    Kind = "handoff"
	Manifest   Kind = "manifest"
	Tick       Kind = "tick"
)

// Terminal reports whether a kind decides its authorization's outcome.
func (k Kind) Terminal() bool {
	return k == Settle || k == Refund || k == Reap || k == Release
}

// Record is one record of a lease's order.
type Record struct {
	Version int    `json:"v"`
	Lease   string `json:"lease"`
	// Epoch is the owner's, which its lease carries, and Seq the lease's
	// owner sequence number, from 1; a republish repeats both. A tick has
	// neither.
	Epoch int64 `json:"epoch,omitempty"`
	Seq   int64 `json:"seq,omitempty"`
	Kind  Kind  `json:"kind"`

	// A heartbeat or a terminal names its authorization, and its hold's
	// estimate.
	Auth     string `json:"a,omitempty"`
	Estimate int64  `json:"est,omitempty"`
	// A terminal's charge (a settle's, or a reap's, its snapshot's running
	// charge), and the owner's shortfall total after it (§4.2).
	Charge    int64 `json:"charge,omitempty"`
	Shortfall int64 `json:"sf,omitempty"`
	// Digest is the full record's, a settle's or the reap's that the owner
	// built (§4.9); Drain is the record ID of the drain-log row an owner
	// adopted and publishes as its own (§4.5); SnapshotSeq is the owner
	// sequence number of the heartbeat record a reap charges.
	Digest      []byte `json:"digest,omitempty"`
	Drain       string `json:"drain,omitempty"`
	SnapshotSeq int64  `json:"snap,omitempty"`
	// Basis is what a reap of the hold needs to build its full record (the
	// request's terms), carried by a heartbeat, the first at least, so a reap
	// never depends on the authorization's record (§4.9). Opaque here.
	Basis []byte `json:"basis,omitempty"`

	Snapshot   *Snapshot     `json:"hb,omitempty"`
	Checkpoint *CheckpointOf `json:"ckpt,omitempty"`
	Holds      []HeldHold    `json:"holds,omitempty"`
	Manifest   *ManifestOf   `json:"manifest,omitempty"`
}

// Snapshot is a heartbeat's: the gateway's sequence and the snapshot's
// hash, its measured usage, opaque here, the running charge priced from it
// and capped at the hold, and the hold's deadline as granted.
type Snapshot struct {
	GatewaySeq int64     `json:"gseq"`
	Hash       []byte    `json:"hash"`
	Usage      []byte    `json:"usage,omitempty"`
	Running    int64     `json:"run"`
	Deadline   time.Time `json:"deadline"`
}

// CheckpointOf is a checkpoint's (§4.2): the owner's cumulative consumed,
// over the terminals with lower sequence numbers; its open holds' count,
// sum and latest end of life; the key-status version it applies; what it
// returns of the lease's free; and whether it is final, after the last
// hold ended.
type CheckpointOf struct {
	Consumed  int64     `json:"consumed"`
	Open      int64     `json:"open"`
	OpenSum   int64     `json:"open_sum"`
	LatestEnd time.Time `json:"latest_end,omitempty"`
	KeyStatus int64     `json:"ks"`
	Return    int64     `json:"return,omitempty"`
	Final     bool      `json:"final,omitempty"`
}

// HeldHold is an open hold in a forced exit's hand-off (§4.2): its
// authorization, estimate and deadline, and its last valid snapshot with
// the owner sequence number of the record that carried it, if it has one.
type HeldHold struct {
	Auth        string    `json:"a"`
	Estimate    int64     `json:"est"`
	Deadline    time.Time `json:"deadline"`
	Snapshot    *Snapshot `json:"hb,omitempty"`
	SnapshotSeq int64     `json:"snap,omitempty"`
	Basis       []byte    `json:"basis,omitempty"`
}

// ManifestOf ends a hand-off: how many chunks it had, the digest of their
// holds (HoldsDigest), and the sequence numbers the chunks used.
type ManifestOf struct {
	Chunks      int     `json:"chunks"`
	HoldsDigest []byte  `json:"holds_digest"`
	Seqs        []int64 `json:"seqs"`
}

// Encode validates a record and returns its bytes.
func Encode(r Record) ([]byte, error) {
	if err := r.Validate(); err != nil {
		return nil, err
	}
	return json.Marshal(r)
}

// Decode reads a record and validates it, refusing a field it does not
// know.
func Decode(data []byte) (Record, error) {
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	var r Record
	if err := dec.Decode(&r); err != nil {
		return Record{}, fmt.Errorf("record: %w", err)
	}
	if dec.More() {
		return Record{}, errors.New("record: more than one value")
	}
	return r, r.Validate()
}

// Validate reports a record its kind does not allow.
func (r Record) Validate() error {
	bad := func(format string, args ...any) error {
		return fmt.Errorf("record: %s %d of lease %q: %s", r.Kind, r.Seq, r.Lease, fmt.Sprintf(format, args...))
	}
	if r.Version != Version {
		return fmt.Errorf("record: version %d", r.Version)
	}
	if r.Lease == "" {
		return errors.New("record: no lease")
	}
	if r.Kind == Tick {
		if !reflect.DeepEqual(r, Record{Version: Version, Lease: r.Lease, Kind: Tick}) {
			return bad("a tick carries only its lease")
		}
		return nil
	}
	if r.Epoch < 1 || r.Seq < 1 {
		return bad("an owner record has an epoch and a sequence number")
	}
	if r.Estimate < 0 || r.Charge < 0 || r.Shortfall < 0 || r.SnapshotSeq < 0 {
		return bad("a negative amount")
	}
	auth := r.Auth != ""
	// What each kind may carry beyond the lease, epoch, sequence and kind.
	type has struct{ auth, charge, digest, snap, snapshot, ckpt, holds, manifest bool }
	want := map[Kind]has{
		Heartbeat:  {auth: true, snapshot: true},
		Settle:     {auth: true, charge: true, digest: true},
		Refund:     {auth: true},
		Reap:       {auth: true, charge: true, digest: true, snap: true},
		Release:    {auth: true},
		Checkpoint: {ckpt: true},
		Handoff:    {holds: true},
		Manifest:   {manifest: true},
	}
	w, ok := want[r.Kind]
	if !ok {
		return bad("no such kind")
	}
	got := has{auth: auth, charge: r.Charge != 0, digest: len(r.Digest) > 0, snap: r.SnapshotSeq != 0,
		snapshot: r.Snapshot != nil, ckpt: r.Checkpoint != nil, holds: len(r.Holds) > 0, manifest: r.Manifest != nil}
	// A charge may be zero; every other field the kind has is required.
	if w.charge && !got.charge {
		got.charge = true
	}
	if got != w {
		return bad("carries %+v, and the kind needs %+v", got, w)
	}
	if !r.Kind.Terminal() && (r.Shortfall != 0 || r.Drain != "") {
		return bad("only a terminal carries a shortfall total or an adopted row")
	}
	if r.Kind != Heartbeat && len(r.Basis) > 0 {
		return bad("only a heartbeat carries a reap's basis")
	}
	if !auth && r.Estimate != 0 {
		return bad("an estimate without an authorization")
	}
	switch r.Kind {
	case Heartbeat:
		if err := r.Snapshot.validate(r.Estimate); err != nil {
			return bad("%v", err)
		}
	case Reap:
		if r.Charge > r.Estimate || r.Drain != "" {
			return bad("a reap charges at most its hold, at its snapshot, and adopts no row")
		}
	case Refund, Release:
		if r.Kind == Release && r.Drain != "" {
			return bad("a release adopts no row")
		}
	case Checkpoint:
		c := r.Checkpoint
		if c.Consumed < 0 || c.Open < 0 || c.OpenSum < 0 || c.KeyStatus < 0 || c.Return < 0 ||
			(c.Open == 0) != (c.OpenSum == 0 && c.LatestEnd.IsZero()) || (c.Final && c.Open != 0) {
			return bad("checkpoint %+v", *c)
		}
	case Handoff:
		seen := map[string]bool{}
		for _, h := range r.Holds {
			if h.Auth == "" || h.Estimate < 0 || seen[h.Auth] || (h.Snapshot == nil) != (h.SnapshotSeq == 0) {
				return bad("held hold %+v", h)
			}
			if h.Snapshot != nil {
				if err := h.Snapshot.validate(h.Estimate); err != nil {
					return bad("%v", err)
				}
			}
			seen[h.Auth] = true
		}
	case Manifest:
		m := r.Manifest
		if m.Chunks < 0 || len(m.Seqs) != m.Chunks || len(m.HoldsDigest) != sha256.Size ||
			!slices.IsSorted(m.Seqs) || slices.Contains(m.Seqs, 0) || len(slices.Compact(slices.Clone(m.Seqs))) != m.Chunks {
			return bad("manifest %+v", *m)
		}
	}
	return nil
}

func (s *Snapshot) validate(estimate int64) error {
	if s.GatewaySeq < 0 || len(s.Hash) == 0 || s.Running < 0 || s.Running > estimate || s.Deadline.IsZero() {
		return fmt.Errorf("snapshot %+v of a hold of %d", *s, estimate)
	}
	return nil
}

// HoldsDigest is a hand-off's digest of its holds, across its chunks:
// SHA-256 over their records' bytes, sorted by authorization, so the auditor
// gets the same from the chunks it applied as the owner from the holds it
// cut them from.
func HoldsDigest(holds []HeldHold) ([]byte, error) {
	sorted := slices.Clone(holds)
	slices.SortFunc(sorted, func(a, b HeldHold) int { return cmpString(a.Auth, b.Auth) })
	h := sha256.New()
	for _, held := range sorted {
		b, err := json.Marshal(held)
		if err != nil {
			return nil, err
		}
		h.Write(b)
		h.Write([]byte{'\n'})
	}
	return h.Sum(nil), nil
}

func cmpString(a, b string) int {
	switch {
	case a < b:
		return -1
	case a > b:
		return 1
	}
	return 0
}
