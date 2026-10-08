package trace

import (
	"bytes"
	"context"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"
)

// TestEventsAreNumberedInTheirProcess: each event of a process takes the
// next number, however many record at once, and its clocks; each is in the
// file once Record returns; they read back as recorded, an owner sequence
// of zero as one stated; and nothing is written after Close.
func TestEventsAreNumberedInTheirProcess(t *testing.T) {
	var buf bytes.Buffer
	r, err := New(&buf, "node-a:8080", 3)
	if err != nil {
		t.Fatal(err)
	}
	var wg sync.WaitGroup
	for i := range 50 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			r.Record("settle", nil, Facts{Lease: "l", Auth: "a", OwnerSeq: Seq(int64(i))})
		}()
	}
	wg.Wait()
	cause := ID{Node: "node-b:8080", Epoch: 1, Seq: 7}
	commit := time.Date(2026, 10, 8, 12, 0, 0, 1000, time.FixedZone("offset", 30))
	last := r.Record("append", &cause, Facts{Commit: commit, Detail: "unreachable"})
	if got := strings.Count(buf.String(), "\n"); got != 51 {
		t.Fatalf("%d lines in the file before Close", got)
	}
	if err := r.Close(); err != nil {
		t.Fatal(err)
	}
	events, err := Read(bytes.NewReader(buf.Bytes()))
	if err != nil {
		t.Fatal(err)
	}
	if len(events) != 51 || last != (ID{Node: "node-a:8080", Epoch: 3, Seq: 51}) {
		t.Fatalf("%d events, the last %+v", len(events), last)
	}
	seqs := map[int64]bool{}
	for i, e := range events {
		if e.ID.Seq != int64(i+1) || e.ID.Node != "node-a:8080" || e.ID.Epoch != 3 || e.Wall.IsZero() ||
			(i > 0 && e.Mono < events[i-1].Mono) {
			t.Fatalf("event %d: %+v", i, e)
		}
		if i < 50 {
			if e.OwnerSeq == nil {
				t.Fatalf("event %d states no owner sequence", i)
			}
			seqs[*e.OwnerSeq] = true
		}
	}
	if len(seqs) != 50 || !seqs[0] {
		t.Fatalf("the owner sequences stated: %v", seqs)
	}
	if e := events[50]; e.Cause == nil || *e.Cause != cause || e.Kind != "append" || e.Detail != "unreachable" ||
		e.OwnerSeq != nil || !e.Commit.Equal(commit) {
		t.Fatalf("the last event: %+v", e)
	}
	n := buf.Len()
	if r.Record("late", nil, Facts{}) != (ID{Node: "node-a:8080", Epoch: 3, Seq: 52}) {
		t.Fatal("an event after Close is not numbered")
	}
	if buf.Len() != n {
		t.Fatalf("an event after Close was written: %q", buf.String()[n:])
	}
}

// TestTheClocksAreTheProcesss: an event's monotonic clock is the time
// since its recorder began, which a step back of the wall clock does not
// move, and its wall clock the time, in UTC.
func TestTheClocksAreTheProcesss(t *testing.T) {
	var buf bytes.Buffer
	r, err := New(&buf, "n", 1)
	if err != nil {
		t.Fatal(err)
	}
	began := time.Date(2026, 10, 8, 12, 0, 0, 0, time.FixedZone("offset", 30))
	var wall time.Time
	var elapsed time.Duration
	r.wall, r.elapsed = func() time.Time { return wall }, func() time.Duration { return elapsed }
	steps := []struct {
		wall    time.Time
		elapsed time.Duration
	}{{began, 0}, {began.Add(1500), 1500}, {began.Add(-time.Hour), time.Second}} // the wall clock steps back
	for _, st := range steps {
		wall, elapsed = st.wall, st.elapsed
		r.Record("tick", nil, Facts{})
	}
	events, err := Read(bytes.NewReader(buf.Bytes()))
	if err != nil {
		t.Fatal(err)
	}
	for i, st := range steps {
		if e := events[i]; e.Mono != st.elapsed.Nanoseconds() || !e.Wall.Equal(st.wall) || e.Wall.Location() != time.UTC {
			t.Fatalf("event %d: mono %d, wall %v; want %d and %v", i, e.Mono, e.Wall, st.elapsed.Nanoseconds(), st.wall)
		}
	}
}

// TestAnIdentityCrossesProcesses: an identity reads back as a message
// carries it, and only as String writes it; a context carries its cause.
func TestAnIdentityCrossesProcesses(t *testing.T) {
	id := ID{Node: "10.0.0.7:8080", Epoch: 12, Seq: 99}
	got, err := ParseID(id.String())
	if err != nil || got != id {
		t.Fatalf("%q read back as %+v %v", id.String(), got, err)
	}
	for _, bad := range []string{"", "a/1", "/1/2", "a/0/2", "a/1/0", "a/x/2", "a/1/2/3", "a/+1/2", "a/01/2",
		"a/1/02", "a/1/+2", " a/1/2", "a b/1/2", "a\xff/1/2", "a\t/1/2"} {
		if _, err := ParseID(bad); err == nil {
			t.Fatalf("%q read as an identity", bad)
		}
	}
	if Cause(context.Background()) != nil {
		t.Fatal("a cause in a context that carries none")
	}
	if c := Cause(WithCause(context.Background(), id)); c == nil || *c != id {
		t.Fatalf("the cause carried: %v", c)
	}
}

// failing is a writer whose writes fail from the after-th on.
type failing struct {
	after, n int
	buf      bytes.Buffer
}

func (f *failing) Write(p []byte) (int, error) {
	if f.n++; f.n >= f.after {
		return 0, errors.New("the disk is full")
	}
	return f.buf.Write(p)
}

// TestARecorderReportsItsFailure: a write that fails is reported at Close,
// and events are numbered still, one after another; so is an event Read
// could not take back; a nil recorder records nothing; a recorder for a
// node whose name a message cannot carry as it is, or with no epoch, is
// refused.
func TestARecorderReportsItsFailure(t *testing.T) {
	w := &failing{after: 3}
	r, err := New(w, "n", 1)
	if err != nil {
		t.Fatal(err)
	}
	for i := range 5 {
		if id := r.Record("heartbeat", nil, Facts{}); id.Seq != int64(i+1) {
			t.Fatalf("event %d numbered %d", i+1, id.Seq)
		}
	}
	if err := r.Close(); err == nil {
		t.Fatal("a recorder whose writes failed closed clean")
	}
	if events, err := Read(bytes.NewReader(w.buf.Bytes())); err != nil || len(events) != 2 {
		t.Fatalf("the events written before the failure: %d %v", len(events), err)
	}
	for name, record := range map[string]func(r *Recorder){
		"no kind":          func(r *Recorder) { r.Record("", nil, Facts{}) },
		"text not UTF-8":   func(r *Recorder) { r.Record("x", nil, Facts{Auth: "a\xff"}) },
		"a kind not UTF-8": func(r *Recorder) { r.Record("x\xfe", nil, Facts{}) },
		"a cause no event": func(r *Recorder) { r.Record("x", &ID{Node: "a b", Epoch: 1, Seq: 1}, Facts{}) },
		"too long":         func(r *Recorder) { r.Record("x", nil, Facts{Detail: strings.Repeat("x", maxLine)}) },
	} {
		var buf bytes.Buffer
		r, err := New(&buf, "n", 1)
		if err != nil {
			t.Fatal(err)
		}
		record(r)
		if err := r.Close(); err == nil || buf.Len() != 0 {
			t.Fatalf("%s: closed with %v, wrote %d bytes", name, err, buf.Len())
		}
	}
	var none *Recorder
	if id := none.Record("x", nil, Facts{}); id != (ID{}) || none.Close() != nil {
		t.Fatal("a nil recorder recorded")
	}
	for _, bad := range []struct {
		node  string
		epoch int64
	}{{"", 1}, {"a/b", 1}, {"a", 0}, {" node", 1}, {"a b", 1}, {"node\xff", 1}, {"node\n", 1}} {
		if _, err := New(&bytes.Buffer{}, bad.node, bad.epoch); err == nil {
			t.Fatalf("a recorder for %+v", bad)
		}
	}
}

// TestReadTakesOnlyEvents: each line must be one JSON object, an event as a
// recorder writes it; Read returns the events before the first line that is
// not.
func TestReadTakesOnlyEvents(t *testing.T) {
	const good = `{"id":{"node":"n","epoch":1,"seq":1},"mono":0,"wall":"2026-10-08T12:00:00Z","kind":"x"}`
	if events, err := Read(strings.NewReader(good + "\n" + good + "\n")); err != nil || len(events) != 2 {
		t.Fatalf("two events: %d %v", len(events), err)
	}
	for name, file := range map[string]string{
		"null":            "null\n",
		"an empty object": "{}\n",
		"a blank line":    "\n",
		"two on a line":   good + good + "\n",
		"one on two lines": `{"id":{"node":"n","epoch":1,"seq":1},"mono":0,` + "\n" +
			`"wall":"2026-10-08T12:00:00Z","kind":"x"}` + "\n",
		"a field more":   strings.Replace(good, `"kind":"x"`, `"kind":"x","extra":1`, 1) + "\n",
		"no kind":        strings.Replace(good, `,"kind":"x"`, ``, 1) + "\n",
		"no wall":        strings.Replace(good, `"wall":"2026-10-08T12:00:00Z",`, ``, 1) + "\n",
		"a clock before": strings.Replace(good, `"mono":0`, `"mono":-1`, 1) + "\n",
		"an array":       "[" + good + "]\n",
		"an identity no": strings.Replace(good, `"epoch":1`, `"epoch":0`, 1) + "\n",
		"a cause no":     strings.Replace(good, `"kind":"x"`, `"kind":"x","cause":{"node":"","epoch":1,"seq":1}`, 1) + "\n",
		"cut short":      good[:len(good)-5],
		"no clock":       strings.Replace(good, `"mono":0,`, ``, 1) + "\n",
		"a null clock":   strings.Replace(good, `"mono":0`, `"mono":null`, 1) + "\n",
		"a clock twice":  strings.Replace(good, `"mono":0`, `"mono":0,"mono":99`, 1) + "\n",
		"a key's case":   strings.Replace(good, `"mono":0`, `"Mono":99`, 1) + "\n",
		"an alias too":   strings.Replace(good, `"mono":0`, `"mono":0,"Mono":99`, 1) + "\n",
		"an identity in two": strings.Replace(good, `"id":{"node":"n","epoch":1,"seq":1}`,
			`"id":{"node":"n"},"id":{"epoch":1,"seq":1}`, 1) + "\n",
		"an identity's key twice": strings.Replace(good, `"seq":1}`, `"seq":1,"seq":2}`, 1) + "\n",
		"a null fact":             strings.Replace(good, `"kind":"x"`, `"kind":"x","lease":null`, 1) + "\n",
	} {
		events, err := Read(strings.NewReader(good + "\n" + file))
		if err == nil || len(events) != 1 {
			t.Fatalf("%s: %d events, %v", name, len(events), err)
		}
	}
}
