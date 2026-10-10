package watch

import (
	"context"
	"fmt"
	"maps"
	"sync"
	"time"

	monitoring "cloud.google.com/go/monitoring/apiv3/v2"
	"cloud.google.com/go/monitoring/apiv3/v2/monitoringpb"
	"google.golang.org/protobuf/types/known/durationpb"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// Monitoring reads Spanner's CPU and the subscriptions' backlogs from Cloud
// Monitoring, each the highest value of its series over the last Window,
// aligned a minute at a time. Their points are sampled every minute and
// visible up to ReportingDelay later, so each series' newest sample must
// be at most Fresh old, five minutes by default, its point's time plus
// the minute it stands for: a series older than that is a read that
// fails, as of a source that has stopped reporting, not a reading that
// stays as it was; and a series that reported before and is gone fails
// the read too, since the highest of the rest would hide it.
type Monitoring struct {
	Client   *monitoring.MetricClient
	Project  string
	Instance string
	Subs     []string
	Window   time.Duration
	Fresh    time.Duration
	Clock    func() time.Time

	// seen are the series each filter has answered with, by their labels.
	mu   sync.Mutex
	seen map[string]map[string]bool
}

// Align is how Monitoring's points are aligned: a minute, their sampling.
// An aligned point stands at the end of its minute for the samples within
// it, so its newest sample can be a minute older than its time.
const Align = time.Minute

// ReportingDelay is how long after its sampling a point can take to show:
// three minutes for Spanner's CPU, two for Pub/Sub's backlogs.
const ReportingDelay = 3 * time.Minute

// SpannerCPU is the instance's high-priority CPU, read as production's
// alarm on it reads it (scripts/deploy/spanner-alerts/high-priority-cpu.yaml).
func (m *Monitoring) SpannerCPU(ctx context.Context) (float64, error) {
	return m.highest(ctx, fmt.Sprintf(`resource.type = "spanner_instance" AND resource.labels.instance_id = %q AND `+
		`metric.type = "spanner.googleapis.com/instance/cpu/utilization_by_priority" AND metric.labels.priority = "high"`,
		m.Instance))
}

// Subscriptions are the subscriptions watched.
func (m *Monitoring) Subscriptions() []string { return m.Subs }

// Undelivered is a subscription's messages not yet delivered, at their
// highest over the window.
func (m *Monitoring) Undelivered(ctx context.Context, sub string) (int64, error) {
	v, err := m.highest(ctx, subscriptionFilter(sub, "num_undelivered_messages"))
	return int64(v), err
}

// OldestAge is the age of a subscription's oldest unacknowledged message,
// at its highest over the window.
func (m *Monitoring) OldestAge(ctx context.Context, sub string) (time.Duration, error) {
	v, err := m.highest(ctx, subscriptionFilter(sub, "oldest_unacked_message_age"))
	return time.Duration(v * float64(time.Second)), err
}

func subscriptionFilter(sub, metric string) string {
	return fmt.Sprintf(`resource.type = "pubsub_subscription" AND resource.labels.subscription_id = %q AND `+
		`metric.type = "pubsub.googleapis.com/subscription/%s"`, sub, metric)
}

// highest is the highest point of the series the filter names over the
// last Window, each aligned to its highest a minute at a time, as the
// alarm's are; they are reduced to their highest here, not by Monitoring,
// so that each series is seen: one whose newest point is older than Fresh,
// one that reported before and is gone, a page Monitoring could not
// complete, and no series at all are each an error.
func (m *Monitoring) highest(ctx context.Context, filter string) (float64, error) {
	if m.Window < Align || m.Fresh <= Align || m.Fresh > m.Window {
		return 0, fmt.Errorf("watch: a window of at least %v, and freshness above that and within the window", Align)
	}
	now := m.Clock()
	it := m.Client.ListTimeSeries(ctx, &monitoringpb.ListTimeSeriesRequest{
		Name:   "projects/" + m.Project,
		Filter: filter,
		Interval: &monitoringpb.TimeInterval{StartTime: timestamppb.New(now.Add(-m.Window)),
			EndTime: timestamppb.New(now)},
		Aggregation: &monitoringpb.Aggregation{AlignmentPeriod: durationpb.New(Align),
			PerSeriesAligner: monitoringpb.Aggregation_ALIGN_MAX},
		View: monitoringpb.ListTimeSeriesRequest_FULL,
	})
	top := 0.0
	newest := map[string]time.Time{} // by series
	// The series seen are remembered as each page arrives, before the read
	// can fail on that page or a later one: a series stale now, or on a
	// page Monitoring could not complete, and gone by the next look, is
	// missed then too. Gone is judged against what was seen before this
	// read.
	m.mu.Lock()
	before := maps.Clone(m.seen[filter])
	m.mu.Unlock()
	remember := func(page []*monitoringpb.TimeSeries) {
		m.mu.Lock()
		defer m.mu.Unlock()
		if m.seen == nil {
			m.seen = map[string]map[string]bool{}
		}
		if m.seen[filter] == nil {
			m.seen[filter] = map[string]bool{}
		}
		for _, series := range page {
			m.seen[filter][seriesKey(series)] = true
		}
	}
	// A page at a time, one fetch each, so that each page's execution
	// errors are seen: the iterator's Next, and its pager, skip an empty
	// page, errors and all.
	token := ""
	for {
		page, next, err := it.InternalFetch(1000, token)
		if err != nil {
			return 0, err
		}
		token = next
		remember(page)
		if resp, ok := it.Response.(*monitoringpb.ListTimeSeriesResponse); ok && len(resp.GetExecutionErrors()) > 0 {
			return 0, fmt.Errorf("an incomplete answer for %s: %s", filter, resp.GetExecutionErrors()[0].GetMessage())
		}
		for _, series := range page {
			key := seriesKey(series)
			for _, p := range series.GetPoints() {
				var v float64
				switch value := p.GetValue().GetValue().(type) {
				case *monitoringpb.TypedValue_DoubleValue:
					v = value.DoubleValue
				case *monitoringpb.TypedValue_Int64Value:
					v = float64(value.Int64Value)
				default:
					return 0, fmt.Errorf("a point of %T", value)
				}
				top = max(top, v)
				if end := p.GetInterval().GetEndTime().AsTime(); end.After(newest[key]) {
					newest[key] = end
				}
			}
		}
		if token == "" {
			break
		}
	}
	if len(newest) == 0 {
		return 0, fmt.Errorf("no point in the last %v for %s", m.Window, filter)
	}
	for key := range before {
		if _, ok := newest[key]; !ok {
			return 0, fmt.Errorf("the series %s reported before and is gone, for %s", key, filter)
		}
	}
	for key, end := range newest {
		// The point's newest sample can be Align older than its time.
		if age := now.Sub(end) + Align; age > m.Fresh {
			return 0, fmt.Errorf("the newest point of %s is up to %v old, past %v, for %s", key, age, m.Fresh, filter)
		}
	}
	return top, nil
}

// seriesKey names a series by its labels, the metric's and the resource's.
func seriesKey(series *monitoringpb.TimeSeries) string {
	return fmt.Sprint(series.GetMetric().GetLabels(), series.GetResource().GetLabels())
}

// Store reads the pending work and what the stage's workspace has booked
// from the service's store. Limit bounds the pending packs one read takes:
// more than that fails the read, so the watch stops a stage whose pending
// work it cannot count. The packs are read Page at a time, 1000 if unset.
type Store struct {
	Store     *store.Store
	Workspace string
	Limit     int
	Page      int
}

// Pending names every pack whose work is not done, by its lease and
// version.
func (s Store) Pending(ctx context.Context) ([]string, error) {
	var out []string
	var after store.PendingPack
	page := s.Page
	if page <= 0 {
		page = 1000
	}
	page = min(page, s.Limit+1)
	for {
		packs, err := s.Store.PendingPacks(ctx, after, page)
		if err != nil {
			return nil, err
		}
		for _, p := range packs {
			out = append(out, fmt.Sprintf("%s/%s/%d", p.Ref.Workspace, p.Ref.LeaseID, p.CommitVersion))
		}
		if len(out) > s.Limit {
			return nil, fmt.Errorf("more than %d packs' work is pending", s.Limit)
		}
		if len(packs) < page {
			return out, nil
		}
		after = packs[len(packs)-1]
	}
}

// Booked is what the workspace has booked, all told.
func (s Store) Booked(ctx context.Context) (int64, error) {
	return s.Store.Booked(ctx, s.Workspace)
}

// Production is the watch's sources in production: Monitoring's and the
// store's.
type Production struct {
	*Monitoring
	Store
}

var _ Sources = Production{}
