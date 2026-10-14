"""What an engine change did to every number, before it is shipped.

The unit tests pin about 320 synthetic fixtures. The product is 5,892 real
companies times some forty figures each, and the gap between those two is where
every defect in this engine has lived: a share count that moved for 62 filers, a
criterion resurrected after the engine withheld it, an option pool of 1.3 billion
per cent. Each was found by hand-writing a one-off comparison script, and three of
those scripts were themselves wrong — they forgot the dimensioned sidecar, or the
cover ratio, and reported companies as broken that were fine.

So the comparison lives here instead, built once, calling exactly what `derive`
calls. It recomputes a sample against the shipped dashboard and reports what moved,
bucketed by field. It answers the only question that matters after a change: did
this fix the eleven companies it was meant to fix, and what else did it touch?

    python -m screener.regress                 # 600 companies, every field
    python -m screener.regress --all           # every company
    python -m screener.regress --field ttm_eps --sample 2000
    python -m screener.regress --ticker EML,MKL,GYRE
"""
from __future__ import annotations

import argparse
import json
import random
from collections import defaultdict
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from pathlib import Path

from . import evidence, store
from .sources.edgar import EdgarClient
from . import profiles
from .sync import (
    DASHBOARD_JSON, PRICEABLE_LISTINGS, _derive_cached_worker, _index_tickers, _mark_peer_efficiency,
    _price_the_ratio_history, _reporting_currency, _strip_detail_only_evidence,
    _valuation_fx_history,
    apply_price,
)

# Fields whose value is a live quote or a clock reading rather than a product of
# the engine. They differ on every run and would bury the fields that matter.
VOLATILE = frozenset({
    "price", "price_asof", "quote_time", "generated", "price_stats",
    "price_session", "market_state", "market_state_asof",
    "engine_version", "as_of",
    # dropped from the payload on the way out: engine-internal, no reader
    "ttm_eps_vintage",
})
# How far a number may drift before it counts as moved: enough to ignore the last
# decimal place of a rounded figure, not enough to hide a real change.
TOLERANCE = 0.005


def _flat(row: dict, prefix: str = "") -> dict:
    """One row as {field: value}, nested dicts flattened onto dotted keys, so a
    change inside `annual_eps` or `profitability` is attributed to its own field
    rather than to the whole object."""
    out = {}
    for key, value in (row or {}).items():
        name = f"{prefix}{key}"
        # Current quote/clock fields really are volatile. Nested historical price
        # fields are not: they are rebuilt from the stored closes and must be
        # compared like every other UI value.
        if not prefix and key in VOLATILE:
            continue
        if isinstance(value, dict):
            out.update(_flat(value, f"{name}."))
        elif isinstance(value, list):
            out[name] = json.dumps(value, sort_keys=True)
        else:
            out[name] = value
    return out


def _moved(before, after) -> bool:
    if before == after:
        return False
    if isinstance(before, (int, float)) and isinstance(after, (int, float)):
        scale = max(abs(before), abs(after), 1e-9)
        return abs(before - after) / scale > TOLERANCE
    return True


def _price_history_for_row(conn, cik: str, row: dict) -> list:
    """Mirror export's identity gate for market history.

    A cached price series can outlive or predate a listing-identity decision.  The
    UI export deliberately withholds that series unless the company is currently
    resolved as listed, so the regression rebuild must do the same.
    """
    if row.get("listed") not in PRICEABLE_LISTINGS:
        return []
    return store.price_history(conn, cik)


def _fx_history_for_row(conn, row: dict) -> dict | None:
    """Mirror export's exact quote-to-reporting historical evidence."""
    return _valuation_fx_history(conn, row)


def compare(sample: int | None, tickers: set[str] | None, field: str | None,
            progress=print, baseline: Path | None = None) -> dict:
    """Recompute and diff. Returns {field: [(ticker, before, after), ...]}."""
    shipped = json.loads(Path(baseline or DASHBOARD_JSON).read_text())["rows"]
    rows = {r["ticker"]: r for r in shipped if r.get("ticker")}
    if tickers:
        chosen = [rows[t] for t in tickers if t in rows]
    elif sample is None:
        chosen = list(rows.values())
    else:
        random.seed(0)  # the same companies every run, so two runs are comparable
        chosen = random.sample(list(rows.values()), min(sample, len(rows)))

    conn = store.connect()
    edgar = EdgarClient()
    index = _index_tickers(conn, edgar)
    loader = evidence.EvidenceLoader(conn, edgar)
    # Export builds the auditor and delisting notes from these, then drops them from
    # the payload. Without them the notes cannot be rebuilt and read as deleted.
    events = store.events_by_cik(conn)
    events_from = {r["cik"]: r["events_from"] for r in
                   conn.execute("SELECT cik, events_from FROM company").fetchall()}

    candidates: list[tuple[dict, dict]] = []
    failed = []
    # Consume completed futures promptly so one unusually large early filer cannot
    # hold thousands of later results in memory. Candidates are sorted back into
    # `chosen` order afterward, keeping two reports directly diffable.
    prepared = []
    for order, row in enumerate(chosen):
        cik = row["cik"]
        path = Path(edgar.cache_dir) / f"companyfacts_{cik}.json"
        if not path.exists():
            continue
        ticker = index.get(cik, (row["ticker"], None))[0] or row["ticker"]
        ticker, receipt = loader.identity(cik, ticker)
        task = (cik, ticker, receipt, str(edgar.cache_dir))
        prepared.append((order, row, task))

    jobs = {}
    completed: dict[int, tuple[dict, dict]] = {}
    max_workers = 4
    max_in_flight = max_workers * 4
    total_jobs = len(prepared)
    progress(f"  {total_jobs} companies queued for recomputation")
    prepared_iter = iter(prepared)

    with ProcessPoolExecutor(max_workers=max_workers) as pool:
        def submit_available() -> None:
            while len(jobs) < max_in_flight:
                try:
                    order, row, task = next(prepared_iter)
                except StopIteration:
                    return
                jobs[pool.submit(_derive_cached_worker, task)] = (order, row)

        submit_available()
        recomputed = 0
        while jobs:
            done, _ = wait(jobs, return_when=FIRST_COMPLETED)
            for future in done:
                order, row = jobs.pop(future)
                cik = row["cik"]
                recomputed += 1
                try:
                    _, result = future.result()
                    status, fresh = result if result is not None else (None, None)
                except Exception as exc:             # one bad filing must not stop the sweep
                    failed.append((row["ticker"], repr(exc)[:80]))
                    continue
                if not fresh:
                    continue
                # `derive` is only half the pipeline. Export merges stored metadata into
                # the row and enriches it; retain fields that derive does not own.
                merged = {**row, **fresh,
                          "filing_events": events.get(cik, []),
                          "events_from": events_from.get(cik),
                          "last_filing": conn.execute(
                              "SELECT last_filing FROM company WHERE cik = ?", (cik,)
                          ).fetchone()["last_filing"]}
                # Settle with the exact quote already shown by the UI, so only engine
                # changes—not a live-price move—reach the comparison.
                apply_price(merged, row.get("price"))
                _price_the_ratio_history(
                    merged, _price_history_for_row(conn, cik, merged),
                    _fx_history_for_row(conn, merged))
                completed[order] = (row, merged)
            if recomputed % 250 < len(done):
                progress(f"  {recomputed}/{total_jobs} recomputed")
            submit_available()

    candidates = [completed[order] for order in sorted(completed)]

    # The full mandatory run can recompute peer medians exactly. A ticker/sample
    # run retains the shipped peer record because an incomplete peer universe is
    # less accurate than the baseline it is diagnosing.
    if tickers is None and sample is None:
        for _, merged in candidates:
            merged.pop("peer_efficiency", None)
        _mark_peer_efficiency([merged for _, merged in candidates])

    changes: dict[str, list] = defaultdict(list)
    for i, (row, merged) in enumerate(candidates, 1):
        merged.update(profiles.enrich(merged))
        for gone in ("filing_events", "events_from", "last_filing",
                     "ttm_eps_vintage"):                 # popped on the way out
            merged.pop(gone, None)
        _strip_detail_only_evidence(merged)
        old, new = _flat(row), _flat(merged)
        for key in sorted(set(old) | set(new)):
            if field and not key.startswith(field):
                continue
            a, b = old.get(key), new.get(key)
            if _moved(a, b):
                changes[key].append((row["ticker"], a, b))
        if i % 250 == 0:
            progress(f"  {i}/{len(candidates)} compared")
    if failed:
        progress(f"  {len(failed)} companies could not be recomputed: {failed[:3]}")
    return dict(changes), len(chosen)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sample", type=int, default=600)
    ap.add_argument("--all", action="store_true", help="every company, not a sample")
    ap.add_argument("--field", help="only fields starting with this prefix")
    ap.add_argument("--ticker", help="comma-separated tickers instead of a sample")
    ap.add_argument("--show", type=int, default=6, help="examples per field")
    ap.add_argument("--baseline", type=Path,
                    help="dashboard payload to compare instead of the currently shipped one")
    args = ap.parse_args(argv)

    tickers = {t.strip().upper() for t in args.ticker.split(",")} if args.ticker else None
    changes, n = compare(None if args.all else args.sample, tickers, args.field,
                         baseline=args.baseline)

    if not changes:
        print(f"\nno field moved across {n} companies")
        return 0
    print(f"\n{sum(len(v) for v in changes.values())} changes across {n} companies:\n")
    for key in sorted(changes, key=lambda k: -len(changes[k])):
        hits = changes[key]
        print(f"  {key}  —  {len(hits)} companies ({len(hits) / n * 100:.1f}%)")
        for ticker, before, after in hits[:args.show]:
            print(f"      {ticker:8s} {before!r} -> {after!r}")
        if len(hits) > args.show:
            print(f"      ... and {len(hits) - args.show} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
