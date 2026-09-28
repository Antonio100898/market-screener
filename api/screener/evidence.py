"""Assemble every local input required to normalize one filer.

Fetching and interpretation remain separate: callers may supply freshly fetched
Company Facts, while this layer consistently adds the slower-moving evidence
kept beside it (DERA dimensions and the filing-cover security description).
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from . import store
from .sources import cover, dera


@dataclass(frozen=True)
class EvidenceBundle:
    cik: str
    ticker: str
    facts: dict
    dimensioned: dict | None
    receipt: dict | None


class EvidenceLoader:
    """One evidence policy shared by every snapshot-producing workflow."""

    def __init__(self, conn, edgar):
        self.edgar = edgar
        # SEC's current ticker file intentionally omits a delisted security, but
        # its cached facts and cover still belong to the last symbol this database
        # knew.  A caller with no current mapping must not replace that security
        # identity with the CIK: share-class selection and cover matching both use
        # the symbol.  Export decides separately whether the security is tradable.
        self._stored_tickers = {
            row["cik"]: row["ticker"]
            for row in conn.execute(
                "SELECT cik, ticker FROM company WHERE ticker IS NOT NULL"
            )
        }
        covers = store.covers_by_cik(conn)
        self._covers_by_cik = covers
        self._covers = {
            (cik, security["symbol"]): security
            for cik, securities in covers.items()
            for security in securities
        }

    def identity(self, cik: str, ticker: str | None) -> tuple[str, dict | None]:
        """Resolve the priced security without reading the large fact files.

        Bulk derivation can do this small lookup in the parent process, then send
        only immutable identity data to process workers.
        """
        ticker = ticker or self._stored_tickers.get(cik) or cik
        receipt = self._covers.get((cik, ticker))
        if receipt is None:
            matches = [
                security for security in self._covers_by_cik.get(cik, ())
                if cover.symbol_matches(
                    security.get("symbol") or "", security.get("title") or "", ticker
                )
            ]
            receipt = matches[0] if len(matches) == 1 else None
        title = (receipt or {}).get("title") or ""
        # Parser bugs used to let a note/debt row overwrite the common cover
        # (HON/PPG), or an unlisted starred ordinary row overwrite the ADS (LX).
        # Those cached descriptions contradict the security being priced; they
        # are missing evidence, not authority to use the wrong share basis.
        if receipt and title and (not cover.is_common_equity_security(title)
                                  or cover.is_untraded_underlying(title)):
            receipt = None
        if receipt and not receipt.get("ratio"):
            # Parser improvements must apply to the title already preserved in
            # SQLite; repairing code may not refetch immutable filings. AMBO's
            # stored title says one ADS represents twenty ordinary shares, but an
            # older grammar missed the parenthetical wording and persisted NULL.
            inferred = cover.depositary_ratio(receipt.get("title") or "")
            if inferred:
                receipt = {**receipt, "ratio": str(inferred)}
        return ticker, receipt

    def load(self, cik: str, ticker: str | None,
             facts: dict | None = None) -> EvidenceBundle:
        if facts is None:
            path = self.edgar.cache_dir / f"companyfacts_{cik}.json"
            facts = json.loads(path.read_text()) if path.exists() else self.edgar.company_facts(cik)
        ticker, receipt = self.identity(cik, ticker)
        return EvidenceBundle(
            cik=cik,
            ticker=ticker,
            facts=facts,
            dimensioned=dera.load_sidecar(self.edgar.cache_dir, cik),
            receipt=receipt,
        )
