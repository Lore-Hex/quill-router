package routingsnapshot

import (
	"encoding/json"
	"go/parser"
	"go/token"
	"io/fs"
	"math"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

func peak() time.Time { return time.Date(2026, 10, 19, 2, 0, 0, 0, time.UTC) }

func ptr[T any](v T) *T { return &v }

func baseRequest() Request {
	return Request{
		Model: "deepseek/deepseek-v4-flash", RouteType: ptr("chat.completions"),
		EstimatedInputTokens: ptr(int64(1000)), At: peak(),
	}
}

// TestThePassRules: each rule that sends a request to Python, beside a
// positive control that differs only in the fact the rule is about and is
// answered or refused by Go.
func TestThePassRules(t *testing.T) {
	s := goldenSnapshot(t)
	type rule struct {
		name     string
		reason   string // a prefix of the pass reason
		outside  func(*Request)
		inside   func(*Request)
		decision Outcome
	}
	rules := []rule{
		{"max_tokens below 1", "a max_* token count below 1",
			func(r *Request) { r.MaxTokens = ptr(int64(0)) }, func(r *Request) { r.MaxTokens = ptr(int64(1)) }, Answered},
		{"max_output_tokens below 1", "a max_* token count below 1",
			func(r *Request) { r.MaxOutputTokens = ptr(int64(-1)) }, func(r *Request) { r.MaxOutputTokens = ptr(int64(1)) }, Answered},
		{"negative prompt estimate", "negative estimated_input_tokens",
			func(r *Request) { r.EstimatedInputTokens = ptr(int64(-1)) }, func(r *Request) { r.EstimatedInputTokens = ptr(int64(0)) }, Answered},
		{"window sum beyond int64", "prompt and completion tokens beyond int64",
			func(r *Request) { r.EstimatedInputTokens, r.MaxTokens = ptr(int64(math.MaxInt64)), ptr(int64(1)) },
			func(r *Request) { r.EstimatedInputTokens, r.MaxTokens = ptr(int64(math.MaxInt64-1)), ptr(int64(1)) }, Refused},
		{"after the window", "the instant is outside",
			func(r *Request) { r.At = time.Unix(s.NotAfter, 0) }, func(r *Request) { r.At = time.Unix(s.NotAfter-1, 999_999_999) }, Answered},
		{"before the window", "the instant is outside",
			func(r *Request) { r.At = time.Unix(s.CompiledAt-1, 999_999_999) }, func(r *Request) { r.At = time.Unix(s.CompiledAt, 0) }, Answered},
		{"meta model", "requested model \"trustedrouter/auto\"",
			func(r *Request) { r.Model = "trustedrouter/auto" }, func(r *Request) {}, Answered},
		{"meta model in models", "requested model \"trustedrouter/zdr\"",
			func(r *Request) { r.Models = []string{"trustedrouter/zdr"} }, func(r *Request) { r.Models = []string{"z-ai/glm-5.3"} }, Answered},
		{"meta model in an ignored models list", "",
			func(r *Request) {}, func(r *Request) {
				r.Models = []string{"trustedrouter/zdr"}
				r.Provider = json.RawMessage(`{"allow_fallbacks":false}`)
			}, Answered},
		{"router rewrite to a meta model", "requested model \"trustedrouter/auto\"",
			func(r *Request) { r.Model = "nyte/auto-routing:nitro" }, func(r *Request) { r.Model = "deepseek/deepseek-v4-flash:nitro" }, Answered},
		{"decision chain", "decision chain \"trustedrouter/dev-1.0\"",
			func(r *Request) { r.Model = "trustedrouter/dev-1.0" }, func(r *Request) {}, Answered},
		{"alias target", "model \"trustedrouter/confidential\" resolves",
			func(r *Request) { r.Model = "trustedrouter/confidential" }, func(r *Request) {}, Answered},
		{"decisionmodelsio rewrite", "model \"system1models/x\" needs a rewrite",
			func(r *Request) { r.Model = "system1models/x" }, func(r *Request) { r.Model = "system1/x" }, Refused},
		{"router namespace", "router model \"trustedrouter/unknown\"",
			func(r *Request) { r.Model = "trustedrouter/unknown" }, func(r *Request) { r.Model = "nope/unknown" }, Refused},
		{"non-ASCII model", "a string field that is not printable ASCII",
			func(r *Request) { r.Model += " " }, func(r *Request) { r.Model += " " }, Answered},
		{"non-ASCII region", "a string field that is not printable ASCII",
			func(r *Request) { r.Region = ptr("us-central1\u00a0") }, func(r *Request) { r.Region = ptr(" us-central1 ") }, Answered},
		{"blank service tier", "a string field that is not printable ASCII, or one the authorize schema refuses",
			func(r *Request) { r.ServiceTier = ptr("   ") }, func(r *Request) { r.ServiceTier = ptr(" default ") }, Refused},
		{"long service tier", "a string field that is not printable ASCII, or one the authorize schema refuses",
			func(r *Request) { r.ServiceTier = ptr("default-default-default") }, func(r *Request) { r.ServiceTier = ptr("default-default-defa") }, Refused},
		{"blank model", "a string field that is not printable ASCII, or one the authorize schema refuses",
			func(r *Request) { r.Model = "  " }, func(r *Request) { r.Model = " nope/x " }, Refused},
		{"route type", "route type \"videos\"",
			func(r *Request) { r.RouteType = ptr("videos") }, func(r *Request) { r.RouteType = ptr("responses") }, Answered},
		{"capability parameters", "requested_parameters",
			func(r *Request) { r.RequestedParameters = []string{"tools"} }, func(r *Request) { r.RequestedParameters = []string{" "} }, Answered},
		{"parameters on a route they do not describe", "",
			func(r *Request) {}, func(r *Request) {
				r.RouteType = ptr("messages")
				r.RequestedParameters = []string{"tools"}
			}, Answered},
		{"unknown provider field", "provider.foo",
			func(r *Request) { r.Provider = json.RawMessage(`{"foo":1}`) }, func(r *Request) { r.Provider = json.RawMessage(`{}`) }, Answered},
		{"null provider options", "",
			func(r *Request) {}, func(r *Request) {
				r.Provider = json.RawMessage(`{"max_price":null,"min_privacy":null,"zdr":null,"require_parameters":false}`)
			}, Answered},
		{"sort spelling", "provider.sort spelling",
			func(r *Request) { r.Provider = json.RawMessage(`{"sort":"fastest"}`) }, func(r *Request) { r.Provider = json.RawMessage(`{"sort":" Latency "}`) }, Answered},
		{"sort partition", "provider.sort spelling",
			func(r *Request) { r.Provider = json.RawMessage(`{"sort":{"by":"price","partition":""}}`) },
			func(r *Request) { r.Provider = json.RawMessage(`{"sort":{"by":"price","partition":"none"}}`) }, Answered},
		{"usage spelling", "provider.usage spelling",
			func(r *Request) { r.Provider = json.RawMessage(`{"usage":"prepaid"}`) }, func(r *Request) { r.Provider = json.RawMessage(`{"usage":"CREDITS"}`) }, Answered},
		{"priority prompt bound", "OpenAI priority prompt bound",
			// Gemini's window is unknown (0), so the context filter keeps it.
			func(r *Request) {
				r.Model, r.ServiceTier, r.EstimatedInputTokens = "google/gemini-3.1-pro-preview", ptr("priority"), ptr(int64(272_001))
			},
			func(r *Request) {
				r.Model, r.ServiceTier, r.EstimatedInputTokens = "google/gemini-3.1-pro-preview", ptr("priority"), ptr(int64(272_000))
			}, Refused},
		{"estimate beyond int64", "the estimate exceeds int64",
			func(r *Request) {
				r.Model, r.EstimatedInputTokens = "google/gemini-3.1-pro-preview", ptr(int64(math.MaxInt64))
			},
			func(r *Request) {
				r.Model, r.EstimatedInputTokens = "google/gemini-3.1-pro-preview", ptr(int64(2_000_000_000))
			}, Answered},
	}
	for _, r := range rules {
		outside, inside := baseRequest(), baseRequest()
		r.outside(&outside)
		r.inside(&inside)
		if r.reason != "" {
			if got := s.Evaluate(outside); got.Outcome != PassToPython || !strings.HasPrefix(got.Reason, r.reason) {
				t.Errorf("%s: want a pass to Python (%s), got %+v", r.name, r.reason, got)
			}
		}
		if got := s.Evaluate(inside); got.Outcome != r.decision {
			t.Errorf("%s: the positive control is not decided by Go: %+v %+v", r.name, got, got.Refusal)
		}
	}
}

// TestNothingAtRuntimeImportsTheEvaluator: the package stays dormant until
// the edge's handler PR wires it.
func TestNothingAtRuntimeImportsTheEvaluator(t *testing.T) {
	const self = "github.com/Lore-Hex/quill-router/fastpath/internal/routingsnapshot"
	root := filepath.Join("..", "..")
	files, internalImports := 0, 0
	err := filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() || !strings.HasSuffix(path, ".go") {
			return err
		}
		parsed, err := parser.ParseFile(token.NewFileSet(), path, nil, parser.ImportsOnly)
		if err != nil {
			return err
		}
		files++
		for _, spec := range parsed.Imports {
			imported, _ := strconv.Unquote(spec.Path.Value)
			if strings.HasPrefix(imported, "github.com/Lore-Hex/quill-router/fastpath/internal/") {
				internalImports++
			}
			if imported == self {
				t.Errorf("%s imports the routing snapshot evaluator", path)
			}
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	// Positive control: the walk sees the module's code and its internal imports.
	if files < 50 || internalImports < 10 {
		t.Fatalf("scanned %d files with %d internal imports", files, internalImports)
	}
}

// TestTheSchemaStripsStringFields pins the authorize schema's
// str_strip_whitespace where the routing cases cannot show it: the region
// decides Bedrock's reachability, and a stripped route type and model must
// answer as the unspaced ones do.
func TestTheSchemaStripsStringFields(t *testing.T) {
	s := goldenSnapshot(t)
	spaced := baseRequest()
	spaced.Model, spaced.Region, spaced.RouteType = "  minimax/minimax-m2 ", ptr(" us-east4 "), ptr(" chat.completions ")
	plain := baseRequest()
	plain.Model, plain.Region = "minimax/minimax-m2", ptr("us-east4")
	got, want := s.Evaluate(spaced), s.Evaluate(plain)
	if got.Outcome != Answered || want.Outcome != Answered || len(got.Candidates) != 1 ||
		got.Candidates[0] != want.Candidates[0] || got.ModelEstimate != 965 {
		t.Fatalf("spaced %+v, plain %+v", got, want)
	}
	// Positive control: an unconfigured region is not stripped into one.
	other := plain
	other.Region = ptr("us-east4x")
	if r := s.Evaluate(other); r.Outcome != Refused {
		t.Fatalf("an unlisted region reached Bedrock: %+v", r)
	}
}

// TestAnOmittedEstimateIsTheSchemaDefault: the authorize schema defaults
// estimated_input_tokens to 1, so a body that omits it needs room for one
// more token than one that sends 0. Flash's window is 65,536: max_tokens of
// 65,536 alone does not fit (1 + 65,536), while an explicit 0 does.
func TestAnOmittedEstimateIsTheSchemaDefault(t *testing.T) {
	s := goldenSnapshot(t)
	omitted := baseRequest()
	omitted.EstimatedInputTokens, omitted.MaxTokens = nil, ptr(int64(65_536))
	if r := s.Evaluate(omitted); r.Outcome != Refused || r.Refusal == nil || r.Refusal.Type != "context_length_exceeded" {
		t.Fatalf("omitted estimate: got %+v, want context_length_exceeded", r)
	}
	zero := omitted
	zero.EstimatedInputTokens = ptr(int64(0))
	if r := s.Evaluate(zero); r.Outcome != Answered {
		t.Fatalf("explicit zero estimate: got %+v, want answered", r)
	}
}
