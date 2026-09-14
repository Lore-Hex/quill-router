export function centsFromText(text) {
  if (!/^\d{1,4}(\.\d{1,2})?$/.test(text.trim())) throw new Error("Enter a USD amount with up to two decimal places.");
  const [whole, fraction = ""] = text.trim().split(".");
  const cents = Number(whole) * 100 + Number(fraction.padEnd(2, "0"));
  if (cents < 1 || cents > 100000) throw new Error("Enter an amount between $0.01 and $1,000.");
  return cents;
}

export function setupFor(agent, model, apiBase) {
  if (!/^[A-Za-z0-9_./:-]{1,180}$/.test(model.id)) throw new Error("Invalid model ID");
  if (apiBase !== "https://api.lightningrouter.ai/v1") throw new Error("Unexpected API endpoint");
  const context = Number.isSafeInteger(model.context) && model.context > 0 ? model.context : 32768;
  const output = Math.min(Number.isSafeInteger(model.output) && model.output > 0 ? model.output : 4096, context);
  const ref = `lightningrouter/${model.id}`;
  if (agent === "opencode") return {
    path: "~/.config/opencode/opencode.json",
    config: JSON.stringify({
      $schema: "https://opencode.ai/config.json", model: ref,
      provider: { lightningrouter: {
        npm: "@ai-sdk/openai-compatible", name: "LightningRouter",
        options: { baseURL: apiBase, apiKey: "{env:LIGHTNINGROUTER_API_KEY}" },
        models: { [model.id]: { name: model.name, limit: { context, output } } },
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
        models: [{ id: model.id, name: model.name, context_window: context, default_max_tokens: output }],
      } },
      models: { large: { provider: "lightningrouter", model: model.id }, small: { provider: "lightningrouter", model: model.id } },
    }, null, 2),
    command: "crush", docs: "https://github.com/charmbracelet/crush#custom-providers",
  };
  if (agent === "omp") return {
    path: "~/.omp/agent/models.yml",
    config: `providers:\n  lightningrouter:\n    baseUrl: ${apiBase}\n    api: openai-completions\n    apiKey: LIGHTNINGROUTER_API_KEY\n    authHeader: true\n    models:\n      - id: ${JSON.stringify(model.id)}\n        name: ${JSON.stringify(model.name)}\n        contextWindow: ${context}\n        maxTokens: ${output}`,
    command: `omp --model '${ref}'`, docs: "https://github.com/can1357/oh-my-pi/blob/main/docs/models.md",
  };
  throw new Error("Unsupported agent");
}
