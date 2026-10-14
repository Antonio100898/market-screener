import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";
import { transformWithEsbuild } from "vite";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { workspaceColumns } from "../src/workspaceSession.js";

const cache = new Map();
async function compile(source, filename) {
  const compiled = await transformWithEsbuild(source, filename, { jsx: "automatic" });
  let code = compiled.code;
  for (const [, specifier] of code.matchAll(/^import\b[\s\S]*?from\s+"([^"]+)";/gm)) {
    let target = specifier.startsWith(".") ? new URL(specifier, filename) : new URL(import.meta.resolve(specifier));
    if (target.pathname.endsWith(".jsx")) {
      if (!cache.has(target.href)) cache.set(target.href, compile(await readFile(target, "utf8"), target.href));
      target = await cache.get(target.href);
    }
    code = code.replaceAll(JSON.stringify(specifier), JSON.stringify(target.href ?? target));
  }
  return `data:text/javascript;base64,${Buffer.from(code).toString("base64")}`;
}
const file = new URL("../src/CompanyTable.jsx", import.meta.url);
const currentSource = await readFile(file, "utf8");
const { default: Table } = await import(await compile(currentSource, file.href));
const knownAnswers = JSON.parse(await readFile(new URL("./privatePresentationTable-fixtures.json", import.meta.url), "utf8"));
const quality = { score: 31.6, roe: null, roic: 5, ronta: -3, fcfRevenue: null, capexOcf: null,
  debtToEquity: 1, fcfRevenueYears: 0, capexOcfYears: 0, inputsPresent: 3, missingInputs: ["ROE"],
  assumptions: [], estimatedInputs: {}, assumedInputs: {}, cashFlowMissing: true };
const fixture = (patch = {}) => ({ security_id: 123, cik: "0000001800", ticker: "ABT", name: "Same-input presentation fixture",
  profile: "OPERATING", currency: "USD", price: 0.0003, mcap: 1234567890, pe3: 37.4, dividendYield: 2.5,
  warnings: [{ kind: "evidence", text: "Same retained warning" }], returnQuality: quality, netNet: true,
  prose_gaps: [{ what: "Manual prose", affects: "Evidence" }], exchange: "OTC", annual_eps: { 2025: 2.5 },
  price_stats: { pct_below_52w_high: 9, pct_below_3y_high: 12, pct_above_52w_low: 10, pct_vs_3y_average: 5, average_3y: 15, price_to_3y_median: 1.2 },
  criteria: [1,2,3,4,5,7].map((n) => ({ n, status: "INSUFFICIENT_DATA", value: n === 1 ? 22.7 : null })), ...patch });
const props = (rows, extra = {}) => ({ rows, sort: [{ key: "returns", dir: 1 }, { key: "pe", dir: -1 }], onSort() {}, onSelect() {}, ...extra });
const render = (Component, value) => renderToStaticMarkup(createElement(Component, value));
const blocks = (html, section, tag) => [...html.match(new RegExp(`<${section}>(.*?)</${section}>`, "s"))[1].matchAll(new RegExp(`<${tag}(?:\\s[^>]*)?>.*?</${tag}>`, "gs"))].map(([value]) => value);

const textOf = (html) => html.replace(/<[^>]*>/g, "").replaceAll("&amp;", "&").replaceAll("&quot;", '"')
  .replaceAll("&#x27;", "'").replace(/\s+/g, " ").trim();

test("absent columns retains known default values, missing values, currency, warnings and tracking", () => {
  assert.equal(knownAnswers.length, 6);
  for (const { patch, tracked, headings, cells } of knownAnswers) {
      const row = fixture(patch);
      const value = props([row], tracked ? { tracked: new Set([row.cik]), portfolioCiks: new Set([row.cik]), onToggleTracked() {} } : {});
      const html = render(Table, value);
      assert.deepEqual(blocks(html, "thead", "th").map(textOf), headings);
      assert.deepEqual(blocks(html, "tbody", "td").map(textOf), cells);
      assert.match(html, /data warning/);
      assert.match(html, /evidence: Same retained warning/);
      assert.match(html, /check filing/);
      assert.equal(html.includes('aria-label="In portfolio"'), tracked);
      assert.equal(html.includes('class="star-btn on"'), tracked);
      assert.equal(html.includes('class="istracked inportfolio"'), tracked);
  }
});

test("all supported columns reorder identical header and cell markup together; empty stays empty", () => {
  const base = props([fixture()]);
  const original = render(Table, base), headings = blocks(original, "thead", "th"), cells = blocks(original, "tbody", "td");
  const columns = ["dividend", "price", "ticker", "pe3", "returns", "sector", "eps10", "offhigh", "name", "pe", "mcap"];
  const html = render(Table, { ...base, columns });
  const baselineOrder = ["ticker", "name", "sector", "eps10", "returns", "mcap", "price", "offhigh", "pe", "pe3", "dividend"];
  assert.equal(columns.length, workspaceColumns.length);
  assert.deepEqual(blocks(html, "thead", "th"), columns.map((id) => headings[baselineOrder.indexOf(id)]));
  assert.deepEqual(blocks(html, "tbody", "td"), columns.map((id) => cells[baselineOrder.indexOf(id)]));
  const hidden = render(Table, { ...base, columns: ["price", "ticker"] });
  assert.equal(blocks(hidden, "thead", "th").length, 2);
  assert.deepEqual(blocks(hidden, "tbody", "td"), [cells[6], cells[0]]);
  assert.doesNotMatch(hidden, /sort-priority/);
  const empty = render(Table, { ...base, columns: [] });
  assert.deepEqual(blocks(empty, "thead", "th"), []); assert.deepEqual(blocks(empty, "tbody", "td"), []);
  assert.match(empty, /<tbody><tr/);
});

test("initial browser-component markup contains no unacknowledged private defaults or ownerless view import", async (t) => {
  const priorWindow = globalThis.window, priorStorage = globalThis.localStorage;
  globalThis.window = { location: { search: "" }, innerWidth: 1000, innerHeight: 800 };
  globalThis.localStorage = { getItem(key) { assert.notEqual(key, "screener-view"); return null; } };
  t.after(() => {
    if (priorWindow === undefined) delete globalThis.window; else globalThis.window = priorWindow;
    if (priorStorage === undefined) delete globalThis.localStorage; else globalThis.localStorage = priorStorage;
  });
  const dashboard = new URL("../src/ApiDashboard.jsx", import.meta.url);
  const { default: Dashboard } = await import(await compile(await readFile(dashboard, "utf8"), dashboard.href));
  const html = render(Dashboard, {});
  assert.match(html, /Shared screening preview/);
  assert.match(html, /Loading published screening data/);
  assert.doesNotMatch(html, /Saved revision|Visible columns|Presentation revisions/);
  assert.equal((html.match(/class="search"/g) ?? []).length, 1);
});

test("existing manual move controls change ordered columns without changing a view", async () => {
  const file = new URL("../src/WorkspacePresentation.jsx", import.meta.url);
  const { default: Presentation } = await import(await compile(await readFile(file, "utf8"), file.href));
  const config = { schema_revision: 1, columns: ["price", "ticker"], view: { q: "kept" } };
  const columns = [], state = { phase: "ready", saved: { edit_revision: 2 }, draft: config,
    history: { phase: "ready", revisions: [], nextBefore: null, selected: null } };
  const tree = Presentation({ state, session: { setColumns: (value) => columns.push(value) } });
  const nodes = [];
  const visit = (value) => {
    if (Array.isArray(value)) return value.forEach(visit);
    if (!value || typeof value !== "object") return;
    nodes.push(value); visit(value.props?.children);
  };
  visit(tree);
  nodes.find(node => node.props?.["aria-label"] === "Move Ticker up").props.onClick();
  nodes.find(node => node.props?.["aria-label"] === "Move Price down").props.onClick();
  assert.deepEqual(columns, [["ticker", "price"], ["ticker", "price"]]);
  assert.deepEqual(config, { schema_revision: 1, columns: ["price", "ticker"], view: { q: "kept" } });
});
