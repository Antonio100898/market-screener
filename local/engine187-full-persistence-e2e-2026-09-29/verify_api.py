#!/usr/bin/env python3
"""Verify the real Uvicorn company API against SQLite and PostgreSQL."""
from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.parse
import urllib.request

from screener import store
from screener.postgres import create_postgres_engine
from screener.shared_companies import SharedCompanyRepository


EXPECTED = (
    "AERO", "AGMB", "ALPS", "AUGO", "CNI", "DAVI", "GCDT", "GMTL",
    "HBNB", "PAYP", "PICS", "TMCR", "VMET", "YMAT", "6752.T", "7974.T",
)
EXCLUDED = ("AHNRF", "BRBI", "CIB", "NXAT")


def get(ticker: str) -> tuple[int, dict]:
    url = "http://127.0.0.1:8000/companies/" + urllib.parse.quote(ticker)
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def main() -> int:
    sqlite_connection = sqlite3.connect(
        f"file:{store.DEFAULT_DB.resolve()}?mode=ro", uri=True
    )
    sqlite_connection.row_factory = sqlite3.Row
    sqlite_connection.execute("PRAGMA query_only = ON")
    engine = create_postgres_engine()
    repository = SharedCompanyRepository(engine)
    try:
        expected_payloads = {
            row["ticker"]: json.loads(row["data"])
            for row in sqlite_connection.execute(
                """SELECT c.ticker, s.data FROM company c JOIN snapshot s USING (cik)
                   WHERE c.ticker IN ({})""".format(
                    ",".join("?" for _ in EXPECTED)
                ),
                EXPECTED,
            )
        }
        results = {}
        for ticker in EXPECTED:
            status, body = get(ticker)
            assert status == 200, (ticker, status, body)
            assert body["canonical_payload"] == expected_payloads[ticker]
            selected = repository.current_by_ticker(ticker)
            assert selected is not None
            assert body["revision"]["snapshot_id"] == selected.snapshot.snapshot_id
            assert body["revision"]["payload_sha256"] == selected.snapshot.payload_sha256
            assert body["artifacts"]
            results[ticker] = {
                "status": status,
                "sqlite_payload_equal": True,
                "postgres_revision_equal": True,
                "snapshot_id": selected.snapshot.snapshot_id,
                "artifact_count": len(body["artifacts"]),
            }
        cni_source = results["CNI"]
        _, cni_body = get("CNI")
        source = cni_body["canonical_payload"]["sources"]["total_assets"]
        assert source["form"] == "6-K" and source["annual_form"] == "40-F"
        cni_source["dual_provenance"] = {
            "source_form": source["form"],
            "source_accession": source["accn"],
            "annual_form": source["annual_form"],
            "annual_accession": source["annual_accn"],
        }
        for ticker in EXCLUDED:
            status, body = get(ticker)
            assert status == 404, (ticker, status, body)
            results[ticker] = {"status": status, "detail": body["detail"]}
        print(json.dumps(results, indent=2, sort_keys=True))
    finally:
        sqlite_connection.close()
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
