import assert from "node:assert/strict";
import test from "node:test";
import { createServer } from "vite";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

const annual = { 2021: 3, 2022: 3, 2023: 3 };
function row(eps = annual, latestFy = 2024) {
  return { security_id: 1, cik: "0000000001", ticker: "SYNTHETIC", name: "Synthetic EPS caller",
    price: 30, price_asof: "2025-01-01", currency: "USD", shares: 100, bvps: 10,
    annual_eps: eps, ch13: { latest_fy: latestFy }, pe3: null,
    criteria: [1, 2, 3, 4, 5, 7].map(n => ({ n, status: "INSUFFICIENT_DATA", value: null })),
    warnings: [], profile: "OPERATING", assumption_mode: { status: "UNAVAILABLE" },
    returnQuality: { score: 0, missingInputs: ["OCF"], assumptions: [], cashFlowMissing: true,
      estimatedInputs: {}, assumedInputs: {} } };
}

test("shared selected-EPS render handoffs and ordinary curve neighbors", async (t) => {
  const previous = globalThis.window;
  globalThis.window = { location: { href: "http://localhost/", search: "" } };
  const vite = await createServer({ appType: "custom", server: { middlewareMode: true, hmr: false },
    optimizeDeps: { noDiscovery: true, entries: [] } });
  t.after(async () => {
    await vite.close();
    if (previous === undefined) delete globalThis.window;
    else globalThis.window = previous;
  });
  const { default: Table } = await vite.ssrLoadModule("/src/CompanyTable.jsx");
  const { default: Evidence } = await vite.ssrLoadModule("/src/EarningsEvidence.jsx");
  const { default: Curve } = await vite.ssrLoadModule("/src/EpsCurve.jsx");
  const { default: Detail } = await vite.ssrLoadModule("/src/Detail.jsx");

  await t.test("actual table passes emitted fiscal label into evidence", () => {
    const html = renderToStaticMarkup(createElement(Table, { rows: [row()], sort: [],
      columns: ["ticker", "eps10", "pe3"], onSort() {}, onSelect() {} }));
    assert.match(html, /2024 —/);
    assert.match(html, /3\/3/);
    assert.match(html, /data-label="P\/E 3y"[^>]*>\s*—/);
  });
  await t.test("empty selected evidence remains a labelled gap", () => {
    const html = renderToStaticMarkup(createElement(Evidence, { annual: {}, latestFy: 2024 }));
    assert.match(html, /2024 —/);
    assert.match(html, /0\/0/);
  });
  await t.test("curve discloses missing selected year without adding numeric point", () => {
    const old = renderToStaticMarkup(createElement(Curve, { annualEps: annual, ttmEps: 4 }));
    const selected = renderToStaticMarkup(createElement(Curve, { annualEps: annual, ttmEps: 4, latestFy: 2024 }));
    assert.match(selected, /FY2024[^<]*unavailable/);
    const svg = html => html.match(/<svg\b[\s\S]*?<\/svg>/)?.[0];
    assert.ok(svg(old));
    assert.equal(svg(selected), svg(old));
  });
  await t.test("missing-year caption survives insufficient curve points", () => {
    for (const eps of [{}, { 2023: 3 }]) {
      const html = renderToStaticMarkup(createElement(Curve, { annualEps: eps, latestFy: 2024 }));
      assert.match(html, /FY2024[^<]*unavailable/);
      assert.ok(!html.includes("<svg"));
    }
  });
  await t.test("equal and lower selected labels preserve ordinary curve markup", () => {
    const ordinary = renderToStaticMarkup(createElement(Curve, { annualEps: annual, ttmEps: 4 }));
    for (const latestFy of [2023, 2020, null, true, "2024", 2024.5]) {
      assert.equal(renderToStaticMarkup(createElement(Curve, { annualEps: annual, ttmEps: 4, latestFy })), ordinary);
    }
  });
  await t.test("actual initial Detail uses missing current multiple and caption", () => {
    const html = renderToStaticMarkup(createElement(Detail, { row: row(), onClose() {} }));
    assert.match(html, /P\/E3 — · product —/);
    assert.match(html, /FY2024[^<]*unavailable/);
  });
});
