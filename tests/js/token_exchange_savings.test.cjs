const assert = require("node:assert/strict");
const { test } = require("node:test");
const { calculate, defaults, encode, decode } = require("../../src/trusted_router/static/token-exchange-savings.js");

test("reference example includes markup only on moved provider spend", () => {
  assert.deepEqual(calculate(defaults), {
    baseline: 335000000, eligible: 134000000, kept: 201000000, provider: 26800000,
    fee: 1474000, total: 229274000, saved: 105726000, annual: 1268712000, percent: 31.56,
  });
});

test("off and zero eligible spend leave the bill unchanged with no fee", () => {
  for (const state of [{ ...defaults, enabled: false }, { ...defaults, share: 0 }]) {
    const cost = calculate(state);
    assert.equal(cost.total, cost.baseline);
    for (const field of ["provider", "fee", "saved", "annual", "eligible"]) assert.equal(cost[field], 0);
  }
});

test("zero spend produces finite zero results", () => {
  for (const value of Object.values(calculate({ ...defaults, spend: 0 }))) assert.equal(value, 0);
});

test("no price advantage honestly shows increased cost, not fake savings", () => {
  const cost = calculate({ ...defaults, spend: 100, share: 100, discount: 0 });
  assert.equal(cost.total, 10550);
  assert.equal(cost.saved, -550);
  assert.equal(cost.annual, -6600);
  assert.equal(cost.percent, -5.5);
});

test("cent rounding balances every displayed line item across boundary scenarios", () => {
  for (const spend of [0, 0.01, 0.03, 1.15, 9.99, 100, 3350000, 99999999.99, 100000000]) {
    for (const share of [0, 1, 33, 40, 99, 100]) {
      for (const discount of [0, 1, 80, 95]) {
        const cost = calculate({ spend, share, discount, enabled: true });
        for (const field of ["baseline", "kept", "eligible", "provider", "fee", "total", "saved", "annual"]) assert(Number.isSafeInteger(cost[field]));
        assert.equal(cost.total, cost.kept + cost.provider + cost.fee);
        assert.equal(cost.baseline, cost.total + cost.saved);
        assert.equal(cost.baseline, cost.eligible + cost.kept);
        assert.equal(cost.annual, cost.saved * 12);
      }
    }
  }
});

test("untrusted or impossible assumptions fail closed", () => {
  for (const spend of [-1, Infinity, NaN, "100", null, 100000001]) assert.throws(() => calculate({ ...defaults, spend }));
  for (const share of [-1, 101, 1.5, NaN]) assert.throws(() => calculate({ ...defaults, share }));
  for (const discount of [-1, 96, 1.5, Infinity]) assert.throws(() => calculate({ ...defaults, discount }));
  assert.throws(() => calculate({ ...defaults, enabled: "false" }));
});

test("shared scenarios roundtrip, including off and zero", () => {
  for (const state of [defaults, { spend: 0, share: 0, discount: 0, enabled: false }, { spend: 123.45, share: 100, discount: 95, enabled: true }]) {
    assert.deepEqual(decode(`#${encode(state)}`), state);
  }
});

test("malformed URL fragments cannot inject or create invalid calculations", () => {
  for (const fragment of ["#spend=Infinity&share=NaN&discount=-1", "#spend=&share=101&discount=100", "#spend=<script>&share=1.2&discount=1e2", "#spend=100000001&share=0x20&discount=+1", "#spend=1.001"]) {
    assert.deepEqual(decode(fragment), defaults);
  }
  assert.deepEqual(decode("#unknown=anything"), defaults);
});
