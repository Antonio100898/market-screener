"""Import companies from retained SQLite evidence into shared storage."""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Iterable, Mapping

from . import store, sync
from .artifacts import EvidenceArtifact, SqlArtifactRepository, store_evidence
from .evidence import EvidenceBundle, EvidenceLoader
from .object_store import ImmutableObjectStore, create_s3_client
from .postgres import create_postgres_engine
from .shared_companies import SharedCompanyRepository, SnapshotArtifact, StoredCompany
from .sources import cover, dera, inline_xbrl
from .sources.edinet_mapper import ADAPTER_KIND as EDINET_ADAPTER
from .sources.edinet import EdinetError, validate_xbrl_archive
from .storage_config import StorageSettings


class CompanyImportError(RuntimeError):
    pass


class RetainedInputError(CompanyImportError):
    pass


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


@dataclass(frozen=True)
class ImportFailure:
    ticker: str
    reason: str


@dataclass(frozen=True)
class BulkImportSummary:
    imported: tuple[str, ...]
    reused: tuple[str, ...]
    failed: tuple[ImportFailure, ...]

    def as_dict(self) -> dict:
        return {
            "imported": list(self.imported),
            "reused": list(self.reused),
            "failed": [
                {"ticker": failure.ticker, "reason": failure.reason}
                for failure in self.failed
            ],
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
    ):
        self.sqlite = sqlite_connection
        self.cache_dir = Path(cache_dir)
        self.objects = object_store
        self.artifacts = artifact_repository
        self.companies = company_repository
        self.derive = derive
        self._sec_ticker_mapping: tuple[dict, RetainedArtifact] | None = None
        self.loader = EvidenceLoader(
            sqlite_connection,
            SimpleNamespace(cache_dir=self.cache_dir),
        )

    def import_tickers(self, tickers: Iterable[str]) -> list[ImportedCompany]:
        requested = [ticker.strip() for ticker in tickers]
        if not requested or any(not ticker for ticker in requested):
            raise ValueError("at least one non-empty ticker is required")
        return [self.import_ticker(ticker) for ticker in requested]

    def import_dashboard(self) -> BulkImportSummary:
        imported = []
        reused = []
        failed = []
        for ticker in self._dashboard_tickers():
            try:
                result = self.import_ticker(ticker)
            except Exception as exc:
                reason = str(exc) or type(exc).__name__
                failed.append(ImportFailure(ticker, reason))
            else:
                (imported if result.stored.created else reused).append(ticker)
        return BulkImportSummary(tuple(imported), tuple(reused), tuple(failed))

    def import_ticker(self, ticker: str) -> ImportedCompany:
        row = self._company(ticker)
        prepared = (
            self._prepare_edinet(row)
            if re.fullmatch(r"E\d{5}", row["cik"])
            else self._prepare_sec(row)
        )
        status, payload = self.derive(prepared.bundle)
        if status != "ok" or payload is None:
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

        retained = [facts_artifact]
        for document in sorted({str(report.get("document") or "") for report in reports}):
            if not re.fullmatch(r"[A-Z0-9]+", document):
                raise RetainedInputError(f"{ticker}: invalid EDINET document in manifest")
            raw, artifact = self._file_bytes(
                self.cache_dir / "edinet" / f"{document}.zip",
                "application/zip",
                "raw_filing",
            )
            try:
                validate_xbrl_archive(raw, document)
            except EdinetError as exc:
                raise RetainedInputError(
                    f"{ticker}: retained EDINET filing is invalid: {exc}"
                ) from exc
            retained.append(artifact)

        latest = max(
            reports,
            key=lambda report: (
                str(report.get("published") or ""),
                str(report.get("document") or ""),
            ),
        )
        quote_currency = str(adapter.get("quote_currency") or "").upper()
        security_basis = str(adapter.get("security_basis") or "")
        if not quote_currency or security_basis != "PRIMARY_ORDINARY_SHARE":
            raise RetainedInputError(
                f"{ticker}: retained EDINET primary-security metadata is incomplete"
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
            raise RetainedInputError(f"missing retained input: {path}") from exc
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
        )
        if args.all_dashboard:
            summary = importer.import_dashboard()
            print(json.dumps(summary.as_dict(), sort_keys=True))
            return 1 if summary.failed else 0

        imported = importer.import_tickers(args.tickers)
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
