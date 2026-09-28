#!/usr/bin/env python3
"""Read-only engine-187 SEC/EDINET persistence audit.

The script reads SQLite, retained cache files, and dashboard.json. It never
opens PostgreSQL/S3 and never writes outside stdout.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from screener import store, sync
from screener.artifacts import EvidenceArtifact
from screener.company_import import CompanyImporter, DerivationError, RetainedInputError
from screener.evidence import EvidenceBundle
from screener.object_store import VerifiedObject, content_address
from screener.shared_companies import StoredCompany
from screener.sources import inline_xbrl


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DASHBOARD = ROOT / "api/screener/static/dashboard.json"


class TransientObjectStore:
    """Exercise importer verification without persisting source bytes."""

    def __init__(self) -> None:
        self._last: tuple[str, bytes] | None = None

    def put_verified(self, data: bytes, media_type: str) -> VerifiedObject:
        del media_type
        digest, key = content_address(data)
        self._last = (key, data)
        return VerifiedObject(
            digest,
            key,
            len(data),
            datetime(1970, 1, 1, tzinfo=timezone.utc),
        )

    def read_verified(
        self, key: str, expected_sha256: str, expected_size: int
    ) -> bytes:
        assert self._last is not None and self._last[0] == key
        data = self._last[1]
        assert hashlib.sha256(data).hexdigest() == expected_sha256
        assert len(data) == expected_size
        self._last = None
        return data


class TransientArtifactRepository:
    def add_verified(
        self, verified: VerifiedObject, media_type: str
    ) -> EvidenceArtifact:
        return EvidenceArtifact(
            verified.content_sha256,
            verified.object_key,
            verified.byte_size,
            media_type,
            datetime(1970, 1, 1, tzinfo=timezone.utc),
            verified.verified_at,
        )


@dataclass
class SelectionResult:
    ticker: str
    payload_match: bool
    roles: tuple[str, ...]


class TransientCompanyRepository:
    """Validate would-be selections against SQLite without storing them."""

    def __init__(self, expected_payload_sha256: dict[str, str]) -> None:
        self.expected = expected_payload_sha256
        self.results: dict[str, SelectionResult] = {}

    def store_and_select(self, **values) -> StoredCompany:
        artifacts = tuple(values["artifacts"])
        payload = json.dumps(
            values["canonical_payload"],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
        digest = hashlib.sha256(payload).hexdigest()
        ticker = values["ticker"]
        self.results[ticker] = SelectionResult(
            ticker,
            digest == self.expected[ticker],
            tuple(sorted(artifact.role for artifact in artifacts)),
        )
        identity = len(self.results)
        return StoredCompany(identity, identity, identity, digest, store.ENGINE_VERSION)


def open_sqlite(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def canonical_sha256(value: dict) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def snapshot_payloads(connection: sqlite3.Connection, ciks: list[str]) -> dict[str, str]:
    placeholders = ",".join("?" for _ in ciks)
    rows = connection.execute(
        f"""SELECT c.ticker, s.data
            FROM company c JOIN snapshot s USING (cik)
            WHERE c.cik IN ({placeholders})""",
        ciks,
    )
    return {row["ticker"]: canonical_sha256(json.loads(row["data"])) for row in rows}


def dashboard_identities(path: Path) -> set[tuple[str, str]]:
    payload = json.loads(path.read_bytes())
    assert payload["engine_version"] == store.ENGINE_VERSION
    return {(row["ticker"], row["cik"]) for row in payload["rows"]}


def classify_failure(ticker: str, reason: str, manifest_tickers: set[str]) -> str:
    if ticker in manifest_tickers and "derived as foreign" in reason:
        return "inline_supplement_not_applied_by_importer"
    if "does not map" in reason:
        return "current_sec_ticker_mapping_mismatch"
    if "derived as pending_facts" in reason:
        return "pending_structured_facts"
    if "depositary receipt ratio is missing" in reason:
        return "missing_depositary_ratio"
    if "derived as foreign" in reason:
        return "source_policy_foreign"
    if "missing retained input" in reason:
        return "missing_retained_artifact"
    return "other"


def sec_inventory(
    connection: sqlite3.Connection,
    cache: Path,
    importer: CompanyImporter,
    dashboard: set[tuple[str, str]],
) -> tuple[list[dict], list[dict]]:
    manifests = []
    artifact_rows = []
    for path in sorted((cache / "sec-inline/current").glob("*.json")):
        raw_manifest = path.read_bytes()
        manifest = json.loads(raw_manifest)
        cik = manifest["cik"]
        company = connection.execute(
            """SELECT c.ticker, s.status, s.engine_version, s.data
               FROM company c JOIN snapshot s USING (cik) WHERE c.cik = ?""",
            (cik,),
        ).fetchone()
        assert company is not None
        ticker = company["ticker"]
        verified = inline_xbrl.verify_manifest(manifest, cache, cik)
        company_row = connection.execute(
            "SELECT cik, ticker, name, exchange FROM company WHERE cik = ?",
            (cik,),
        ).fetchone()
        prepare_error = None
        try:
            prepared = importer._prepare_sec(company_row)
        except (RetainedInputError, DerivationError) as exc:
            prepared = None
            prepare_error = str(exc)
            raw_status = "prepare_error"
        else:
            raw_status, _ = sync._derive_evidence(prepared.bundle)
        raw_facts = json.loads((cache / f"companyfacts_{cik}.json").read_bytes())
        supplemented_bundle = importer.loader.load(cik, ticker, raw_facts)
        supplement_state = supplemented_bundle.supplement_state
        supplemented_status, supplemented_payload = sync._derive_evidence(
            supplemented_bundle
        )
        expected = json.loads(company["data"]) if company["data"] else None
        manifests.append(
            {
                "cik": cik,
                "ticker": ticker,
                "sqlite_status": company["status"],
                "sqlite_engine": company["engine_version"],
                "dashboard_present": (ticker, cik) in dashboard,
                "relationship": manifest["relationship"],
                "annual_accession": manifest["annual"]["accession"],
                "source_accession": manifest["source"]["accession"],
                "source_document": manifest["source"]["document"],
                "manifest_sha256": hashlib.sha256(raw_manifest).hexdigest(),
                "manifest_size": len(raw_manifest),
                "manifest_verified": bool(verified),
                "current_importer_artifact_roles": (
                    sorted(item.role for item in prepared.artifacts)
                    if prepared is not None
                    else []
                ),
                "current_importer_status": raw_status,
                "current_importer_error": prepare_error,
                "supplement_state": supplement_state,
                "supplemented_status": supplemented_status,
                "supplemented_payload_matches_sqlite": (
                    expected is not None
                    and supplemented_payload is not None
                    and canonical_sha256(supplemented_payload)
                    == canonical_sha256(expected)
                ),
            }
        )
        artifact_rows.append(
            {
                "cik": cik,
                "ticker": ticker,
                "kind": "manifest",
                "role": "sec_inline_current_manifest",
                "accession": manifest["annual"]["accession"],
                "document": path.name,
                "sha256": hashlib.sha256(raw_manifest).hexdigest(),
                "size": len(raw_manifest),
                "currently_retained_by_importer": False,
            }
        )
        for role, record in sorted(manifest["files"].items()):
            artifact_rows.append(
                {
                    "cik": cik,
                    "ticker": ticker,
                    "kind": "source",
                    "role": role,
                    "accession": (
                        manifest["annual"]["accession"]
                        if manifest["relationship"] == "direct_annual"
                        or role.startswith("annual_")
                        else manifest["source"]["accession"]
                    ),
                    "document": record["document"],
                    "sha256": record["sha256"],
                    "size": record["size"],
                    "currently_retained_by_importer": False,
                }
            )
    return manifests, artifact_rows


def edinet_inventory(
    connection: sqlite3.Connection,
    cache: Path,
    importer: CompanyImporter,
    dashboard: set[tuple[str, str]],
    selections: TransientCompanyRepository,
) -> list[dict]:
    result = []
    rows = connection.execute(
        """SELECT c.cik, c.ticker, c.name, c.exchange, c.listed,
                  s.status, s.engine_version, s.data
           FROM company c LEFT JOIN snapshot s USING (cik)
           WHERE c.cik GLOB 'E[0-9][0-9][0-9][0-9][0-9]'
           ORDER BY c.cik"""
    )
    for row in rows:
        path = cache / f"companyfacts_{row['cik']}.json"
        raw_facts = path.read_bytes()
        facts = json.loads(raw_facts)
        adapter = facts["_adapter"]
        import_error = None
        try:
            imported = importer.import_ticker(row["ticker"])
        except Exception as exc:  # exact audit result, not publication control flow
            imported = None
            import_error = str(exc) or type(exc).__name__
        archives = []
        for report in adapter["reports"]:
            archive = cache / "edinet" / f"{report['document']}.zip"
            payload = archive.read_bytes()
            archives.append(
                {
                    "document": report["document"],
                    "published": report["published"],
                    "period_start": report["period_start"],
                    "period_end": report["period_end"],
                    "fiscal_year": report["fiscal_year"],
                    "mapped_facts": report["mapped_facts"],
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "size": len(payload),
                }
            )
        selection = selections.results.get(row["ticker"])
        result.append(
            {
                "cik": row["cik"],
                "ticker": row["ticker"],
                "name": row["name"],
                "exchange": row["exchange"],
                "listed": row["listed"],
                "security_code": adapter["security_code"],
                "security_basis": adapter["security_basis"],
                "reporting_currency": adapter["reporting_currency"],
                "quote_currency": adapter["quote_currency"],
                "sqlite_status": row["status"],
                "sqlite_engine": row["engine_version"],
                "dashboard_present": (row["ticker"], row["cik"]) in dashboard,
                "canonical_facts_sha256": hashlib.sha256(raw_facts).hexdigest(),
                "canonical_facts_size": len(raw_facts),
                "archives": archives,
                "import_status": "ok" if imported is not None else "error",
                "import_error": import_error,
                "import_payload_matches_sqlite": bool(
                    selection and selection.payload_match
                ),
                "import_artifact_roles": list(selection.roles) if selection else [],
                "source_accession": (
                    max(
                        adapter["reports"],
                        key=lambda report: (
                            report["published"], report["document"]
                        ),
                    )["document"]
                ),
            }
        )
    return result


def run(
    sqlite_path: Path,
    cache: Path,
    dashboard_path: Path,
    *,
    reconcile: bool = True,
) -> dict:
    connection = open_sqlite(sqlite_path)
    try:
        ciks = store.dashboard_ciks(connection)
        expected = snapshot_payloads(connection, ciks)
        dashboard = dashboard_identities(dashboard_path)
        placeholders = ",".join("?" for _ in ciks)
        sqlite_identities = {
            tuple(row)
            for row in connection.execute(
                f"SELECT ticker, cik FROM company WHERE cik IN ({placeholders})",
                ciks,
            )
        }
        selections = TransientCompanyRepository(expected)
        importer = CompanyImporter(
            connection,
            cache,
            TransientObjectStore(),
            TransientArtifactRepository(),
            selections,
        )
        manifests, sec_artifacts = sec_inventory(
            connection, cache, importer, dashboard
        )
        manifest_tickers = {row["ticker"] for row in manifests}
        summary = importer.import_dashboard() if reconcile else None
        failures = (
            [
                {
                    "ticker": failure.ticker,
                    "class": classify_failure(
                        failure.ticker, failure.reason, manifest_tickers
                    ),
                    "reason": failure.reason,
                }
                for failure in summary.failed
            ]
            if summary is not None
            else []
        )
        edinet = edinet_inventory(
            connection, cache, importer, dashboard, selections
        )
        unique_sources = {
            row["sha256"]: row["size"]
            for row in sec_artifacts
            if row["kind"] == "source"
        }
        manifest_artifacts = [
            row for row in sec_artifacts if row["kind"] == "manifest"
        ]
        return {
            "engine": store.ENGINE_VERSION,
            "dashboard": {
                "sqlite_eligible": len(ciks),
                "json_rows": len(dashboard),
                "identities_equal": dashboard == sqlite_identities,
            },
            "import_reconciliation": {
                "selected": (
                    len(summary.imported) + len(summary.reused)
                    if summary is not None
                    else None
                ),
                "payload_matches": sum(
                    result.payload_match for result in selections.results.values()
                ),
                "payload_mismatches": sum(
                    not result.payload_match
                    for result in selections.results.values()
                ),
                "failed": len(failures) if summary is not None else None,
                "failure_classes": dict(
                    sorted(Counter(row["class"] for row in failures).items())
                ),
                "failures": failures,
            },
            "sec": {
                "manifest_count": len(manifests),
                "relationship_counts": dict(
                    sorted(Counter(row["relationship"] for row in manifests).items())
                ),
                "manifest_bytes": sum(row["size"] for row in manifest_artifacts),
                "file_role_references": sum(
                    row["kind"] == "source" for row in sec_artifacts
                ),
                "unique_source_hashes": len(unique_sources),
                "unique_source_bytes": sum(unique_sources.values()),
                "manifests": manifests,
                "artifacts": sec_artifacts,
            },
            "edinet": edinet,
            "runtime": {
                "postgres_port_55432_checked_separately": "closed",
                "s3_port_8333_checked_separately": "closed",
                "current_postgres_s3_state": "unavailable",
            },
        }
    finally:
        connection.close()


def write_csv(rows: list[dict]) -> str:
    output = io.StringIO(newline="")
    fields = list(rows[0]) if rows else []
    writer = csv.DictWriter(output, fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--sqlite", type=Path, default=store.DEFAULT_DB
    )
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--dashboard", type=Path, default=DEFAULT_DASHBOARD)
    parser.add_argument(
        "--format",
        choices=(
            "json",
            "sec-csv",
            "sec-manifests-json",
            "failures-csv",
            "edinet-json",
            "counts-json",
        ),
        default="counts-json",
    )
    args = parser.parse_args()
    result = run(
        args.sqlite,
        args.cache or args.sqlite.parent,
        args.dashboard,
        reconcile=args.format in {"json", "failures-csv", "counts-json"},
    )
    if args.format == "sec-csv":
        print(write_csv(result["sec"]["artifacts"]), end="")
    elif args.format == "sec-manifests-json":
        print(json.dumps(result["sec"]["manifests"], indent=2, sort_keys=True))
    elif args.format == "failures-csv":
        print(write_csv(result["import_reconciliation"]["failures"]), end="")
    elif args.format == "edinet-json":
        print(json.dumps(result["edinet"], indent=2, sort_keys=True))
    elif args.format == "counts-json":
        compact = {
            "dashboard": result["dashboard"],
            "engine": result["engine"],
            "import_reconciliation": {
                key: value
                for key, value in result["import_reconciliation"].items()
                if key != "failures"
            },
            "runtime": result["runtime"],
            "sec": {
                key: value
                for key, value in result["sec"].items()
                if key not in {"manifests", "artifacts"}
            },
        }
        print(json.dumps(compact, indent=2, sort_keys=True))
    else:
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
