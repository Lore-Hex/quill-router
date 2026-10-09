package watch

import (
	"context"
	"errors"
	"fmt"
	"time"

	monitoring "cloud.google.com/go/monitoring/apiv3/v2"
	"cloud.google.com/go/monitoring/apiv3/v2/monitoringpb"
	"google.golang.org/api/iterator"
	"google.golang.org/protobuf/types/known/durationpb"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// Monitoring reads Spanner's CPU and the subscriptions' backlogs from Cloud
// Monitoring, each the highest value of its series over the last Window,
// aligned a minute at a time. Their points are sampled every minute and
// visible up to three minutes later, so the newest must be at most Fresh
// old, four minutes by default: a series older than that is a read that
// fails, as of a source that has stopped reporting, not a reading that
// stays as it was.
type Monitoring struct {
	Client        *monitoring.MetricClient
	Project       string
	Instance      string
	Subscriptions []string
	Window        time.Duration
	Fresh         time.Duration
	Clock         func() time.Time
}

// align is how Monitoring's points are aligned: a minute, their sampling.
const align = time.Minute

// SpannerCPU is the instance's high-priority CPU, read as production's
// alarm on it reads it (scripts/deploy/spanner-alerts/high-priority-cpu.yaml).
func (m Monitoring) SpannerCPU(ctx context.Context) (float64, error) {
	return m.highest(ctx, fmt.Sprintf(`resource.type = "spanner_instance" AND resource.labels.instance_id = %q AND `+
		`metric.type = "spanner.googleapis.com/instance/cpu/utilization_by_priority" AND metric.labels.priority = "high"`,
		m.Instance))
}

// Backlogs are each subscription's undelivered messages and oldest
// unacknowledged message's age; a subscription Monitoring reports nothing
// for fails the read.
func (m Monitoring) Backlogs(ctx context.Context) (map[string]Backlog, error) {
	out := make(map[string]Backlog, len(m.Subscriptions))
	for _, sub := range m.Subscriptions {
		of := func(metric string) string {
			return fmt.Sprintf(`resource.type = "pubsub_subscription" AND resource.labels.subscription_id = %q AND `+
				`metric.type = "pubsub.googleapis.com/subscription/%s"`, sub, metric)
		}
		undelivered, err := m.highest(ctx, of("num_undelivered_messages"))
		if err != nil {
			return nil, fmt.Errorf("%s: %w", sub, err)
		}
		age, err := m.highest(ctx, of("oldest_unacked_message_age"))
		if err != nil {
			return nil, fmt.Errorf("%s: %w", sub, err)
		}
		out[sub] = Backlog{Undelivered: int64(undelivered), OldestAge: time.Duration(age * float64(time.Second))}
	}
	return out, nil
}

// highest is the highest value of the series the filter names over the
// last Window, each aligned to its highest a minute at a time and reduced
// to the highest of them; none, or none newer than Fresh, is an error.
func (m Monitoring) highest(ctx context.Context, filter string) (float64, error) {
	if m.Window < align || m.Fresh <= 0 || m.Fresh > m.Window {
		return 0, fmt.Errorf("watch: a window of at least %v, and freshness above 0 and within it", align)
	}
	now := m.Clock()
	it := m.Client.ListTimeSeries(ctx, &monitoringpb.ListTimeSeriesRequest{
		Name:   "projects/" + m.Project,
		Filter: filter,
		Interval: &monitoringpb.TimeInterval{StartTime: timestamppb.New(now.Add(-m.Window)),
			EndTime: timestamppb.New(now)},
		Aggregation: &monitoringpb.Aggregation{AlignmentPeriod: durationpb.New(align),
			PerSeriesAligner:   monitoringpb.Aggregation_ALIGN_MAX,
			CrossSeriesReducer: monitoringpb.Aggregation_REDUCE_MAX},
		View: monitoringpb.ListTimeSeriesRequest_FULL,
	})
	found, top, newest := false, 0.0, time.Time{}
	for {
		series, err := it.Next()
		if errors.Is(err, iterator.Done) {
			break
		}
		if err != nil {
			return 0, err
		}
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
			if !found || v > top {
				found, top = true, v
			}
			if end := p.GetInterval().GetEndTime().AsTime(); end.After(newest) {
				newest = end
			}
		}
	}
	if !found {
		return 0, fmt.Errorf("no point in the last %v for %s", m.Window, filter)
	}
	if age := now.Sub(newest); age > m.Fresh {
		return 0, fmt.Errorf("the newest point is %v old, past %v, for %s", age, m.Fresh, filter)
	}
	return top, nil
}

// Store reads the pending work and what the stage's workspace has booked
// from the service's store. Limit bounds the pending packs one read takes:
// more than that fails the read, so the watch stops a stage whose pending
// work it cannot count.
type Store struct {
	Store     *store.Store
	Workspace string
	Limit     int
}

// Pending names every pack whose work is not done, by its lease and
// version.
func (s Store) Pending(ctx context.Context) ([]string, error) {
	var out []string
	var after store.PendingPack
	page := min(1000, s.Limit+1)
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
	Monitoring
	Store
}

var _ Sources = Production{}
