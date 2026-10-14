"""Observe full retained worker rows through the existing independent audit."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import traceback

ROOT = Path(__file__).resolve().parents[1]
OWNED = ROOT / "local/japanese-retained-native-audit-owned3-2026-10-13"
LIMIT = 3 * 1024**3


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify(records):
    observed = []
    seen = set()
    for record in records:
        assert Path(record["path"]).is_absolute()
        path = Path(record["path"]).resolve()
        assert path.is_absolute() and path not in seen
        seen.add(path)
        assert type(record["bytes"]) is int and record["bytes"] >= 0
        assert path.stat().st_size == record["bytes"]
        actual = digest(path)
        assert actual == record["sha256"], str(path)
        observed.append(dict(path=str(path), bytes=record["bytes"], sha256=actual))
    return observed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--manifest-sha256", required=True)
    args = parser.parse_args()
    assert sys.dont_write_bytecode and Path.cwd().resolve() == ROOT
    assert args.manifest.resolve().is_relative_to(ROOT / "local")
    assert digest(args.manifest) == args.manifest_sha256
    manifest = json.loads(args.manifest.read_bytes())
    assert manifest["phase"] == "japanese-retained-native-audit"
    assert type(manifest["family_limit_bytes"]) is int and manifest["family_limit_bytes"] == LIMIT
    assert type(manifest["expected_engine"]) is int
    assert manifest["output_directory"] == OWNED.relative_to(ROOT).as_posix()
    assert not OWNED.exists(), "fresh outputs only; preserve prior attempt"
    if sys.stdin.buffer.read(1) != b"\x01":
        raise RuntimeError("exact held Python-family startup byte absent")

    cache = Path(manifest["cache_directory"]).resolve()
    database = Path(manifest["receipt_database"]).resolve()
    assert cache.is_relative_to(ROOT / "local") and database.is_relative_to(ROOT / "local")
    companies = manifest["japanese_companies"]
    assert len(companies) == 38 and len({row["cik"] for row in companies}) == 38
    records = manifest["protected_files"]
    protected = {Path(record["path"]).resolve() for record in records}
    assert Path(__file__).resolve() in protected and database in protected
    required = {cache / ("companyfacts_" + row["cik"] + ".json") for row in companies}
    required.add(cache / "companyfacts_0000313838.json")
    assert required <= protected
    absent = [Path(path).resolve() for path in manifest["absent_files"]]
    assert all(not path.exists() for path in absent)
    readonly_uri = database.as_uri() + "?mode=ro&immutable=1"

    def boundary(event, values):
        if event == "open" and not isinstance(values[0], int):
            path, mode, flags = values
            path = Path(path).resolve()
            writing = isinstance(mode, str) and any(char in mode for char in "wax+") or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
            if writing and not path.is_relative_to(OWNED):
                raise RuntimeError("write outside new audit output")
            if not writing and path in absent and path.exists():
                raise RuntimeError("declared-absent source appeared before read: " + str(path))
            if not writing and path.is_relative_to(cache) and path not in protected and path not in absent:
                raise RuntimeError("unbracketed retained input read: " + str(path))
        if event == "sqlite3.connect" and values[0] != readonly_uri:
            raise RuntimeError("only owned immutable receipt database admitted")
        if event == "os.mkdir" and not Path(os.fsdecode(values[0])).resolve().is_relative_to(OWNED):
            raise RuntimeError("directory outside new audit output")
        if event in {"os.remove", "os.rmdir", "os.rename"} or event.startswith(("socket.", "subprocess.", "os.spawn", "os.exec")) or event == "os.system":
            raise RuntimeError("network/process/cleanup operation refused")

    sys.addaudithook(boundary)
    OWNED.mkdir()
    report = dict(status="STARTED", japanese_rows=0, sony_rows=0, bad_companies=[],
                  input_before=[], input_after=[], failure=None,
                  financial_acceptance=False, currentness_acceptance=False, release_acceptance=False,
                  resource_qualification="External Root c10/native capture owns actual family evidence")
    try:
        report["input_before"] = verify(records)
        from screener import audit, store, sync
        assert store.ENGINE_VERSION == manifest["expected_engine"]
        report["engine"] = store.ENGINE_VERSION
        class NoNetwork:
            def _get_text(self, url):
                raise RuntimeError("native Japanese audit cannot request SEC statements")

        with (OWNED / "japanese-full-payloads.jsonl").open("x", encoding="utf-8") as payloads, (OWNED / "japanese-audit.jsonl").open("x", encoding="utf-8") as audits:
            for company in companies:
                cik, ticker = company["cik"], company["ticker"]
                source = json.loads((cache / ("companyfacts_" + cik + ".json")).read_bytes())
                assert source["cik"] == cik and source["_adapter"]["kind"] == "edinet_xbrl" and source["_adapter"]["ticker"] == ticker
                returned_cik, result = sync._derive_cached_worker((cik, ticker, None, str(cache)))
                assert returned_cik == cik and result is not None and result[0] == "ok", (cik, result)
                row = result[1]
                assert row["cik"] == cik and row["ticker"] == ticker
                payloads.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
                payloads.flush()
                checked = audit.audit([row], cache, quiet=True, edgar=NoNetwork())
                audits.write(json.dumps(dict(cik=cik, ticker=ticker, **checked), ensure_ascii=False, separators=(",", ":")) + "\n")
                audits.flush()
                report["japanese_rows"] += 1
                if checked["problems"]:
                    report["bad_companies"].append(ticker)

        connection = sqlite3.connect(readonly_uri, uri=True)
        connection.row_factory = sqlite3.Row
        try:
            receipt = next(item for item in store.covers_by_cik(connection).get("0000313838", []) if item["symbol"] == "SONY")
        finally:
            connection.close()
        returned_cik, result = sync._derive_cached_worker(("0000313838", "SONY", receipt, str(cache)))
        assert returned_cik == "0000313838" and result is not None and result[0] == "ok", result
        row = result[1]
        assert row["cik"] == "0000313838" and row["ticker"] == "SONY"
        with (OWNED / "sony-full-payload.json").open("x", encoding="utf-8") as stream:
            json.dump(row, stream, ensure_ascii=False, separators=(",", ":"))
        checked = audit.audit([row], cache, quiet=True)
        with (OWNED / "sony-audit.json").open("x", encoding="utf-8") as stream:
            json.dump(checked, stream, ensure_ascii=False, separators=(",", ":"))
        report["sony_rows"] = 1
        if checked["problems"]:
            report["bad_companies"].append("SONY")
        report["status"] = "OBSERVED_NOT_ACCEPTED"
    except BaseException as error:
        report["status"] = "FAILED"
        report["failure"] = dict(type=type(error).__name__, message=str(error), traceback=traceback.format_exc())
    finally:
        try:
            report["input_after"] = verify(records)
            assert all(not path.exists() for path in absent)
            assert digest(args.manifest) == args.manifest_sha256
        except BaseException as error:
            report["status"] = "FAILED"
            report["preservation_failure"] = dict(type=type(error).__name__, message=str(error))
        with (OWNED / "report.json").open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, separators=(",", ":"))
    print(json.dumps({key: report[key] for key in ("status", "japanese_rows", "sony_rows", "bad_companies")}))
    return 1 if report["status"] == "FAILED" or report["bad_companies"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
