import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

async function render(t, row) {
  const previousWindow = globalThis.window;
  globalThis.window = { location: { href: "http://localhost/", search: "" } };
  t.after(() => {
    if (previousWindow === undefined) delete globalThis.window;
    else globalThis.window = previousWindow;
  });
  const vite = await createServer({ appType: "custom", server: { middlewareMode: true } });
  t.after(() => vite.close());
  const { default: Detail } = await vite.ssrLoadModule("/src/Detail.jsx");
  return renderToStaticMarkup(React.createElement(Detail, {
    row: {
      ticker: "TEST", name: "Holdings fixture", cik: "0000000001",
      criteria: [1, 2, 3, 4, 5, 7].map((n) => ({ n, status: "INSUFFICIENT_DATA", value: null })),
      ...row,
    },
    onClose() {},
  }));
}

const person = (name, shares, filed, accn) => ({
  name, title: "Director", shares, indirect_shares: 0, filed, accn, form: "4", last_filed: filed,
});

test("management holdings show unknown, a lower-bound total and possibly former people", async (t) => {
  const markup = await render(t, { management_holdings: {
    source: "SEC",
    people: [person("DOE JANE", 1000, "2026-01-02", "0000000001-26-000001"),
             person("ROE RICHARD", null, null, null)],
    possibly_former: [person("OLD OFFICER", 5000, "2020-01-02", "0000000001-20-000001")],
    total_shares: 1000, unknown_holdings: 1, percent_of_outstanding: 10, percent_withheld: false,
  } });
  assert.match(markup, />Management holdings</);
  assert.match(markup, /at least 1,000 shares/);
  assert.match(markup, /10% of shares outstanding/);
  assert.match(markup, />unknown</);
  assert.match(markup, /Possibly former/);
  assert.match(markup, /OLD OFFICER/);
  assert.match(markup, /0000000001-26-000001-index\.htm/);
});

test("a company without any management filing shows unknown, not zero", async (t) => {
  const markup = await render(t, {});
  assert.match(markup, /Unknown: no director or officer holding/);
  assert.doesNotMatch(markup, /Total: 0/);
});
