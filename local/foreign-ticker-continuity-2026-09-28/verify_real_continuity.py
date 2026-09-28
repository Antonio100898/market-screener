"""Verify exact later-SEC ticker continuity against retained real filings."""
import hashlib
import json

from screener import evidence, store, sync
from screener.sources.edgar import EdgarClient


CASES = {
    "BRNX": ("0001901215", "Nasdaq", "2026-04-01", "0001213900-26-034046"),
    "ZTG": ("0002011458", "Nasdaq", "2026-01-20", "0001493152-26-002776"),
}


def main():
    conn = store.connect()
    edgar = EdgarClient()
    loader = evidence.EvidenceLoader(conn, edgar)
    output = []
    for ticker, (cik, exchange, annual_filed, annual_accn) in CASES.items():
        prior = [dict(row) for row in conn.execute(
            """SELECT symbol, accn, title, ratio, basis_accn
               FROM security_cover_observation WHERE cik = ? AND accn = ?""",
            (cik, annual_accn),
        )]
        proved = sync._later_ticker_continuity(
            edgar, cik, ticker, exchange, annual_filed, prior
        )
        if proved is None:
            raise AssertionError(f"{ticker}: no exact continuity evidence")
        security, later_accn, later_filed, path = proved
        facts_path = edgar.cache_dir / f"companyfacts_{cik}.json"
        facts = json.loads(facts_path.read_text())
        status, snapshot = sync._derive_evidence(loader.load(cik, ticker, facts))
        if status != "ok" or not snapshot or snapshot.get("ticker") != ticker:
            raise AssertionError(f"{ticker}: continuity did not reach a supported snapshot")
        output.append({
            "ticker": ticker,
            "cik": cik,
            "annual_accession": annual_accn,
            "annual_symbol": prior[0]["symbol"],
            "class_title": security["title"],
            "exchange": security["exchange"],
            "later_accession": later_accn,
            "later_filed": later_filed,
            "later_path": str(path),
            "later_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "snapshot_status": status,
            "snapshot_ticker": snapshot["ticker"],
            "balance_sheet_date": snapshot["balance_sheet_date"],
            "verdict": snapshot["verdict"],
        })
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
