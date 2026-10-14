import assert from "node:assert/strict";
import test from "node:test";

import { payloadWarnings } from "../src/warnings.js";

test("an unavailable retained input keeps its actual reason beside the empty figures", () => {
  assert.deepEqual(payloadWarnings({ data_availability: { status: "UNAVAILABLE",
    assessment: { reason: "Missing locally; remote availability was not checked." } } }), [{
    kind: "Financial data unavailable", text: "Missing locally; remote availability was not checked.",
  }]);
});

test("a pending annual filing discloses that the previous complete filing is in use", () => {
  assert.deepEqual(payloadWarnings({ data_pending: {
    note: "Calculations use the last complete filing.",
    filed: "2026-08-27",
    accession: "000000-26-000001",
  }}), [{
    kind: "Latest filing pending",
    text: "Calculations use the last complete filing.",
    filed: "2026-08-27",
    accession: "000000-26-000001",
  }]);
});

test("a rejected historical-price refresh is visible without inventing a replacement", () => {
  const warnings = payloadWarnings({ price_history_warning: {
    note: "unexplained rescaling; the previously validated history remains in use",
  }});
  assert.equal(warnings[0].kind, "Price history retained");
  assert.match(warnings[0].text, /previously validated history/);
});

test("ordinary rows have no warning marker", () => {
  assert.deepEqual(payloadWarnings({}), []);
});

test("saved-data preview limitations stay beside the company with other actual warnings", () => {
  const limitations = ["Uses saved filing data.", "Newer filings have not been fully checked."];
  const warnings = payloadWarnings({ publication_inputs: { capture_mode: "regression_import", limitations },
    data_availability: { status: "UNAVAILABLE", assessment: { reason: "Missing locally." } } });
  assert.deepEqual(warnings, [
    { kind: "Saved-data preview", text: limitations.join(" ") },
    { kind: "Financial data unavailable", text: "Missing locally." },
  ]);
  assert.deepEqual(payloadWarnings({ publication_inputs: { capture_mode: "caller_captured_enrichment", limitations } }), []);
  assert.deepEqual(payloadWarnings({ publication_inputs: { capture_mode: "regression_import", limitations: [] } }), []);
});

test("an hourly quote failure discloses that the dated quote was retained", () => {
  const warnings = payloadWarnings({ quote_refresh_warning: {
    note: "the hourly provider request failed; the previous dated quote remains in use",
  }});
  assert.equal(warnings[0].kind, "Quote refresh failed");
  assert.match(warnings[0].text, /previous dated quote/);
});
