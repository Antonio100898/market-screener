import assert from "node:assert/strict";
import test from "node:test";
import { createServer } from "vite";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

test("public server HTML escapes source text, retains missing evidence and returns honest HTTP failures", async (t) => {
  const vite = await createServer({ appType: "custom", server: { middlewareMode: true, hmr: false }, optimizeDeps: { noDiscovery: true, entries: [] } });
  t.after(() => vite.close());
  const { getServerSideProps, default: CompanyPage } = await vite.ssrLoadModule("/pages/stocks/[ticker].jsx");
  const release = { release_id: 9, projection_revision: 1, published_at: "2026-10-04T10:00:00Z", total: 1 };
  let mode = "ok";
  t.mock.method(globalThis, "fetch", async (url) => {
    if (mode === "unsupported" || mode === "outage") return new Response("{}", { status: mode === "unsupported" ? 404 : 503 });
    if (url.endsWith("/capabilities")) return Response.json({ api_contract_major: 1 });
    if (url.endsWith("/data-status")) return Response.json({ projection_ready: true, release });
    if (mode === "absent") return new Response("{}", { status: 404 });
    return Response.json({ release, row: { ticker: "ABT", name: "<script>private()</script>", cik: "0001806777",
      total_assets: 0, current_assets: null, asset_quality: { cash: 6803000000 },
      long_term_debt: 29638000000, short_term_debt: 4409000000, debt: 34047000000,
      criteria: [{ n: 1, status: "INSUFFICIENT" }],
      context_notes: [{ kind: "test", text: "Source unavailable" }], sources: { total_assets: {
        tag: "<script>source()</script>", form: "10-K", accn: "0001806777-24-000020", end: "2025-12-31", unit: "USD",
      } } } });
  });
  const headers = new Map();
  const context = { params: { ticker: "ABT" }, res: { setHeader: (key, value) => headers.set(key, value) } };
  const result = await getServerSideProps(context);
  const html = renderToStaticMarkup(createElement(CompanyPage, result.props));
  assert.equal(context.res.statusCode, 200);
  assert.equal(headers.get("Cache-Control"), "no-store");
  assert.equal(headers.get("X-Robots-Tag"), "noindex, nofollow");
  assert.ok(html.includes("Data release 9"));
  assert.ok(html.includes("Missing evidence"));
  assert.ok(html.includes("<td>0</td>"));
  assert.ok(html.includes("<td>6,803,000,000</td>"));
  assert.ok(html.includes("<td>29,638,000,000</td>"));
  assert.ok(html.includes("Debt used by the stored screen: 34,047,000,000"));
  assert.ok(html.includes("Statement currency: Unavailable"));
  assert.ok(html.includes("&lt;script&gt;private()&lt;/script&gt;"));
  assert.ok(html.includes("&lt;script&gt;source()&lt;/script&gt;"));
  assert.ok(!html.includes("<script>private()"));
  assert.ok(html.includes("Source unavailable"));
  assert.ok(html.includes("0001806777-24-000020-index.htm"));
  for (const [failure, status] of [["absent", 404], ["unsupported", 503], ["outage", 503]]) {
    mode = failure;
    const result = await getServerSideProps(context);
    assert.equal(context.res.statusCode, status);
    assert.equal(result.props.error.status, status);
    assert.equal(headers.get("X-Robots-Tag"), "noindex, nofollow");
  }
});
