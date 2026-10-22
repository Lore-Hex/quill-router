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
		EstimatedInputTokens: 1000, OutputTokens: 512, At: peak(),
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
		{"context window", "context-window fitting",
			func(r *Request) { r.ContextPromptTokens = 1000 }, func(r *Request) {}, Answered},
		{"context window (max_tokens)", "context-window fitting",
			func(r *Request) { r.ContextCompletionTokens = 1 }, func(r *Request) {}, Answered},
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
		{"non-ASCII model", "a model string that is not printable ASCII",
			func(r *Request) { r.Model += " " }, func(r *Request) { r.Model += " " }, Answered},
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
			func(r *Request) { r.ServiceTier, r.EstimatedInputTokens = ptr("priority"), 272_001 },
			func(r *Request) { r.ServiceTier, r.EstimatedInputTokens = ptr("priority"), 272_000 }, Refused},
		{"estimate beyond int64", "the estimate exceeds int64",
			func(r *Request) { r.Model, r.EstimatedInputTokens = "google/gemini-3.1-pro-preview", math.MaxInt64 },
			func(r *Request) { r.Model, r.EstimatedInputTokens = "google/gemini-3.1-pro-preview", 2_000_000_000 }, Answered},
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
		if filepath.Dir(path) == "." {
			return nil
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
