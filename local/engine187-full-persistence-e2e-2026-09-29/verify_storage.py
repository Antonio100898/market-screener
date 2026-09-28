#!/usr/bin/env python3
"""Read-only engine-187 PostgreSQL/S3 reconciliation."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from pathlib import Path

from sqlalchemy import func, select

from screener import store
from screener.object_store import ImmutableObjectStore, create_s3_client
from screener.postgres import (
    company_snapshot,
    company_snapshot_artifact,
    current_company_snapshot,
    create_postgres_engine,
    evidence_artifact,
    issuer,
    priced_security,
)
from screener.sources import inline_xbrl
from screener.storage_config import StorageSettings


SEC_ELIGIBLE = {
    "AERO", "AGMB", "ALPS", "AUGO", "CNI", "DAVI", "GCDT",
    "GMTL", "HBNB", "PAYP", "PICS", "TMCR", "VMET", "YMAT",
}
SEC_EXCLUDED = {"AHNRF", "BRBI", "CIB", "NXAT"}
EDINET = {"6752.T", "7974.T"}


def canonical_hash(value: dict) -> str:
    raw = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def differences(left, right, path="") -> list[dict]:
    if type(left) is not type(right):
        return [{"path": path, "sqlite": repr(left), "postgres": repr(right)}]
    if isinstance(left, dict):
        result = []
        for key in sorted(set(left) | set(right)):
            child = f"{path}.{key}" if path else key
            if key not in left or key not in right:
                result.append(
                    {
                        "path": child,
                        "sqlite": repr(left.get(key, "<missing>")),
                        "postgres": repr(right.get(key, "<missing>")),
                    }
                )
            else:
                result.extend(differences(left[key], right[key], child))
            if len(result) >= 20:
                break
        return result
    if isinstance(left, list):
        if len(left) != len(right):
            return [{"path": path + ".length", "sqlite": len(left), "postgres": len(right)}]
        result = []
        for index, (left_item, right_item) in enumerate(zip(left, right)):
            result.extend(differences(left_item, right_item, f"{path}[{index}]"))
            if len(result) >= 20:
                break
        return result
    if left != right or repr(left) != repr(right):
        return [{"path": path, "sqlite": repr(left), "postgres": repr(right)}]
    return []


def main() -> int:
    settings = StorageSettings.from_env()
    engine = create_postgres_engine(settings)
    objects = ImmutableObjectStore(create_s3_client(settings), settings.s3_bucket)
    sqlite_db = store.DEFAULT_DB
    cache = sqlite_db.parent
    sqlite_connection = sqlite3.connect(
        f"file:{sqlite_db.resolve()}?mode=ro", uri=True
    )
    sqlite_connection.row_factory = sqlite3.Row
    sqlite_connection.execute("PRAGMA query_only = ON")
    try:
        eligible_ciks = store.dashboard_ciks(sqlite_connection)
        placeholders = ",".join("?" for _ in eligible_ciks)
        expected = {
            row["ticker"]: {
                "cik": row["cik"],
                "payload": json.loads(row["data"]),
            }
            for row in sqlite_connection.execute(
                f"""SELECT c.ticker, c.cik, s.data
                    FROM company c JOIN snapshot s USING (cik)
                    WHERE c.cik IN ({placeholders})""",
                eligible_ciks,
            )
        }

        with engine.connect() as connection:
            counts = {
                table.name: connection.scalar(select(func.count()).select_from(table))
                for table in (
                    evidence_artifact,
                    company_snapshot,
                    company_snapshot_artifact,
                    current_company_snapshot,
                    issuer,
                    priced_security,
                )
            }
            rows = connection.execute(
                select(
                    issuer.c.source_system,
                    issuer.c.source_identifier,
                    priced_security.c.security_identifier,
                    company_snapshot,
                )
                .join(priced_security, priced_security.c.issuer_id == issuer.c.issuer_id)
                .join(
                    current_company_snapshot,
                    current_company_snapshot.c.security_id
                    == priced_security.c.security_id,
                )
                .join(
                    company_snapshot,
                    company_snapshot.c.snapshot_id
                    == current_company_snapshot.c.snapshot_id,
                )
            ).mappings().all()
            current = {row["ticker"]: row for row in rows}
            selected = {ticker: current[ticker] for ticker in expected if ticker in current}
            payload_hash_mismatches = [
                ticker
                for ticker, row in selected.items()
                if row["payload_sha256"]
                != canonical_hash(expected[ticker]["payload"])
            ]
            semantic_payload_mismatches = [
                ticker
                for ticker, row in selected.items()
                if row["canonical_payload"] != expected[ticker]["payload"]
            ]
            jsonb_lexical_normalizations = [
                ticker
                for ticker, row in selected.items()
                if canonical_hash(row["canonical_payload"])
                != row["payload_sha256"]
            ]
            engine_mismatches = [
                ticker
                for ticker, row in selected.items()
                if row["engine_revision"] != store.ENGINE_VERSION
            ]
            roleless = [
                ticker
                for ticker, row in selected.items()
                if not connection.scalar(
                    select(func.count())
                    .select_from(company_snapshot_artifact)
                    .where(
                        company_snapshot_artifact.c.snapshot_id == row["snapshot_id"]
                    )
                )
            ]
            non_ui = sorted(set(current) - set(expected))

            assert len(expected) == 6995
            assert len(selected) == 6721
            if payload_hash_mismatches or semantic_payload_mismatches:
                print(
                    json.dumps(
                        {
                            "payload_hash_mismatches": payload_hash_mismatches,
                            "semantic_payload_mismatches": semantic_payload_mismatches,
                            "sample_differences": {
                                ticker: differences(
                                    expected[ticker]["payload"],
                                    selected[ticker]["canonical_payload"],
                                )
                                for ticker in semantic_payload_mismatches[:5]
                            },
                        },
                        sort_keys=True,
                    )
                )
            assert not payload_hash_mismatches
            assert not semantic_payload_mismatches
            assert not engine_mismatches
            assert not roleless
            assert non_ui == ["PERSIST.NEW"]

            sec = {}
            for ticker in sorted(SEC_ELIGIBLE | SEC_EXCLUDED):
                company = sqlite_connection.execute(
                    "SELECT cik FROM company WHERE ticker = ?", (ticker,)
                ).fetchone()
                assert company is not None
                cik = company["cik"]
                facts = json.loads((cache / f"companyfacts_{cik}.json").read_bytes())
                retained = inline_xbrl.current_retained_artifacts(cache, cik, facts)
                assert retained
                verified_roles = []
                for item in retained:
                    row = connection.execute(
                        select(evidence_artifact).where(
                            evidence_artifact.c.content_sha256 == item.sha256
                        )
                    ).mappings().one_or_none()
                    assert row is not None, (ticker, item.role, item.sha256)
                    raw = objects.read_verified(
                        row["object_key"], row["content_sha256"], row["byte_size"]
                    )
                    assert raw == item.payload
                    verified_roles.append(item.role)

                selected_row = current.get(ticker)
                linked = set()
                if selected_row is not None:
                    linked = set(
                        connection.execute(
                            select(
                                company_snapshot_artifact.c.role,
                                company_snapshot_artifact.c.content_sha256,
                            ).where(
                                company_snapshot_artifact.c.snapshot_id
                                == selected_row["snapshot_id"]
                            )
                        ).all()
                    )
                expected_links = {(item.role, item.sha256) for item in retained}
                if ticker in SEC_ELIGIBLE:
                    assert selected_row is not None
                    assert expected_links <= linked
                else:
                    assert selected_row is None
                sec[ticker] = {
                    "cik": cik,
                    "selected": selected_row is not None,
                    "inline_roles": len(retained),
                    "all_inline_objects_read_back": True,
                    "all_inline_roles_linked": expected_links <= linked,
                }

            edinet = {}
            for ticker in sorted(EDINET):
                expected_row = expected[ticker]
                facts_path = cache / f"companyfacts_{expected_row['cik']}.json"
                raw_facts = facts_path.read_bytes()
                facts = json.loads(raw_facts)
                adapter = facts["_adapter"]
                selected_row = current[ticker]
                assert selected_row["source_system"] == "EDINET"
                assert selected_row["source_identifier"] == expected_row["cik"]
                assert selected_row["exchange_code"] == "TSE"
                assert selected_row["quote_currency"] == "JPY"
                assert selected_row["security_basis"] == "PRIMARY_ORDINARY_SHARE"
                assert selected_row["receipt_ratio"] is None
                links = set(
                    connection.execute(
                        select(
                            company_snapshot_artifact.c.role,
                            company_snapshot_artifact.c.content_sha256,
                        ).where(
                            company_snapshot_artifact.c.snapshot_id
                            == selected_row["snapshot_id"]
                        )
                    ).all()
                )
                expected_objects = [("canonical_edinet_facts", raw_facts)]
                expected_objects.extend(
                    (
                        "raw_filing",
                        (cache / "edinet" / f"{report['document']}.zip").read_bytes(),
                    )
                    for report in adapter["reports"]
                )
                for role, raw in expected_objects:
                    digest = hashlib.sha256(raw).hexdigest()
                    artifact = connection.execute(
                        select(evidence_artifact).where(
                            evidence_artifact.c.content_sha256 == digest
                        )
                    ).mappings().one()
                    assert objects.read_verified(
                        artifact["object_key"], digest, artifact["byte_size"]
                    ) == raw
                    assert (role, digest) in links
                edinet[ticker] = {
                    "entity": expected_row["cik"],
                    "canonical_and_zip_objects_read_back": True,
                    "payload_equal": canonical_hash(selected_row["canonical_payload"])
                    == canonical_hash(expected_row["payload"]),
                    "quote_currency": selected_row["quote_currency"],
                    "exchange": selected_row["exchange_code"],
                    "security_basis": selected_row["security_basis"],
                }

            cni = current["CNI"]
            cni_source = cni["canonical_payload"]["sources"]["total_assets"]
            assert cni_source["form"] == "6-K"
            assert cni_source["annual_form"] == "40-F"

        output = {
            "engine": store.ENGINE_VERSION,
            "counts": counts,
            "sqlite_dashboard": len(expected),
            "ui_current_selected": len(selected),
            "selected_snapshot_ids_sha256": canonical_hash(
                {
                    ticker: row["snapshot_id"]
                    for ticker, row in sorted(selected.items())
                }
            ),
            "payload_hash_matches": len(selected) - len(payload_hash_mismatches),
            "payload_hash_mismatches": payload_hash_mismatches,
            "semantic_payload_mismatches": semantic_payload_mismatches,
            "jsonb_negative_zero_normalizations": len(jsonb_lexical_normalizations),
            "engine_mismatches": engine_mismatches,
            "roleless_current": roleless,
            "non_ui_current": non_ui,
            "current_engine_distribution": dict(
                sorted(Counter(row["engine_revision"] for row in rows).items())
            ),
            "sec": sec,
            "edinet": edinet,
            "cni_dual_provenance": {
                "source_form": cni_source["form"],
                "annual_form": cni_source["annual_form"],
                "source_accession": cni_source["accn"],
                "annual_accession": cni_source["annual_accn"],
            },
        }
        print(json.dumps(output, indent=2, sort_keys=True))
    finally:
        sqlite_connection.close()
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
