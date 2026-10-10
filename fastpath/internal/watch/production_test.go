package watch

import (
	"context"
	"net"
	"strings"
	"sync"
	"testing"
	"time"

	monitoring "cloud.google.com/go/monitoring/apiv3/v2"
	"cloud.google.com/go/monitoring/apiv3/v2/monitoringpb"
	"google.golang.org/api/option"
	"google.golang.org/genproto/googleapis/api/monitoredres"
	"google.golang.org/genproto/googleapis/rpc/status"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// metrics is a stand-in for Cloud Monitoring's metric service: it answers a
// request with the points set for its filter, each ending age before the
// watch's clock, as one series, and the extra series set for it, each a
// region's, with points and an age of its own; and keeps each request.
type metrics struct {
	monitoringpb.UnimplementedMetricServiceServer
	mu       sync.Mutex
	points   map[string][]*monitoringpb.TypedValue // by filter
	age      time.Duration
	extra    map[string][]extraSeries // by filter
	requests []*monitoringpb.ListTimeSeriesRequest
	// incomplete marks every answer as one Monitoring could not complete;
	// errorPage answers first with an empty page marked so, and a next
	// page; split answers the first series on one page and the rest on a
	// next.
	incomplete bool
	errorPage  bool
	split      bool
}

type extraSeries struct {
	region string
	values []*monitoringpb.TypedValue
	age    time.Duration
}

func (m *metrics) ListTimeSeries(_ context.Context, req *monitoringpb.ListTimeSeriesRequest) (*monitoringpb.ListTimeSeriesResponse, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.requests = append(m.requests, req)
	resp := &monitoringpb.ListTimeSeriesResponse{}
	if m.errorPage && req.GetPageToken() == "" {
		resp.ExecutionErrors = []*status.Status{{Code: int32(codes.Unavailable), Message: "a replica did not answer"}}
		resp.NextPageToken = "after-the-error"
		return resp, nil
	}
	series := func(region string, values []*monitoringpb.TypedValue, age time.Duration) *monitoringpb.TimeSeries {
		var points []*monitoringpb.Point
		for _, v := range values {
			points = append(points, &monitoringpb.Point{Value: v,
				Interval: &monitoringpb.TimeInterval{EndTime: timestamppb.New(start.Add(-age))}})
		}
		s := &monitoringpb.TimeSeries{Points: points}
		if region != "" {
			s.Resource = &monitoredres.MonitoredResource{Labels: map[string]string{"region": region}}
		}
		return s
	}
	var all []*monitoringpb.TimeSeries
	if values, ok := m.points[req.GetFilter()]; ok {
		all = append(all, series("", values, m.age))
	}
	for _, e := range m.extra[req.GetFilter()] {
		all = append(all, series(e.region, e.values, e.age))
	}
	switch {
	case m.split && req.GetPageToken() == "" && len(all) > 0:
		resp.TimeSeries, resp.NextPageToken = all[:1], "the-rest"
	case m.split:
		resp.TimeSeries = all[min(1, len(all)):]
	default:
		resp.TimeSeries = all
	}
	if m.incomplete {
		resp.ExecutionErrors = []*status.Status{{Code: int32(codes.Unavailable), Message: "a replica did not answer"}}
	}
	return resp, nil
}

func double(v float64) *monitoringpb.TypedValue {
	return &monitoringpb.TypedValue{Value: &monitoringpb.TypedValue_DoubleValue{DoubleValue: v}}
}

func integer(v int64) *monitoringpb.TypedValue {
	return &monitoringpb.TypedValue{Value: &monitoringpb.TypedValue_Int64Value{Int64Value: v}}
}

// served is a Monitoring source on a stand-in for the metric service.
func served(t *testing.T, m *metrics, subs ...string) *Monitoring {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	srv := grpc.NewServer()
	monitoringpb.RegisterMetricServiceServer(srv, m)
	go func() { _ = srv.Serve(ln) }()
	t.Cleanup(srv.Stop)
	client, err := monitoring.NewMetricClient(context.Background(), option.WithEndpoint(ln.Addr().String()),
		option.WithoutAuthentication(), option.WithGRPCDialOption(grpc.WithTransportCredentials(insecure.NewCredentials())))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = client.Close() })
	return &Monitoring{Client: client, Project: "proj", Instance: "trusted-router-nam6", Subs: subs,
		Window: 5 * time.Minute, Fresh: 4 * time.Minute, Clock: func() time.Time { return start }}
}

const cpuFilter = `resource.type = "spanner_instance" AND resource.labels.instance_id = "trusted-router-nam6" AND ` +
	`metric.type = "spanner.googleapis.com/instance/cpu/utilization_by_priority" AND metric.labels.priority = "high"`

func subFilter(sub, metric string) string {
	return `resource.type = "pubsub_subscription" AND resource.labels.subscription_id = "` + sub + `" AND ` +
		`metric.type = "pubsub.googleapis.com/subscription/` + metric + `"`
}

// TestMonitoringReadsAsTheAlarmDoes: Spanner's CPU is the high-priority
// series production's alarm reads, aligned to their highest a minute at a
// time over the window, in the project, each series read on its own; its
// highest point is the reading.
func TestMonitoringReadsAsTheAlarmDoes(t *testing.T) {
	m := &metrics{points: map[string][]*monitoringpb.TypedValue{cpuFilter: {double(0.21), double(0.34), double(0.3)}}}
	src := served(t, m)
	cpu, err := src.SpannerCPU(context.Background())
	if err != nil || cpu != 0.34 {
		t.Fatalf("Spanner's CPU: %v %v", cpu, err)
	}
	req := m.requests[0]
	a := req.GetAggregation()
	switch {
	case req.GetName() != "projects/proj":
		t.Errorf("the project: %s", req.GetName())
	case a.GetPerSeriesAligner() != monitoringpb.Aggregation_ALIGN_MAX ||
		a.GetCrossSeriesReducer() != monitoringpb.Aggregation_REDUCE_NONE || a.GetAlignmentPeriod().AsDuration() != time.Minute:
		t.Errorf("the aggregation: %v", a)
	case !req.GetInterval().GetEndTime().AsTime().Equal(start) ||
		!req.GetInterval().GetStartTime().AsTime().Equal(start.Add(-5*time.Minute)):
		t.Errorf("the interval: %v", req.GetInterval())
	}
}

// TestMonitoringReadsEachBacklog: each subscription's undelivered messages
// and oldest message's age, at their highest; a metric with no series
// fails its read, as does Spanner's CPU with none.
func TestMonitoringReadsEachBacklog(t *testing.T) {
	m := &metrics{points: map[string][]*monitoringpb.TypedValue{
		subFilter("auditor", "num_undelivered_messages"):   {integer(3), integer(7)},
		subFilter("auditor", "oldest_unacked_message_age"): {integer(12)},
	}}
	src := served(t, m, "auditor", "archive")
	ctx := context.Background()
	if subs := src.Subscriptions(); len(subs) != 2 || subs[0] != "auditor" || subs[1] != "archive" {
		t.Fatalf("the subscriptions: %v", subs)
	}
	if n, err := src.Undelivered(ctx, "auditor"); err != nil || n != 7 {
		t.Fatalf("the auditor's undelivered: %v %v", n, err)
	}
	if age, err := src.OldestAge(ctx, "auditor"); err != nil || age != 12*time.Second {
		t.Fatalf("the auditor's oldest: %v %v", age, err)
	}
	if _, err := src.Undelivered(ctx, "archive"); err == nil || !strings.Contains(err.Error(), "archive") {
		t.Fatalf("a subscription with no series: %v", err)
	}
	if _, err := src.OldestAge(ctx, "archive"); err == nil {
		t.Fatal("a subscription with no age series is read")
	}
	if _, err := served(t, &metrics{}).SpannerCPU(ctx); err == nil {
		t.Fatal("Spanner's CPU with no series is read")
	}
}

// TestAStaleSeriesIsAFailedRead: a series whose newest point is older than
// Fresh, as of a source that stopped reporting, fails the read; one exactly
// that old is read.
func TestAStaleSeriesIsAFailedRead(t *testing.T) {
	m := &metrics{points: map[string][]*monitoringpb.TypedValue{cpuFilter: {double(0.1)}}, age: 4 * time.Minute}
	if cpu, err := served(t, m).SpannerCPU(context.Background()); err != nil || cpu != 0.1 {
		t.Fatalf("a point as old as allowed: %v %v", cpu, err)
	}
	m.age = 4*time.Minute + time.Second
	if _, err := served(t, m).SpannerCPU(context.Background()); err == nil || !strings.Contains(err.Error(), "old") {
		t.Fatalf("a stale point: %v", err)
	}
}

// TestAnIncompleteAnswerIsAFailedRead: an answer Monitoring marks as one it
// could not complete, though it carries a fresh point under the ceiling,
// fails the read: the series it lacks may be the one past it.
func TestAnIncompleteAnswerIsAFailedRead(t *testing.T) {
	m := &metrics{points: map[string][]*monitoringpb.TypedValue{cpuFilter: {double(0.1)}}, incomplete: true}
	if _, err := served(t, m).SpannerCPU(context.Background()); err == nil || !strings.Contains(err.Error(), "incomplete") {
		t.Fatalf("an incomplete answer: %v", err)
	}
	m.incomplete = false
	if cpu, err := served(t, m).SpannerCPU(context.Background()); err != nil || cpu != 0.1 {
		t.Fatalf("the same answer complete: %v %v", cpu, err)
	}
}

// TestEverySeriesIsReadOnItsOwn: the reading is the highest point of every
// series, a region's each; a series whose newest point is stale fails the
// read though another is fresh and low, naming it; one that reported
// before and is gone fails it too; and one not seen before is read.
func TestEverySeriesIsReadOnItsOwn(t *testing.T) {
	m := &metrics{points: map[string][]*monitoringpb.TypedValue{cpuFilter: {double(0.1)}},
		extra: map[string][]extraSeries{cpuFilter: {{region: "b", values: []*monitoringpb.TypedValue{double(0.5)}}}}}
	src := served(t, m)
	ctx := context.Background()
	if cpu, err := src.SpannerCPU(ctx); err != nil || cpu != 0.5 {
		t.Fatalf("two regions: %v %v", cpu, err)
	}
	m.mu.Lock()
	m.extra[cpuFilter][0].age = 4*time.Minute + time.Second
	m.mu.Unlock()
	if _, err := src.SpannerCPU(ctx); err == nil || !strings.Contains(err.Error(), "old") || !strings.Contains(err.Error(), "b") {
		t.Fatalf("a region stale beside a fresh one: %v", err)
	}
	m.mu.Lock()
	m.extra[cpuFilter] = nil
	m.mu.Unlock()
	if _, err := src.SpannerCPU(ctx); err == nil || !strings.Contains(err.Error(), "gone") || !strings.Contains(err.Error(), "b") {
		t.Fatalf("a region gone: %v", err)
	}
	m.mu.Lock()
	m.extra[cpuFilter] = []extraSeries{{region: "b", values: []*monitoringpb.TypedValue{double(0.2)}},
		{region: "c", values: []*monitoringpb.TypedValue{double(0.3)}}}
	m.mu.Unlock()
	if cpu, err := src.SpannerCPU(ctx); err != nil || cpu != 0.3 {
		t.Fatalf("the region back, and one not seen before: %v %v", cpu, err)
	}
	// Another source's series are its own: the subscription's are not
	// expected of the CPU's.
	if _, err := src.Undelivered(ctx, "auditor"); err == nil {
		t.Fatal("a subscription with no series is read")
	}
	if cpu, err := src.SpannerCPU(ctx); err != nil || cpu != 0.3 {
		t.Fatalf("the CPU after another filter's read: %v %v", cpu, err)
	}
}

// TestEveryPageIsRead: the series on a later page count, and an empty page
// Monitoring could not complete fails the read though the next page is
// whole, which the iterator's Next would have skipped.
func TestEveryPageIsRead(t *testing.T) {
	m := &metrics{points: map[string][]*monitoringpb.TypedValue{cpuFilter: {double(0.1)}},
		extra: map[string][]extraSeries{cpuFilter: {{region: "b", values: []*monitoringpb.TypedValue{double(0.5)}}}}, split: true}
	src := served(t, m)
	ctx := context.Background()
	if cpu, err := src.SpannerCPU(ctx); err != nil || cpu != 0.5 || len(m.requests) != 2 {
		t.Fatalf("two pages: %v %v, %d requests", cpu, err, len(m.requests))
	}
	m.mu.Lock()
	m.split, m.errorPage = false, true
	m.mu.Unlock()
	if _, err := src.SpannerCPU(ctx); err == nil || !strings.Contains(err.Error(), "incomplete") {
		t.Fatalf("an empty page that could not be completed, before a whole one: %v", err)
	}
}
