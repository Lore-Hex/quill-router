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
		{"non-ASCII requested parameter", "a string field that is not printable ASCII",
			func(r *Request) { r.RequestedParameters = []string{"tools\t"} }, func(r *Request) { r.RequestedParameters = []string{" tools "} }, Answered},
		{"non-ASCII min_privacy", "provider.min_privacy is not printable ASCII",
			func(r *Request) { r.Provider = json.RawMessage(`{"min_privacy":"zdr\u00a0"}`) },
			func(r *Request) { r.Provider = json.RawMessage(`{"min_privacy":" ZDR "}`) }, Refused},
		{"unknown provider field", "provider.foo",
			func(r *Request) { r.Provider = json.RawMessage(`{"foo":1}`) }, func(r *Request) { r.Provider = json.RawMessage(`{}`) }, Answered},
		{"null provider options", "",
			func(r *Request) {}, func(r *Request) {
				r.Provider = json.RawMessage(`{"max_price":null,"min_privacy":null,"zdr":null,"require_parameters":false,"preferred_max_latency":null}`)
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

// TestOnlyTheEdgeImportsTheEvaluator: the edge's poll and shadow comparator
// are the one runtime reader, and nothing decides with the evaluator until
// the edge's handler PR wires it. gwauth, the edge's authorize request
// parse, builds the evaluator's Request (decision 16 of
// docs/design/fast-admission-e2-decide.md) and may import it too.
func TestOnlyTheEdgeImportsTheEvaluator(t *testing.T) {
	const self = "github.com/Lore-Hex/quill-router/fastpath/internal/routingsnapshot"
	root := filepath.Join("..", "..")
	edgeDir := filepath.Join(root, "internal", "edge")
	gwauthDir := filepath.Join(root, "internal", "gwauth")
	files, internalImports, edgeImports, gwauthImports := 0, 0, 0, 0
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
				switch filepath.Dir(path) {
				case edgeDir:
					edgeImports++
					continue
				case gwauthDir:
					gwauthImports++
					continue
				}
				t.Errorf("%s imports the routing snapshot evaluator", path)
			}
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	// Positive control: the walk sees the module's code and its internal
	// imports, and each allowed importer's import of the evaluator.
	if files < 50 || internalImports < 10 || edgeImports == 0 || gwauthImports == 0 {
		t.Fatalf("scanned %d files with %d internal imports, %d of the evaluator by the edge, %d by gwauth", files,
			internalImports, edgeImports, gwauthImports)
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

// TestTheReservationPricesAtMostHoldOutputTokens: the reservation prices
// at most HoldOutputTokens output tokens, as
// authorize_candidates.hold_output_tokens does in Python, while the
// estimate prices the whole budget. Gemini's window is unknown, so a
// max_tokens of 131,072 (which counts toward the window) fits.
func TestTheReservationPricesAtMostHoldOutputTokens(t *testing.T) {
	s := goldenSnapshot(t)
	figures := func(maxTokens int64) (estimate, reservation int64) {
		t.Helper()
		r := baseRequest()
		r.Model, r.MaxTokens = "google/gemini-3.1-pro-preview", ptr(maxTokens)
		got := s.Evaluate(r)
		if got.Outcome != Answered {
			t.Fatalf("max_tokens %d: %+v %+v", maxTokens, got, got.Refusal)
		}
		return got.ModelEstimate, got.Reservation
	}
	atBound, bound := figures(HoldOutputTokens)
	if atBound != bound {
		t.Errorf("at the bound the estimate %d and the reservation %d differ", atBound, bound)
	}
	for _, budget := range []int64{HoldOutputTokens + 1, 131_072} {
		if estimate, reservation := figures(budget); reservation != bound || estimate <= atBound {
			t.Errorf("max_tokens %d: reservation %d, want the bound's %d; estimate %d, want above %d",
				budget, reservation, bound, estimate, atBound)
		}
	}
	if estimate, reservation := figures(HoldOutputTokens - 1); reservation >= bound || estimate != reservation {
		t.Errorf("max_tokens %d: reservation %d, want below %d; estimate %d, want the reservation",
			HoldOutputTokens-1, reservation, bound, estimate)
	}
	// A stream's reservation is its estimate for now (the Stage D heartbeat
	// reader deployed before this bounds a stream by the credit hold).
	r := baseRequest()
	r.Model, r.MaxTokens, r.Stream = "google/gemini-3.1-pro-preview", ptr(int64(131_072)), ptr(true)
	if got := s.Evaluate(r); got.Outcome != Answered || got.Reservation != got.ModelEstimate || got.Reservation <= bound {
		t.Errorf("a stream with max_tokens 131,072: reservation %d, want the estimate %d (above %d): %+v",
			got.Reservation, got.ModelEstimate, bound, got.Refusal)
	}
}

// TestTheParameterTablesAreData: the capability rules are read from the
// snapshot's tables, not copied into Go. With structured output held to its
// route declarations too, a request for tools and response_format pinned to
// Together (which declares response_format only) and Fireworks (tools only)
// has a route for each parameter but none for both: Python's "together"
// refusal. The expected answers are Python's, from routing_cases_vectors.py's
// evaluate with routing._ROUTE_DECLARED_CAPABILITIES patched the same way;
// with the golden tables the same request keeps Fireworks.
func TestTheParameterTablesAreData(t *testing.T) {
	request := func() Request {
		r := baseRequest()
		r.Model = "z-ai/glm-5.3"
		r.RequestedParameters = []string{"tools", "response_format"}
		r.Provider = json.RawMessage(`{"only":["together","fireworks"]}`)
		return r
	}
	s := goldenSnapshot(t)
	if got := s.Evaluate(request()); got.Outcome != Answered ||
		strings.Join(got.Candidates, ",") != "z-ai/glm-5.3@fireworks/prepaid" {
		t.Fatalf("golden tables: %+v %+v", got, got.Refusal)
	}
	s.Parameters.RouteDeclared = [][]string{{"response_format", "structured_outputs"}, {"tools"}}
	want := Refusal{Status: 400, Type: "model_not_supported", Param: "response_format",
		Message: "No route for the requested model supports these parameters together: response_format, tools"}
	if got := s.Evaluate(request()); got.Outcome != Refused || *got.Refusal != want {
		t.Fatalf("structured output route-declared: %+v %+v", got, got.Refusal)
	}
	only := request()
	only.RequestedParameters = []string{"response_format"}
	if got := s.Evaluate(only); got.Outcome != Answered ||
		strings.Join(got.Candidates, ",") != "z-ai/glm-5.3@together/prepaid" || got.ModelEstimate != 2640 {
		t.Fatalf("response_format alone: %+v %+v", got, got.Refusal)
	}
}

// TestANonStringMinPrivacyIsRefusedOnlyWhenNoSpellingCouldBeIt: Python looks
// up str(value).strip().lower(), so a number or boolean is the 400 while no
// accepted spelling is a str Python could make of one; a table with such a
// spelling sends it to Python.
func TestANonStringMinPrivacyIsRefusedOnlyWhenNoSpellingCouldBeIt(t *testing.T) {
	s := goldenSnapshot(t)
	for _, value := range []string{`2`, `true`, `[]`, `{}`, `-1`, `1.5`} {
		r := baseRequest()
		r.Provider = json.RawMessage(`{"min_privacy":` + value + `}`)
		if got := s.Evaluate(r); got.Outcome != Refused || got.Refusal.Message != minPrivacyMessage {
			t.Errorf("%s: %+v", value, got)
		}
	}
	for _, spelling := range []string{"true", "false", "2", "-1", "1.5", "inf", "nan"} {
		if !spelledByNonString(map[string]int64{"zdr": 2, spelling: 2}) {
			t.Errorf("%q could be a non-string's str", spelling)
		}
	}
	if spelledByNonString(s.Aliases.PrivacyTiers) {
		t.Fatal("the golden spellings include one a non-string could spell")
	}
	s.Aliases.PrivacyTiers = map[string]int64{"zdr": 2, "true": 2}
	r := baseRequest()
	r.Provider = json.RawMessage(`{"min_privacy":true}`)
	if got := s.Evaluate(r); got.Outcome != PassToPython || got.Reason != "provider.min_privacy is not a string" {
		t.Errorf("a table spelling \"true\": %+v", got)
	}
}
