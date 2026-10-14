import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";
import { priceToPass, valuationFx } from "../src/screen.js";

const rate = (base, counter, value) => ({ base, counter, rate: value, asof: "2026-10-05", source: "synthetic-FX" });
const row = () => ({ ticker: "CONTROL", cik: "1", name: "Directed FX fixture", price: 40,
  quote_currency: "CAD", reporting_currency: "JPY", currency: "JPY", price_reporting_currency: 4320,
  ttm_eps: 300, tbvps: 2000, annual_ratios: {},
  criteria: [1, 2, 3, 4, 5, 7].map(n => ({ n, status: n === 1 ? "FAIL" : "PASS", value: null })) });

test("price threshold uses valuation pair while USD size record stays separate", () => {
  const value = row();
  value.fx = rate("USD", "JPY", 150);
  value.valuation_fx = rate("CAD", "JPY", 108);
  assert.equal(valuationFx(value), value.valuation_fx);
  assert.equal(priceToPass(value), 9.99 * 300 / 108);
  assert.equal(value.fx.base, "USD");
});

for (const bad of [rate("USD", "JPY", 150), rate("JPY", "CAD", 1 / 108),
  rate("CAD", "JPY", Infinity), rate("CAD", "JPY", NaN), rate("CAD", "JPY", 0),
  rate("CAD", "JPY", -1), rate("CAD", "JPY", true), null]) {
  test(`invalid explicit valuation rate stays unavailable ${JSON.stringify(bad)}`, () => {
    const value = { ...row(), fx: rate("CAD", "JPY", 108), valuation_fx: bad };
    assert.equal(valuationFx(value), null);
    assert.equal(priceToPass(value), null);
  });
}

test("native and legacy USD quote thresholds remain compatible", () => {
  const native = { ...row(), quote_currency: "JPY", fx: rate("USD", "JPY", 150) };
  assert.equal(valuationFx(native), null);
  assert.equal(priceToPass(native), 9.99 * 300);
  const legacy = { ...row(), quote_currency: "USD", fx: rate("USD", "JPY", 150) };
  assert.equal(priceToPass(legacy), 9.99 * 300 / 150);
});

test("file-only rendered detail discloses the valuation bridge, never USD size evidence", async t => {
  const previous = globalThis.window;
  globalThis.window = { location: { href: "http://localhost/", search: "" }, setTimeout, clearTimeout };
  t.after(() => { if (previous === undefined) delete globalThis.window; else globalThis.window = previous; });
  const vite = await createServer({ appType: "custom", server: { middlewareMode: true } });
  t.after(() => vite.close());
  const { default: Detail } = await vite.ssrLoadModule("/src/Detail.jsx");
  const value = { ...row(), fx: rate("USD", "JPY", 150), valuation_fx: rate("CAD", "JPY", 108) };
  const render = source => renderToStaticMarkup(React.createElement(Detail, { row: source, onClose() {} }));
  const markup = render(value);
  assert.match(markup, /1 CAD = 108 JPY/);
  assert.doesNotMatch(markup, /1 USD = 150 JPY/);
  assert.match(markup, /market cap remain in CAD/);
  assert.match(markup, /filing totals stay in JPY/);
  assert.doesNotMatch(render({ ...value, valuation_fx: rate("USD", "JPY", 150) }), /Currency bridge:/);
  assert.doesNotMatch(render({ ...value, quote_currency: "JPY" }), /Currency bridge:/);
});
