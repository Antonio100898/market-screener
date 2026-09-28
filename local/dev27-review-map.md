# Developer 27 — review map

Developer state: **IDLE — accepted after Review 1**
Current item: **Engine-187 SEC/EDINET persistence audit**
Review state: **ACCEPTED — Review 1**

The developer updates only `## Developer update`. The reviewer owns the header and reviewer response.

## Developer update

**READY_FOR_REVIEW — read-only persistence audit complete.**

### Finding

`CompanyImporter` is the correct shared owner, but `_prepare_sec` retains Company Facts/security
evidence and calls `_derive_evidence` without the verified engine-187 Inline-XBRL supplement. An
exact transient simulation across all 6,995 engine-187 dashboard companies selected 6,707; every
selected canonical payload matched SQLite and none differed. The 288 failures are:

- 147 current official SEC ticker-map mismatches;
- 117 pending structured-fact states;
- ten missing filing-backed depositary ratios;
- 14 importer code failures caused by omitting the verified Inline-XBRL supplement.

The 14 are AERO, AGMB, ALPS, AUGO, CNI, DAVI, GCDT, GMTL, HBNB, PAYP, PICS, TMCR,
VMET, and YMAT. Applying the existing `EvidenceLoader`/Inline-XBRL path in memory made all 14
`ok` with exact SQLite payload hashes. AHNRF, BRBI, NXAT, and CIB remained `foreign` under the
existing security rules. There are zero stale engine snapshots and zero missing local retained
inputs in the current dashboard universe. After the SEC persistence fix, the expected current
selection is 6,721, with the other 274 still withheld.

### SEC artifact inventory

The 18 current manifests are 17 direct annual filings and one incorporated relationship, CNI.
They contain 110 file-role references, 108 unique source hashes, and 199,958,932 unique source
bytes. The 18 manifests add 33,425 bytes. All manifests and source hashes verified.

The importer currently omits all 128 manifest/source role links. A direct filing requires the
manifest, index, primary HTML, extracted instance, filing summary, schema, and presentation.
CNI requires the manifest, annual 40-F index/wrapper, source 6-K index, linked audited exhibit,
instance, summary, schema, and presentation. The manifest carries annual/source accessions, exact
URLs, relationship text, hashes, and sizes.

Exact inventory: `local/engine187-persistence-audit-2026-09-28/sec-artifacts.csv` and
`sec-manifests.json`.

### EDINET inventory

The retained supported universe is exactly:

- Panasonic `6752.T`, `E01772`, `S100YETA`, JPY, TSE ordinary share;
- Nintendo `7974.T`, `E02367`, `S100Y9NX`, JPY, TSE ordinary share.

Both SQLite snapshots are engine 187 `ok`, both are in `dashboard.json`, and both current importer
runs returned exact SQLite payload hashes with `canonical_edinet_facts` and `raw_filing` roles.
Panasonic's ZIP hash is `95cc16e5afa261db468633743e8e46cf0be0b827b6fee5c4d625d87327cbc35f`;
Nintendo's is `a9eb08b6687ec604fddcf997ffb8c97867084e4353d372afeb7637456981e73d`.

Exact inventory: `local/engine187-persistence-audit-2026-09-28/edinet-inventory.json`.

### Reuse and correctly-built owner shape

Reuse `inline_xbrl` for manifest/relationship/hash verification, `EvidenceLoader` for supplement
application, `CompanyImporter._retain` for S3 write/read verification, and
`SharedCompanyRepository` for immutable snapshots/current selection. Add one public retained-file
enumerator to the existing Inline-XBRL owner, then extend `_prepare_sec`. Do not add another parser,
storage path, EDINET database, or ticker exception. The EDINET production path already has the
correct generic shape.

No Alembic migration is needed. Existing evidence artifacts and snapshot artifact-role links can
store all new hashes, including one hash under two roles. A new artifact set creates a new immutable
snapshot; old snapshots remain history.

Full decision: `local/engine187-persistence-audit-2026-09-28/reuse-decision.md`.

### Proposed disjoint increments

1. **SEC retention/application.** Change `company_import.py` and the existing Inline-XBRL owner;
   add focused/unit and real storage tests. Accept on 18 verified manifests, 110 exact source-role
   readbacks, 14 exact engine-187 payloads, four unchanged exclusions, and retry identity reuse.
2. **All retained EDINET.** Add Nintendo beside Panasonic in the real integration test. Import both,
   verify canonical JSON and ZIP readback, exact payloads, JPY/security metadata, retry, and restart.
   Change production code only if this real check exposes a generic defect.
3. **Full reconciliation/API restart.** Run the 6,995-company bulk import. Expect 6,721 exact current
   selections, zero mismatches, and 274 named evidence failures. Verify all 14 SEC additions and both
   EDINET companies through `GET /companies/{ticker}`, restart services/API, repeat, and prove every
   selected snapshot has artifacts.

Exact files and acceptance commands are in
`local/engine187-persistence-audit-2026-09-28/README.md`.

### Checks and limits

- `verify_audit.py` opens SQLite with `mode=ro` plus `PRAGMA query_only`; all S3/PostgreSQL owners
  are transient in-memory fakes. It performs no network call or source acquisition.
- Full exact run: 6,995 identities reconcile; 6,707 selected/6,707 payload matches/zero mismatch;
  288 failures with the four exact buckets above.
- Static reconciliation: 128 SEC rows = 18 manifests + 110 file roles; 108 unique source hashes;
  14 dashboard manifests + four exclusions; two EDINET rows; all assertions passed.
- Syntax: `python -m py_compile verify_audit.py` passed.
- Whitespace: `git diff --check` passed.
- Source inventories were regenerated and parsed repeatedly with the same rows and hashes. The
  manager stopped a second 6,995-company derivation after the first exact run; it made no writes.
- Project PostgreSQL/SeaweedFS containers were already stopped; ports 55432/8333 were closed. No
  service was started. Current PostgreSQL rows, S3 readback, API parity, and restart behavior remain
  unverified. Earlier engine-184 results are not treated as current evidence.
- No provider model, source fetch, SQLite/cache mutation, commit, stash, branch switch, or service
  mutation occurred.
- Concurrent pre-existing edit: `local/company-import-plan-2026-09-27.md` remained modified by the
  reviewing session. Guidance/review-map files were added by that session. No concurrent edit
  touched this audit directory.

Affected product decisions: P-02, P-03, P-04, P-09, and P-17.

## Reviewer response

### Review 1 — accepted

Accepted. The audit is read-only and deterministic. It identifies one generic SEC importer gap:
18 verified manifests and 110 source-role links are omitted, withholding 14 engine-187 companies.
The current importer otherwise selects 6,707 companies with 6,707 exact SQLite payload hashes and
zero mismatches. Panasonic and Nintendo both pass the existing EDINET importer exactly. Services
were stopped, so PostgreSQL/S3/restart state correctly remains unverified.
