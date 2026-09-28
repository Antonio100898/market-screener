# Engine-187 SEC/EDINET persistence audit

## Result

The existing importer is the correct owner, but its SEC preparation stops before the engine-187
Inline-XBRL layer. It retains Company Facts, optional DERA facts, and security evidence, then passes
raw Company Facts directly to `_derive_evidence`. The 14 new engine-187 dashboard companies
therefore derive as `foreign` and cannot enter PostgreSQL.

A read-only simulation of the exact 6,995-company dashboard universe found:

- 6,707 current importer selections; all 6,707 canonical payload hashes exactly match SQLite.
- Zero selected payload mismatches and zero stale engine-187 SQLite dashboard snapshots.
- 288 withheld: 147 current SEC ticker-map mismatches, 117 pending structured facts, ten missing
  filing-backed depositary ratios, and the 14 missing Inline-XBRL importer applications.
- Applying the existing verified supplement in memory makes all 14 derive `ok` with exact SQLite
  payload hashes. AHNRF, BRBI, NXAT, and CIB remain `foreign` as required by security evidence.

The expected post-fix selection is 6,721 of 6,995. The remaining 274 are existing evidence-policy
gaps, not part of this persistence fix.

| Reconciliation class | Count | Meaning |
|---|---:|---|
| Current engine-187 dashboard selection | 6,707 | Exact payload match through current importer |
| Stale SQLite engine snapshot | 0 | Every dashboard row is engine 187 |
| Missing local retained importer input | 0 | No dashboard failure named a missing cache input |
| SEC identity mismatch | 147 | Current official ticker mapping does not prove this ticker/CIK |
| Pending structured facts | 117 | Newer filing is not yet represented coherently |
| Missing depositary ratio | 10 | Security basis cannot be priced safely |
| Importer code defect | 14 | Verified Inline-XBRL exists but `_prepare_sec` never applies it |

PostgreSQL artifact omission is separate: all 128 engine-187 manifest/source role links are absent
from the importer contract even though the local retained bytes exist and verify.

## SEC artifact gap

There are 18 verified current SEC manifests: 17 direct annual filings and one incorporated case,
CNI. They reference 110 file roles, 108 unique source hashes, and 199,958,932 unique source bytes.
The 18 manifests add 33,425 bytes. The current importer retains none of these 128 manifest/source
role links.

Each direct filing needs these exact bytes in S3 and links on its immutable snapshot:

- current manifest;
- SEC accession `index.json`;
- primary HTML filing;
- extracted Inline-XBRL instance;
- `FilingSummary.xml`;
- issuer schema;
- presentation linkbase, including a second role link when it shares the schema bytes.

CNI also needs the annual 40-F index and wrapper, the source 6-K index, the linked audited statement
exhibit, its extracted instance, summary, schema, and presentation linkbase. The manifest preserves
the annual/source accessions, exact SEC URLs, incorporation text, link text, hashes, and sizes.

Exact rows are in `sec-artifacts.csv`; per-company derivation and parity are in
`sec-manifests.json`.

## Retained EDINET universe

Exactly two supported EDINET companies are retained:

- Panasonic `6752.T`, entity `E01772`, report `S100YETA`.
- Nintendo `7974.T`, entity `E02367`, report `S100Y9NX`.

Both are engine 187 `ok`, present in the dashboard, JPY statements/quotes, TSE primary ordinary
shares, and have one verified ZIP. The current generic importer returns `ok`, retains
`canonical_edinet_facts` plus `raw_filing`, and exactly matches each SQLite payload. No EDINET code
change is required unless the real storage/restart run finds a defect. Exact evidence is in
`edinet-inventory.json`.

## Correct owner shape

```text
official source acquisition
  -> source-owned verified manifest and retained bytes
  -> CompanyImporter enumerates every evidence object
  -> existing S3 write/read verification
  -> existing source parser/normalizer and one canonical payload
  -> immutable PostgreSQL snapshot plus exact role/hash links
  -> current selection
  -> FastAPI PostgreSQL-only read
```

SEC and EDINET differ only in acquisition, parsing, and artifact enumeration. They share storage,
snapshot identity, selection, restart behavior, API serialization, and calculations.

## Build increments

### 1. Retain engine-187 SEC evidence

Files: `api/screener/company_import.py`, `api/screener/sources/inline_xbrl.py`,
`api/tests/test_company_import.py`, `api/tests/test_inline_xbrl.py`, and
`api/tests/integration/test_company_import_storage.py`.

Add one verified manifest-file enumerator to the existing Inline-XBRL owner. Make `_prepare_sec`
apply the existing current supplement and pass the manifest plus all named bytes through `_retain`.
Use source-prefixed artifact roles; do not copy path, URL, relationship, or hash rules.

Accept when all 18 manifests and 110 role references read back byte-for-byte from S3; the 14
dashboard companies store exact engine-187 SQLite payloads; the four policy exclusions remain out;
a repeated import reuses snapshot identities; direct and CNI incorporated roles are complete.

### 2. Import every retained supported EDINET company

Files: normally only `api/tests/integration/test_company_import_storage.py`; change production code
only if the real check exposes a generic defect.

Import Panasonic and Nintendo through `CompanyImporter`. Verify both canonical JSON files and both
ZIP archives by S3 readback, JPY and primary-security metadata, immutable snapshot selection, exact
SQLite payload parity, retry reuse, and restart survival.

### 3. Full reconciliation and API restart

Files: verification evidence only unless a test exposes a defect; use the existing bulk command and
`GET /companies/{ticker}`.

Run the full 6,995-company import after increment 1. Expected current selections: 6,721 exact
payload matches and zero mismatches. Expected exclusions: 147 ticker-map, 117 pending facts, and ten
missing ratios. Verify engine 187 through FastAPI for all 14 SEC additions and both EDINET rows,
restart PostgreSQL/S3/API, repeat reads, and prove every current snapshot has artifact links.

No Alembic migration is expected in any increment. Existing content-addressed artifacts, immutable
snapshots, role links, and current pointers cover the requirement.

## Runtime limit

The project PostgreSQL and SeaweedFS containers were already stopped. Ports 55432 and 8333 were
closed. This audit did not start them. Current PostgreSQL/S3 rows, selected engine revisions, object
readback, and restart/API parity are therefore unavailable now. Earlier accepted engine-184 storage
evidence is historical evidence only; it is not claimed as current engine-187 state.

## Checks

- `verify_audit.py` opens SQLite with `mode=ro` and `PRAGMA query_only`, reads retained files and
  `dashboard.json`, and uses transient repositories instead of PostgreSQL/S3.
- Dashboard identities: 6,995 SQLite equals 6,995 JSON; unique by the existing engine gate.
- SEC totals: 18 manifest rows plus 110 file-role rows equals 128 inventory rows.
- EDINET totals: two entities, two canonical JSON files, two ZIPs, two exact importer matches.
- The full reconciliation run produced `source-hash-counts.json`. Repeated source-inventory runs
  reproduced the same 128 SEC rows and two EDINET rows; the manager stopped a second expensive
  full-universe derivation after the first exact run had completed.
- No provider model, source fetch, service start, database mutation, commit, stash, or branch change
  was made.

Affected decisions preserved: P-02, P-03, P-04, P-09, and P-17.
