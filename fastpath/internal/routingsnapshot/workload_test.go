package routingsnapshot

import "testing"

func TestWorkloadRankingRejectsMalformedEvidence(t *testing.T) {
	for name, mutate := range map[string]func(*ModelWorkloadRanking){
		"version": func(r *ModelWorkloadRanking) { r.Version = 2 },
		"window":  func(r *ModelWorkloadRanking) { r.MeasuredThroughUS = r.MeasuredFromUS - 1 },
		"sort": func(r *ModelWorkloadRanking) {
			r.Workloads["unit/model|0|0"] = map[string]map[string]int64{"unknown": {}}
		},
		"negative rank": func(r *ModelWorkloadRanking) {
			r.Workloads["unit/model|0|0"] = map[string]map[string]int64{
				"default": {"provider": -1}, "latency": {}, "throughput": {},
			}
		},
		"excessive rank": func(r *ModelWorkloadRanking) {
			r.Workloads["unit/model|0|0"] = map[string]map[string]int64{
				"default": {"provider": 20001}, "latency": {}, "throughput": {},
			}
		},
	} {
		t.Run(name, func(t *testing.T) {
			s := goldenSnapshot(t)
			mutate(&s.Ranking.ModelWorkload)
			if err := s.validate(); err == nil {
				t.Fatal("accepted malformed model/workload evidence")
			}
		})
	}
}

func TestWorkloadRankingUsesExactModelSizeAndWindow(t *testing.T) {
	s := &Snapshot{Ranking: Ranking{ModelWorkload: ModelWorkloadRanking{
		Version: 1, MeasuredFromUS: 100, MeasuredThroughUS: 200,
		Workloads: map[string]map[string]map[string]int64{
			"unit/model|0|0": {"default": {"short": 0}},
			"unit/model|1|1": {"default": {"medium": 0}},
			"unit/model|2|2": {"default": {"large": 0}},
			"unit/model|3|3": {"default": {"largest": 0}},
		},
	}}}
	for _, tc := range []struct {
		input, output int64
		provider      string
	}{{4096, 256, "short"}, {32768, 2048, "medium"}, {131072, 8192, "large"}, {131073, 8193, "largest"}} {
		for _, at := range []int64{100, 150, 200} {
			if _, ok := s.workloadRanks("unit/model", tc.input, tc.output, at)["default"][tc.provider]; !ok {
				t.Fatalf("wrong workload for %+v at %d", tc, at)
			}
		}
	}
	for _, at := range []int64{99, 201} {
		if s.workloadRanks("unit/model", 100, 100, at) != nil {
			t.Fatalf("used evidence outside its validity window at %d", at)
		}
	}
	if s.workloadRanks("another/model", 100, 100, 150) != nil {
		t.Fatal("borrowed another model's speed")
	}
}
