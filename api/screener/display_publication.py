"""Capture caller-enriched display inputs before the SQL publication transaction.

This boundary verifies identities, captured bytes and output consistency. It does
not certify financial derivation: the existing engine and financial regression
gate still own that proof. No source or mutable market reads occur at query time.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from math import isfinite
from typing import Mapping, Sequence

from sqlalchemy import select

from .artifacts import SqlArtifactRepository, store_evidence
from .company_projection import PROJECTION_REVISION, project_companies
from .company_query_schema import release_company_projection
from .complete_accounting import CompleteAccounting, load_accounting
from .postgres import company_snapshot, company_snapshot_artifact, issuer, priced_security


# Exact captured output fields. Raw provider inputs can also be retained in
# each capture's input_artifacts; they are never called filed observations.
CAPTURE_FIELDS = {
    "quote": ("price", "price_asof", "quote_currency", "price_source", "price_session", "market_state", "market_timezone", "market_state_asof", "quote_refresh_warning"),
    "fx": ("fx", "valuation_fx", "price_reporting_currency", "fx_warning"),
    "history": ("price_stats", "price_history_warning", "annual_ratios", "ttm_eps_vintage"),
    "metadata": ("name", "sector", "industry", "exchange", "graham_profile", "incorporation", "currency", "reporting_currency"),
    "filing_metadata": ("filing_events", "latest_quarterly_filing", "events_from", "last_filing"),
    "index": ("index_memberships",),
}


@dataclass(frozen=True)
class DisplayCompanyInput:
    security_id: int
    snapshot_id: int
    strict_row: Mapping
    captures: Mapping[str, Mapping]
    source_observation_ids: tuple[int, ...] = ()
    assumed_row: Mapping | None = None
    assumption_unavailable_reason: str = "Pinned assumption inputs have not been assembled"
    exact_bundle_id: int | None = None
    expected_exact_run_key: str | None = None


@dataclass(frozen=True)
class PreparedDisplayRelease:
    """Only immutable JSON bytes cross from preparation into publication."""
    projection_revision: int
    input_bundle_sha256: str
    manifest_bytes: bytes
    rows_bytes: bytes


def canonical_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def capture_fields(role, row):
    return {key: row[key] for key in CAPTURE_FIELDS[role] if key in row}


def _gap(row):
    screen = (row.get("alignment") or {}).get("enterprising")
    if not screen or row["profile"] != "OPERATING":
        return "OUT_OF_SCOPE"
    if (screen.get("unknown") or 0) > 0 or screen.get("verdict") == "EVIDENCE_INCOMPLETE":
        return "INCOMPLETE"
    gaps = (screen.get("total") or 0) - (screen.get("passed") or 0)
    return {0: "ALL_PASS", 1: "ONE_GAP", 2: "TWO_GAPS"}.get(gaps, "THREE_PLUS")


def typed_projection(row, *, ordinal, security_id, input_bundle_sha256, assumed_row=None):
    """Query columns come only from the accepted projected row."""
    alignment = row.get("alignment") or {}
    values = dict(security_id=security_id, projection_revision=PROJECTION_REVISION,
                  enriched_payload=row, assumed_payload=assumed_row,
                  assumption_status="APPLIED" if assumed_row is not None else "UNAVAILABLE",
                  input_bundle_sha256=input_bundle_sha256, universe_ordinal=ordinal,
                  ticker=row["ticker"], name=row.get("name") or "", sector=row.get("sector"), exchange=row.get("exchange"),
                  profile=row["profile"], search_hay=row["hay"], index_memberships=row["idx"],
                  enterprising_verdict=(alignment.get("enterprising") or {}).get("verdict"),
                  defensive_verdict=(alignment.get("defensive") or {}).get("verdict"),
                  enterprising_gap=_gap(row), criteria_passed=sum(c["status"] == "PASS" for c in row.get("criteria", ())),
                  positive_eps_years=(row["eps10"] or {}).get("positive"), unjudged=row["unjudged"],
                  inapplicable=row["inapplicable"], net_net=row["netNet"], market_cap=row["mcap"], price=row.get("price"),
                  off_high=(row.get("price_stats") or {}).get("pct_below_52w_high"), pe=row["pe"], pe3=row["pe3"],
                  dividend_yield=row["dividendYield"], return_quality=row["returnQuality"]["score"])
    for key in ("market_cap", "price", "off_high", "pe", "pe3", "dividend_yield", "return_quality"):
        value = values[key]
        if value is not None and (type(value) not in (int, float) or not isfinite(value)):
            raise ValueError(f"nonfinite or nonnumeric display field: {key}")
    if "n_pass" in row and row["n_pass"] != values["criteria_passed"]:
        raise ValueError("strict criteria count does not match row")
    if not isinstance(row["ticker"], str) or row["ticker"] != row["ticker"].upper() or not row["ticker"]:
        raise ValueError("display ticker must be an exact uppercase identity")
    if not isinstance(values["index_memberships"], list) or any(not isinstance(value, str) for value in values["index_memberships"]):
        raise ValueError("index memberships must be strings")
    return values


class DisplayReleasePreparer:
    def __init__(self, engine, object_store):
        self.engine = engine
        self.object_store = object_store
        self.artifacts = SqlArtifactRepository(engine)

    def _read(self, artifact_hash):
        artifact = self.artifacts.get(artifact_hash)
        return self.object_store.read_verified(artifact.object_key, artifact.content_sha256, artifact.byte_size)

    def _store(self, value):
        return store_evidence(canonical_bytes(value), "application/json", self.object_store, self.artifacts).content_sha256

    def record_accounting(self, accounting: CompleteAccounting) -> str:
        """Retain diagnostic accounting without creating or activating a release."""
        accounting = self._verify_accounting(accounting)
        return self._store(dict(kind="COMPLETE_ACCOUNTING_DIAGNOSTIC", accounting=accounting.model_dump(mode="json")))

    def _verify_accounting(self, accounting):
        if not isinstance(accounting, CompleteAccounting):
            raise ValueError("complete accounting requires the typed contract")
        accounting = load_accounting(accounting.model_dump_json().encode())
        for artifact in sorted({digest for item in accounting.universe for digest in item.artifact_sha256s}):
            self._read(artifact)
        return accounting

    def prepare_complete(self, companies: Sequence[DisplayCompanyInput], *, accounting: CompleteAccounting,
                         capture_mode: str, limitations: Sequence[str]) -> PreparedDisplayRelease:
        accounting = self._verify_accounting(accounting)
        accounting.validate_display([dict(security_id=c.security_id, snapshot_id=c.snapshot_id,
            exact_bundle_id=c.exact_bundle_id) for c in companies])
        return self.prepare(companies, capture_mode=capture_mode, limitations=limitations, _accounting=accounting)

    def prepare(self, companies: Sequence[DisplayCompanyInput], *, capture_mode: str, limitations: Sequence[str],
                _accounting: CompleteAccounting | None = None) -> PreparedDisplayRelease:
        if capture_mode not in ("caller_captured_enrichment", "regression_import"):
            raise ValueError("unsupported display capture mode")
        if not companies or not limitations or any(not isinstance(item, str) or not item.strip() for item in limitations):
            raise ValueError("display preparation requires a cohort and explicit financial verification limitations")
        # Copy first: caller mutation cannot change retained rows after preparation.
        strict = json.loads(canonical_bytes([company.strict_row for company in companies]))
        projections = project_companies(strict)
        rows, entries, tickers, ciks, securities = [], [], set(), set(), set()
        for ordinal, (company, row, projection) in enumerate(zip(companies, strict, projections)):
            with self.engine.connect() as connection:
                snapshot = connection.execute(select(company_snapshot, issuer.c.source_system, issuer.c.source_identifier).join(
                    priced_security, priced_security.c.security_id == company_snapshot.c.security_id).join(issuer).where(
                    company_snapshot.c.snapshot_id == company.snapshot_id,
                    company_snapshot.c.security_id == company.security_id)).mappings().one_or_none()
                source_artifacts = [dict(artifact) for artifact in connection.execute(select(
                    company_snapshot_artifact.c.content_sha256, company_snapshot_artifact.c.role,
                ).where(company_snapshot_artifact.c.snapshot_id == company.snapshot_id).order_by(
                    company_snapshot_artifact.c.role, company_snapshot_artifact.c.content_sha256,
                )).mappings()]
            if snapshot is None or row.get("ticker") != snapshot["ticker"]:
                raise ValueError("display input must identify its exact canonical snapshot/security/ticker")
            from .company_availability import validate_unavailable_row, validate_source_rows
            unavailable = validate_unavailable_row(row)
            if unavailable is not None:
                assessment, diagnostic_sha256 = unavailable
                if row != snapshot["canonical_payload"] or company.assumed_row is not None:
                    raise ValueError("unavailable display must keep its exact diagnosis without assumptions")
                if json.loads(self._read(diagnostic_sha256)) != assessment.json_record():
                    raise ValueError("unavailable diagnostic artifact does not match the stored assessment")
                from .publication import PublicationRepository
                with self.engine.connect() as connection:
                    actual = PublicationRepository._source_rows(connection, company.source_observation_ids)
                validate_source_rows(assessment, diagnostic_sha256, actual)
            sources = tuple(sorted(set(company.source_observation_ids)))
            binding = None
            if company.exact_bundle_id is None and company.expected_exact_run_key is not None:
                raise ValueError("expected_exact_run_key requires exact_bundle_id")
            if company.exact_bundle_id is not None:
                from .figure_history import FigureHistoryRepository
                with self.engine.connect() as connection:
                    binding = FigureHistoryRepository(self.engine).validate_publication_binding_on_connection(connection,
                        company.exact_bundle_id, security_id=company.security_id, snapshot_id=company.snapshot_id,
                        expected_run_key=company.expected_exact_run_key)
                sources = tuple(sorted(set(sources) | set(binding["source_observation_ids"])))
            if sources or capture_mode == "caller_captured_enrichment":
                from .publication import PublicationRepository
                with self.engine.connect() as connection:
                    sources = PublicationRepository(self.engine)._bound_dependencies(connection, sources, company.security_id, company.snapshot_id)
            for artifact in source_artifacts:
                self._read(artifact["content_sha256"])
            cik = row.get("cik")
            canonical_cik = snapshot["canonical_payload"].get("cik")
            if canonical_cik is None and snapshot["source_system"] == "SEC":
                canonical_cik = snapshot["source_identifier"]
            if cik != canonical_cik or not cik:
                raise ValueError("display CIK must match canonical issuer identity")
            if row["ticker"] in tickers or cik in ciks or company.security_id in securities:
                raise ValueError("duplicate display ticker, CIK or security")
            tickers.add(row["ticker"]); ciks.add(cik); securities.add(company.security_id)
            if set(company.captures) != set(CAPTURE_FIELDS):
                raise ValueError("every capture role must be present or explicitly absent")
            captures = json.loads(canonical_bytes(company.captures))
            for role, capture in captures.items():
                expected = capture_fields(role, row)
                if capture.get("status") == "ABSENT":
                    if not capture.get("reason") or any(value not in (None, [], {}) for value in expected.values()):
                        raise ValueError(f"absent {role} capture contradicts retained display fields")
                elif capture.get("status") == "CAPTURED":
                    content = json.loads(self._read(capture.get("artifact_sha256")))
                    if content.get("role") != role or content.get("row_fields") != expected:
                        raise ValueError(f"{role} capture does not match display inputs")
                    if not content.get("provider") or not content.get("captured_at"):
                        raise ValueError("captured inputs require provider and capture time")
                    inputs = content.get("input_artifacts", [])
                    if capture_mode == "caller_captured_enrichment" and not inputs:
                        raise ValueError("caller captures require retained derivation input bytes")
                    for artifact_hash in inputs:
                        self._read(artifact_hash)
                else:
                    raise ValueError("unsupported input capture status")
            enriched = {**row, **projection}
            input_status = dict(capture_mode=capture_mode, financial_verification="REQUIRES_EXISTING_ENGINE_REGRESSION_GATE", limitations=list(limitations))
            enriched["publication_inputs"] = input_status
            assumed = None
            if company.assumed_row is not None:
                assumed = json.loads(canonical_bytes(company.assumed_row))
                for role in CAPTURE_FIELDS:
                    if role != "history" and capture_fields(role, assumed) != capture_fields(role, row):
                        raise ValueError("assumed view must use the same captured market and metadata inputs")
                if assumed.get("ticker") != row["ticker"] or assumed.get("cik") != cik or (assumed.get("assumption_mode") or {}).get("status") != "APPLIED":
                    raise ValueError("assumed view requires pinned identity and explicit APPLIED disclosure")
                assumed["publication_inputs"] = input_status
            elif not company.assumption_unavailable_reason.strip():
                raise ValueError("unavailable assumed view requires a reason")
            entry = dict(security_id=company.security_id, snapshot_id=company.snapshot_id,
                         snapshot_sha256=snapshot["snapshot_sha256"], canonical_payload_sha256=snapshot["payload_sha256"],
                         canonical_input_artifacts=source_artifacts,
                         strict_row_sha256=digest(enriched), assumed_row_sha256=digest(assumed) if assumed is not None else None,
                         assumption_status="APPLIED" if assumed is not None else "UNAVAILABLE",
                         assumption_unavailable_reason=company.assumption_unavailable_reason if assumed is None else None,
                         source_observation_ids=list(sources), captures=captures,
                         capture_mode=capture_mode, limitations=list(limitations))
            if binding is not None:
                entry.update({key: binding[key] for key in ("exact_bundle_id", "exact_bundle_sha256", "expected_exact_run_key")})
            bundle = self._store(entry)
            entries.append({**entry, "input_bundle_sha256": bundle})
            rows.append(typed_projection(enriched, ordinal=ordinal, security_id=company.security_id, input_bundle_sha256=bundle, assumed_row=assumed))
        manifest = dict(contract_revision=2 if any(company.exact_bundle_id is not None for company in companies) else 1, projection_revision=PROJECTION_REVISION,
                        financial_verification="REQUIRES_EXISTING_ENGINE_REGRESSION_GATE", members=entries)
        if _accounting is not None:
            _accounting.validate_artifacts(entries)
            manifest.update(contract_revision=3, complete_accounting=_accounting.model_dump(mode="json"))
        manifest_bytes = canonical_bytes(manifest)
        return PreparedDisplayRelease(PROJECTION_REVISION, self._store(manifest), manifest_bytes, canonical_bytes(rows))

    def verify(self, prepared: PreparedDisplayRelease):
        if prepared.projection_revision != PROJECTION_REVISION or self._read(prepared.input_bundle_sha256) != prepared.manifest_bytes:
            raise ValueError("display release manifest/revision does not match verified bytes")
        manifest, rows = json.loads(prepared.manifest_bytes), json.loads(prepared.rows_bytes)
        if manifest.get("contract_revision") not in (1, 2, 3) or manifest.get("projection_revision") != PROJECTION_REVISION:
            raise ValueError("unsupported display manifest contract")
        if manifest["contract_revision"] == 3:
            from .sources.sec_json import decode_object
            manifest = decode_object(prepared.manifest_bytes, "complete display manifest")
            rows = decode_object(b'{"rows":' + prepared.rows_bytes + b'}', "complete display rows")["rows"]
            if any(type(value) is not int for value in (manifest["contract_revision"],
                    manifest["projection_revision"], prepared.projection_revision)):
                raise ValueError("complete display revisions require exact integers")
            accounting = self._verify_accounting(load_accounting(canonical_bytes(manifest["complete_accounting"])))
            accounting.validate_display(manifest["members"])
            accounting.validate_artifacts(manifest["members"])
            for entry in manifest["members"]:
                if any(type(value) is not int or not 0 < value <= 2**63 - 1
                       for value in entry["source_observation_ids"]):
                    raise ValueError("complete display source identities require positive exact integers")
            for ordinal, row in enumerate(rows):
                if (type(row["security_id"]) is not int or type(row["projection_revision"]) is not int
                        or type(row["universe_ordinal"]) is not int or row["universe_ordinal"] != ordinal):
                    raise ValueError("complete display row identities require exact integers")
        elif "complete_accounting" in manifest:
            raise ValueError("complete accounting requires its named display contract")
        members = manifest["members"]
        if len(rows) != len(members) or not rows:
            raise ValueError("display projection coverage differs from manifest")
        for ordinal, (entry, row) in enumerate(zip(members, rows)):
            keys = {"exact_bundle_id", "exact_bundle_sha256", "expected_exact_run_key"}
            present = keys.intersection(entry)
            if (manifest["contract_revision"] == 1 and present) or (present and present != keys):
                raise ValueError("display manifest exact binding contract differs")
            if present:
                from .figure_history import FigureHistoryRepository
                with self.engine.connect() as connection:
                    identity = FigureHistoryRepository.publication_identity_on_connection(connection, entry["exact_bundle_id"],
                        security_id=entry["security_id"], snapshot_id=entry["snapshot_id"], expected_run_key=entry["expected_exact_run_key"])
                if identity["exact_bundle_sha256"] != entry["exact_bundle_sha256"]:
                    raise ValueError("display exact graph hash differs from sealed binding")
            for artifact in entry["canonical_input_artifacts"]:
                self._read(artifact["content_sha256"])
            bundle = row["input_bundle_sha256"]
            if bundle != entry["input_bundle_sha256"] or json.loads(self._read(bundle)) != {key: value for key, value in entry.items() if key != "input_bundle_sha256"}:
                raise ValueError("company input manifest differs from retained bytes")
            if digest(row["enriched_payload"]) != entry["strict_row_sha256"] or (digest(row["assumed_payload"]) if row["assumed_payload"] is not None else None) != entry["assumed_row_sha256"]:
                raise ValueError("display rows differ from retained hashes")
            expected = typed_projection(row["enriched_payload"], ordinal=ordinal, security_id=entry["security_id"], input_bundle_sha256=bundle, assumed_row=row["assumed_payload"])
            if row != expected:
                raise ValueError("typed query values differ from the actual projected row")
            for capture in entry["captures"].values():
                if capture["status"] == "CAPTURED":
                    content = json.loads(self._read(capture["artifact_sha256"]))
                    for artifact_hash in content.get("input_artifacts", []):
                        self._read(artifact_hash)
        return manifest, rows

    @staticmethod
    def insert(connection, release_id, memberships, manifest, rows):
        actual = {(member["security_id"], member["snapshot_id"], member.get("exact_bundle_id")) for member in memberships}
        captured = {(member["security_id"], member["snapshot_id"], member.get("exact_bundle_id")) for member in manifest["members"]}
        if actual != captured or len(actual) != len(rows):
            raise ValueError("display release requires exactly one projection per selected snapshot")
        for entry in manifest["members"]:
            if entry.get("exact_bundle_id") is not None:
                from .figure_history import FigureHistoryRepository
                identity = FigureHistoryRepository(connection.engine).validate_publication_binding_on_connection(connection,
                    entry["exact_bundle_id"], security_id=entry["security_id"], snapshot_id=entry["snapshot_id"],
                    expected_run_key=entry.get("expected_exact_run_key"))
                if identity["exact_bundle_sha256"] != entry.get("exact_bundle_sha256"):
                    raise ValueError("display exact graph hash differs from sealed binding")
            snapshot = connection.execute(select(company_snapshot).where(company_snapshot.c.snapshot_id == entry["snapshot_id"])).mappings().one()
            if snapshot["snapshot_sha256"] != entry["snapshot_sha256"] or snapshot["payload_sha256"] != entry["canonical_payload_sha256"]:
                raise ValueError("canonical display selection changed")
        connection.execute(release_company_projection.insert(), [dict(release_id=release_id, **row) for row in rows])
