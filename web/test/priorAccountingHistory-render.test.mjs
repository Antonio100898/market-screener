import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";
import { filingUrl } from "../src/sourceLink.js";

test("prior accounting table labels currency, share limits, overlap and exact source links", async (t) => {
  const vite = await createServer({ appType: "custom", server: { middlewareMode: true, hmr: false },
    optimizeDeps: { noDiscovery: true, entries: [] } });
  t.after(() => vite.close());
  const { default: History } = await vite.ssrLoadModule("/src/PriorAccountingHistory.jsx");
  const source = { tag: "us-gaap:EarningsPerShareDiluted", form: "20-F", end: "2021-03-31",
    accn: "0001193125-21-190000" };
  const point = (value, src = source) => ({ value, source: src });
  const history = [{ basis: "US-GAAP", current_basis: "IFRS", currency: "JPY",
    share_basis: "Source-reported; later split and receipt adjustments are not assessed.",
    series: { eps: { 2021: point(0), 2020: point(10) }, net_income: { 2021: point(300) },
      free_cash_flow: { 2021: point(100, { components: [source, { ...source, accn: "0001193125-20-180000" }] }) } } }];
  const props = { history, formatMoney: (value, currency) => `${currency} ${value}`,
    formatEps: (value, currency) => `${currency} ${value}`,
    renderSource: (src) => React.createElement("a", { href: filingUrl("0000313838", src.accn) }, src.accn) };
  const html = renderToStaticMarkup(React.createElement(History, props));
  assert.match(html, /US-GAAP · JPY · historical/);
  assert.match(html, /Current tables and calculations use IFRS/);
  assert.match(html, /overlapping years are not equivalent/i);
  assert.match(html, /adjustments are not assessed/);
  assert.match(html, /JPY 0/);
  assert.match(html, /JPY 10/);
  assert.match(html, /—/);
  assert.ok(html.includes(filingUrl("0000313838", source.accn)));
  assert.ok(html.includes(filingUrl("0000313838", "0001193125-20-180000")));
  assert.equal(renderToStaticMarkup(React.createElement(History, { ...props, history: undefined })), "");
  const unknownCurrency = renderToStaticMarkup(React.createElement(History,
    { ...props, history: [{ ...history[0], currency: "CLP" }] }));
  assert.match(unknownCurrency, /CLP 10/);
  assert.doesNotMatch(unknownCurrency, /\$10/);
});

test("company Detail connects the supplementary field to existing format and source owners", async (t) => {
  const oldWindow = globalThis.window, oldStorage = globalThis.localStorage;
  globalThis.window = { location: { href: "http://localhost/", search: "" }, setTimeout, clearTimeout };
  globalThis.localStorage = { getItem() { return null; }, setItem() {} };
  t.after(() => {
    if (oldWindow === undefined) delete globalThis.window; else globalThis.window = oldWindow;
    if (oldStorage === undefined) delete globalThis.localStorage; else globalThis.localStorage = oldStorage;
  });
  const vite = await createServer({ appType: "custom", server: { middlewareMode: true, hmr: false },
    optimizeDeps: { noDiscovery: true, entries: [] } });
  t.after(() => vite.close());
  const { default: Detail } = await vite.ssrLoadModule("/src/Detail.jsx");
  const row = { cik: "0000313838", ticker: "TEST", name: "Detail history contract", currency: "USD",
    criteria: [1, 2, 3, 4, 5, 7].map((n) => ({ n, status: "INSUFFICIENT_DATA", value: null })),
    annual_ratios: {}, annual_eps: { 2025: 2 },
    prior_accounting_history: [{ basis: "US-GAAP", current_basis: "IFRS", currency: "JPY",
      share_basis: "Source-reported; later split and receipt adjustments are not assessed.",
      series: { eps: { 2021: { value: 10, source: { tag: "us-gaap:EarningsPerShareDiluted", form: "20-F",
        end: "2021-03-31", accn: "0001193125-21-190000" } } } } }] };
  const html = renderToStaticMarkup(React.createElement(Detail, { row, onClose() {} }));
  assert.match(html, /Earlier accounting history/);
  assert.match(html, /Current tables and calculations use IFRS/);
  assert.match(html, /¥10\.00/);
  assert.ok(html.includes(filingUrl(row.cik, "0001193125-21-190000")));
});
