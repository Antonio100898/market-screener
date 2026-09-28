# Retained cover evidence result

## Result

SEC rendered cover responses are retained byte-for-byte at
`covers/<accession-without-dashes>/R<report>.htm` under the existing cache. A cached report is read
before any source request. A successful source response is atomically installed before parsing.

Default cover sync still skips an accession already represented in SQLite. The explicit
`reparse=True` path requires a CIK set. It rebuilds covered accessions from cached bytes, fetches only
missing report files, and preserves an existing filing-backed ratio when a cached rendered report
does not carry that ratio itself.

The company importer matches the current structured cover row to the exact cached report. When it
finds one, it verifies the bytes through S3 and links the artifact as `raw_cover_filing`. The
existing `structured_cover_identity` link remains. A historical row without raw bytes still imports.

## Trace and reuse decision

- `sources/cover.py` owns the canonical report cache path and cover parsing.
- `sync.cover_pages` remains the only cover fetch, skip, parse, and SQLite update flow.
- `CompanyImporter` already owns retained-file verification, S3 storage, and snapshot artifact
  links; raw cover bytes use that path without a new store or manifest.
- Correct-from-zero shape: fetch writes immutable bytes first, parsing reads those bytes, SQLite
  stores the queryable identity, and PostgreSQL snapshots link both raw and structured evidence.
- P-02, P-03, P-04, P-09, P-17, and the foreign security invariant remain unchanged. Missing raw
  history is explicit and does not become guessed evidence.

## Checks

```text
PYTHONPATH=. .venv/bin/pytest -q tests/test_cover.py tests/test_sync.py tests/test_company_import.py
116 passed in 0.52s

RUN_STORAGE_INTEGRATION=1 .venv/bin/pytest -q tests/integration/test_company_import_storage.py
1 passed in 4.11s

make test
746 passed, 6 skipped, 1 warning in 3.23s
113 web tests passed

api/.venv/bin/python -m compileall -q api/screener api/tests local/foreign-cover-parser-2026-09-28/verify_real_covers.py
passed, no output

git diff --check
passed, no output
```

The real local PostgreSQL/S3 test read back the exact 141-byte ABT cover fixture from
`raw/sha256/134b85e1066ecb5e0a73f3f01b1e5830fe047c8b65c1865cb4591dfb9f272fea`. It retained 12
unique artifacts and 12 snapshot links across four companies. A repeated import kept the same
snapshot IDs and artifact counts.

## Manager command

This call reparses only the accepted 47 parser rows and 57 missing-cover rows. It asserts the
expected 104-CIK boundary before any source work.

```sh
PYTHONPATH=api api/.venv/bin/python - <<'PY'
import json
from pathlib import Path

from screener import store, sync

rows = json.loads(Path(
    "local/foreign-coverage-investigation-2026-09-28/inventory.json"
).read_text())
ciks = {
    row["cik"] for row in rows
    if row["subclass"] in {"empty_parser_failure", "missing_raw_cover_evidence"}
}
assert len(ciks) == 104

conn = store.connect()
try:
    sync.cover_pages(
        conn,
        foreign_only=True,
        ciks=ciks,
        reparse=True,
    )
finally:
    conn.close()
PY
```

## Limits

No live SEC cover was fetched during this increment. The 104-CIK reparse, derive, export,
regression, audit, and filing audit remain manager-owned after review. The integration fixture
proves exact cache-to-S3 readback; it does not claim that historical raw covers already exist.
