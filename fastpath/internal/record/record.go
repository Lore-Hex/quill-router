// Package record is the settle log's wire format (design §4.2, §4.5, §4.8,
// §4.9): what an owner publishes on its lease's ordering key, and the fence
// ticks the auditor publishes there. Every record carries money fields only,
// about 250 bytes; a request's full record goes to the record topic, and a
// terminal names it by its digest.
//
// A record is versioned JSON in one canonical form, the bytes Encode writes:
// Decode takes nothing else, so a record decodes one way and encodes one
// way, and an owner and an auditor of two releases never read a record
// differently. A version, kind or field the reader does not know is refused,
// as is a record missing what its kind needs or carrying what it may not.
package record

import (
	"bytes"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"slices"
	"time"
)

// Version is the format's.
const Version = 1

// Kind is what a record is.
type Kind string

// The owner's kinds, each with the lease's next owner sequence number, and
// the auditor's fence tick, with its own number instead.
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

// DigestSize is the size of a full record's digest and of a heartbeat
// snapshot's hash, SHA-256's, as the store's columns hold them.
const DigestSize = sha256.Size

// Record is one record of a lease's order. Every time in it is UTC.
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
	// Boot is the boot binding a refund's or a release's disposition record
	// carries (§4.9), from the authorization's envelope.
	Boot []byte `json:"boot,omitempty"`
	// First marks a hold's first heartbeat record, which carries Basis, what
	// a reap of the hold needs to build its full record (the request's terms
	// and boot binding), so a reap never depends on the authorization's
	// record (§4.9). Opaque here.
	First bool   `json:"first,omitempty"`
	Basis []byte `json:"basis,omitempty"`
	// TickNumber is the auditor's own number for a tick of the lease, from
	// 1, and TickAt the auditor's clock when it published it.
	TickNumber int64     `json:"tick,omitempty"`
	TickAt     time.Time `json:"at,omitzero"`

	Snapshot   *Snapshot     `json:"hb,omitempty"`
	Checkpoint *CheckpointOf `json:"ckpt,omitempty"`
	Holds      []HeldHold    `json:"holds,omitempty"`
	Manifest   *ManifestOf   `json:"manifest,omitempty"`
}

// Snapshot is a heartbeat's: the gateway's sequence, from 1, and the
// snapshot's hash, its measured usage, opaque here, the running charge
// priced from it and capped at the hold, and the hold's deadline as the
// heartbeat's answer granted it.
type Snapshot struct {
	GatewaySeq int64     `json:"gseq"`
	Hash       []byte    `json:"hash"`
	Usage      []byte    `json:"usage"`
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
	LatestEnd time.Time `json:"latest_end,omitzero"`
	KeyStatus int64     `json:"ks"`
	Return    int64     `json:"return,omitempty"`
	Final     bool      `json:"final,omitempty"`
}

// HeldHold is an open hold in a forced exit's hand-off (§4.2): its
// authorization, estimate and deadline, the boot binding a disposition of it
// needs (§4.9), heartbeat or none, and, if it has one, its last valid
// snapshot, whose deadline is the hold's, with the owner sequence number of
// the record that carried it and the basis its first carried.
type HeldHold struct {
	Auth        string    `json:"a"`
	Estimate    int64     `json:"est"`
	Deadline    time.Time `json:"deadline"`
	Boot        []byte    `json:"boot"`
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

// Encode validates a record and returns its bytes, the canonical form.
func Encode(r Record) ([]byte, error) {
	if err := r.Validate(); err != nil {
		return nil, err
	}
	return json.Marshal(r)
}

// Decode reads a record in its canonical form and validates it: bytes that
// Encode would not write for the record they decode to are refused, so a
// duplicated or differently cased field, a null or zero field Encode leaves
// out, or anything after the record, does not pass.
func Decode(data []byte) (Record, error) {
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	var r Record
	if err := dec.Decode(&r); err != nil {
		return Record{}, fmt.Errorf("record: %w", err)
	}
	canonical, err := Encode(r)
	if err != nil {
		return Record{}, err
	}
	if !bytes.Equal(canonical, data) {
		return Record{}, errors.New("record: not in its canonical form")
	}
	return r, nil
}

// Validate reports a record its kind does not allow.
func (r Record) Validate() error {
	bad := func(format string, args ...any) error {
		return fmt.Errorf("record: %s %d of lease %q: %s", r.Kind, r.Seq, r.Lease, fmt.Sprintf(format, args...))
	}
	if r.Version != Version {
		return fmt.Errorf("record: version %d", r.Version)
	}
	if !identifier(r.Lease, maxLease) {
		return fmt.Errorf("record: lease %q", r.Lease)
	}
	if r.Estimate < 0 || r.Charge < 0 || r.Shortfall < 0 || r.SnapshotSeq < 0 || r.TickNumber < 0 {
		return bad("a negative number")
	}
	// What each kind may carry beyond the version, lease and kind: a field
	// the kind needs must be there, and one it does not have must not.
	type has struct {
		owner, auth, digest, snap, boot, basis, tick, snapshot, ckpt, holds, manifest bool
	}
	got := has{owner: r.Epoch != 0 || r.Seq != 0, auth: r.Auth != "", digest: len(r.Digest) > 0,
		snap: r.SnapshotSeq != 0, boot: len(r.Boot) > 0, basis: len(r.Basis) > 0 || r.First,
		tick: r.TickNumber != 0 || !r.TickAt.IsZero(), snapshot: r.Snapshot != nil, ckpt: r.Checkpoint != nil,
		holds: len(r.Holds) > 0, manifest: r.Manifest != nil}
	want := map[Kind]has{
		Heartbeat:  {owner: true, auth: true, snapshot: true, basis: got.basis},
		Settle:     {owner: true, auth: true, digest: true},
		Refund:     {owner: true, auth: true, boot: true},
		Reap:       {owner: true, auth: true, digest: true, snap: true},
		Release:    {owner: true, auth: true, boot: true},
		Checkpoint: {owner: true, ckpt: true},
		Handoff:    {owner: true, holds: true},
		Manifest:   {owner: true, manifest: true},
		Tick:       {tick: true},
	}
	w, ok := want[r.Kind]
	if !ok {
		return bad("no such kind")
	}
	if got != w {
		return bad("carries %+v, and the kind needs %+v", got, w)
	}
	if got.owner && (r.Epoch < 1 || r.Seq < 1) {
		return bad("an owner record has an epoch and a sequence number")
	}
	if !r.Kind.Terminal() && (r.Charge != 0 || r.Shortfall != 0 || r.Drain != "") {
		return bad("only a terminal carries a charge, a shortfall total or an adopted row")
	}
	if !got.auth && r.Estimate != 0 {
		return bad("an estimate without an authorization")
	}
	if got.auth && !identifier(r.Auth, maxAuth) {
		return bad("authorization %q", r.Auth)
	}
	if r.Drain != "" && !identifier(r.Drain, maxRecordID) {
		return bad("drain record %q", r.Drain)
	}
	if got.digest && len(r.Digest) != DigestSize {
		return bad("a digest of %d bytes", len(r.Digest))
	}
	switch r.Kind {
	case Tick:
		if r.TickNumber < 1 || !utc(r.TickAt) {
			return bad("a tick has its number and a UTC time")
		}
	case Heartbeat:
		// The lease's first record, and a gateway's first heartbeat, can only
		// be a hold's first heartbeat, since a lease has one owner and a
		// later heartbeat's gateway sequence is above its first's.
		if r.First != (len(r.Basis) > 0) || (!r.First && (r.Seq == 1 || r.Snapshot.GatewaySeq == 1)) {
			return bad("a hold's first heartbeat, and only it, carries the reap's basis")
		}
		if err := r.Snapshot.validate(r.Estimate); err != nil {
			return bad("%v", err)
		}
	case Reap:
		if r.Charge > r.Estimate || r.Drain != "" || r.SnapshotSeq >= r.Seq {
			return bad("a reap charges at most its hold, at an earlier heartbeat record's snapshot, and adopts no row")
		}
	case Release:
		if r.Charge != 0 || r.Drain != "" {
			return bad("a release charges nothing and adopts no row")
		}
	case Refund:
		if r.Charge != 0 {
			return bad("a refund charges nothing")
		}
	case Checkpoint:
		c := r.Checkpoint
		if c.Consumed < 0 || c.Open < 0 || c.OpenSum < 0 || c.KeyStatus < 0 || c.Return < 0 ||
			(c.Open == 0) != c.LatestEnd.IsZero() || (c.Open == 0 && c.OpenSum != 0) ||
			(!c.LatestEnd.IsZero() && !utc(c.LatestEnd)) || (c.Final && c.Open != 0) {
			return bad("checkpoint %+v", *c)
		}
	case Handoff:
		seen := map[string]bool{}
		for _, h := range r.Holds {
			if err := h.validate(r.Seq); err != nil || seen[h.Auth] {
				return bad("held hold %+v: %v", h, err)
			}
			seen[h.Auth] = true
		}
	case Manifest:
		m := r.Manifest
		if m.Chunks < 0 || len(m.Seqs) != m.Chunks || len(m.HoldsDigest) != DigestSize || !slices.IsSorted(m.Seqs) ||
			len(slices.Compact(slices.Clone(m.Seqs))) != m.Chunks {
			return bad("manifest %+v", *m)
		}
		for _, s := range m.Seqs {
			if s < 1 || s >= r.Seq {
				return bad("a manifest names chunk %d, not an earlier record", s)
			}
		}
	}
	return nil
}

func (s *Snapshot) validate(estimate int64) error {
	if s.GatewaySeq < 1 || len(s.Hash) != DigestSize || len(s.Usage) == 0 || s.Running < 0 || s.Running > estimate ||
		!utc(s.Deadline) {
		return fmt.Errorf("snapshot %+v of a hold of %d", *s, estimate)
	}
	return nil
}

func (h HeldHold) validate(seq int64) error {
	switch {
	case !identifier(h.Auth, maxAuth) || h.Estimate < 0 || !utc(h.Deadline) || len(h.Boot) == 0:
		return errors.New("an authorization, an estimate, a UTC deadline and a boot binding")
	case (h.Snapshot == nil) != (h.SnapshotSeq == 0) || (h.Snapshot == nil && len(h.Basis) > 0):
		return errors.New("a snapshot with its record's number and basis, or none of them")
	case h.Snapshot == nil:
		return nil
	case h.SnapshotSeq < 1 || h.SnapshotSeq >= seq || len(h.Basis) == 0 || !h.Deadline.Equal(h.Snapshot.Deadline):
		return errors.New("a snapshot from an earlier record, with its basis and the hold's deadline")
	}
	return h.Snapshot.validate(h.Estimate)
}

// The longest IDs the store's columns hold: a lease ID, an authorization ID
// and a drain-log record ID.
const (
	maxLease    = 32
	maxAuth     = 64
	maxRecordID = 64
)

// ValidLease and ValidAuth report whether a lease or an authorization ID is
// one the format carries, so an owner takes no lease and mints no hold it
// could not record.
func ValidLease(id string) bool { return identifier(id, maxLease) }
func ValidAuth(id string) bool  { return identifier(id, maxAuth) }

// identifier: an ID the format carries is printable ASCII, so no two IDs
// encode alike, and no longer than the store's column holds.
func identifier(s string, longest int) bool {
	if s == "" || len(s) > longest {
		return false
	}
	for i := 0; i < len(s); i++ {
		if s[i] < 0x21 || s[i] > 0x7e {
			return false
		}
	}
	return true
}

// utc: a time the format carries is set and in UTC, so one instant has one
// form, and within the years Spanner's TIMESTAMP holds, 1 to 9999.
func utc(t time.Time) bool {
	return !t.IsZero() && t.Location() == time.UTC && t.Year() >= 1 && t.Year() <= 9999
}

// HoldsDigest is a hand-off's digest of its holds, across its chunks:
// SHA-256 over their canonical bytes, sorted by authorization, so the
// auditor gets the same from the chunks it applied as the owner from the
// holds it cut them from. Holds a hand-off could not carry have none.
func HoldsDigest(holds []HeldHold) ([]byte, error) {
	sorted := slices.Clone(holds)
	slices.SortFunc(sorted, func(a, b HeldHold) int {
		switch {
		case a.Auth < b.Auth:
			return -1
		case a.Auth > b.Auth:
			return 1
		}
		return 0
	})
	h := sha256.New()
	for i, held := range sorted {
		if err := held.validate(math.MaxInt64); err != nil || (i > 0 && sorted[i-1].Auth == held.Auth) {
			return nil, fmt.Errorf("record: held hold %+v: %v", held, err)
		}
		b, err := json.Marshal(held)
		if err != nil {
			return nil, err
		}
		h.Write(b)
		h.Write([]byte{'\n'})
	}
	return h.Sum(nil), nil
}
