package store

import (
	"bytes"
	"encoding/gob"
	"fmt"
	"reflect"
	"slices"
	"testing"
	"time"

	"cloud.google.com/go/spanner"
)

// The detached copies the walks hand the store, and the test that they
// share nothing: in a file of its own, so the race detector's run has it
// too, only the walks being excluded from it.

// TestDetachedSharesNothing: detached's copy of each request the walks send
// equals the request, and writing every field, element and byte of the copy,
// through every slice and pointer, leaves the request as it was. The same
// writes through a plain copy reach the request, so the test sees memory
// shared where there is any.
func TestDetachedSharesNothing(t *testing.T) {
	seq, fault := int64(7), int64(3)
	when := time.Date(2026, 10, 9, 8, 0, 0, 0, time.UTC)
	commit := func() []CommitRequest {
		return []CommitRequest{{Ref: LeaseRef{"w", "l"}, ReadVersion: 1, AppliedSeq: 2, LastTick: 3, AuditOsum: 4,
			Money: []MoneyOp{Book(5, 6), Return(7)},
			PutHolds: []HoldRow{{AuthorizationID: "a", Estimate: 8, Deadline: when, Listed: true,
				SnapshotSeq: spanner.NullInt64{Int64: 9, Valid: true}, SnapshotHash: []byte("h"),
				SnapshotUsage: []byte("u"), RunningCharge: spanner.NullInt64{Int64: 10, Valid: true},
				SnapshotOwnerSeq: spanner.NullInt64{Int64: 11, Valid: true}, ReapBasis: []byte("b")}},
			Winners:  []Winner{{AuthorizationID: "a", Kind: "settle", Charge: 12, RecordID: "r", Work: []byte(`{}`)}},
			Boundary: &Boundary{S: 13, T: when}, HoldsListedSeq: &seq, AuditFault: &fault}}
	}
	terminal := func() DrainTerminal {
		return DrainTerminal{Ref: LeaseRef{"w", "l"}, AuthorizationID: "a", RecordID: "r", Kind: "settle", Charge: 1,
			Estimate: 2, Digest: []byte("d"), Money: []byte("m"), Cause: "c"}
	}
	reap := func() ReapRow {
		return ReapRow{AuthorizationID: "a", RecordID: "r", Charge: 1, Estimate: 2, SnapshotOwnerSeq: 3,
			Digest: []byte("d"), Money: []byte("m")}
	}
	grant := func() GrantRequest {
		return GrantRequest{Workspace: "w", LeaseID: "l", Region: "r", WorkspaceShard: 1, Owner: Owner{"n", 2},
			Amount: 3, KeyStatusVersion: 4}
	}
	sharesNothing(t, "a commit", commit, detached[[]CommitRequest])
	sharesNothing(t, "an append", terminal, detached[DrainTerminal])
	sharesNothing(t, "a reap", reap, detached[ReapRow])
	sharesNothing(t, "a grant", grant, detached[GrantRequest])
	// The positive control: a plain copy shares the request's slices and
	// pointers, and the writes reach it.
	if !shares(t, commit, func(v []CommitRequest) []CommitRequest { return slices.Clone(v) }) {
		t.Error("writes through a plain copy of a commit's requests left them as they were: the test sees no sharing")
	}
}

func sharesNothing[T any](t *testing.T, name string, made func() T, copied func(T) T) {
	t.Helper()
	v := made()
	if c := copied(v); !reflect.DeepEqual(c, v) {
		t.Errorf("%s: the copy %+v is not the request %+v", name, c, v)
	}
	if shares(t, made, copied) {
		t.Errorf("%s: writing the copy wrote the request", name)
	}
}

// shares reports whether writing every field of a copy of a request changes
// the request, read whole, through its pointers, before and after.
func shares[T any](t *testing.T, made func() T, copied func(T) T) bool {
	t.Helper()
	whole := func(v T) []byte {
		t.Helper()
		var b bytes.Buffer
		if err := gob.NewEncoder(&b).Encode(v); err != nil {
			t.Fatal(err)
		}
		return b.Bytes()
	}
	v := made()
	before := whole(v)
	c := copied(v)
	touchAll(reflect.ValueOf(&c).Elem())
	if bytes.Equal(whole(c), before) {
		t.Fatal("writing every field of the copy left it as it was")
	}
	return !bytes.Equal(whole(v), before)
}

// touchAll writes every field, element and byte v reaches.
func touchAll(v reflect.Value) {
	switch v.Kind() {
	case reflect.Pointer:
		if !v.IsNil() {
			touchAll(v.Elem())
		}
	case reflect.Slice:
		for i := range v.Len() {
			touchAll(v.Index(i))
		}
	case reflect.Struct:
		if t, ok := v.Interface().(time.Time); ok {
			v.Set(reflect.ValueOf(t.Add(time.Second)))
			return
		}
		for i := range v.NumField() {
			if v.Type().Field(i).IsExported() {
				touchAll(v.Field(i))
			}
		}
	case reflect.String:
		v.SetString(v.String() + "x")
	case reflect.Bool:
		v.SetBool(!v.Bool())
	case reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64:
		v.SetInt(v.Int() + 1)
	case reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64:
		v.SetUint(v.Uint() ^ 0xff)
	default:
		panic(fmt.Sprintf("touchAll: a %s", v.Type()))
	}
}

// detached is a copy of v that shares no memory with it: every slice, map
// and pointer in it new, a struct with unexported fields, such as a
// time.Time, copied whole. The walks give the store the copy and keep v for
// what they expect, so a store that wrote into its request could not move
// the expectation with it (TestDetachedSharesNothing).
func detached[T any](v T) T {
	var out T
	copyInto(reflect.ValueOf(&out).Elem(), reflect.ValueOf(&v).Elem())
	return out
}

func copyInto(dst, src reflect.Value) {
	switch src.Kind() {
	case reflect.Pointer:
		if !src.IsNil() {
			p := reflect.New(src.Type().Elem())
			copyInto(p.Elem(), src.Elem())
			dst.Set(p)
		}
	case reflect.Slice:
		if !src.IsNil() {
			c := reflect.MakeSlice(src.Type(), src.Len(), src.Len())
			for i := range src.Len() {
				copyInto(c.Index(i), src.Index(i))
			}
			dst.Set(c)
		}
	case reflect.Map:
		if !src.IsNil() {
			c := reflect.MakeMapWithSize(src.Type(), src.Len())
			for it := src.MapRange(); it.Next(); {
				v := reflect.New(src.Type().Elem()).Elem()
				copyInto(v, it.Value())
				c.SetMapIndex(it.Key(), v)
			}
			dst.Set(c)
		}
	case reflect.Struct:
		for i := range src.NumField() {
			if !src.Type().Field(i).IsExported() {
				dst.Set(src)
				return
			}
		}
		for i := range src.NumField() {
			copyInto(dst.Field(i), src.Field(i))
		}
	case reflect.Interface, reflect.Chan, reflect.Func, reflect.UnsafePointer:
		panic(fmt.Sprintf("detached: a %s, which no request holds", src.Type()))
	default:
		dst.Set(src)
	}
}
