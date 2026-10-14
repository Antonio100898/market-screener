"""Assemble every local input required to normalize one filer.

Fetching and interpretation remain separate: callers may supply freshly fetched
Company Facts, while this layer consistently adds the slower-moving evidence
kept beside it (DERA dimensions and the filing-cover security description).
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Mapping

from . import store
from .models import (AdmittedPrimaryInput, CapturedSource, PrimaryInputMapping, RawFactOrigin,
                     UnavailablePrimaryInput)
from .sources import cover, dera, inline_xbrl, sec_json
from .sources.edinet_mapper import rebuild_companyfacts as rebuild_edinet_companyfacts


@dataclass(frozen=True)
class EvidenceBundle:
    cik: str
    ticker: str
    facts: dict
    dimensioned: dict | None
    receipt: dict | None
    supplement_state: str | None = None
    captured_sources: tuple[CapturedSource, ...] = ()
    original_company_facts: dict | None = None
    primary_inputs: tuple[AdmittedPrimaryInput | UnavailablePrimaryInput, ...] = field(default=(), repr=False)


def admit_primary_inputs(
    cik: str, mappings: tuple[PrimaryInputMapping, ...], payloads: Mapping[str, bytes],
) -> tuple[AdmittedPrimaryInput | UnavailablePrimaryInput, ...]:
    """Copy verified private inputs without altering Company Facts or choosing facts."""
    if not mappings:
        return ()
    return _admit_primary_inputs(cik, mappings, payloads, {})


def decode_and_admit_primary_inputs(cik, mappings, payloads, *, company_facts_source):
    """Own one fresh raw root and copied admission; accept no predecoded tree."""
    _validate_primary_company_facts_source(company_facts_source)
    original_facts = sec_json.decode_company_facts(
        payloads.get(company_facts_source.artifact_sha256), source=company_facts_source)
    issuer = original_facts.get("cik")
    if (type(issuer) not in {str, int}
            or inline_xbrl._cik(str(issuer)) != inline_xbrl._cik(cik)):
        raise ValueError("primary Company Facts root issuer mismatch")
    outcomes = _admit_primary_inputs(cik, mappings, payloads, {company_facts_source: original_facts})
    return original_facts, outcomes


def _admit_primary_inputs(cik, mappings, payloads, decoded_targets):
    from .figure_history import ActionEvidence, DocumentEvidence, ShareBasis, decimal_text

    results = []
    identities = [(m.original.source.artifact_sha256, m.original.location) for m in mappings]
    keys = [m.key for m in mappings]
    targets, target_errors = [], []
    for mapping in mappings:
        try:
            target = mapping.target_origin
            if target is not None:
                _validate_primary_target(target)
            targets.append(None if target is None else (target.source.artifact_sha256, target.pointer))
            target_errors.append(None)
        except ValueError as exc:
            targets.append(None)
            target_errors.append(str(exc))
    for mapping, identity, target_identity, target_error in zip(mappings, identities, targets, target_errors, strict=True):
        try:
            if target_error is not None:
                raise ValueError(target_error)
            if target_identity is not None:
                if targets.count(target_identity) != 1:
                    raise ValueError("duplicate primary target artifact/pointer")
                target = mapping.target_origin
                if target.source not in decoded_targets:
                    decoded_targets[target.source] = sec_json.decode_company_facts(
                        payloads.get(target.source.artifact_sha256), source=target.source)
                original_facts = decoded_targets[target.source]
                issuer = original_facts.get("cik")
                if (type(issuer) not in {str, int}
                        or inline_xbrl._cik(str(issuer)) != inline_xbrl._cik(cik)):
                    raise ValueError("primary target body issuer mismatch")
                sec_json.verify_origin(target, original_facts)
            if (not mapping.key.strip() or keys.count(mapping.key) != 1
                    or identities.count(identity) != 1):
                raise ValueError("duplicate or missing primary mapping identity")
            if (mapping.canonical_namespace not in {"us-gaap", "ifrs-full"}
                    or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", mapping.canonical_tag)):
                raise ValueError("invalid declared canonical role")
            if inline_xbrl._cik(mapping.original.source.cik) != inline_xbrl._cik(cik):
                raise ValueError("primary mapping issuer mismatch")
            if mapping.replacement is not None and mapping.unary_factor_text is not None:
                raise ValueError("replacement and unary correction must be separate")
            if mapping.unary_factor_text is not None and Decimal(decimal_text(mapping.unary_factor_text)) == 0:
                raise ValueError("unary correction requires exact nonzero factor")
            documents = tuple(DocumentEvidence.model_validate(d.model_dump()) for d in mapping.documents)
            actions = tuple(ActionEvidence.model_validate(a.model_dump()) for a in mapping.actions)
            basis = None if mapping.share_basis is None else ShareBasis.model_validate(mapping.share_basis.model_dump())
            by_key = {d.key: d for d in documents}
            if len(by_key) != len(documents) or len({a.key for a in actions}) != len(actions):
                raise ValueError("duplicate primary evidence identity")
            if len({a.legal_identity for a in actions}) != len(actions):
                raise ValueError("repeated disclosure is not another legal action")
            if not any("CORRECTION_EXPLANATION" in d.roles for d in documents):
                raise ValueError("declared role/correction lacks documentary evidence")
            for document in documents:
                reference, span = document.reference, document.reference.location
                if reference.source_system != "SEC" or inline_xbrl._cik(reference.issuer_source_identifier) != inline_xbrl._cik(cik):
                    raise ValueError("primary evidence issuer mismatch")
                body = payloads.get(reference.artifact_sha256)
                if (type(body) is not bytes or hashlib.sha256(body).hexdigest() != reference.artifact_sha256
                        or span.end_byte > len(body)
                        or hashlib.sha256(body[span.start_byte:span.end_byte]).hexdigest() != span.span_sha256):
                    raise ValueError("primary documentary bytes/span mismatch")
                body[span.start_byte:span.end_byte].decode(span.encoding, errors="strict")
                parsed = inline_xbrl.urlparse(reference.source_document)
                if (parsed.scheme != "https" or parsed.netloc != "www.sec.gov" or parsed.query or parsed.fragment
                        or not re.fullmatch(rf"/Archives/edgar/data/{int(cik)}/[0-9]{{18}}/[A-Za-z0-9_.-]+", parsed.path)):
                    raise ValueError("primary evidence document binding mismatch")
            for action in actions:
                if any(k not in by_key or "LEGAL_ACTION" not in by_key[k].roles for k in action.evidence_keys):
                    raise ValueError("legal action lacks actual documentary witness")
            if basis is not None:
                by_action = {a.key: a for a in actions}
                if (any(k not in by_key or "INCORPORATED_BASIS" not in by_key[k].roles for k in basis.evidence_keys)
                        or any(k not in by_action or by_action[k].security_class != basis.security_class
                               for k in basis.incorporated_actions)):
                    raise ValueError("share basis lacks actual class/action witnesses")
            original = inline_xbrl.read_native_numeric(payloads.get(mapping.original.source.artifact_sha256), mapping.original)
            replacement = None
            if mapping.replacement is not None:
                replacement = inline_xbrl.read_native_numeric(payloads.get(mapping.replacement.source.artifact_sha256), mapping.replacement)
                if (original.request.context != replacement.request.context
                        or original.request.unit_numerator != replacement.request.unit_numerator
                        or original.request.unit_denominator != replacement.request.unit_denominator):
                    raise ValueError("replacement context/unit mismatch")
            results.append(AdmittedPrimaryInput(copy.deepcopy(mapping), original, replacement))
        except (ValueError, UnicodeError) as exc:
            results.append(UnavailablePrimaryInput(mapping.key, mapping.canonical_namespace,
                                                   mapping.canonical_tag, str(exc),
                                                   copy.deepcopy(mapping.original),
                                                   copy.deepcopy(mapping.target_origin)))
    return tuple(results)


def _validate_primary_target(target: RawFactOrigin) -> None:
    if (not isinstance(target, RawFactOrigin) or not isinstance(target.source, CapturedSource)
            or any(type(value) is not str or not value for value in
                   (target.pointer, target.lexical_text, target.namespace, target.concept, target.unit))
            or any(value is not None and type(value) is not str for value in
                   (target.start, target.end, target.accession, target.form, target.filed))):
        raise ValueError("primary target requires immutable exact origin fields")
    _validate_primary_company_facts_source(target.source)


def _validate_primary_company_facts_source(source):
    if (not isinstance(source, CapturedSource)
            or any(type(value) is not str for value in
            (source.role, source.artifact_sha256, source.source_document, source.fidelity))
            or source.role != "company_facts"
            or not re.fullmatch(r"[0-9a-f]{64}", source.artifact_sha256)
            or not source.source_document.strip()
            or source.fidelity not in {"EXACT_LEXICAL", "DECODED_DECIMAL", "UNKNOWN"}
            or any(type(value) is not int or value <= 0 for value in
                   (source.source_item_id, source.source_observation_id, source.occurrence_id))):
        raise ValueError("primary target requires exact captured delivery identity")


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
             facts: dict | None = None, *, primary_mappings: tuple[PrimaryInputMapping, ...] = (),
             primary_payloads: Mapping[str, bytes] | None = None) -> EvidenceBundle:
        if facts is None:
            path = self.edgar.cache_dir / f"companyfacts_{cik}.json"
            facts = json.loads(path.read_text()) if path.exists() else self.edgar.company_facts(cik)
        facts = rebuild_edinet_companyfacts(
            facts, lambda document: (self.edgar.cache_dir / "edinet" / f"{document}.zip").read_bytes(),
        )
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
            primary_inputs=admit_primary_inputs(cik, primary_mappings, primary_payloads or {}),
        )
