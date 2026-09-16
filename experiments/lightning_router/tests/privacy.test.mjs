import assert from "node:assert/strict";
import test from "node:test";
import { parse } from "yaml";
import { createOpenAICompatible } from "@ai-sdk/openai-compatible";
import { modelMatchesPrivacy, setupFor } from "../setup.mjs";

const base = "https://api.trustedrouter.com/v1";
const model = {id: "test/model", name: "Test", privacy: {
  confidential: ["tee"], zdr: ["retention"], no_store: ["retention"], any: ["tee", "retention", "unknown"],
}};
function providerFor(agent, config) {
  if (agent === "opencode") return JSON.parse(config).provider.lightningrouter.models[model.id].options.provider;
  if (agent === "crush") return JSON.parse(config).providers.lightningrouter.extra_body.provider;
  return parse(config).providers.lightningrouter.models[0].compat.extraBody.provider;
}

test("privacy eligibility is explicit, never a numeric tier implication", () => {
  for (const value of [null, undefined, {}, {confidential: []}, {confidential: [null]}, {confidential: ["bad/slug"]}]) {
    assert.equal(modelMatchesPrivacy({...model, privacy: value}, "confidential"), false);
  }
  assert.equal(modelMatchesPrivacy({...model, privacy: {confidential: ["tee"]}}, "zdr"), false);
  assert.equal(modelMatchesPrivacy(model, "confidential"), true);
  assert.equal(modelMatchesPrivacy(model, "zdr"), true);
  assert.equal(modelMatchesPrivacy({}, "any"), true);
  assert.equal(modelMatchesPrivacy(model, "unknown"), false);
});

for (const agent of ["opencode", "crush", "omp"]) {
  test(`${agent} defaults to hard E2EE routing even without a provider order`, () => {
    assert.deepEqual(providerFor(agent, setupFor(agent, model, base).config), {min_privacy: "confidential"});
    assert.throws(() => setupFor(agent, {...model, privacy: undefined}, base));
  });
  test(`${agent} retains the chosen privacy constraint alongside provider order`, () => {
    for (const [privacy, slug] of [["confidential", "tee"], ["zdr", "retention"], ["no_store", "retention"]]) {
      assert.deepEqual(providerFor(agent, setupFor(agent, model, base, [slug], privacy).config),
        {min_privacy: privacy, order: [slug]});
    }
    assert.throws(() => setupFor(agent, model, base, ["unknown"], "confidential"));
    assert.throws(() => setupFor(agent, model, base, [], "invalid"));
  });
}

test("OpenCode transports min_privacy on streaming and non-streaming requests", async () => {
  const config = JSON.parse(setupFor("opencode", model, base, ["tee"]).config).provider.lightningrouter;
  for (const streaming of [false, true]) {
    let body;
    const client = createOpenAICompatible({name: "lightningrouter", baseURL: base, apiKey: "local-test",
      fetch: async (_, init) => {
        body = JSON.parse(init.body);
        return streaming ? new Response('data: [DONE]\n\n', {headers: {"Content-Type": "text/event-stream"}})
          : Response.json({id: "test", object: "chat.completion", created: 1, model: model.id,
            choices: [{index: 0, message: {role: "assistant", content: "Hi"}, finish_reason: "stop"}],
            usage: {prompt_tokens: 1, completion_tokens: 1, total_tokens: 2}});
      }});
    await client.chatModel(model.id)[streaming ? "doStream" : "doGenerate"]({
      prompt: [{role: "user", content: [{type: "text", text: "Hi"}]}],
      providerOptions: {lightningrouter: config.models[model.id].options},
    });
    assert.deepEqual(body.provider, {min_privacy: "confidential", order: ["tee"]});
    assert.equal(body.min_privacy, undefined);
  }
});
