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
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// metrics is a stand-in for Cloud Monitoring's metric service: it answers a
// request with the points set for its filter, and keeps each request.
type metrics struct {
	monitoringpb.UnimplementedMetricServiceServer
	mu       sync.Mutex
	points   map[string][]*monitoringpb.TypedValue // by filter
	requests []*monitoringpb.ListTimeSeriesRequest
}

func (m *metrics) ListTimeSeries(_ context.Context, req *monitoringpb.ListTimeSeriesRequest) (*monitoringpb.ListTimeSeriesResponse, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.requests = append(m.requests, req)
	values, ok := m.points[req.GetFilter()]
	if !ok {
		return &monitoringpb.ListTimeSeriesResponse{}, nil
	}
	var points []*monitoringpb.Point
	for _, v := range values {
		points = append(points, &monitoringpb.Point{Value: v,
			Interval: &monitoringpb.TimeInterval{EndTime: timestamppb.New(start)}})
	}
	return &monitoringpb.ListTimeSeriesResponse{TimeSeries: []*monitoringpb.TimeSeries{{Points: points}}}, nil
}

func double(v float64) *monitoringpb.TypedValue {
	return &monitoringpb.TypedValue{Value: &monitoringpb.TypedValue_DoubleValue{DoubleValue: v}}
}

func integer(v int64) *monitoringpb.TypedValue {
	return &monitoringpb.TypedValue{Value: &monitoringpb.TypedValue_Int64Value{Int64Value: v}}
}

// served is a Monitoring source on a stand-in for the metric service.
func served(t *testing.T, m *metrics, subs ...string) Monitoring {
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
	return Monitoring{Client: client, Project: "proj", Instance: "trusted-router-nam6", Subscriptions: subs,
		Window: 5 * time.Minute, Clock: func() time.Time { return start }}
}

const cpuFilter = `resource.type = "spanner_instance" AND resource.labels.instance_id = "trusted-router-nam6" AND ` +
	`metric.type = "spanner.googleapis.com/instance/cpu/utilization_by_priority" AND metric.labels.priority = "high"`

func subFilter(sub, metric string) string {
	return `resource.type = "pubsub_subscription" AND resource.labels.subscription_id = "` + sub + `" AND ` +
		`metric.type = "pubsub.googleapis.com/subscription/` + metric + `"`
}

// TestMonitoringReadsAsTheAlarmDoes: Spanner's CPU is the high-priority
// series production's alarm reads, aligned and reduced to their highest
// over the window, in the project; its highest point is the reading.
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
		a.GetCrossSeriesReducer() != monitoringpb.Aggregation_REDUCE_MAX || a.GetAlignmentPeriod().AsDuration() != 5*time.Minute:
		t.Errorf("the aggregation: %v", a)
	case !req.GetInterval().GetEndTime().AsTime().Equal(start) ||
		!req.GetInterval().GetStartTime().AsTime().Equal(start.Add(-5*time.Minute)):
		t.Errorf("the interval: %v", req.GetInterval())
	}
}

// TestMonitoringReadsEachBacklog: each subscription's undelivered messages
// and oldest message's age, at their highest; a subscription with no
// series, or a metric with none, fails the read, as does Spanner's CPU
// with none.
func TestMonitoringReadsEachBacklog(t *testing.T) {
	m := &metrics{points: map[string][]*monitoringpb.TypedValue{
		subFilter("auditor", "num_undelivered_messages"):   {integer(3), integer(7)},
		subFilter("auditor", "oldest_unacked_message_age"): {integer(12)},
		subFilter("stager", "num_undelivered_messages"):    {integer(0)},
		subFilter("stager", "oldest_unacked_message_age"):  {integer(0)},
	}}
	got, err := served(t, m, "auditor", "stager").Backlogs(context.Background())
	if err != nil || got["auditor"] != (Backlog{7, 12 * time.Second}) || got["stager"] != (Backlog{}) || len(got) != 2 {
		t.Fatalf("the backlogs: %v %v", got, err)
	}
	if _, err := served(t, m, "auditor", "archive").Backlogs(context.Background()); err == nil ||
		!strings.Contains(err.Error(), "archive") {
		t.Fatalf("a subscription with no series: %v", err)
	}
	delete(m.points, subFilter("stager", "oldest_unacked_message_age"))
	if _, err := served(t, m, "stager").Backlogs(context.Background()); err == nil {
		t.Fatal("a subscription with no age series is read")
	}
	if _, err := served(t, &metrics{}).SpannerCPU(context.Background()); err == nil {
		t.Fatal("Spanner's CPU with no series is read")
	}
}
