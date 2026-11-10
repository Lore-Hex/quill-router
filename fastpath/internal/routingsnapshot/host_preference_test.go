package routingsnapshot

import "testing"

func hostPreferenceSnapshot(lab, host string) *Snapshot {
	model := lab + "/unit"
	return &Snapshot{
		Endpoints: []Endpoint{
			{ModelID: model, Provider: lab, UsageType: usageCredits, Privacy: Privacy{Meets: []int64{2}}, Epochs: []Epoch{{Headline: Headline{Prompt: 100, Completion: 100}}}},
			{ModelID: model, Provider: host, UsageType: usageCredits, Privacy: Privacy{Meets: []int64{2}}, Epochs: []Epoch{{Headline: Headline{Prompt: 101, Completion: 100}}}},
		},
		Ranking: Ranking{
			MeasuredFromUS: 100, MeasuredThroughUS: 200, UnknownRank: 1000,
			Default: map[string]int64{lab: 0, host: 0},
			Latency: map[string]int64{lab: 0, host: 0}, Throughput: map[string]int64{lab: 0, host: 0},
			ModelWorkload: ModelWorkloadRanking{Version: 1, MeasuredFromUS: 100, MeasuredThroughUS: 200,
				Workloads: map[string]map[string]map[string]int64{
					model + "|0|0": {"default": {lab: 0, host: 0}, "latency": {lab: 0, host: 1}, "throughput": {lab: 0, host: 1}},
				},
			},
		},
	}
}

func TestIndependentZDRHostPreference(t *testing.T) {
	for _, lab := range []string{"anthropic", "openai"} {
		for _, host := range []string{"amazon-bedrock", "google-vertex"} {
			for _, tc := range []struct {
				name   string
				prefs  prefs
				mutate func(*Snapshot)
				want   int
			}{
				{name: "comparable ZDR", want: 1},
				{name: "host lacks ZDR", mutate: func(s *Snapshot) { s.Endpoints[1].Privacy.Meets = nil }},
				{name: "unhealthy host", mutate: func(s *Snapshot) { s.Ranking.Default[host] = 2000 }},
				{name: "unknown host", mutate: func(s *Snapshot) { s.Ranking.Default[host] = 1000 }},
				{name: "slower workload", mutate: func(s *Snapshot) { s.Ranking.ModelWorkload.Workloads[lab+"/unit|0|0"]["default"][host] = 1 }},
				{name: "explicit order", prefs: prefs{order: []string{lab}}},
				{name: "price", prefs: prefs{sort: "price"}},
				{name: "latency", prefs: prefs{sort: "latency"}},
				{name: "throughput", prefs: prefs{sort: "throughput"}},
				{name: "another model", mutate: func(s *Snapshot) { s.Endpoints[1].ModelID = lab + "/another" }},
				{name: "no direct lab route", mutate: func(s *Snapshot) {
					s.Endpoints[0].Provider = "tinfoil"
					s.Ranking.Default["tinfoil"] = 0
					s.Ranking.ModelWorkload.Workloads[lab+"/unit|0|0"]["default"]["tinfoil"] = 0
				}},
			} {
				t.Run(lab+"/"+host+"/"+tc.name, func(t *testing.T) {
					s := hostPreferenceSnapshot(lab, host)
					if tc.mutate != nil {
						tc.mutate(s)
					}
					got, ok := s.sortCandidates([]int{0, 1}, tc.prefs, 150, 150, 100, 100)
					if !ok || got[0] != tc.want {
						t.Fatalf("got %v, ok=%v, wanted first %d", got, ok, tc.want)
					}
				})
			}
		}
	}
}
