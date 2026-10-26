package auditor

import (
	"context"
	"errors"
	"fmt"
	"log"
	"sync"
	"time"

	"cloud.google.com/go/spanner"
	"google.golang.org/grpc/codes"

	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// StoreDeadline bounds each Spanner call the auditor's parts make: the
// runtime's reads and commits, the ticker's, the pending work's and the
// stager's. Spanner's streaming reads have no deadline of their own, and on
// 2026-10-26 one of them held a member's commit loop, and with it every
// acknowledgement of its records, for 37 minutes. A commit of a full batch
// (MaxBatch leases) takes well under a second; Spanner retries an aborted
// transaction inside the call, and 15 s leaves room for many such retries
// while staying a quarter of CommitStall, so a round that meets one slow
// call still finishes before the watchdog's limit.
//
// A call that runs out of time fails as any Spanner failure does, and each
// caller treats a failed write as one whose outcome is unknown: the
// runtime's commit, gap stop and close drop the lease's member, which is
// read again and its records applied again, each write conditional on the
// version the member read, so a write that landed after all refuses the
// second; a reap is refused once its row is in the drain log; the ticker's
// draining write is conditional on the expiry it read; the pending work's
// writes are idempotent. So a timeout never applies anything twice.
const StoreDeadline = 15 * time.Second

// logEvery is how often a kind of failure is logged at most: the first at
// once, then one line a period with how many more there were.
const logEvery = 30 * time.Second

// journal writes the auditor's own lines to the process's standard error,
// which the node's journal keeps, each kind at most once per logEvery.
// Lines carry what failed and a lease ID at most, which the alerts carry
// too; never a workspace, an error's text (the store's name the lease's
// row, its workspace with it) or a record's contents.
type journal struct {
	logf func(format string, args ...any)

	mu       sync.Mutex
	last     map[string]time.Time
	withheld map[string]int
}

func newJournal(logf func(format string, args ...any)) *journal {
	if logf == nil {
		logf = log.Printf
	}
	return &journal{logf: logf, last: map[string]time.Time{}, withheld: map[string]int{}}
}

// printf logs a line of kind key, unless one was logged less than logEvery
// ago, when it is counted and the next line says how many were withheld.
func (j *journal) printf(key, format string, args ...any) {
	j.mu.Lock()
	now := time.Now()
	if last, ok := j.last[key]; ok && now.Sub(last) < logEvery {
		j.withheld[key]++
		j.mu.Unlock()
		return
	}
	n := j.withheld[key]
	j.last[key], j.withheld[key] = now, 0
	j.mu.Unlock()
	line := fmt.Sprintf(format, args...)
	if n > 0 {
		line += fmt.Sprintf(" (and %d more in the last %v)", n, logEvery)
	}
	j.logf("%s", line)
}

// errorClass is a failure's class, which is all of it a line carries: a
// deadline, the gRPC code of a Spanner error, or "other" for the store's
// own refusals.
func errorClass(err error) string {
	switch {
	case errors.Is(err, context.DeadlineExceeded):
		return codes.DeadlineExceeded.String()
	case errors.Is(err, context.Canceled):
		return codes.Canceled.String()
	}
	if c := spanner.ErrCode(err); c != codes.Unknown {
		return c.String()
	}
	return "other"
}

// bound is a part's deadline on each Spanner call, and its journal of the
// calls that fail.
type bound struct {
	d time.Duration
	j *journal
}

// call runs f under the deadline and logs its failure, unless ctx itself
// ended, as when the process stops, or the failure is an answer the
// callers act on (no such lease, a lease retired).
func call[T any](b bound, ctx context.Context, op, lease string, f func(context.Context) (T, error)) (T, error) {
	cctx, cancel := context.WithTimeout(ctx, b.d)
	defer cancel()
	v, err := f(cctx)
	b.failed(ctx, op, lease, err)
	return v, err
}

// call2 is call for the store's calls that answer a commit timestamp too.
func call2[T any](b bound, ctx context.Context, op, lease string,
	f func(context.Context) (T, time.Time, error)) (T, time.Time, error) {
	cctx, cancel := context.WithTimeout(ctx, b.d)
	defer cancel()
	v, at, err := f(cctx)
	b.failed(ctx, op, lease, err)
	return v, at, err
}

func (b bound) failed(ctx context.Context, op, lease string, err error) {
	if err == nil || ctx.Err() != nil || errors.Is(err, store.ErrNoLease) || errors.Is(err, store.ErrRetired) {
		return
	}
	of := ""
	if lease != "" {
		of = " of lease " + lease
	}
	b.j.printf("spanner "+op, "auditor: spanner %s%s failed: %s", op, of, errorClass(err))
}

// boundStore is the runtime's Store, each call under the deadline.
type boundStore struct {
	s Store
	bound
}

func (s boundStore) FindLease(ctx context.Context, leaseID string) (store.LeaseRef, error) {
	return call(s.bound, ctx, "find lease", leaseID, func(ctx context.Context) (store.LeaseRef, error) {
		return s.s.FindLease(ctx, leaseID)
	})
}

func (s boundStore) Load(ctx context.Context, ref store.LeaseRef) (store.Loaded, error) {
	return call(s.bound, ctx, "load", ref.LeaseID, func(ctx context.Context) (store.Loaded, error) {
		return s.s.Load(ctx, ref)
	})
}

func (s boundStore) ReadLease(ctx context.Context, ref store.LeaseRef) (store.Lease, time.Time, error) {
	return call2(s.bound, ctx, "read lease", ref.LeaseID, func(ctx context.Context) (store.Lease, time.Time, error) {
		return s.s.ReadLease(ctx, ref)
	})
}

func (s boundStore) LoadWinners(ctx context.Context, ref store.LeaseRef) ([]store.Pack, time.Time, error) {
	return call2(s.bound, ctx, "load winners", ref.LeaseID, func(ctx context.Context) ([]store.Pack, time.Time, error) {
		return s.s.LoadWinners(ctx, ref)
	})
}

func (s boundStore) Commit(ctx context.Context, reqs []store.CommitRequest) ([]store.CommitResult, time.Time, error) {
	return call2(s.bound, ctx, "commit", "", func(ctx context.Context) ([]store.CommitResult, time.Time, error) {
		return s.s.Commit(ctx, reqs)
	})
}

func (s boundStore) StopForGap(ctx context.Context, ref store.LeaseRef, readVersion, seq int64) (bool, time.Time, error) {
	return call2(s.bound, ctx, "stop for gap", ref.LeaseID, func(ctx context.Context) (bool, time.Time, error) {
		return s.s.StopForGap(ctx, ref, readVersion, seq)
	})
}

func (s boundStore) ReadDrainSince(ctx context.Context, ref store.LeaseRef, cursor time.Time) ([]store.DrainRow, time.Time, error) {
	return call2(s.bound, ctx, "read drain", ref.LeaseID, func(ctx context.Context) ([]store.DrainRow, time.Time, error) {
		return s.s.ReadDrainSince(ctx, ref, cursor)
	})
}

func (s boundStore) Reap(ctx context.Context, ref store.LeaseRef, readVersion int64, tick time.Time,
	r store.ReapRow) (store.Refusal, time.Time, error) {
	return call2(s.bound, ctx, "reap", ref.LeaseID, func(ctx context.Context) (store.Refusal, time.Time, error) {
		return s.s.Reap(ctx, ref, readVersion, tick, r)
	})
}

func (s boundStore) CloseLease(ctx context.Context, ref store.LeaseRef, readVersion int64,
	drainRead, now time.Time) (store.CloseResult, error) {
	return call(s.bound, ctx, "close", ref.LeaseID, func(ctx context.Context) (store.CloseResult, error) {
		return s.s.CloseLease(ctx, ref, readVersion, drainRead, now)
	})
}

// boundTickStore is the ticker's TickStore, each call under the deadline.
type boundTickStore struct {
	s TickStore
	bound
}

func (s boundTickStore) ScanExpired(ctx context.Context, now time.Time, limit int) ([]store.Expired, time.Time, error) {
	return call2(s.bound, ctx, "scan expired", "", func(ctx context.Context) ([]store.Expired, time.Time, error) {
		return s.s.ScanExpired(ctx, now, limit)
	})
}

func (s boundTickStore) AuditorMarkDraining(ctx context.Context, ref store.LeaseRef, readExpiry time.Time) (bool, time.Time, error) {
	return call2(s.bound, ctx, "mark draining", ref.LeaseID, func(ctx context.Context) (bool, time.Time, error) {
		return s.s.AuditorMarkDraining(ctx, ref, readExpiry)
	})
}

func (s boundTickStore) ScanDraining(ctx context.Context, region string, after store.LeaseRef, limit int) ([]store.LeaseRef, error) {
	return call(s.bound, ctx, "scan draining", "", func(ctx context.Context) ([]store.LeaseRef, error) {
		return s.s.ScanDraining(ctx, region, after, limit)
	})
}

// boundPendingStore is the pending work's PendingStore, each call under the
// deadline.
type boundPendingStore struct {
	s PendingStore
	bound
}

func (s boundPendingStore) PendingPacks(ctx context.Context, after store.PendingPack, limit int) ([]store.PendingPack, error) {
	return call(s.bound, ctx, "pending packs", "", func(ctx context.Context) ([]store.PendingPack, error) {
		return s.s.PendingPacks(ctx, after, limit)
	})
}

func (s boundPendingStore) LoadWinners(ctx context.Context, ref store.LeaseRef) ([]store.Pack, time.Time, error) {
	return call2(s.bound, ctx, "load winners", ref.LeaseID, func(ctx context.Context) ([]store.Pack, time.Time, error) {
		return s.s.LoadWinners(ctx, ref)
	})
}

func (s boundPendingStore) ReadStaged(ctx context.Context, authorization string, digest []byte) (store.StagedRecord, bool, error) {
	type staged struct {
		r  store.StagedRecord
		ok bool
	}
	got, err := call(s.bound, ctx, "read staged", "", func(ctx context.Context) (staged, error) {
		r, ok, err := s.s.ReadStaged(ctx, authorization, digest)
		return staged{r, ok}, err
	})
	return got.r, got.ok, err
}

func (s boundPendingStore) WriteRecords(ctx context.Context, records []store.LeaseRecord) error {
	_, err := call(s.bound, ctx, "write records", "", func(ctx context.Context) (struct{}, error) {
		return struct{}{}, s.s.WriteRecords(ctx, records)
	})
	return err
}

func (s boundPendingStore) MarkPackDone(ctx context.Context, ref store.LeaseRef, version int64) (bool, error) {
	return call(s.bound, ctx, "mark pack done", ref.LeaseID, func(ctx context.Context) (bool, error) {
		return s.s.MarkPackDone(ctx, ref, version)
	})
}

func (s boundPendingStore) DropStaged(ctx context.Context, keys ...store.StagedKey) error {
	_, err := call(s.bound, ctx, "drop staged", "", func(ctx context.Context) (struct{}, error) {
		return struct{}{}, s.s.DropStaged(ctx, keys...)
	})
	return err
}

func (s boundPendingStore) RetiredStaged(ctx context.Context, limit int) ([]store.StagedKey, error) {
	return call(s.bound, ctx, "retired staged", "", func(ctx context.Context) ([]store.StagedKey, error) {
		return s.s.RetiredStaged(ctx, limit)
	})
}

// boundStagingStore is the stager's StagingStore, each call under the
// deadline.
type boundStagingStore struct {
	s StagingStore
	bound
}

func (s boundStagingStore) FindLease(ctx context.Context, leaseID string) (store.LeaseRef, error) {
	return call(s.bound, ctx, "find lease", leaseID, func(ctx context.Context) (store.LeaseRef, error) {
		return s.s.FindLease(ctx, leaseID)
	})
}

func (s boundStagingStore) StageRecord(ctx context.Context, r store.StagedRecord) error {
	_, err := call(s.bound, ctx, "stage record", r.Ref.LeaseID, func(ctx context.Context) (struct{}, error) {
		return struct{}{}, s.s.StageRecord(ctx, r)
	})
	return err
}
