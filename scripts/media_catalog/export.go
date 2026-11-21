// Run through refresh.sh inside the pinned gateway module. Never submit jobs
// or use credentials: all quote calculations here must be local.
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"slices"

	"github.com/Lore-Hex/quill-cloud-proxy/enclave-go/internal/video"
)

type fields = map[string]any
type noNetwork struct{}

func (noNetwork) RoundTrip(*http.Request) (*http.Response, error) {
	panic("catalog export attempted HTTP")
}

func main() {
	const k = "offline"
	reg := video.NewRegistry(video.ProviderKeys{BytePlus: k, Venice: k, FAL: k, Google: k, MiniMax: k,
		AtlasCloud: k, XAI: k, Alibaba: k, LTX: k, Runway: k, OpenAI: k, Kling: k, Decart: k,
		VertexProject: k, VertexLocation: k}, &http.Client{Transport: noNetwork{}})
	rows := map[string]any{}
	var legacy struct{ Data []fields }
	body, err := video.ModelsJSON(reg)
	if err != nil {
		panic(err)
	}
	if err := json.Unmarshal(body, &legacy); err != nil {
		panic(err)
	}
	descriptions := map[string]fields{}
	for _, row := range legacy.Data {
		descriptions[row["id"].(string)] = fields{"architecture": row["architecture"], "description": row["description"], "trustedrouter": row["trustedrouter"]}
	}
	for _, m := range video.Models() {
		d := fields{"type": "integer", "unit": "second"}
		if len(m.AllowedDurations) > 0 {
			d["values"] = m.AllowedDurations
		} else {
			d["min"] = max(1, m.MinimumDuration)
			d["max"] = m.MaximumDuration
		}
		if m.RequiresExplicitDuration {
			d["required"] = true
		} else {
			d["default"] = m.DefaultDuration
		}
		res := fields{"type": "string", "default": m.DefaultResolution}
		if len(m.AllowedResolutions) > 0 {
			res["values"] = m.AllowedResolutions
		}
		params := fields{"prompt": fields{"type": "string", "required": true, "max_characters": m.PromptCharacterLimit},
			"duration": d, "resolution": res}
		if !m.VideoUsesSourceAspect {
			aspects := m.AllowedAspectRatios
			if len(aspects) == 0 {
				aspects = []string{"1:1", "2:3", "3:2", "3:4", "4:3", "9:16", "16:9", "21:9"}
			}
			params["aspect_ratio"] = fields{"type": "string", "values": aspects, "default": m.DefaultAspectRatio}
			params["size"] = fields{"type": "string", "format": "WIDTHxHEIGHT", "alternative_to": []string{"resolution", "aspect_ratio"}}
		}
		if m.SupportsImage {
			params["frame_images"] = fields{"type": "array", "frame_types": []string{"first_frame", "last_frame"}}
		}
		if m.SupportsReferences {
			types := []string{"image"}
			if m.SupportsAudioReference {
				types = append(types, "audio")
			}
			if m.SupportsVideoReference {
				types = append(types, "video")
			}
			p := fields{"type": "array", "reference_types": types}
			if m.RequiresVideoReference {
				p["required_reference_type"] = "video"
			}
			params["input_references"] = p
		}
		if m.SupportsAudio {
			params["generate_audio"] = fields{"type": "boolean", "default": true}
		} else if m.AudioAlwaysOn {
			params["generate_audio"] = fields{"type": "boolean", "values": []bool{true}, "default": true}
		}
		if m.ID == "google/veo-3.1" || m.ID == "google/veo-3.1-fast" {
			res["requires_duration_seconds"] = fields{"1080p": 8, "4k": 8}
		}
		prices := map[string][]fields{}
		resolutions := m.AllowedResolutions
		if len(resolutions) == 0 {
			resolutions = []string{m.DefaultResolution}
		}
		for _, resolution := range resolutions {
			audioOptions := []*bool{nil}
			if m.SupportsAudio && !m.AudioAlwaysOn {
				silent := false
				audioOptions = append(audioOptions, &silent)
			}
			for _, audio := range audioOptions {
				req := video.CreateRequest{Model: m.ID, Prompt: "Catalog", Duration: m.DefaultDuration, Resolution: resolution, GenerateAudio: audio}
				if m.RequiresVideoReference {
					req.InputReferences = []video.InputReference{{Type: "video", URL: "https://example.org/sample.mp4"}}
				}
				rr, err := video.ResolveRequest(&req)
				if err != nil && req.Duration != 8 {
					req.Duration = 8
					rr, err = video.ResolveRequest(&req)
				}
				if err != nil {
					continue
				}
				for _, p := range reg.Supporting(rr) {
					if p.ID() == "venice" {
						continue
					} // Live quote; never call from the exporter.
					if _, ok := p.(video.TokenBilledProvider); ok {
						continue
					}
					cost, err := p.QuoteResolved(context.Background(), rr)
					if err != nil || cost <= 0 {
						continue
					}
					prices[p.ID()] = append(prices[p.ID()], fields{"resolution": rr.Resolution,
						"duration": rr.DurationSeconds, "aspect_ratio": rr.AspectRatio, "input_mode": rr.InputMode,
						"generate_audio": rr.GenerateAudio, "cost_usd": fmt.Sprintf("%d.%06d", cost/1000000, cost%1000000)})
				}
			}
		}
		seedProviders := []string{}
		z := int64(0)
		req := video.CreateRequest{Model: m.ID, Prompt: "Catalog", Duration: m.DefaultDuration, Seed: &z}
		if m.RequiresVideoReference {
			req.InputReferences = []video.InputReference{{Type: "video", URL: "https://example.org/sample.mp4"}}
		}
		if rr, err := video.ResolveRequest(&req); err == nil {
			for _, p := range reg.Supporting(rr) {
				seedProviders = append(seedProviders, p.ID())
			}
		}
		slices.Sort(seedProviders)
		rows[m.ID] = fields{"parameters": params, "seed_providers": seedProviders, "examples": prices, "legacy": descriptions[m.ID]}
	}
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	if err := enc.Encode(fields{"gateway_commit": os.Args[1], "models": rows}); err != nil {
		panic(err)
	}
}
