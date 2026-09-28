#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "api"))

from screener import store, sync  # noqa: E402
from screener.artifacts import EvidenceArtifact  # noqa: E402
from screener.company_import import (  # noqa: E402
    CompanyImporter,
    DerivationError,
    RetainedInputError,
    _canonical_json,
    _read_only_sqlite,
)
from screener.object_store import VerifiedObject, content_address  # noqa: E402
from screener.shared_companies import StoredCompany  # noqa: E402
from screener.sources import inline_xbrl  # noqa: E402


HERE = Path(__file__).resolve().parent
AUDIT = ROOT / "local" / "engine187-persistence-audit-2026-09-28"


class MemoryObjectStore:
    def __init__(self):
        self.data: dict[str, bytes] = {}
        self.reads: list[str] = []

    def put_verified(self, data: bytes, _media_type: str) -> VerifiedObject:
        digest, key = content_address(data)
        self.data.setdefault(key, data)
        return VerifiedObject(digest, key, len(data), datetime.now(timezone.utc))

    def read_verified(self, key: str, digest: str, size: int) -> bytes:
        data = self.data[key]
        assert hashlib.sha256(data).hexdigest() == digest
        assert len(data) == size
        self.reads.append(key)
        return data


class MemoryArtifacts:
    def __init__(self):
        self.rows: dict[str, EvidenceArtifact] = {}

    def add_verified(self, verified: VerifiedObject, media_type: str) -> EvidenceArtifact:
        artifact = EvidenceArtifact(
            verified.content_sha256,
            verified.object_key,
            verified.byte_size,
            media_type,
            datetime.now(timezone.utc),
            verified.verified_at,
        )
        return self.rows.setdefault(verified.content_sha256, artifact)


class MemoryCompanies:
    def __init__(self):
        self.rows: dict[tuple, StoredCompany] = {}
        self.payloads: dict[int, dict] = {}
        self.calls: list[dict] = []

    def store_and_select(self, **values) -> StoredCompany:
        values["artifacts"] = tuple(values["artifacts"])
        self.calls.append(values)
        payload_sha256 = hashlib.sha256(
            _canonical_json(values["canonical_payload"])
        ).hexdigest()
        identity = (
            values["security_identifier"],
            values["engine_revision"],
            payload_sha256,
            values["ticker"],
            values["source_accession"],
            str(values["receipt_ratio"]),
            tuple(sorted(
                (artifact.content_sha256, artifact.role)
                for artifact in values["artifacts"]
            )),
        )
        stored = self.rows.get(identity)
        if stored is not None:
            return StoredCompany(
                stored.issuer_id,
                stored.security_id,
                stored.snapshot_id,
                stored.payload_sha256,
                stored.engine_revision,
                created=False,
            )
        number = len(self.rows) + 1
        stored = StoredCompany(number, number, number, payload_sha256, store.ENGINE_VERSION)
        self.rows[identity] = stored
        self.payloads[number] = values["canonical_payload"]
        return stored


def sqlite_payload(connection: sqlite3.Connection, ticker: str) -> dict:
    row = connection.execute(
        """SELECT s.data FROM company c JOIN snapshot s USING (cik)
           WHERE c.ticker = ?""",
        (ticker,),
    ).fetchone()
    assert row is not None and row["data"]
    return json.loads(row["data"])


def main() -> int:
    cache = store.DEFAULT_DB.parent
    manifests = json.loads((AUDIT / "sec-manifests.json").read_bytes())
    connection = _read_only_sqlite(store.DEFAULT_DB)
    objects = MemoryObjectStore()
    artifacts = MemoryArtifacts()
    companies = MemoryCompanies()
    importer = CompanyImporter(
        connection, cache, objects, artifacts, companies, sync._derive_evidence,
    )

    enumerated = {}
    expected_reads = Counter()
    for record in manifests:
        cik = record["cik"]
        facts = json.loads((cache / f"companyfacts_{cik}.json").read_bytes())
        retained = inline_xbrl.current_retained_artifacts(cache, cik, facts)
        enumerated[record["ticker"]] = retained
        expected_reads.update(
            content_address(artifact.payload)[1] for artifact in retained
        )

    assert len(enumerated) == 18
    assert sum(len(items) - 1 for items in enumerated.values()) == 110
    source_hashes = {
        artifact.sha256
        for items in enumerated.values()
        for artifact in items[1:]
    }
    assert len(source_hashes) == 108
    assert len({items[0].sha256 for items in enumerated.values()}) == 18

    successes = {}
    failures = {}
    for record in manifests:
        ticker = record["ticker"]
        try:
            imported = importer.import_ticker(ticker)
        except (DerivationError, RetainedInputError) as exc:
            failures[ticker] = str(exc)
        else:
            expected = sqlite_payload(connection, ticker)
            actual = companies.payloads[imported.stored.snapshot_id]
            assert _canonical_json(actual) == _canonical_json(expected)
            assert imported.stored.payload_sha256 == hashlib.sha256(
                _canonical_json(expected)
            ).hexdigest()
            expected_roles = {item.role for item in enumerated[ticker]}
            assert expected_roles <= {item.role for item in imported.artifacts}
            successes[ticker] = imported

    assert set(successes) == {
        record["ticker"] for record in manifests
        if record["supplemented_status"] == "ok"
    }
    assert set(failures) == {"AHNRF", "BRBI", "CIB", "NXAT"}
    assert failures == {
        "AHNRF": "AHNRF: retained evidence derived as foreign, not ok",
        "BRBI": "BRBI: retained evidence derived as foreign, not ok",
        "CIB": "CIB: depositary receipt ratio is missing",
        "NXAT": "NXAT: retained evidence derived as foreign, not ok",
    }
    assert Counter(objects.reads) >= expected_reads

    first_object_count = len(objects.data)
    first_artifact_count = len(artifacts.rows)
    first_snapshot_count = len(companies.rows)
    first_ids = {
        ticker: imported.stored.snapshot_id for ticker, imported in successes.items()
    }
    second_successes = {}
    second_failures = {}
    for record in manifests:
        ticker = record["ticker"]
        try:
            imported = importer.import_ticker(ticker)
        except (DerivationError, RetainedInputError) as exc:
            second_failures[ticker] = str(exc)
        else:
            second_successes[ticker] = imported

    assert second_failures == failures
    assert {
        ticker: imported.stored.snapshot_id
        for ticker, imported in second_successes.items()
    } == first_ids
    assert all(not imported.stored.created for imported in second_successes.values())
    assert len(objects.data) == first_object_count
    assert len(artifacts.rows) == first_artifact_count
    assert len(companies.rows) == first_snapshot_count == 14

    result = {
        "engine": store.ENGINE_VERSION,
        "manifests": len(enumerated),
        "file_roles": sum(len(items) - 1 for items in enumerated.values()),
        "unique_source_hashes": len(source_hashes),
        "manifest_objects": len({items[0].sha256 for items in enumerated.values()}),
        "exact_payloads": {
            ticker: imported.stored.payload_sha256
            for ticker, imported in sorted(successes.items())
        },
        "failures": failures,
        "first_pass": {
            "objects": first_object_count,
            "artifact_rows": first_artifact_count,
            "snapshots": first_snapshot_count,
        },
        "second_pass_reused_snapshot_ids": first_ids,
    }
    (HERE / "real-import-results.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps(result, sort_keys=True))
    connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
