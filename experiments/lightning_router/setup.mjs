export function centsFromText(text) {
  if (!/^\d{1,4}(\.\d{1,2})?$/.test(text.trim())) throw new Error("Enter a USD amount with up to two decimal places.");
  const [whole, fraction = ""] = text.trim().split(".");
  const cents = Number(whole) * 100 + Number(fraction.padEnd(2, "0"));
  if (cents < 1 || cents > 100000) throw new Error("Enter an amount between $0.01 and $1,000.");
  return cents;
}

const EFFORTS = ["low", "medium", "high"];

export function setupFor(agent, model, apiBase, effort = "default") {
  if (!/^[A-Za-z0-9_./:-]{1,180}$/.test(model.id)) throw new Error("Invalid model ID");
  if (!["https://api.lightningrouter.ai/v1", "https://api.trustedrouter.com/v1"].includes(apiBase)) throw new Error("Unexpected API endpoint");
  const explicit = effort !== "default";
  if (explicit && (!EFFORTS.includes(effort) || model.reasoning_effort !== true)) throw new Error("Unsupported reasoning effort");
  const context = Number.isSafeInteger(model.context) && model.context > 0 ? model.context : null;
  const output = Number.isSafeInteger(model.output) && model.output > 0 ? Math.min(model.output, context || model.output) : null;
  const defaultOutput = Number.isSafeInteger(model.default_output) && model.default_output > 0
    ? Math.min(model.default_output, output || model.default_output) : null;
  const ref = `lightningrouter/${model.id}`;
  if (agent === "opencode") return {
    path: "~/.config/opencode/opencode.json",
    config: JSON.stringify({
      $schema: "https://opencode.ai/config.json", model: ref,
      provider: { lightningrouter: {
        npm: "@ai-sdk/openai-compatible", name: "LightningRouter",
        options: { baseURL: apiBase, apiKey: "{env:LIGHTNINGROUTER_API_KEY}" },
        models: { [model.id]: { name: model.name, ...(context && output ? { limit: { context, output } } : {}),
          ...(explicit ? { reasoning: true, options: { reasoningEffort: effort } } : {}),
        } },
      } },
    }, null, 2),
    command: "opencode", docs: "https://opencode.ai/docs/providers/#custom-provider",
  };
  if (agent === "crush") return {
    path: "~/.config/crush/crush.json",
    config: JSON.stringify({
      $schema: "https://charm.land/crush.json",
      providers: { lightningrouter: {
        name: "LightningRouter", type: "openai-compat", base_url: apiBase,
        api_key: "$LIGHTNINGROUTER_API_KEY",
        models: [{ id: model.id, name: model.name, ...(context ? { context_window: context } : {}),
          ...(defaultOutput || output ? { default_max_tokens: defaultOutput || output } : {}),
          ...(explicit ? { can_reason: true, reasoning_levels: EFFORTS } : {}),
        }],
      } },
      models: Object.fromEntries(["large", "small"].map((role) => [role, {
        provider: "lightningrouter", model: model.id, ...(explicit ? { reasoning_effort: effort } : {}),
      }])),
    }, null, 2),
    command: "crush", docs: "https://github.com/charmbracelet/crush#custom-providers",
  };
  if (agent === "omp") return {
    path: "~/.omp/agent/models.yml",
    config: `providers:\n  lightningrouter:\n    baseUrl: ${apiBase}\n    api: openai-completions\n    apiKey: LIGHTNINGROUTER_API_KEY\n    authHeader: true\n    models:\n      - id: ${JSON.stringify(model.id)}\n        name: ${JSON.stringify(model.name)}` +
      (context ? `\n        contextWindow: ${context}` : "") + (output ? `\n        maxTokens: ${output}` : "") +
      (explicit ? `\n        reasoning: true\n        thinking:\n          mode: effort\n          efforts: [low, medium, high]\n          defaultLevel: ${effort}\n        compat:\n          supportsReasoningEffort: true\n          thinkingFormat: openai` : ""),
    command: `omp --model '${ref}'` + (explicit ? ` --thinking ${effort}` : ""), docs: "https://github.com/can1357/oh-my-pi/blob/main/docs/models.md",
  };
  throw new Error("Unsupported agent");
}
