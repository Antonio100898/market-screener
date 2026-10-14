import assert from "node:assert/strict";
import test from "node:test";
import { epsEvidence } from "../src/earningsEvidence.js";
import { pe3, priceToBook, currentRatio } from "../src/screen.js";

const row = (annual = { 2023: 3, 2022: 3, 2021: 3 }, latestFy = 2024) => ({
  annual_eps: annual, ch13: { latest_fy: latestFy }, price: 30, price_asof: "2025-01-01",
  currency: "USD", bvps: 10, current_assets: 40, current_liabilities: 20,
});

test("selected missing current EPS does not become an older current multiple", () => {
  const value = row(), before = structuredClone(value);
  assert.equal(pe3(value), null);
  const evidence = epsEvidence(value.annual_eps, value.ch13.latest_fy);
  assert.equal(evidence.latest, 2024);
  assert.equal(evidence.values.at(-1), null);
  assert.equal(evidence.present, 3);
  assert.equal(evidence.positive, 3);
  assert.equal(priceToBook(value), 3);
  assert.equal(currentRatio(value), 2);
  assert.deepEqual(value, before);
});

test("empty selected record shows labelled gaps while empty legacy remains absent", () => {
  const evidence = epsEvidence({}, 2024);
  assert.deepEqual(evidence.years, Array.from({ length: 10 }, (_, index) => 2015 + index));
  assert.deepEqual(evidence.values, Array(10).fill(null));
  assert.equal(evidence.present, 0);
  assert.equal(evidence.positive, 0);
  assert.equal(evidence.stable, false);
  assert.equal(evidence.growth, null);
  assert.equal(epsEvidence({}), null);
});

test("selected missing year replaces older stability and growth windows", () => {
  const annual = Object.fromEntries(Array.from({ length: 13 }, (_, index) => [2011 + index, 3]));
  const before = structuredClone(annual), evidence = epsEvidence(annual, 2024);
  assert.equal(evidence.present, 9);
  assert.equal(evidence.positive, 9);
  assert.equal(evidence.stable, false);
  assert.equal(evidence.growth, null);
  assert.deepEqual(evidence.growthRecentYears, [2022, 2023, 2024]);
  assert.deepEqual(annual, before);
});

for (const latest of [undefined, null, true, "2024", 2024.5, 2020, 2023]) {
  test(`non-newer accepted fiscal label preserves old math: ${String(latest)}`, () => {
    const value = row(undefined, latest);
    // Explicitly set undefined: default parameters otherwise supply 2024.
    value.ch13.latest_fy = latest;
    assert.equal(pe3(value), 10);
    assert.equal(epsEvidence(value.annual_eps, latest).latest, 2023);
  });
}

test("legacy nonconsecutive and explicit-null arithmetic is preserved", () => {
  assert.equal(pe3(row({ 2025: 6, 2022: 3, 2020: null }, 2025)), 10);
  assert.equal(pe3(row({ 2025: 6, 2022: 3, 2020: 3 }, 2025)), 7.5);
});

test("known current loss and zero are observations, not selected gaps", () => {
  for (const latest of [0, -1]) {
    const value = row({ 2024: latest, 2023: 3, 2022: 3 });
    const evidence = epsEvidence(value.annual_eps, 2024);
    assert.equal(evidence.values.at(-1), latest);
    assert.equal(evidence.present, 3);
    assert.equal(evidence.positive, 2);
    assert.equal(pe3(value), 30 / ((latest + 6) / 3));
  }
});

test("historical missing observation does not erase known current EPS", () => {
  const value = row({ 2024: 3, 2023: 3, 2022: 3, 2020: null });
  assert.equal(pe3(value), 10);
  const evidence = epsEvidence(value.annual_eps, 2024);
  assert.equal(evidence.latest, 2024);
  assert.equal(evidence.values.at(-1), 3);
  assert.equal(evidence.values[5], null);
});
