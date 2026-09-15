export function centsFromText(text) {
  if (!/^\d{1,4}(\.\d{1,2})?$/.test(text.trim())) throw new Error("Enter a USD amount with up to two decimal places.");
  const [whole, fraction = ""] = text.trim().split(".");
  const cents = Number(whole) * 100 + Number(fraction.padEnd(2, "0"));
  if (cents < 1 || cents > 100000) throw new Error("Enter an amount between $0.01 and $1,000.");
  return cents;
}

const CLIENT_EFFORTS = ["none", "minimal", "low", "medium", "high", "xhigh", "max"];

export function setupFor(agent, model, apiBase) {
  if (!/^[A-Za-z0-9_./:-]{1,180}$/.test(model.id)) throw new Error("Invalid model ID");
  if (!["https://api.lightningrouter.ai/v1", "https://api.trustedrouter.com/v1"].includes(apiBase)) throw new Error("Unexpected API endpoint");
  const profile = model.reasoning;
  const efforts = profile?.status === "reviewed" ? profile.setup_efforts || [] : [];
  const effort = profile?.status === "reviewed" ? profile.setup_default : null;
  if (!Array.isArray(efforts) || efforts.some(value => !CLIENT_EFFORTS.includes(value)) ||
      (effort != null && !efforts.includes(effort))) throw new Error("Invalid model reasoning metadata");
  const explicit = effort != null && efforts.length > 0;
  const canReason = profile?.status === "reviewed" && Boolean(profile.field);
  const reasoningSummary = explicit ? `reasoning_effort = ${effort}`
    : "No effort override. Client and selected provider defaults apply.";
  // OpenCode merges built-in variants with custom variants. Explicitly disable
  // unsupported built-ins so a generic medium/xhigh option cannot reappear.
  const variants = Object.fromEntries(CLIENT_EFFORTS.map(value => [value,
    explicit && efforts.includes(value) ? { reasoningEffort: value } : { disabled: true }]));
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
          reasoning: canReason, variants,
          ...(explicit ? { options: { reasoningEffort: effort } } : {}),
        } },
      } },
    }, null, 2),
    command: "opencode", docs: "https://opencode.ai/docs/providers/#custom-provider", reasoningSummary,
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
          can_reason: canReason,
          ...(explicit ? { reasoning_levels: efforts, default_reasoning_effort: effort } : {}),
        }],
      } },
      models: Object.fromEntries(["large", "small"].map((role) => [role, {
        provider: "lightningrouter", model: model.id, ...(explicit ? { reasoning_effort: effort } : {}),
      }])),
    }, null, 2),
    command: "crush", docs: "https://github.com/charmbracelet/crush#custom-providers", reasoningSummary,
  };
  if (agent === "omp") return {
    path: "~/.omp/agent/models.yml",
    config: `providers:\n  lightningrouter:\n    baseUrl: ${apiBase}\n    api: openai-completions\n    apiKey: LIGHTNINGROUTER_API_KEY\n    authHeader: true\n    models:\n      - id: ${JSON.stringify(model.id)}\n        name: ${JSON.stringify(model.name)}` +
      (context ? `\n        contextWindow: ${context}` : "") + (output ? `\n        maxTokens: ${output}` : "") +
      (explicit ? `\n        reasoning: true\n        thinking:\n          mode: effort\n          efforts: [${efforts.filter(value => value !== "none").join(", ")}]\n          requiresEffort: ${!efforts.includes("none")}` +
        (effort !== "none" ? `\n          defaultLevel: ${effort}` : "") +
        "\n        compat:\n          supportsReasoningEffort: true\n          thinkingFormat: openai"
        : `\n        reasoning: ${canReason}\n        compat:\n          supportsReasoningParams: false`),
    command: `omp --model '${ref}'` + (explicit ? ` --thinking ${effort === "none" ? "off" : effort}` : ""),
    docs: "https://github.com/can1357/oh-my-pi/blob/main/docs/models.md", reasoningSummary,
  };
  throw new Error("Unsupported agent");
}
