import assert from "node:assert/strict";
import test from "node:test";
import { createServer } from "vite";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { initialQueryView } from "../src/querySession.js";

test("shared presentation retains stored financial projections and full-cohort server labels", async (t) => {
  const vite = await createServer({ appType: "custom", server: { middlewareMode: true, hmr: false }, optimizeDeps: { noDiscovery: true, entries: [] } });
  t.after(() => vite.close());
  const { default: Table } = await vite.ssrLoadModule("/src/CompanyTable.jsx");
  const { default: Filters } = await vite.ssrLoadModule("/src/DashboardFilters.jsx");
  const { default: Reference } = await vite.ssrLoadModule("/src/MarketReference.jsx");
  const quality = { score: 31.6, roe: null, roic: null, ronta: null, fcfRevenue: null, capexOcf: null,
    debtToEquity: null, fcfRevenueYears: 0, capexOcfYears: 0, inputsPresent: 0, missingInputs: ["ROE"],
    assumptions: [], estimatedInputs: {}, assumedInputs: {}, cashFlowMissing: true };
  const row = { security_id: 123, cik: "0000001800", ticker: "ABT", name: "Stored release fixture", profile: "OPERATING",
    price: null, mcap: 1234567890, pe3: 37.4, dividendYield: 2.5, warnings: [], returnQuality: quality,
    criteria: [1,2,3,4,5,7].map((n) => ({ n, status: "INSUFFICIENT_DATA", value: n === 1 ? 22.7 : null })) };
  const html = renderToStaticMarkup(createElement(Table, { rows: [row], sort: [{ key: "returns", dir: 1 }, { key: "pe", dir: -1 }], onSort() {}, onSelect() {} }));
  assert.ok(html.includes("31.6"));
  assert.ok(html.includes("22.7"));
  assert.ok(html.includes("37.4"));
  assert.ok(html.includes("2.5%"));
  assert.ok(html.includes("$1.23B"));
  assert.ok(html.includes("Missing OCF/FCF"));
  assert.ok(html.includes("sort-priority"));
  assert.ok(!html.includes("star-btn"));
  const privateHtml = renderToStaticMarkup(createElement(Table, { rows: [row], sort: [], onSort() {}, onSelect() {},
    tracked: new Set([row.cik]), portfolioCiks: new Set([row.cik]), onToggleTracked() {} }));
  assert.ok(privateHtml.includes("istracked inportfolio"));
  assert.ok(privateHtml.includes("star-btn on"));
  assert.ok(privateHtml.includes("In portfolio"));
  const counts = { unjudged: 7, inapplicable: 3, net_net: 21, met_6: 111, eps_9: 222 };
  const options = { profiles: [["OPERATING", 5000]], sectors: [["Energy", 900]], venues: [], indexes: [] };
  const filters = renderToStaticMarkup(createElement(Filters, { view: initialQueryView(), onChange() {}, facets: options,
    counts, total: 6676, matched: 426, activeFilters: false, onClear() {} }));
  assert.ok(filters.includes("showing 426 of 6,676"));
  assert.ok(filters.includes("6/6 and above (111)"));
  assert.ok(filters.includes("9+ positive EPS years (222)"));
  assert.ok(!filters.includes("★ Tracked"));
  const legacyFilters = renderToStaticMarkup(createElement(Filters, { view: { ...initialQueryView(), trackedOnly: true },
    onChange() {}, facets: options, counts, total: 6676, matched: 426, trackedCount: 4, activeFilters: true, onClear() {} }));
  assert.ok(legacyFilters.includes("★ Tracked"));
  assert.ok(legacyFilters.includes("clear all"));
  const reference = renderToStaticMarkup(createElement(Reference, { overview: { members: 500,
    pe: { median: 24.3, count: 426 }, pe3: { median: null, count: 0 } } }));
  assert.ok(reference.includes("500 index members")); assert.ok(reference.includes("24.3×"));
  assert.ok(reference.includes("426 companies")); assert.ok(reference.includes("<strong>—</strong>"));
});
