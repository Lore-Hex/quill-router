package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"google.golang.org/api/googleapi"
	logging "google.golang.org/api/logging/v2"
	"google.golang.org/api/option"

	"github.com/Lore-Hex/quill-router/fastpath/internal/edge"
)

// The edge report (fastpathwatch edge, G12 of
// docs/design/fast-admission-e2-decide.md): the edge's count lines of a
// span, read from Cloud Logging and summed, with the figures the I2 gates
// are judged by. Each line is a period's counts alone, so the sum is the
// span's counts, exactly, for every period whose line reached Cloud
// Logging: periods gives each node's lines, to hold against the span's
// minutes. It reads; it stops nothing.
//
//	fastpathwatch edge -project P -since 2026-10-17T00:00:00Z [-until 2026-10-24T00:00:00Z]

// logEntry is what the report reads of a log entry: its id (Cloud Logging
// can hand an entry over twice), its node and its message.
type logEntry struct {
	InsertID, Node, Message string
}

// edgeSums are one kind of line's counts, summed: the plain counts and the
// counts by name (classes, fields, passed, unavailable_by_code, ...).
type edgeSums struct {
	Counts map[string]int64            `json:"counts"`
	ByName map[string]map[string]int64 `json:"by_name"`
}

func newEdgeSums() *edgeSums {
	return &edgeSums{Counts: map[string]int64{}, ByName: map[string]map[string]int64{}}
}

func (s *edgeSums) add(l edge.CountsLine) {
	for k, v := range l.Counts {
		s.Counts[k] += v
	}
	for name, m := range l.ByName {
		if s.ByName[name] == nil {
			s.ByName[name] = map[string]int64{}
		}
		for k, v := range m {
			s.ByName[name][k] += v
		}
	}
}

// i2 are the I2 gates' figures (§11). The comparable requests are those
// whose answers were compared, agree_full and differ (Go would answer, and
// Python answered a 200 that is not a replay, a request naming a
// cache_affinity_key included), and agree_full_ratio is agree_full's share
// of them; differ_affinity_warm is the differs that are Python's warm
// re-rank of Go's cold order (differ.affinity_warm, §1 item 5), counted in
// differ. The cohort-eligible requests are every one Go
// would answer (each counts its decide row's gate), by class: the
// comparable ones and the rest (would_admit_python_refused, Python's errors
// and unavailable answers, the scope taken, replays, unread), which the
// gates account for rather than ratio. Then the differences by field
// (differ.* and row.*), the estimate's and the idempotency fingerprint's,
// fields_other (a period's fields past the line's bound, counted "other",
// which could hide either: non-zero, those two are not known to be zero),
// would_admit_python_refused by class, the stale passes against every
// request the shadow followed, and the decide path's unavailable answers by
// the insert's gRPC code.
type i2 struct {
	Comparable                int64            `json:"comparable"`
	AgreeFull                 int64            `json:"agree_full"`
	AgreeFullRatio            *float64         `json:"agree_full_ratio"`
	CohortEligible            int64            `json:"cohort_eligible"`
	CohortByClass             map[string]int64 `json:"cohort_by_class"`
	Differ                    int64            `json:"differ"`
	DifferAffinityWarm        int64            `json:"differ_affinity_warm"`
	DifferByField             map[string]int64 `json:"differ_by_field"`
	DifferEstimate            int64            `json:"differ_estimate"`
	RowIdempotencyFingerprint int64            `json:"row_idempotency_fingerprint"`
	FieldsOther               int64            `json:"fields_other"`
	WouldAdmitPythonRefused   int64            `json:"would_admit_python_refused"`
	WouldAdmitByClass         map[string]int64 `json:"would_admit_python_refused_by_class"`
	Stale                     int64            `json:"stale"`
	StaleByCache              map[string]int64 `json:"stale_by_cache"`
	Followed                  int64            `json:"followed"`
	StaleRate                 *float64         `json:"stale_rate"`
	Unavailable               int64            `json:"unavailable"`
	UnavailableByCode         map[string]int64 `json:"unavailable_by_code"`
	// SettlementHeaderPasses are the classes passed under
	// header.settlement_async (any X-TR-Settlement-Mode but exactly
	// async-v1, which G15c composes), and SettlementHeaderPassShare their
	// share of followed: a pass share under one reason, not the async-v1
	// share of traffic (docs/design/fast-admission-e2-g15-async.md §5).
	SettlementHeaderPasses    int64    `json:"settlement_header_passes"`
	SettlementHeaderPassShare *float64 `json:"settlement_header_pass_share"`
}

// i3 are the I3 gate's figures (docs/design/fast-admission-e2-g15-async.md
// §4.4, §5, §6.2), each one number from the shadow's async-v1 maps:
// async_seen (every followed request whose X-TR-Settlement-Mode is exactly
// async-v1, plain, affinity and unparsed) and its share of followed; the
// async-v1 requests whose async members were compared, in all and by
// population, each as a daily rate over the span (the soak's projection),
// and the agreement ratio of each population; the async members that
// differed, by field, both populations summed; and Python's shape C answers
// (its admission on), which restart the sample.
type i3 struct {
	AsyncSeen                       int64    `json:"async_seen"`
	AsyncSeenShare                  *float64 `json:"async_seen_share"`
	AsyncCompared                   int64    `json:"async_compared"`
	AsyncComparedPerDay             *float64 `json:"async_compared_per_day"`
	AsyncComparedPlain              int64    `json:"async_compared_plain"`
	AsyncComparedPlainPerDay        *float64 `json:"async_compared_plain_per_day"`
	AsyncComparedPlainAgreeRatio    *float64 `json:"async_compared_plain_agree_ratio"`
	AsyncComparedAffinity           int64    `json:"async_compared_affinity"`
	AsyncComparedAffinityAgreeRatio *float64 `json:"async_compared_affinity_agree_ratio"`
	AsyncBillingSnapshotHash        int64    `json:"async_fields_billing_snapshot_hash"`
	AsyncBillingSnapshot            int64    `json:"async_fields_billing_snapshot"`
	AsyncEligible                   int64    `json:"async_fields_async_eligible"`
	AsyncShape                      int64    `json:"async_fields_async_shape"`
	AsyncPythonAdmitted             int64    `json:"async_python_admitted"`
}

// edgeReport is the report's answer.
type edgeReport struct {
	Since time.Time `json:"since"`
	Until time.Time `json:"until"`
	// Periods are each node's period lines, by kind: a line of the decide
	// path, and the shadow's line of the requests it did not follow, one
	// each a period.
	Periods      map[string]map[string]int64 `json:"periods"`
	DecideShadow *edgeSums                   `json:"decide_shadow"`
	Decide       *edgeSums                   `json:"decide"`
	I2           i2                          `json:"i2"`
	I3           i3                          `json:"i3"`

	seen map[string]bool
}

// add counts one entry's line; a count line that does not read fails the
// report, since its counts would otherwise be lost.
func (r *edgeReport) add(e logEntry) error {
	if e.InsertID != "" {
		if r.seen[e.InsertID] {
			return nil
		}
		r.seen[e.InsertID] = true
	}
	l, ok, err := edge.ParseCountsLine(e.Message)
	if err != nil {
		return fmt.Errorf("entry %s of %s: %w", e.InsertID, e.Node, err)
	}
	if !ok {
		return nil
	}
	sums := r.Decide
	if l.Of == edge.CountsDecideShadow {
		sums = r.DecideShadow
	}
	sums.add(l)
	if l.Digest == "" {
		if r.Periods[e.Node] == nil {
			r.Periods[e.Node] = map[string]int64{}
		}
		r.Periods[e.Node][l.Of]++
	}
	return nil
}

// gates fills the I2 figures from the sums.
func (r *edgeReport) gates() {
	sh := r.DecideShadow
	g := i2{AgreeFull: sh.Counts["agree_full"], Differ: sh.Counts["differ"],
		WouldAdmitPythonRefused: sh.Counts["would_admit_python_refused"], DifferByField: map[string]int64{},
		WouldAdmitByClass: map[string]int64{}, StaleByCache: map[string]int64{}, CohortByClass: map[string]int64{},
		Unavailable: r.Decide.Counts["unavailable"], UnavailableByCode: map[string]int64{}}
	g.Comparable = g.AgreeFull + g.Differ
	for _, n := range sh.ByName["decide"] {
		g.CohortEligible += n
	}
	for class, n := range sh.ByName["cohort"] {
		g.CohortByClass[class] = n
	}
	for field, n := range sh.ByName["fields"] {
		g.DifferByField[field] = n
		if strings.HasPrefix(field, "differ.estimate") {
			g.DifferEstimate += n
		}
	}
	g.DifferAffinityWarm = sh.ByName["fields"][edge.FieldAffinityWarm]
	g.RowIdempotencyFingerprint = sh.ByName["fields"]["row.idempotency_fingerprint"]
	g.FieldsOther = sh.ByName["fields"]["other"]
	for class, n := range sh.ByName["classes"] {
		g.Followed += n
		if c, ok := strings.CutPrefix(class, edge.ClassWouldAdmit+"."); ok {
			g.WouldAdmitByClass[c] = n
		}
		if c, ok := strings.CutPrefix(class, edge.ClassStale+"."); ok {
			g.StaleByCache[c] = n
			g.Stale += n
		}
		if strings.HasSuffix(class, "."+edge.PassSettlementHeader) {
			g.SettlementHeaderPasses += n
		}
	}
	for code, n := range r.Decide.ByName["unavailable_by_code"] {
		g.UnavailableByCode[code] = n
	}
	g.AgreeFullRatio = ratio(g.AgreeFull, g.Comparable)
	g.StaleRate = ratio(g.Stale, g.Followed)
	g.SettlementHeaderPassShare = ratio(g.SettlementHeaderPasses, g.Followed)
	r.I2 = g
	r.I3 = r.asyncGates(g.Followed)
}

// asyncGates fills the I3 figures from the shadow's async-v1 maps, over
// followed (every request the shadow followed).
func (r *edgeReport) asyncGates(followed int64) i3 {
	sh := r.DecideShadow
	var g i3
	for _, n := range sh.ByName["async_seen"] {
		g.AsyncSeen += n
	}
	agree := map[string]int64{}
	for key, n := range sh.ByName["async_compared"] {
		pop, outcome, _ := strings.Cut(key, ".")
		g.AsyncCompared += n
		switch pop {
		case edge.AsyncPlain:
			g.AsyncComparedPlain += n
		case edge.AsyncAffinity:
			g.AsyncComparedAffinity += n
		}
		if outcome == "agree" {
			agree[pop] += n
		}
	}
	for key, n := range sh.ByName["async_fields"] {
		_, field, _ := strings.Cut(key, ".")
		switch field {
		case edge.FieldBillingSnapshotHash:
			g.AsyncBillingSnapshotHash += n
		case edge.FieldBillingSnapshot:
			g.AsyncBillingSnapshot += n
		case edge.FieldAsyncEligible:
			g.AsyncEligible += n
		case edge.FieldAsyncShape:
			g.AsyncShape += n
		}
	}
	for _, n := range sh.ByName["async_python_admitted"] {
		g.AsyncPythonAdmitted += n
	}
	g.AsyncSeenShare = ratio(g.AsyncSeen, followed)
	g.AsyncComparedPlainAgreeRatio = ratio(agree[edge.AsyncPlain], g.AsyncComparedPlain)
	g.AsyncComparedAffinityAgreeRatio = ratio(agree[edge.AsyncAffinity], g.AsyncComparedAffinity)
	if days := r.Until.Sub(r.Since).Hours() / 24; days > 0 {
		perDay := float64(g.AsyncCompared) / days
		plainPerDay := float64(g.AsyncComparedPlain) / days
		g.AsyncComparedPerDay, g.AsyncComparedPlainPerDay = &perDay, &plainPerDay
	}
	return g
}

// ratio is n over of, nil when of is 0.
func ratio(n, of int64) *float64 {
	if of == 0 {
		return nil
	}
	r := float64(n) / float64(of)
	return &r
}

// edgeFilter selects the span's lines of the edge's decide path and shadow
// from the nodes' journal; the report reads which are count lines.
func edgeFilter(project string, since, until time.Time) string {
	return fmt.Sprintf(`logName="projects/%s/logs/fastpath_journal" AND timestamp>="%s" AND timestamp<"%s" AND `+
		`jsonPayload.MESSAGE:"fastpath edge decide"`, project, since.UTC().Format(time.RFC3339),
		until.UTC().Format(time.RFC3339))
}

// logReader reads a span's entries from Cloud Logging, oldest first, a
// page at a time: at least pace apart, since entries.list allows 60 calls a
// minute a project, and again after a 429 or a 5xx, waiting twice as long
// each time, readAttempts times in all.
type logReader struct {
	opts []option.ClientOption
	pace time.Duration
}

// productionLogs is the API's, a little under a page a second.
var productionLogs = logReader{pace: 1100 * time.Millisecond}

const readAttempts = 6

func (lr logReader) entries(ctx context.Context, project string, since, until time.Time, each func(logEntry) error) error {
	svc, err := logging.NewService(ctx, lr.opts...)
	if err != nil {
		return err
	}
	req := &logging.ListLogEntriesRequest{ResourceNames: []string{"projects/" + project},
		Filter: edgeFilter(project, since, until), OrderBy: "timestamp asc", PageSize: 1000}
	for first := true; ; first = false {
		resp, err := lr.page(ctx, svc, req, first)
		if err != nil {
			return err
		}
		for _, e := range resp.Entries {
			var payload struct {
				Message string `json:"MESSAGE"`
			}
			if len(e.JsonPayload) > 0 {
				if err := json.Unmarshal(e.JsonPayload, &payload); err != nil {
					return fmt.Errorf("entry %s: %w", e.InsertId, err)
				}
			}
			if err := each(logEntry{InsertID: e.InsertId, Node: e.Labels["compute.googleapis.com/resource_name"],
				Message: payload.Message}); err != nil {
				return err
			}
		}
		if resp.NextPageToken == "" {
			return nil
		}
		req.PageToken = resp.NextPageToken
	}
}

// page reads one page: pace after the last read (none before the first),
// and after a 429 or a 5xx twice the last wait, at most readAttempts times.
func (lr logReader) page(ctx context.Context, svc *logging.Service, req *logging.ListLogEntriesRequest,
	first bool) (*logging.ListLogEntriesResponse, error) {
	wait := lr.pace
	if first {
		wait = 0
	}
	for attempt := 1; ; attempt++ {
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(wait):
		}
		resp, err := svc.Entries.List(req).Context(ctx).Do()
		var apiErr *googleapi.Error
		if err == nil || attempt == readAttempts || !errors.As(err, &apiErr) ||
			(apiErr.Code != http.StatusTooManyRequests && apiErr.Code < 500) {
			return resp, err
		}
		wait = max(2*wait, 2*lr.pace)
	}
}

// runEdge is the edge report: its exit code (0 with the report printed, 1
// if it cannot read or a line does not, 2 for its usage) and what went
// wrong.
func runEdge(ctx context.Context, args []string, out io.Writer, d deps) (int, error) {
	fs := flag.NewFlagSet("fastpathwatch edge", flag.ContinueOnError)
	project := fs.String("project", "", "the project whose Cloud Logging has the nodes' journal")
	since := fs.String("since", "", "the span's start, RFC 3339")
	until := fs.String("until", "", "the span's end, RFC 3339; now when empty")
	if err := fs.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return 0, nil
		}
		return 2, err
	}
	r := &edgeReport{Until: d.clock().UTC(), Periods: map[string]map[string]int64{}, DecideShadow: newEdgeSums(),
		Decide: newEdgeSums(), seen: map[string]bool{}}
	var err error
	if r.Since, err = time.Parse(time.RFC3339, *since); err != nil || *project == "" || fs.NArg() != 0 {
		return 2, errors.New("edge: -project, and -since in RFC 3339; no arguments past the flags")
	}
	if *until != "" {
		if r.Until, err = time.Parse(time.RFC3339, *until); err != nil {
			return 2, fmt.Errorf("edge: -until: %w", err)
		}
	}
	if !r.Since.Before(r.Until) {
		return 2, errors.New("edge: -since must be before -until")
	}
	if err := d.entries(ctx, *project, r.Since, r.Until, r.add); err != nil {
		return 1, err
	}
	r.gates()
	encoder := json.NewEncoder(out)
	encoder.SetIndent("", "  ")
	if err := encoder.Encode(r); err != nil {
		return 1, err
	}
	return 0, nil
}
