import { test, expect } from "bun:test";
import { execFileSync } from "node:child_process";
import { parse } from "yaml";
import { buildModel } from "@oh-my-pi/pi-catalog/build";
import { applyChatCompletionsReasoningParams } from "@oh-my-pi/pi-ai/providers/openai-shared";
import { streamOpenAICompletions } from "@oh-my-pi/pi-ai/providers/openai-completions";
import { setupFor } from "../setup.mjs";

const profiles = JSON.parse(execFileSync(".venv/bin/python", ["-c",
  "import json; from lightning_router.reasoning import PROFILES; print(json.dumps(PROFILES))"], { encoding: "utf8" }));

test("OMP sends the privacy constraint and order in the actual HTTP request", async () => {
  const config = parse(setupFor("omp", {id: "test/model", name: "Test", privacy: {confidential: ["tee"]}},
    "https://api.trustedrouter.com/v1", ["tee"]).config).providers.lightningrouter;
  const model = buildModel({...config.models[0], provider: "lightningrouter", api: config.api,
    baseUrl: config.baseUrl, contextWindow: 8192, maxTokens: 16, input: ["text"],
    cost: {input: 0, output: 0, cacheRead: 0, cacheWrite: 0}});
  const original = globalThis.fetch;
  const requests: any[] = [];
  globalThis.fetch = (async (url: any, init: any) => {
    expect(String(url)).toBe("https://api.trustedrouter.com/v1/chat/completions");
    requests.push(JSON.parse(init.body));
    return new Response('data: {"choices":[{"index":0,"delta":{"content":"Hi"},"finish_reason":null}]}\n\n' +
      'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n',
      {headers: {"Content-Type": "text/event-stream"}});
  }) as typeof fetch;
  try {
    const result = await streamOpenAICompletions(model as any, {
      messages: [{role: "user", content: "Hi", timestamp: 1}],
    }, {apiKey: "local-test", maxTokens: 16}).result();
    expect(result.stopReason).toBe("stop");
    expect(requests).toHaveLength(1);
    expect(requests[0].provider).toEqual({min_privacy: "confidential", order: ["tee"]});
    expect(requests[0].extraBody).toBeUndefined();
  } finally { globalThis.fetch = original; }
});

for (const [id, reasoning] of Object.entries(profiles) as [string, any][]) {
  if (!reasoning.setup_default) continue;
  for (const effort of reasoning.setup_efforts) {
    test(`OMP sends ${id} effort=${effort}`, () => {
      const config = parse(setupFor("omp", {
        id, name: id, context: 131072, output: 16384, reasoning, privacy: {confidential: ["tinfoil"]},
      }, "https://api.lightningrouter.ai/v1").config);
      const provider = config.providers.lightningrouter;
      const model = buildModel({
        ...provider.models[0], provider: "lightningrouter", api: provider.api,
        baseUrl: provider.baseUrl, input: ["text"],
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
      });
      const params: any = {};
      applyChatCompletionsReasoningParams(params, model, model.compat,
        effort === "none" ? { disableReasoning: true } : { reasoning: effort });
      expect(params.reasoning_effort).toBe(effort);
      expect(params.reasoning).toBeUndefined();
      expect(model.baseUrl).toBe("https://api.lightningrouter.ai/v1");
    });
  }
}
