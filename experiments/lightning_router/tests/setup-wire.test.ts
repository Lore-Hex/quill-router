import { test, expect } from "bun:test";
import { execFileSync } from "node:child_process";
import { parse } from "yaml";
import { buildModel } from "@oh-my-pi/pi-catalog/build";
import { applyChatCompletionsReasoningParams } from "@oh-my-pi/pi-ai/providers/openai-shared";
import { setupFor } from "../setup.mjs";

const profiles = JSON.parse(execFileSync(".venv/bin/python", ["-c",
  "import json; from lightning_router.reasoning import PROFILES; print(json.dumps(PROFILES))"], { encoding: "utf8" }));

for (const [id, reasoning] of Object.entries(profiles) as [string, any][]) {
  if (!reasoning.setup_default) continue;
  for (const effort of reasoning.setup_efforts) {
    test(`OMP sends ${id} effort=${effort}`, () => {
      const config = parse(setupFor("omp", {
        id, name: id, context: 131072, output: 16384, reasoning,
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
