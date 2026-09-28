# Developer 28 — review map

Developer state: **IDLE — accepted after Review 1**
Current item: **Retain/apply engine-187 SEC evidence in importer**
Review state: **ACCEPTED — Review 1**

The developer updates only `## Developer update`. The reviewer owns the header and reviewer response.

## Developer update

State: **READY_FOR_REVIEW — guidance revision 1**

### Result

- Added `current_retained_artifacts` to the existing Inline-XBRL owner. It
  returns exact manifest/source bytes only after schema, issuer, relationship,
  URL, safe-name, size, hash, incorporation, and current-annual checks.
- The SEC importer retains and reads back every returned byte, then calls the
  existing `EvidenceLoader` to apply the supplement before derivation.
- A missing manifest takes the prior path exactly, including
  `supplement_state=None`. EDINET, ticker mapping, DERA, cover, and calculations
  were not changed.
- Stored roles contain no local path:
  `sec_inline_current_manifest`, `sec_inline_direct_annual_*`,
  `sec_inline_annual_wrapper_*`, and
  `sec_inline_incorporated_source_*`.
- Equal schema/presentation bytes produce one object and two role links.

### Real retained evidence

`local/engine187-sec-import-2026-09-28/verify_real_import.py` ran the exact 18
manifest companies twice with in-memory object, artifact, and company stores.

- 18 verified manifests, 110 file roles, 108 unique source hashes, and 18
  manifest objects.
- Exact engine-187 SQLite payloads: AERO
  `db7c02a7c1664ae47f39d4d15368a54eb25df76544486c862aa28a0dbcf0abad`;
  AGMB `ab06dfe1a18f6f1d2f84ae167be690d66b4241b8682f795a7366e4377ea1a14d`;
  ALPS `a134fec472023bf626791472fac768f53b532b74e0d2025a8d5a9eae53d4207f`;
  AUGO `f46bcf10ccfe4cb5a5bb58d22a5ff0f5675fd0b75e0f7eda93cd5609a11e34a8`;
  CNI `f2ef3efb6f7622df3a67ca1b349d9179174d8ab277af04c2d16c7cd80fd1ded6`;
  DAVI `285392d912f40f8083feb0c336a33c33fe462b5375388c2c0fc0d82a1d9d5255`;
  GCDT `2b26cfc00c32aa152e4f756f267bb318e12e6dba6750b51c0ba39fd771c2b7f1`;
  GMTL `f620d3292ad2a9514497baba9ddfcb3b4b6dcd8759250cdb910651cb30bf5f96`;
  HBNB `0aaf52e786dbca7c934211697293b8e645b6d234dde4584fb1e70c72692a1660`;
  PAYP `6a06e8886f9c099aa6934dc858a33fc141d24e3d074bfd561331bbb69d4821a5`;
  PICS `b5a15f1c92352f5ff825e8092e1716b18c986d353fbb5c9f4e9106b1109e3977`;
  TMCR `37644d2b83a6a50c645d1bf9f8b8050628771c9be920f8d70cec1536015b8638`;
  VMET `a05e20a91ef92c1cc3c29cbe1d47210255d37558b25206cb0b42b39541569a54`;
  YMAT `153078c5d1f9d51f892ad61114e179aa2c8051e72474bc5415715e51c8c5af0e`.
- Exact unchanged failures: AHNRF, BRBI, and NXAT derive `foreign`; CIB reports
  `depositary receipt ratio is missing`.
- Second pass reused all 163 objects, 163 artifact rows, and 14 snapshot IDs.

### Checks

- Focused: `75 passed, 1 skipped`.
- Full Python: `851 passed, 6 skipped, 1 warning`.
- Real 18-case verifier: passed twice.
- Syntax and `git diff --check`: passed.
- Ruff was unavailable in the project environment; no Ruff result is claimed.

### Product and limits

P-02, P-03, P-04, P-09, and P-17 remain intact. Missing stays missing, the four
security failures remain withheld, retained source revisions remain immutable,
and one Python/Alembic storage owner remains.

No storage-service integration run or full-universe regression was performed;
both belong to the review/milestone-9 gates. No service, network, PostgreSQL,
S3, SQLite, cache, EDINET, API, UI, migration, commit, stash, or branch action
was performed.

Concurrent work observed: the manager changed
`local/company-import-plan-2026-09-27.md` from queued to active and created the
guidance/review files. I did not edit the plan or any path outside my ownership.

## Reviewer response

### Review 1 — accepted

Accepted. The source owner now verifies and enumerates the active manifest plus every direct or
incorporated file role. The existing importer writes/reads every byte before EvidenceLoader applies
the supplement and derives. The reviewer independently observed 53 unit passes and the real 18-case
verifier: 18 manifests, 110 file roles, 108 unique source hashes, 14 exact engine-187 payloads, four
unchanged security failures, and full object/artifact/snapshot reuse on retry. EDINET and schema are
unchanged.
