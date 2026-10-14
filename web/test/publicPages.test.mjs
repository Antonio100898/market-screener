import assert from "node:assert/strict";
import test from "node:test";
import { filingUrl } from "../src/sourceLink.js";
import { PublicDataError, figureText, readPublicCompany, readPublicCompanyIndex } from "../app/publicData.js";
import { articles, findArticle } from "../app/articles.js";
import sitemap from "../app/sitemap.js";
import robots from "../app/robots.js";

const release = { release_id: 9, projection_revision: 1, total: 2 };
const response = (body, status = 200) => new Response(JSON.stringify(body), { status });

test("public filing links preserve SEC and EDINET source identities", () => {
  assert.equal(filingUrl("0001806777", "0001806777-24-000020"),
    "https://www.sec.gov/Archives/edgar/data/1806777/000180677724000020/0001806777-24-000020-index.htm");
  assert.equal(filingUrl("E12345", "S100ABCD"), "https://disclosure2dl.edinet-fsa.go.jp/searchdocument/pdf/S100ABCD.pdf");
  assert.equal(filingUrl("E12345", "invalid"), null);
  assert.equal(filingUrl(null, "0001806777-24-000020"), null);
});

test("public figures distinguish missing, measured zero and invalid numbers", () => {
  assert.equal(figureText(null), "Missing evidence");
  assert.equal(figureText(undefined), "Missing evidence");
  assert.equal(figureText(0), "0");
  assert.equal(figureText(-25), "-25");
  assert.equal(figureText(NaN), "Unavailable");
});

test("public company reads one strict pinned release without browser credentials", async () => {
  const calls = [];
  const fetcher = async (url, options) => {
    calls.push({ url, options });
    if (url.endsWith("/capabilities")) return response({ api_contract_major: 1 });
    if (url.endsWith("/data-status")) return response({ projection_ready: true, release });
    return response({ release, row: { ticker: "ABT", current_assets: null, current_liabilities: 0 } });
  };
  const result = await readPublicCompany("ABT", fetcher);
  assert.equal(result.row.current_assets, null);
  assert.equal(result.row.current_liabilities, 0);
  assert.ok(calls[2].url.endsWith("/companies/by-ticker/ABT?release=9&assume_absent_zero=false"));
  for (const { options } of calls) {
    assert.equal(options.cache, "no-store");
    assert.deepEqual(options.headers, { Accept: "application/json" });
  }
});

test("company outages, absent tickers and mismatched releases stay explicit", async () => {
  const fetcher = (detail) => async (url) => {
    if (url.endsWith("/capabilities")) return response({ api_contract_major: 1 });
    if (url.endsWith("/data-status")) return response({ projection_ready: true, release });
    return detail;
  };
  await assert.rejects(readPublicCompany("ABT", fetcher(response({}, 404))), (error) => error instanceof PublicDataError && error.status === 404);
  await assert.rejects(readPublicCompany("ABT", fetcher(response({}, 503))), (error) => error.status === 503);
  await assert.rejects(readPublicCompany("ABT", fetcher(response({ release: { ...release, release_id: 10 }, row: { ticker: "ABT" } }))),
    (error) => error.status === 503);
  await assert.rejects(readPublicCompany("ABT", async () => { throw new Error("private backend details"); }),
    (error) => error.status === 503 && !error.message.includes("private"));
});

test("unsupported null, primitive and consumed row shapes return unavailable", async () => {
  for (const body of [null, 1, "unsupported", []])
    await assert.rejects(readPublicCompany("ABT", async () => response(body)), (error) => error instanceof PublicDataError && error.status === 503);
  for (const row of [null, "ABT", { ticker: "ABT", criteria: {} }, { ticker: "ABT", context_notes: [{}] },
    { ticker: "ABT", sources: { cash: { tag: {} } } }, { ticker: "ABT", assumptions: [null] }])
    await assert.rejects(readPublicCompany("ABT", async (url) => response(url.endsWith("/capabilities")
      ? { api_contract_major: 1 } : url.endsWith("/data-status") ? { projection_ready: true, release } : { release, row })),
    (error) => error instanceof PublicDataError && error.status === 503);
});

test("company directory requests bounded identity pages and keeps cursor release", async () => {
  const search = new URLSearchParams({ cursor: "opaque+cursor", release: "9", token: "must-not-forward" });
  const result = await readPublicCompanyIndex(search, async (url, options) => {
    const query = new URL(url).searchParams;
    assert.equal(query.get("row_fields"), "identity");
    assert.equal(query.get("limit"), "100");
    assert.equal(query.get("cursor"), "opaque+cursor");
    assert.equal(query.get("release"), "9");
    assert.equal(query.has("token"), false);
    assert.equal(options.cache, "no-store");
    return response({ release, rows: [{ security_id: 1, ticker: "ABT", name: "Abbott" }], matched: 2, next_cursor: "next" });
  });
  assert.equal(result.next_cursor, "next");
});

test("directory release replacement and malformed identities are rejected", async () => {
  await assert.rejects(readPublicCompanyIndex(new URLSearchParams({ release: "9" }), async () =>
    response({ release: { ...release, release_id: 10 }, rows: [], next_cursor: null })), (error) => error.status === 503);
  for (const rows of [null, [null], ["ABT"], [{ security_id: 0, ticker: "ABT", name: "Abbott" }]])
    await assert.rejects(readPublicCompanyIndex(new URLSearchParams(), async () => response({ release, rows })),
      (error) => error instanceof PublicDataError && error.status === 503);
});

test("draft, unverified company and private routes are excluded from the sitemap", () => {
  const paths = sitemap().map((entry) => new URL(entry.url).pathname);
  assert.deepEqual(paths, ["/"]);
  const blocked = robots().rules.disallow;
  for (const path of ["/dashboard", "/research", "/api", "/stocks", "/blog", "/guides", "/product"])
    assert.ok(blocked.includes(path));
  assert.equal(new Set(articles.map((article) => `/${article.section}/${article.slug}`)).size, articles.length);
  assert.equal(findArticle("blog", "missing"), undefined);
});
