import assert from "node:assert/strict";
import test from "node:test";
import { parse } from "yaml";
import { createOpenAICompatible } from "@ai-sdk/openai-compatible";
import { providerOrderFromText, setupFor as buildSetup } from "../setup.mjs";

// Test unrestricted ordering separately from the privacy gate in privacy.test.mjs.
const setupFor = (agent, model, base, order) => buildSetup(agent, model, base, order, "any");

const model = {id: "deepseek/deepseek-v4.1-flash", name: "DeepSeek V4.1 Flash",
  reasoning: {status: "reviewed", field: "reasoning_effort", setup_efforts: ["low", "high", "max"], setup_default: "high"}};
const base = "https://api.trustedrouter.com/v1";
const order = ["deepinfra", "novita", "deepseek"];

test("JSON array parser preserves order and empty input means automatic routing", () => {
  assert.deepEqual(providerOrderFromText(' ["novita", "deepinfra"] '), ["novita", "deepinfra"]);
  assert.deepEqual(providerOrderFromText("  "), []);
  assert.deepEqual(providerOrderFromText("[]"), []);
});
for (const value of ['deepinfra,novita', '"deepinfra"', '{}', 'null', '[null]', '[1]', '[""]',
  '["DeepInfra"]', '["deepinfra", "deepinfra"]', '["a;curl evil.test"]', '["../deepinfra"]',
  '["<script>"]', '["us-east1/deepinfra"]', JSON.stringify(["x".repeat(65)]),
  JSON.stringify(Array.from({length: 17}, (_, i) => `provider${i}`)), " ".repeat(2049)]) {
  test(`reject invalid provider array: ${value.slice(0, 70)}`, () => {
    assert.throws(() => providerOrderFromText(value));
  });
}
test("direct config callers cannot bypass validation", () => {
  for (const invalid of [null, {}, "deepinfra", [1], ["bad\nslug"], ["deepinfra", "deepinfra"]]) {
    for (const agent of ["opencode", "crush", "omp"]) assert.throws(() => setupFor(agent, model, base, invalid));
  }
});
test("maximum valid provider list round trips without truncation", () => {
  const list = Array.from({length: 16}, (_, i) => `p${i}`.padEnd(64, "a"));
  assert.deepEqual(providerOrderFromText(JSON.stringify(list)), list);
});

function bodyFor(agent, config) {
  if (agent === "opencode") return JSON.parse(config).provider.lightningrouter.models[model.id].options;
  if (agent === "crush") return JSON.parse(config).providers.lightningrouter.extra_body;
  return parse(config).providers.lightningrouter.models[0].compat.extraBody;
}

for (const agent of ["opencode", "crush", "omp"]) {
  test(`${agent}: provider order is a nested array, not a string or an allowlist`, () => {
    const original = structuredClone(model);
    const before = [...order];
    const setup = setupFor(agent, model, base, order);
    assert.deepEqual(bodyFor(agent, setup.config).provider, {order});
    assert.deepEqual(order, before);
    assert.deepEqual(model, original);
    assert.ok(setup.config.includes(base));
    assert.ok(setup.config.includes("high"));
    assert.ok(!setup.config.includes("allow_fallbacks"));
    assert.ok(!setup.config.includes('"only"'));
  });
  test(`${agent}: changing order is preserved exactly`, () => {
    const reverse = [...order].reverse();
    assert.deepEqual(bodyFor(agent, setupFor(agent, model, base, reverse).config).provider.order, reverse);
  });
  test(`${agent}: empty order omits routing overrides`, () => {
    assert.equal(bodyFor(agent, setupFor(agent, model, base, []).config)?.provider, undefined);
  });
  test(`${agent}: provider ordering also works without reasoning support`, () => {
    const selected = {...model, reasoning: undefined};
    assert.deepEqual(bodyFor(agent, setupFor(agent, selected, base, order).config).provider.order, order);
  });
}

test("OpenCode actual SDK sends order and reasoning together in streaming and nonstreaming requests", async () => {
  const config = JSON.parse(setupFor("opencode", model, base, order).config).provider.lightningrouter;
  for (const stream of [false, true]) {
    let sent;
    const client = createOpenAICompatible({name: "lightningrouter", baseURL: config.options.baseURL,
      apiKey: "local-fake-key", fetch: async (url, init) => {
        assert.equal(url, base + "/chat/completions");
        sent = JSON.parse(init.body);
        return stream ? new Response('data: [DONE]\n\n', {headers: {"Content-Type": "text/event-stream"}})
          : Response.json({id: "test", object: "chat.completion", created: 1, model: model.id,
            choices: [{index: 0, message: {role: "assistant", content: "Hello"}, finish_reason: "stop"}],
            usage: {prompt_tokens: 1, completion_tokens: 1, total_tokens: 2}});
      }});
    const selected = client.chatModel(model.id);
    await selected[stream ? "doStream" : "doGenerate"]({
      prompt: [{role: "user", content: [{type: "text", text: "Hi"}]}],
      providerOptions: {lightningrouter: {...config.models[model.id].options, ...config.models[model.id].variants.max}},
    });
    assert.deepEqual(sent.provider, {order});
    assert.equal(sent.model, model.id);
    assert.equal(sent.reasoning_effort, "max");
    assert.equal(sent.extraBody, undefined);
    assert.equal(sent.extra_body, undefined);
  }
});
