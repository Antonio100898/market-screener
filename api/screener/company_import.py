"""Import companies from retained SQLite evidence into shared storage."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Iterable, Mapping

from . import store, sync
from .artifacts import EvidenceArtifact, SqlArtifactRepository, store_evidence
from .evidence import EvidenceBundle, EvidenceLoader
from .object_store import ImmutableObjectStore, create_s3_client
from .postgres import create_postgres_engine
from .shared_companies import SharedCompanyRepository, SnapshotArtifact, StoredCompany
from .sources import cover, dera, inline_xbrl, edinet
from .sources.edinet_mapper import ADAPTER_KIND as EDINET_ADAPTER, rebuild_companyfacts
from .sources.edinet import EdinetError, EdinetInputCeilingError, validate_xbrl_archive
from .sources import ifrs_workbook
from .storage_config import StorageSettings


class CompanyImportError(RuntimeError):
    pass


class RetainedInputError(CompanyImportError):
    pass


class RetainedInputMissing(RetainedInputError):
    def __init__(self, path):
        super().__init__(f"missing retained input: {path}")
        self.input_name = Path(path).name


class RetainedInputCeiling(RetainedInputError):
    def __init__(self, reason, *, inputs=()):
        super().__init__(reason)
        self.inputs = tuple(inputs)


class DerivationError(CompanyImportError):
    pass


@dataclass(frozen=True)
class RetainedArtifact:
    role: str
    artifact: EvidenceArtifact


@dataclass(frozen=True)
class ImportedCompany:
    ticker: str
    issuer_source: str
    issuer_identifier: str
    security_identifier: str
    stored: StoredCompany
    artifacts: tuple[RetainedArtifact, ...]
    data_status: str = "AVAILABLE"
    unavailability_reason: str | None = None


@dataclass(frozen=True)
class ImportFailure:
    ticker: str
    reason: str


@dataclass(frozen=True)
class BulkImportSummary:
    imported: tuple[str, ...]
    reused: tuple[str, ...]
    failed: tuple[ImportFailure, ...]
    unavailable: tuple[ImportFailure, ...] = ()

    def as_dict(self) -> dict:
        return {
            "imported": list(self.imported),
            "reused": list(self.reused),
            "failed": [
                {"ticker": failure.ticker, "reason": failure.reason}
                for failure in self.failed
            ],
            **({"unavailable": [{"ticker": item.ticker, "reason": item.reason}
                                 for item in self.unavailable]} if self.unavailable else {}),
        }


@dataclass(frozen=True)
class _PreparedCompany:
    ticker: str
    issuer_source: str
    issuer_identifier: str
    security_identifier: str
    exchange_code: str | None
    quote_currency: str
    security_title: str | None
    source_accession: str | None
    security_basis: str
    receipt_ratio: Decimal | str | int | None
    bundle: EvidenceBundle
    artifacts: tuple[RetainedArtifact, ...]


class CompanyImporter:
    """Move retained evidence through the existing derivation and storage owners."""

    def __init__(
        self,
        sqlite_connection: sqlite3.Connection,
        cache_dir: str | Path,
        object_store: ImmutableObjectStore,
        artifact_repository: SqlArtifactRepository,
        company_repository: SharedCompanyRepository,
        derive: Callable[[EvidenceBundle], tuple[str, dict | None]] = sync._derive_evidence,
        *, company_adapter_declaration: str | Path | None = None,
    ):
        self.sqlite = sqlite_connection
        self.cache_dir = Path(cache_dir)
        self.objects = object_store
        self.artifacts = artifact_repository
        self.companies = company_repository
        self.derive = derive
        self.company_adapter_declaration = (
            Path(company_adapter_declaration) if company_adapter_declaration is not None else None)
        self._sec_ticker_mapping: tuple[dict, RetainedArtifact] | None = None
        self.loader = EvidenceLoader(
            sqlite_connection,
            SimpleNamespace(cache_dir=self.cache_dir),
        )

    def import_tickers(self, tickers: Iterable[str], *, preserve_unavailable: bool = False) -> list[ImportedCompany]:
        if type(preserve_unavailable) is not bool:
            raise TypeError("preserve_unavailable must be a boolean")
        requested = [ticker.strip() for ticker in tickers]
        if not requested or any(not ticker for ticker in requested):
            raise ValueError("at least one non-empty ticker is required")
        return [self.import_ticker(ticker, **({"preserve_unavailable": True} if preserve_unavailable else {})) for ticker in requested]

    def import_dashboard(self, *, preserve_unavailable: bool = False) -> BulkImportSummary:
        if type(preserve_unavailable) is not bool:
            raise TypeError("preserve_unavailable must be a boolean")
        imported = []
        reused = []
        failed = []
        unavailable = []
        for ticker in self._dashboard_tickers():
            try:
                result = self.import_ticker(ticker, **({"preserve_unavailable": True} if preserve_unavailable else {}))
            except Exception as exc:
                reason = str(exc) or type(exc).__name__
                failed.append(ImportFailure(ticker, reason))
            else:
                (imported if result.stored.created else reused).append(ticker)
                if getattr(result, "data_status", "AVAILABLE") == "UNAVAILABLE":
                    unavailable.append(ImportFailure(ticker, result.unavailability_reason))
        return BulkImportSummary(tuple(imported), tuple(reused), tuple(failed), tuple(unavailable))

    def import_ticker(self, ticker: str, *, preserve_unavailable: bool = False) -> ImportedCompany:
        if type(preserve_unavailable) is not bool:
            raise TypeError("preserve_unavailable must be a boolean")
        row = self._company(ticker)
        try:
            source = _issuer_source(str(row["cik"]))
            if source == "COMPANY_ADAPTER":
                prepared = self._prepare_company_adapter(row)
            elif source == "EDINET":
                prepared = self._prepare_edinet(row)
            else:
                prepared = self._prepare_sec(row)
        except RetainedInputMissing as error:
            if not preserve_unavailable:
                raise
            return self._store_unavailable(row, "LOCAL_INPUT_MISSING",
                f"Retained input {error.input_name} is missing locally; remote availability was not checked.")
        except RetainedInputCeiling as error:
            if not preserve_unavailable:
                raise
            return self._store_unavailable(row, "NORMALIZATION_UNSUPPORTED",
                f"Retained EDINET evidence exceeded a supported input ceiling: {error}. "
                "The filing was not fully validated; remote availability was not checked.",
                inputs=error.inputs)
        status, payload = self.derive(prepared.bundle)
        pending_row = status == "pending_facts" and payload is not None and (
            "cik" in payload or "ticker" in payload
        )
        if pending_row and (
            (payload.get("cik"), payload.get("ticker")) != (prepared.issuer_identifier, prepared.ticker)
            or not isinstance(payload.get("data_pending"), Mapping) or not payload["data_pending"]
        ):
            raise DerivationError(f"{prepared.ticker}: pending row identity or warning is invalid")
        if (status != "ok" and not pending_row) or payload is None:
            if preserve_unavailable and status in {"foreign", "pending_facts"}:
                return self._store_unavailable(row,
                    "FILING_FACTS_PENDING" if status == "pending_facts" else "NORMALIZATION_UNSUPPORTED",
                    "Retained evidence does not support a current normalized financial snapshot.",
                    inputs=prepared.artifacts)
            raise DerivationError(
                f"{prepared.ticker}: retained evidence derived as {status}, not ok"
            )
        stored = self.companies.store_and_select(
            issuer_source=prepared.issuer_source,
            issuer_identifier=prepared.issuer_identifier,
            security_identifier=prepared.security_identifier,
            ticker=prepared.ticker,
            exchange_code=prepared.exchange_code,
            quote_currency=prepared.quote_currency,
            security_title=prepared.security_title,
            source_accession=prepared.source_accession,
            security_basis=prepared.security_basis,
            receipt_ratio=prepared.receipt_ratio,
            engine_revision=store.ENGINE_VERSION,
            canonical_payload=payload,
            artifacts=(
                SnapshotArtifact(item.artifact.content_sha256, item.role)
                for item in prepared.artifacts
            ),
        )
        return ImportedCompany(
            ticker=prepared.ticker,
            issuer_source=prepared.issuer_source,
            issuer_identifier=prepared.issuer_identifier,
            security_identifier=prepared.security_identifier,
            stored=stored,
            artifacts=prepared.artifacts,
        )

    def import_source_unavailable(self, ticker: str, *, observation_ids: Iterable[int], reason: str) -> ImportedCompany:
        """Keep an actual captured unavailable result, without making a source request."""
        from .company_availability import REMOTE_KIND, validate_source_rows
        from .publication import PublicationRepository
        row = self._company(ticker)
        ids = tuple(observation_ids)
        assessment = self._unavailable_assessment(row, "REMOTE_SOURCE_UNAVAILABLE", reason,
            (), kind=REMOTE_KIND, observation_ids=ids)
        with self.companies.engine.connect() as connection:
            actual = PublicationRepository._source_rows(connection, ids)
        validate_source_rows(assessment, "0" * 64, actual)
        return self._store_unavailable(row, "REMOTE_SOURCE_UNAVAILABLE", reason,
            kind=REMOTE_KIND, observation_ids=ids)

    def record_unavailability(self, imported: ImportedCompany, lease, observations, *, item_outcome_key: str) -> tuple[int, ...]:
        """Bind a real local assessment to the existing source-generation ledger."""
        from .company_availability import (ARTIFACT_ROLE, REMOTE_KIND, load_assessment,
            local_observation_metadata, local_source_item, local_source_url)
        diagnostic = next(item.artifact for item in imported.artifacts if item.role == ARTIFACT_ROLE)
        raw = self.objects.read_verified(diagnostic.object_key, diagnostic.content_sha256, diagnostic.byte_size)
        assessment = load_assessment(json.loads(raw))
        if assessment.kind == REMOTE_KIND:
            return assessment.remote_observation_ids
        recorded = observations.record(lease, item=local_source_item(assessment),
            item_outcome_key=item_outcome_key, state="present",
            metadata=local_observation_metadata(assessment, diagnostic.content_sha256),
            source_url=local_source_url(assessment), artifact_sha256=diagnostic.content_sha256,
            detected_at=assessment.assessed_at)
        return (recorded.observation.source_observation_id,)

    @staticmethod
    def _unavailable_assessment(row, code, reason, inputs, *, kind=None, observation_ids=()):
        from .company_availability import LOCAL_KIND, load_assessment
        from .shared_companies import _issuer_values
        cik = str(row["cik"])
        identity = _issuer_values(_issuer_source(cik), cik)
        return load_assessment(dict(kind=kind or LOCAL_KIND,
            issuer_source=identity["source_system"],
            issuer_identifier=identity["source_identifier"], ticker=str(row["ticker"]).upper(), reason_code=code,
            reason=reason, assessed_at=datetime.now(timezone.utc).isoformat(),
            input_artifact_sha256s=sorted({item.artifact.content_sha256 for item in inputs}),
            remote_observation_ids=list(observation_ids)))

    def _store_unavailable(self, row, code, reason, *, inputs=(), kind=None, observation_ids=()):
        from .company_availability import ARTIFACT_ROLE, unavailable_row
        inputs = list(inputs)
        identity, identity_artifact = self._json_bytes(_canonical_json(dict(
            kind="RETAINED_COMPANY_IDENTITY", cik=str(row["cik"]), ticker=str(row["ticker"]),
            name=row["name"], exchange=row["exchange"])), "retained_company_identity")
        inputs.append(identity_artifact)
        # Remote IDs were checked before retaining the diagnostic, not inferred from a local read failure.
        assessment = self._unavailable_assessment(row, code, reason, inputs,
            kind=kind, observation_ids=observation_ids)
        _, diagnostic = self._json_bytes(_canonical_json(assessment.json_record()), ARTIFACT_ROLE)
        payload = unavailable_row(assessment, diagnostic.artifact.content_sha256,
            name=identity["name"], exchange=identity["exchange"])
        source, cik = assessment.issuer_source, assessment.issuer_identifier
        security = (f"company-adapter:{cik}:primary-ordinary" if source == "COMPANY_ADAPTER" else
                    f"edinet:{cik}:primary-ordinary" if source == "EDINET" else f"sec:{cik}:primary-listed-security")
        artifacts = (diagnostic, *inputs)
        stored = self.companies.store_and_select(issuer_source=source, issuer_identifier=cik,
            security_identifier=security, ticker=assessment.ticker, exchange_code=row["exchange"],
            quote_currency=None, security_title=None, source_accession=None, security_basis="UNASSESSED",
            receipt_ratio=None, engine_revision=store.ENGINE_VERSION, canonical_payload=payload,
            artifacts=(SnapshotArtifact(item.artifact.content_sha256, item.role) for item in artifacts))
        return ImportedCompany(assessment.ticker, source, cik, security, stored, tuple(artifacts),
                               "UNAVAILABLE", assessment.reason)

    def _company(self, ticker: str) -> sqlite3.Row:
        rows = self.sqlite.execute(
            """SELECT cik, ticker, name, exchange
               FROM company
               WHERE upper(ticker) = upper(?)""",
            (ticker,),
        ).fetchall()
        if not rows:
            raise RetainedInputError(f"{ticker}: no retained SQLite company")
        if len(rows) != 1:
            raise RetainedInputError(f"{ticker}: retained SQLite ticker is ambiguous")
        return rows[0]

    def _prepare_sec(self, row: Mapping[str, str | None]) -> _PreparedCompany:
        cik = str(row["cik"])
        if not (cik.isdigit() and len(cik) == 10):
            raise RetainedInputError(f"{row['ticker']}: unsupported retained issuer {cik}")

        facts, facts_artifact = self._json_file(
            self.cache_dir / f"companyfacts_{cik}.json",
            "official_api_facts",
        )
        retained = [facts_artifact]
        sidecar_path = dera.sidecar_path(self.cache_dir, cik)
        dimensioned = None
        if sidecar_path.exists():
            dimensioned, sidecar_artifact = self._json_file(
                sidecar_path,
                "dimensioned_facts",
            )
            retained.append(sidecar_artifact)

        inline_artifacts = inline_xbrl.current_retained_artifacts(
            self.cache_dir, cik, facts,
        )
        for source in inline_artifacts:
            artifact, verified = self._retain(
                source.payload, source.media_type, source.role,
            )
            if (artifact.artifact.content_sha256 != source.sha256
                    or artifact.artifact.byte_size != source.byte_size
                    or verified != source.payload):
                raise RetainedInputError(
                    f"{row['ticker']}: retained Inline-XBRL readback mismatch"
                )
            retained.append(artifact)

        if inline_artifacts:
            bundle = self.loader.load(cik, str(row["ticker"]), facts)
            ticker, receipt = bundle.ticker, bundle.receipt
        else:
            ticker, receipt = self.loader.identity(cik, str(row["ticker"]))
            bundle = EvidenceBundle(cik, ticker, facts, dimensioned, receipt)
        title = str((receipt or {}).get("title") or "")
        accession = str((receipt or {}).get("accn") or "")
        raw_cover = self._raw_cover_artifact(receipt)
        if raw_cover is not None:
            retained.append(raw_cover)
        raw_primary = self._raw_cover_primary_document_artifact(receipt)
        if raw_primary is not None:
            retained.append(raw_primary)
        if receipt is None or not title or not accession:
            mapping, mapping_artifact = self._official_sec_ticker_mapping()
            _verify_sec_ticker_mapping(mapping, cik, ticker)
            retained.append(mapping_artifact)
            return _PreparedCompany(
                ticker=ticker,
                issuer_source="SEC",
                issuer_identifier=cik,
                security_identifier=f"sec:{cik}:primary-listed-security",
                exchange_code=row["exchange"],
                quote_currency="USD",
                security_title=None,
                source_accession=None,
                security_basis="SEC_TICKER_MAPPING_ONLY",
                receipt_ratio=None,
                bundle=bundle,
                artifacts=tuple(retained),
            )
        receipt, cover_artifact = self._json_bytes(
            _canonical_json(receipt),
            "structured_cover_identity",
        )
        retained.append(cover_artifact)
        depositary = cover.is_depositary_security(title)
        ratio = receipt.get("ratio")
        if depositary and ratio is None:
            raise RetainedInputError(f"{ticker}: depositary receipt ratio is missing")

        return _PreparedCompany(
            ticker=ticker,
            issuer_source="SEC",
            issuer_identifier=cik,
            security_identifier=f"sec:{cik}:primary-listed-security",
            exchange_code=row["exchange"],
            quote_currency="USD",
            security_title=title,
            source_accession=accession,
            security_basis=(
                "PRIMARY_DEPOSITARY_RECEIPT" if depositary else "PRIMARY_COMMON_SHARE"
            ),
            receipt_ratio=ratio,
            bundle=bundle,
            artifacts=tuple(retained),
        )

    def _raw_cover_artifact(
        self, receipt: Mapping | None
    ) -> RetainedArtifact | None:
        if receipt is None or not receipt.get("accn"):
            return None
        for report_number in cover.COVER_REPORTS:
            path = cover.report_cache_path(
                self.cache_dir, str(receipt["accn"]), report_number
            )
            if not path.exists():
                continue
            raw = path.read_bytes()
            securities = cover.securities(raw.decode("utf-8", errors="replace"))
            if any(
                security.get("symbol") == receipt.get("symbol")
                and security.get("title") == receipt.get("title")
                for security in securities
            ):
                return self._file_bytes(path, "text/html", "raw_cover_filing")[1]
        return None

    def _raw_cover_primary_document_artifact(
        self, receipt: Mapping | None
    ) -> RetainedArtifact | None:
        if (
            receipt is None
            or not receipt.get("accn")
            or receipt.get("ratio") is None
            or not cover.is_depositary_security(str(receipt.get("title") or ""))
        ):
            return None
        primary_dir = cover.primary_document_cache_dir(
            self.cache_dir, str(receipt["accn"])
        )
        paths = sorted(path for path in primary_dir.glob("*") if path.is_file())
        if len(paths) != 1:
            return None
        path = paths[0]
        raw = path.read_bytes()
        ratio = cover.depositary_ratio(
            cover.text_of(raw.decode("utf-8", errors="replace"))
        )
        if ratio != Decimal(str(receipt["ratio"])):
            return None
        return self._file_bytes(
            path, "text/html", "raw_cover_primary_document"
        )[1]

    def _official_sec_ticker_mapping(self) -> tuple[dict, RetainedArtifact]:
        if self._sec_ticker_mapping is None:
            self._sec_ticker_mapping = self._json_file(
                self.cache_dir / "company_tickers.json",
                "official_sec_ticker_mapping",
            )
        return self._sec_ticker_mapping

    def _dashboard_tickers(self) -> list[str]:
        eligible = set(store.dashboard_ciks(self.sqlite))
        rows = self.sqlite.execute(
            "SELECT cik, ticker FROM company WHERE ticker IS NOT NULL"
        ).fetchall()
        return sorted(
            (str(row["ticker"]) for row in rows if row["cik"] in eligible),
            key=lambda ticker: (ticker.upper(), ticker),
        )

    def _prepare_edinet(self, row: Mapping[str, str | None]) -> _PreparedCompany:
        entity_id = str(row["cik"])
        facts, facts_artifact = self._json_file(
            self.cache_dir / f"companyfacts_{entity_id}.json",
            "canonical_edinet_facts",
        )
        adapter = facts.get("_adapter") or {}
        if adapter.get("kind") != EDINET_ADAPTER:
            raise RetainedInputError(
                f"{row['ticker']}: retained canonical facts are not EDINET evidence"
            )
        ticker = str(row["ticker"])
        if adapter.get("ticker") != ticker:
            raise RetainedInputError(
                f"{ticker}: retained EDINET primary security does not match SQLite"
            )
        reports = adapter.get("reports") or []
        if not reports:
            raise RetainedInputError(f"{ticker}: retained EDINET report manifest is empty")
        quote_currency = str(adapter.get("quote_currency") or "").upper()
        security_basis = str(adapter.get("security_basis") or "")
        if not quote_currency or security_basis != "PRIMARY_ORDINARY_SHARE":
            raise RetainedInputError(
                f"{ticker}: retained EDINET primary-security metadata is incomplete"
            )

        retained = []
        archives = {}
        for document in sorted({str(report.get("document") or "") for report in reports}):
            if not re.fullmatch(r"[A-Z0-9]+", document):
                raise RetainedInputError(f"{ticker}: invalid EDINET document in manifest")
            try:
                path = self.cache_dir / "edinet" / f"{document}.zip"
                try:
                    with path.open("rb") as source:
                        raw = source.read(edinet._XBRL_INPUT_LIMIT + 1)
                except FileNotFoundError as exc:
                    raise RetainedInputMissing(path) from exc
                if len(raw) > edinet._XBRL_INPUT_LIMIT:
                    raise EdinetInputCeilingError("EDINET archive exceeds the 16 MiB source ceiling")
                validate_xbrl_archive(raw, document)
            except EdinetInputCeilingError as exc:
                raise RetainedInputCeiling(f"{ticker}: {exc}",
                    inputs=(facts_artifact, *retained)) from exc
            except EdinetError as exc:
                raise RetainedInputError(
                    f"{ticker}: retained EDINET filing is invalid: {exc}"
                ) from exc
            artifact, verified = self._retain(raw, "application/zip", "raw_filing")
            if verified != raw:
                raise RetainedInputError(f"{ticker}: retained EDINET archive readback differs")
            retained.append(artifact)
            archives[document] = verified

        try:
            facts = rebuild_companyfacts(facts, archives.__getitem__)
        except EdinetInputCeilingError as exc:
            raise RetainedInputCeiling(f"{ticker}: {exc}",
                inputs=(facts_artifact, *retained)) from exc
        except (EdinetError, ValueError) as exc:
            raise RetainedInputError(f"{ticker}: {exc}") from exc
        facts, facts_artifact = self._json_bytes(_canonical_json(facts), "canonical_edinet_facts")
        retained.insert(0, facts_artifact)
        adapter = facts["_adapter"]
        reports = adapter["reports"]

        latest = max(
            reports,
            key=lambda report: (
                str(report.get("published") or ""),
                str(report.get("document") or ""),
            ),
        )
        return _PreparedCompany(
            ticker=ticker,
            issuer_source="EDINET",
            issuer_identifier=entity_id,
            security_identifier=f"edinet:{entity_id}:primary-ordinary",
            exchange_code=row["exchange"],
            quote_currency=quote_currency,
            security_title=security_basis,
            source_accession=str(latest["document"]),
            security_basis=security_basis,
            receipt_ratio=None,
            bundle=EvidenceBundle(entity_id, ticker, facts, None, None),
            artifacts=tuple(retained),
        )

    def _prepare_company_adapter(self, row: Mapping[str, str | None]) -> _PreparedCompany:
        entity = ifrs_workbook.DEFAULT_ENTITY_ID
        ticker = ifrs_workbook.DEFAULT_TICKER
        if row["cik"] != entity or row["ticker"] != ticker:
            raise RetainedInputError("company adapter must identify its configured Adidas security")
        cached, original = self._json_file(self.cache_dir / f"companyfacts_{entity}.json",
                                          "retained_company_adapter_facts")
        adapter = cached.get("_adapter") or {}
        if (cached.get("cik") != entity or adapter.get("kind") != ifrs_workbook.ADAPTER_KIND
                or adapter.get("ticker") != ticker or adapter.get("security_basis") != "PRIMARY_ORDINARY_SHARE"
                or adapter.get("reporting_currency") != "EUR"):
            raise RetainedInputError("retained Adidas adapter/security metadata is invalid")
        retained = [original]
        declared_quote = None
        if self.company_adapter_declaration is not None:
            declared_quote, declaration_inputs = self._company_adapter_security()
            retained.extend(declaration_inputs)
        cached_quote = adapter.get("quote_currency")
        if cached_quote is not None and cached_quote != "EUR":
            raise RetainedInputError("retained Adidas quote currency conflicts with its configured security")
        quote_currency = cached_quote if cached_quote is not None else declared_quote
        if quote_currency != "EUR":
            raise RetainedInputError("Adidas requires an explicit retained EUR quote declaration")
        reports = adapter.get("reports")
        if not isinstance(reports, list) or not reports:
            raise RetainedInputError("retained Adidas report manifest is empty")
        directory = self.cache_dir / "adapters" / entity
        names, originals = set(), {}
        for report in reports:
            if not isinstance(report, dict):
                raise RetainedInputError("invalid Adidas report manifest")
            document, expected = report.get("document"), report.get("sha256")
            if (not isinstance(document, str) or Path(document).name != document
                    or not re.fullmatch(r"[A-Za-z0-9_-]*adidas[A-Za-z0-9_-]*ar\d{2}\.(?:xls|xlsx)", document)
                    or document in names or not isinstance(expected, str)
                    or not re.fullmatch(r"[a-f0-9]{64}", expected)):
                raise RetainedInputError("invalid Adidas document/hash in report manifest")
            names.add(document)
            media = ("application/vnd.ms-excel" if document.endswith(".xls") else
                     "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            raw, artifact = self._file_bytes(directory / document, media, "raw_filing")
            if hashlib.sha256(raw).hexdigest() != expected:
                raise RetainedInputError(f"Adidas retained report hash differs: {document}")
            originals[document] = expected
            retained.append(artifact)
        # The existing owner scans its directory; every consumed workbook must be declared.
        consumed = {path.name for path in directory.iterdir() if path.is_file()
            and path.suffix.lower() in {".xls", ".xlsx"}
            and re.search(r"adidas.*ar\d{2}", path.name, re.IGNORECASE)}
        if consumed != names:
            raise RetainedInputError("Adidas workbook directory differs from retained report manifest")
        facts = ifrs_workbook.build_adidas_companyfacts(directory, entity_id=entity, ticker=ticker)
        mapped = facts["_adapter"]
        if ({report["document"]: report["sha256"] for report in mapped["reports"]} != originals
                or any(hashlib.sha256((directory / name).read_bytes()).hexdigest() != digest
                       for name, digest in originals.items())):
            raise RetainedInputError("Adidas source workbooks changed during remapping")
        mapped["security_basis"] = adapter["security_basis"]
        mapped["quote_currency"] = quote_currency
        facts, generated = self._json_bytes(_canonical_json(facts), "canonical_company_adapter_facts")
        retained.append(generated)
        latest = max(mapped["reports"], key=lambda report: (report["published"], report["document"]))
        return _PreparedCompany(ticker, "COMPANY_ADAPTER", entity,
            f"company-adapter:{entity}:primary-ordinary", row["exchange"], quote_currency,
            adapter["security_basis"], f"adidas-ar{str(latest['fiscal_year'])[-2:]}-{latest['sha256'][:12]}",
            adapter["security_basis"], None, EvidenceBundle(entity, ticker, facts, None, None), tuple(retained))

    def _company_adapter_security(self):
        declaration, artifact = self._json_file(self.company_adapter_declaration,
                                               "company_adapter_security_declaration")
        expected = dict(issuer_source="COMPANY_ADAPTER", issuer_identifier=ifrs_workbook.DEFAULT_ENTITY_ID,
            adapter_kind=ifrs_workbook.ADAPTER_KIND, ticker=ifrs_workbook.DEFAULT_TICKER,
            security_identifier=f"company-adapter:{ifrs_workbook.DEFAULT_ENTITY_ID}:primary-ordinary",
            security_basis="PRIMARY_ORDINARY_SHARE", quote_currency="EUR")
        if set(declaration) != {*expected, "sources"} or any(declaration.get(key) != value for key, value in expected.items()):
            raise RetainedInputError("invalid explicit Adidas security declaration")
        sources = declaration["sources"]
        if not isinstance(sources, list) or not sources:
            raise RetainedInputError("Adidas security declaration requires retained source documents")
        from .source_observations import _canonical_url
        retained = [artifact]
        seen = set()
        for source in sources:
            if not isinstance(source, dict) or set(source) != {"path", "sha256", "source_url"}:
                raise RetainedInputError("invalid Adidas security source pointer")
            if not isinstance(source["path"], str) or not Path(source["path"]).is_absolute():
                raise RetainedInputError("Adidas security source must be an exact retained absolute path")
            url = _canonical_url(source["source_url"])
            if url in seen:
                raise RetainedInputError("duplicate Adidas security source")
            seen.add(url)
            raw, source_artifact = self._file_bytes(Path(source["path"]), "text/html", "company_adapter_security_source")
            if source["sha256"] != hashlib.sha256(raw).hexdigest():
                raise RetainedInputError("Adidas security source hash differs from declaration")
            retained.append(source_artifact)
        return declaration["quote_currency"], tuple(retained)

    def _json_file(self, path: Path, role: str) -> tuple[dict, RetainedArtifact]:
        raw, artifact = self._file_bytes(path, "application/json", role)
        return _parse_json(raw, path), artifact

    def _json_bytes(self, raw: bytes, role: str) -> tuple[dict, RetainedArtifact]:
        artifact, verified = self._retain(raw, "application/json", role)
        return _parse_json(verified, Path(role)), artifact

    def _file_bytes(
        self,
        path: Path,
        media_type: str,
        role: str,
    ) -> tuple[bytes, RetainedArtifact]:
        try:
            raw = path.read_bytes()
        except FileNotFoundError as exc:
            raise RetainedInputMissing(path) from exc
        artifact, verified = self._retain(raw, media_type, role)
        return verified, artifact

    def _retain(
        self,
        raw: bytes,
        media_type: str,
        role: str,
    ) -> tuple[RetainedArtifact, bytes]:
        artifact = store_evidence(raw, media_type, self.objects, self.artifacts)
        verified = self.objects.read_verified(
            artifact.object_key,
            artifact.content_sha256,
            artifact.byte_size,
        )
        return RetainedArtifact(role, artifact), verified


def _issuer_source(identifier: str) -> str:
    if identifier == ifrs_workbook.DEFAULT_ENTITY_ID:
        return "COMPANY_ADAPTER"
    if re.fullmatch(r"E\d{5}", identifier):
        return "EDINET"
    if identifier.isdigit():
        return "SEC"
    raise RetainedInputError("retained issuer has no supported explicit source kind")


def _canonical_json(value: Mapping) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()


def _parse_json(raw: bytes, source: Path) -> dict:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RetainedInputError(f"invalid retained JSON: {source}") from exc
    if not isinstance(value, dict):
        raise RetainedInputError(f"retained JSON must be an object: {source}")
    return value


def _verify_sec_ticker_mapping(mapping: Mapping, cik: str, ticker: str) -> None:
    for row in mapping.values():
        if not isinstance(row, Mapping):
            continue
        try:
            mapped_cik = f"{int(row.get('cik_str')):010d}"
        except (TypeError, ValueError):
            continue
        if mapped_cik == cik and row.get("ticker") == ticker:
            return
    raise RetainedInputError(
        f"{ticker}: retained SEC ticker mapping does not map {cik} to {ticker}"
    )


def _read_only_sqlite(path: Path) -> sqlite3.Connection:
    try:
        connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    except sqlite3.OperationalError as exc:
        raise RetainedInputError(f"cannot open retained SQLite store: {path}") from exc
    connection.row_factory = sqlite3.Row
    return connection


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Import companies from retained evidence without source fetches."
    )
    parser.add_argument("tickers", nargs="*")
    parser.add_argument(
        "--all-dashboard",
        action="store_true",
        help="import every SQLite dashboard-eligible company",
    )
    parser.add_argument("--sqlite", type=Path, default=store.DEFAULT_DB)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--preserve-unavailable", action="store_true",
                        help="retain explicit diagnoses for missing or unsupported inputs, without financial figures")
    parser.add_argument("--company-adapter-declaration", type=Path,
                        help="explicit retained Adidas security declaration and official source hashes")
    args = parser.parse_args(argv)
    if args.all_dashboard and args.tickers:
        parser.error("--all-dashboard cannot be combined with named tickers")
    if not args.all_dashboard and not args.tickers:
        parser.error("provide named tickers or --all-dashboard")

    settings = StorageSettings.from_env()
    sqlite_connection = _read_only_sqlite(args.sqlite)
    engine = create_postgres_engine(settings)
    try:
        importer = CompanyImporter(
            sqlite_connection,
            args.cache_dir or args.sqlite.parent,
            ImmutableObjectStore(create_s3_client(settings), settings.s3_bucket),
            SqlArtifactRepository(engine),
            SharedCompanyRepository(engine),
            **({"company_adapter_declaration": args.company_adapter_declaration}
               if args.company_adapter_declaration is not None else {}),
        )
        if args.all_dashboard:
            summary = importer.import_dashboard(**({"preserve_unavailable": True} if args.preserve_unavailable else {}))
            print(json.dumps(summary.as_dict(), sort_keys=True))
            return 1 if summary.failed else 0

        imported = importer.import_tickers(args.tickers, **({"preserve_unavailable": True} if args.preserve_unavailable else {}))
        for result in imported:
            print(
                json.dumps(
                    {
                        "ticker": result.ticker,
                        "security_identifier": result.security_identifier,
                        "snapshot_id": result.stored.snapshot_id,
                        "payload_sha256": result.stored.payload_sha256,
                        "artifacts": [
                            {
                                "role": retained.role,
                                "content_sha256": retained.artifact.content_sha256,
                            }
                            for retained in result.artifacts
                        ],
                        **({"data_status": result.data_status, "unavailability_reason": result.unavailability_reason}
                           if result.data_status == "UNAVAILABLE" else {}),
                    },
                    sort_keys=True,
                )
            )
    finally:
        sqlite_connection.close()
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
