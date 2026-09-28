"""Assemble every local input required to normalize one filer.

Fetching and interpretation remain separate: callers may supply freshly fetched
Company Facts, while this layer consistently adds the slower-moving evidence
kept beside it (DERA dimensions and the filing-cover security description).
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from . import store
from .sources import cover, dera, inline_xbrl


@dataclass(frozen=True)
class EvidenceBundle:
    cik: str
    ticker: str
    facts: dict
    dimensioned: dict | None
    receipt: dict | None
    supplement_state: str | None = None


def continuity_security(
    prior_securities: list[dict], document: str, ticker: str, exchange: str | None
) -> dict | None:
    """Resolve one later symbol to one exact earlier filed class.

    A listing sentence may shorten the class name only when the same filing also
    contains the full annual title and exactly one supported prior class matches.
    """
    expected_exchange = (exchange or "").casefold()
    if expected_exchange not in {"nasdaq", "nyse", "cboe"}:
        return None

    filed_text = cover.normalized_title(cover.text_of(document))
    matches: dict[tuple[str, str], dict] = {}
    for listed in cover.listed_securities(document):
        if listed["symbol"] != ticker or listed["exchange"].casefold() != expected_exchange:
            continue
        listed_title = cover.normalized_title(listed["title"])
        for prior in prior_securities:
            prior_title = cover.normalized_title(prior.get("title") or "")
            same_reference = (
                prior_title == listed_title
                or prior_title.startswith(f"{listed_title},")
                or prior_title.startswith(f"{listed_title} ")
            )
            if (not prior_title or prior_title not in filed_text or not same_reference
                    or not cover.is_common_equity_security(prior.get("title") or "")
                    or cover.is_untraded_underlying(prior.get("title") or "")):
                continue
            matches[(prior.get("symbol") or "", prior_title)] = prior

    if len(matches) != 1:
        return None
    prior = next(iter(matches.values()))
    return {
        **prior,
        "previous_symbol": prior.get("symbol"),
        "symbol": ticker,
        "exchange": exchange,
        "ratio": prior.get("ratio"),
        "basis_accn": prior.get("basis_accn") or prior.get("accn"),
    }


class EvidenceLoader:
    """One evidence policy shared by every snapshot-producing workflow."""

    def __init__(self, conn, edgar, *, inline_cache_dir=None, inline_retained_root=None):
        self.edgar = edgar
        self._inline_cache_dir = inline_cache_dir or edgar.cache_dir
        self._inline_retained_root = inline_retained_root
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
        facts, supplement_state = inline_xbrl.load_retained_supplement(
            self._inline_cache_dir, cik, facts,
            retained_root=self._inline_retained_root,
        )
        ticker, receipt = self.identity(cik, ticker)
        return EvidenceBundle(
            cik=cik,
            ticker=ticker,
            facts=facts,
            dimensioned=dera.load_sidecar(self.edgar.cache_dir, cik),
            receipt=receipt,
            supplement_state=supplement_state,
        )
